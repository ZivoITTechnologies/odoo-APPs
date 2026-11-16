# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import ZlmsCommon


@tagged('post_install', '-at_install', 'zlms')
class TestLearningFlow(ZlmsCommon):

    def test_01_auto_assignment_and_enrolment(self):
        learner = self._new_learner()
        self.assertTrue(learner.partner_id, 'A contact is created for enrolment')
        assignment = learner.assignment_ids
        self.assertEqual(len(assignment), 1)
        self.assertEqual(assignment.path_id, self.path)
        self.assertEqual(len(assignment.line_ids), 3)
        self.assertEqual(assignment.due_date, assignment.assign_date + timedelta(days=30))
        membership = self.env['slide.channel.partner'].search([('channel_id', '=', self.channel.id),
                                                                ('partner_id', '=', learner.partner_id.id)])
        self.assertTrue(membership, 'Learner is enrolled in the path courses')
        # idempotent: re-assigning does not duplicate
        self.path._assign_to_learners(learner)
        self.assertEqual(len(learner.assignment_ids), 1)

    def test_02_role_change_assigns_new_path(self):
        other_role = self.env['zlms.role'].create({'name': 'Cashier T', 'code': 'TCSH'})
        learner = self._new_learner(role_id=other_role.id)
        self.assertFalse(learner.assignment_ids)
        learner.role_id = self.role
        self.assertEqual(learner.assignment_ids.path_id, self.path)

    def test_03_full_journey_completion_and_certificate(self):
        learner = self._new_learner('Full Journey')
        assignment = learner.assignment_ids
        # course completion through native eLearning triggers the sync hook
        self._complete_course(learner, attempts=2)
        course_line = assignment.line_ids.filtered(lambda l: l.line_type == 'course')
        self.assertEqual(course_line.state, 'done')
        self.assertEqual(course_line.score, 80.0, 'Second quiz attempt scores 80')
        self.assertEqual(assignment.state, 'in_progress')
        # role-play (offline engine) with good phrases
        session = self.env['zlms.roleplay.session'].create({'scenario_id': self.scenario.id, 'learner_id': learner.id})
        session.action_start()
        self.assertEqual(session.engine, 'offline')
        self.assertEqual(session.message_line_ids[0].content, 'Just looking')
        reply = session.send_message('Hello, welcome! What kind of activity are you looking for?')
        self.assertEqual(reply, 'I go to the gym')
        session.send_message('This Test DryTech tee helps you stay dry, which means comfortable workouts. '
                             'I understand. Would you like to try your size? It pairs with our joggers too.')
        session.action_evaluate()
        self.assertEqual(session.state, 'evaluated')
        self.assertGreaterEqual(session.score, 50)
        self.assertTrue(session.criterion_ids)
        rp_line = assignment.line_ids.filtered(lambda l: l.line_type == 'roleplay')
        self.assertEqual(rp_line.state, 'done')
        # OJT sign-off by the store manager completes the journey
        ojt = assignment.line_ids.filtered(lambda l: l.line_type == 'ojt')
        ojt.with_user(self.sm_user).write({'manager_rating': '4'})
        ojt.with_user(self.sm_user).action_sign_off()
        self.assertEqual(ojt.state, 'done')
        self.assertEqual(ojt.signed_by_id, self.sm_user)
        self.assertEqual(assignment.state, 'completed')
        self.assertEqual(assignment.progress, 100.0)
        self.assertTrue(assignment.passed)
        self.assertTrue(assignment.certificate_id)
        self.assertEqual(assignment.certificate_id.state, 'valid')
        self.assertTrue(assignment.certificate_id.name.startswith('CERT/'))
        self.assertGreaterEqual(learner.points, self.path.completion_points)
        # certificate PDF renders
        pdf, _fmt = self.env['ir.actions.report']._render_qweb_pdf(
            'zivoit_retail_lms.action_report_zlms_certificate', assignment.certificate_id.ids)
        self.assertTrue(pdf)

    def test_04_ojt_sign_off_restricted(self):
        learner = self._new_learner('Restricted')
        ojt = learner.assignment_ids.line_ids.filtered(lambda l: l.line_type == 'ojt')
        with self.assertRaises(UserError):
            ojt.with_user(self.sm_other).sudo(False).action_sign_off()

    def test_05_overdue_and_risk(self):
        learner = self._new_learner('Late Learner')
        assignment = learner.assignment_ids
        assignment.write({'assign_date': fields.Date.today() - timedelta(days=60),
                          'due_date': fields.Date.today() - timedelta(days=5)})
        self.assertTrue(assignment.is_overdue)
        self.assertIn(assignment, self.env['zlms.assignment'].search([('is_overdue', '=', True)]))
        learner.invalidate_recordset()
        self.assertEqual(learner.risk_level, 'high')
        self.assertIn(learner, self.env['zlms.learner'].search([('risk_level', '=', 'high')]))
        self.env['zlms.assignment']._cron_overdue_reminders()
        self.assertTrue(assignment.activity_ids, 'Store manager gets a follow-up activity')

    def test_06_certificate_expiry_triggers_recertification(self):
        learner = self._new_learner('Expiring')
        assignment = learner.assignment_ids
        cert = self.env['zlms.certificate'].create({'learner_id': learner.id, 'path_id': self.path.id,
                                                    'assignment_id': assignment.id, 'score': 90})
        assignment.state = 'completed'
        cert.write({'issue_date': fields.Date.today() - timedelta(days=400)})
        self.env['zlms.certificate']._cron_check_expiry()
        self.assertEqual(cert.state, 'expired')
        self.assertEqual(assignment.state, 'expired')
        new = learner.assignment_ids.filtered(lambda a: a.state == 'assigned')
        self.assertEqual(len(new), 1, 'A re-certification assignment is created')

    def test_07_daily_boost_leitner(self):
        learner = self._new_learner('Booster')
        Card = self.env['zlms.boost.card']
        cards = Card._seed_for_learners(learner)
        self.assertEqual(len(cards), 1)
        card = cards
        right = card.question_id.answer_ids.filtered('is_correct')
        wrong = card.question_id.answer_ids - right
        res = card.answer(right.id, confident=True)
        self.assertTrue(res['correct'])
        self.assertEqual(card.box, 2)
        self.assertEqual(card.next_review_date, fields.Date.today() + timedelta(days=2))
        self.assertFalse(Card._get_due_cards(learner))
        card.answer(right.id, confident=False)
        self.assertEqual(card.box, 2, 'Unsure answers are not promoted')
        res = card.answer(wrong.id, confident=True)
        self.assertFalse(res['correct'])
        self.assertEqual(card.box, 1)
        self.assertEqual(res['correct_answer'], '10 seconds')
        self.assertEqual(self.env['zlms.boost.answer'].search_count([('learner_id', '=', learner.id)]), 3)
        self.assertEqual(len(Card._seed_for_learners(learner)), 0, 'Seeding is idempotent')

    def test_08_kpi_ratios_and_dashboard(self):
        learner = self._new_learner('Dash')
        self._complete_course(learner)
        kpi = self.env['zlms.store.kpi'].create({'store_id': self.store.id, 'period_date': '2026-10-15',
                                                 'footfall': 1000, 'bills': 250, 'units_sold': 500, 'net_sales': 200000})
        self.assertEqual(str(kpi.period_date), '2026-10-01')
        self.assertEqual(kpi.conversion_rate, 25.0)
        self.assertEqual(kpi.atv, 800.0)
        self.assertEqual(kpi.upt, 2.0)
        data = self.env['zlms.dashboard'].get_dashboard_data(store_id=self.store.id)
        self.assertEqual(data['kpis']['stores'], 1)
        self.assertGreaterEqual(data['kpis']['learners'], 1)
        self.assertEqual(len(data['trend']), 6)
        self.assertEqual(data['stores'][0]['id'], self.store.id)
        self.assertGreater(self.store.avg_completion, 0)

    def test_09_assign_wizard(self):
        l1 = self._new_learner('Wiz One', store=self.store_b)
        path2 = self.env['zlms.learning.path'].create({
            'name': 'Launch', 'path_type': 'launch', 'duration_days': 7,
            'line_ids': [(0, 0, {'name': 'Course', 'line_type': 'course', 'channel_id': self.channel.id})]})
        path2.action_publish()
        wizard = self.env['zlms.assign.path.wizard'].create({'path_id': path2.id, 'target': 'stores',
                                                             'store_ids': [(6, 0, self.store_b.ids)]})
        wizard.action_assign()
        self.assertIn(path2, l1.assignment_ids.path_id)

    def test_10_create_learner_from_employee(self):
        employee = self.env['hr.employee'].create({'name': 'Emp Learner', 'work_email': 'emp@example.com'})
        action = employee.action_zlms_create_learner()
        learner = self.env['zlms.learner'].search(action['domain'])
        self.assertEqual(learner.employee_id, employee)
        self.assertEqual(learner.email, 'emp@example.com')
