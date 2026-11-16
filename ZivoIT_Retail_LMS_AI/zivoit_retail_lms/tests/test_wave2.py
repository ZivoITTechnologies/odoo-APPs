# -*- coding: utf-8 -*-
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.http import Request
from odoo.tests import HttpCase, tagged

from odoo.addons.zivoit_retail_lms.models.ai_service import ZlmsAiService

from .common import ZlmsCommon


@tagged('post_install', '-at_install', 'zlms')
class TestWave2(ZlmsCommon):

    def test_01_bulletin_targeting_and_ack(self):
        a1 = self._new_learner('Bul A', store=self.store)
        b1 = self._new_learner('Bul B', store=self.store_b)
        bulletin = self.env['zlms.bulletin'].create({
            'name': 'Launch brief', 'body': '<p>New range</p>', 'store_ids': [(6, 0, self.store.ids)],
            'channel_id': self.channel.id, 'due_date': fields.Date.today() - timedelta(days=1)})
        bulletin.action_publish()
        self.assertEqual(bulletin.state, 'published')
        self.assertIn(a1, bulletin.ack_ids.learner_id)
        self.assertNotIn(b1, bulletin.ack_ids.learner_id, 'Audience filter by store')
        ack = bulletin.ack_ids.filtered(lambda a: a.learner_id == a1)
        self.assertTrue(ack.is_overdue)
        ack.action_mark_read()
        self.assertEqual(ack.state, 'read')
        ack.action_acknowledge()
        self.assertEqual(ack.state, 'acknowledged')
        self.assertEqual(bulletin.ack_rate, 100.0 * bulletin.ack_count / bulletin.target_count)
        self.assertTrue(self.env['zlms.points.log'].search([('learner_id', '=', a1.id), ('points', '=', 5)]))
        # republish is idempotent and adds newcomers only
        a2 = self._new_learner('Bul C', store=self.store)
        bulletin.action_publish()
        self.assertEqual(len(bulletin.ack_ids.filtered(lambda a: a.learner_id == a1)), 1)
        self.assertIn(a2, bulletin.ack_ids.learner_id)
        # overdue reminder for store manager
        self.env['zlms.bulletin']._cron_overdue_bulletins()
        self.assertTrue(bulletin.activity_ids.filtered(lambda a: a.user_id == self.sm_user))

    def test_02_badges_points_and_streak(self):
        self.env['zlms.badge'].search([]).active = False
        learner = self._new_learner('Badger')
        badge = self.env['zlms.badge'].create({'name': 'T50', 'criterion': 'points', 'threshold': 50, 'bonus_points': 7})
        learner._add_points(60, reason='test')
        self.assertIn(badge, learner.badge_award_ids.badge_id)
        self.assertEqual(learner.points, 67, 'Badge bonus points are added')
        learner._add_points(1)
        self.assertEqual(len(learner.badge_award_ids.filtered(lambda a: a.badge_id == badge)), 1, 'Awarded once')
        self.assertEqual(learner.month_points, 68)
        # streak: answers on 3 consecutive days ending today
        card = self.env['zlms.boost.card']._seed_for_learners(learner)
        Answer = self.env['zlms.boost.answer']
        for days in (0, 1, 2, 4):
            Answer.create({'card_id': card.id, 'learner_id': learner.id, 'correct': True,
                           'answer_date': fields.Date.today() - timedelta(days=days)})
        learner.invalidate_recordset(['boost_streak'])
        self.assertEqual(learner.boost_streak, 3)
        # store challenge normalises by headcount
        self.store.invalidate_recordset(['month_points', 'challenge_score'])
        self.assertGreaterEqual(self.store.month_points, 68)
        rows = {r['id']: r for r in self.env['zlms.store'].get_store_challenge(limit=100)}
        self.assertEqual(rows[self.store.id]['score'], round(self.store.month_points / self.store.headcount, 1))
        ranked = self.env['zlms.store'].get_store_challenge(limit=100)
        self.assertEqual([r['score'] for r in ranked], sorted([r['score'] for r in ranked], reverse=True))

    def test_03_observation_skills_and_coaching_plan(self):
        skill = self.env['zlms.skill'].create({'name': 'T Greeting', 'skill_area': 'service'})
        skill2 = self.env['zlms.skill'].create({'name': 'T Upsell', 'skill_area': 'selling'})
        self.env['zlms.role.skill'].create({'role_id': self.role.id, 'skill_id': skill.id, 'target_level': 4})
        self.env['zlms.role.skill'].create({'role_id': self.role.id, 'skill_id': skill2.id, 'target_level': 3})
        template = self.env['zlms.observation.template'].create({'name': 'Walk', 'item_ids': [
            (0, 0, {'name': 'Greets fast', 'skill_id': skill.id, 'guidance': 'Scan the door every minute'}),
            (0, 0, {'name': 'Suggests add-on', 'skill_id': skill2.id, 'guidance': 'Practise the pairing line'})]})
        learner = self._new_learner('Observed')
        obs = self.env['zlms.observation'].with_user(self.sm_user).create({'learner_id': learner.id,
                                                                           'template_id': template.id})
        self.assertEqual(len(obs.line_ids), 2)
        with self.assertRaises(UserError):
            obs.action_done()
        obs.line_ids[0].rating = '5'
        obs.line_ids[1].rating = '2'
        self.assertEqual(obs.score, 70.0)
        obs.action_done()
        self.assertEqual(obs.state, 'done')
        self.assertIn('Practise the pairing line', obs.coaching_html)
        levels = {s.skill_id: s for s in learner.skill_ids}
        self.assertEqual(levels[skill].level, 5.0)
        self.assertEqual(levels[skill2].gap, 1.0)
        self.assertEqual(learner.skill_gap_count, 1)
        self.assertAlmostEqual(learner.role_fit, round(100 * (4 + 2) / 7, 1))
        # a second observation is blended, not overwritten
        obs2 = self.env['zlms.observation'].create({'learner_id': learner.id, 'template_id': template.id})
        obs2.line_ids[1].rating = '4'
        obs2.action_done()
        self.assertEqual(levels[skill2].level, 3.2)

    def test_04_promotion_readiness(self):
        senior = self.env['zlms.role'].create({'name': 'T Senior', 'code': 'TSEN'})
        skill = self.env['zlms.skill'].create({'name': 'T Lead', 'skill_area': 'leadership'})
        self.env['zlms.role.skill'].create({'role_id': senior.id, 'skill_id': skill.id, 'target_level': 4})
        self.role.write({'next_role_id': senior.id, 'promotion_path_ids': [(6, 0, self.path.ids)], 'min_tenure_days': 100})
        learner = self._new_learner('Climber', joining_date=fields.Date.today() - timedelta(days=200))
        self.assertEqual(learner.promotion_readiness, 10.0, 'Only tenure is met')
        self.env['zlms.learner.skill']._record(learner, skill, 4, 'manual', weight=1)
        learner.assignment_ids.write({'state': 'completed'})
        learner.invalidate_recordset(['promotion_readiness', 'promotion_ready'])
        self.assertEqual(learner.promotion_readiness, 100.0)
        self.assertTrue(learner.promotion_ready)
        self.assertIn(learner, self.env['zlms.learner'].search([('promotion_ready', '=', True)]))

    def test_05_training_session_completes_journey_step(self):
        ilt = self.env['zlms.ilt.course'].create({'name': 'T Classroom'})
        self.path.write({'line_ids': [(0, 0, {'name': 'Workshop', 'line_type': 'session', 'ilt_course_id': ilt.id})]})
        learner = self._new_learner('Classroom')
        self.assertIn('session', learner.assignment_ids.line_ids.mapped('line_type'))
        late = self._new_learner('Late')
        session = self.env['zlms.training.session'].create({
            'name': 'S1', 'ilt_course_id': ilt.id, 'capacity': 1,
            'start_datetime': fields.Datetime.now() + timedelta(days=1),
            'end_datetime': fields.Datetime.now() + timedelta(days=1, hours=2)})
        session.action_invite_due_learners()
        self.assertEqual(session.seats_taken, 1, 'Invites fill up to capacity')
        with self.assertRaises(UserError):
            session.register(late if session.attendee_ids.learner_id != late else learner)
        attendee = session.attendee_ids
        attendee.state = 'attended'
        session.action_done()
        line = attendee.learner_id.assignment_ids.line_ids.filtered(lambda l: l.line_type == 'session')
        self.assertEqual(line.state, 'done')
        attendee.feedback_rating = '5'
        self.assertEqual(session.avg_rating, 5.0)

    def test_06_new_step_reaches_open_assignments(self):
        learner = self._new_learner('Existing')
        before = len(learner.assignment_ids.line_ids)
        self.env['zlms.learning.path.line'].create({'path_id': self.path.id, 'name': 'New OJT', 'line_type': 'ojt'})
        self.assertEqual(len(learner.assignment_ids.line_ids), before + 1)

    def test_07_ai_path_builder_offline_and_ai(self):
        self.channel.zlms_retail_category = 'onboarding'
        wizard = self.env['zlms.ai.path.wizard'].create({'name': 'Built', 'path_type': 'onboarding',
                                                         'duration_days': 20, 'use_ai': False})
        path = self.env['zlms.learning.path'].browse(wizard.action_build()['res_id'])
        self.assertEqual(path.state, 'draft')
        types = path.line_ids.mapped('line_type')
        self.assertIn('course', types)
        self.assertIn('roleplay', types)
        self.assertIn('ojt', types)
        self.assertTrue(all(0 < l.day_offset <= 20 for l in path.line_ids))
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('zivoit_retail_lms.ai_provider', 'openai')
        icp.set_param('zivoit_retail_lms.ai_api_key', 'k')
        steps = {'steps': [{'type': 'course', 'id': self.channel.id, 'name': 'Start', 'day': 2},
                           {'type': 'course', 'id': 999999, 'name': 'Hallucinated', 'day': 3},
                           {'type': 'ojt', 'name': 'Do it', 'day': 9, 'instructions': 'On the floor'}]}
        with patch.object(ZlmsAiService, '_call_provider', return_value=json.dumps(steps)):
            wizard = self.env['zlms.ai.path.wizard'].create({'name': 'AI Built', 'path_type': 'selling'})
            path = self.env['zlms.learning.path'].browse(wizard.action_build()['res_id'])
        self.assertEqual(path.line_ids.mapped('name'), ['Start', 'Do it'], 'Unknown ids are dropped')

    def test_08_roleplay_feeds_skills(self):
        skill = self.env['zlms.skill'].create({'name': 'T Discovery', 'skill_area': 'selling'})
        self.scenario.skill_id = skill
        learner = self._new_learner('Practiser')
        session = self.env['zlms.roleplay.session'].create({'scenario_id': self.scenario.id, 'learner_id': learner.id})
        session.action_start()
        session.send_message('Hello, welcome! What are you looking for? I understand, would you like to try?')
        session.action_evaluate()
        self.assertEqual(learner.skill_ids.skill_id, skill)
        self.assertEqual(learner.skill_ids.source, 'roleplay')

    def test_09_store_manager_scoping_new_models(self):
        mine = self._new_learner('SM Mine', store=self.store)
        theirs = self._new_learner('SM Theirs', store=self.store_b)
        bulletin = self.env['zlms.bulletin'].create({'name': 'All', 'body': '<p>x</p>'})
        bulletin.action_publish()
        visible = self.env['zlms.bulletin.ack'].with_user(self.sm_user).search([('bulletin_id', '=', bulletin.id)])
        self.assertIn(mine, visible.learner_id)
        self.assertNotIn(theirs, visible.learner_id)
        data = self.env['zlms.dashboard'].with_user(self.sm_user).get_dashboard_data()
        self.assertIn('bulletin_ack_rate', data['kpis'])
        self.assertTrue(all(r['id'] == self.store.id for r in data['challenge']))


@tagged('post_install', '-at_install', 'zlms')
class TestWave2Portal(HttpCase, ZlmsCommon):

    def test_portal_news_and_sessions(self):
        learner = self._new_learner('Portal W2', user_id=self.portal_user.id, partner_id=self.portal_user.partner_id.id)
        bulletin = self.env['zlms.bulletin'].create({'name': 'Portal brief', 'body': '<p>Read me carefully</p>'})
        bulletin.action_publish()
        ilt = self.env['zlms.ilt.course'].create({'name': 'Portal Class'})
        session = self.env['zlms.training.session'].create({
            'name': 'Portal Session', 'ilt_course_id': ilt.id, 'capacity': 5,
            'start_datetime': fields.Datetime.now() + timedelta(days=2),
            'end_datetime': fields.Datetime.now() + timedelta(days=2, hours=1)})
        self.authenticate('zlms_portal', 'zlms_portal')
        home = self.url_open('/my/learning').text
        self.assertIn('Store news', home)
        self.assertIn('Store challenge', home)
        self.assertIn('Portal brief', self.url_open('/my/learning/news').text)
        ack = bulletin.ack_ids.filtered(lambda a: a.learner_id == learner)
        resp = self.url_open('/my/learning/news/%s' % ack.id)
        self.assertIn('Read me carefully', resp.text)
        resp = self.url_open('/my/learning/news/%s' % ack.id, data={'acknowledge': '1', 'csrf_token': Request.csrf_token(self)})
        self.assertIn('Acknowledged on', resp.text)
        self.assertEqual(ack.state, 'acknowledged')
        resp = self.url_open('/my/learning/sessions')
        self.assertIn('Portal Session', resp.text)
        self.url_open('/my/learning/sessions', data={'session_id': session.id, 'action': 'register',
                                                     'csrf_token': Request.csrf_token(self)})
        self.assertIn(learner, session.attendee_ids.learner_id)
        session.attendee_ids.state = 'attended'
        self.url_open('/my/learning/sessions', data={'session_id': session.id, 'action': 'feedback', 'rating': '4',
                                                     'comment': 'Useful', 'csrf_token': Request.csrf_token(self)})
        self.assertEqual(session.attendee_ids.feedback_rating, '4')
        # someone else's acknowledgement is not reachable
        other = self._new_learner('Other W2')
        bulletin.action_publish()
        other_ack = bulletin.ack_ids.filtered(lambda a: a.learner_id == other)
        resp = self.url_open('/my/learning/news/%s' % other_ack.id)
        self.assertNotIn('I have read and understood', resp.text)
