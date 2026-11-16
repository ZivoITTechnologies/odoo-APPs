# -*- coding: utf-8 -*-
from odoo.http import Request
from odoo.tests import HttpCase, tagged

from .common import ZlmsCommon


@tagged('post_install', '-at_install', 'zlms')
class TestSecurity(ZlmsCommon):

    def test_store_manager_sees_only_own_team(self):
        mine = self._new_learner('Mine', store=self.store)
        theirs = self._new_learner('Theirs', store=self.store_b)
        Learner = self.env['zlms.learner'].with_user(self.sm_user)
        visible = Learner.search([('id', 'in', (mine | theirs).ids)])
        self.assertEqual(visible, mine)
        Assignment = self.env['zlms.assignment'].with_user(self.sm_user)
        self.assertEqual(Assignment.search([('learner_id', 'in', (mine | theirs).ids)]).learner_id, mine)
        stores = self.env['zlms.store'].with_user(self.sm_user).search([('id', 'in', (self.store | self.store_b).ids)])
        self.assertEqual(stores, self.store)
        # dashboard is automatically scoped by record rules
        data = self.env['zlms.dashboard'].with_user(self.sm_user).get_dashboard_data()
        self.assertEqual({s['id'] for s in data['stores']}, {self.store.id})

    def test_lnd_sees_everything(self):
        mine = self._new_learner('A1', store=self.store)
        theirs = self._new_learner('B1', store=self.store_b)
        visible = self.env['zlms.learner'].with_user(self.lnd_user).search([('id', 'in', (mine | theirs).ids)])
        self.assertEqual(visible, mine | theirs)

    def test_grant_portal_access(self):
        learner = self._new_learner('Portal Grant', email='portal.grant@example.com')
        learner.action_grant_portal_access()
        self.assertTrue(learner.user_id)
        self.assertTrue(learner.user_id._is_portal())
        self.assertEqual(learner.user_id.partner_id, learner.partner_id)


@tagged('post_install', '-at_install', 'zlms')
class TestPortal(HttpCase, ZlmsCommon):

    def test_portal_pages(self):
        learner = self._new_learner('Portal Learner', user_id=self.portal_user.id,
                                    partner_id=self.portal_user.partner_id.id)
        self.authenticate('zlms_portal', 'zlms_portal')
        for url in ('/my/learning', '/my/learning/boost', '/my/learning/ask', '/my/learning/roleplay'):
            resp = self.url_open(url)
            self.assertEqual(resp.status_code, 200, url)
        home = self.url_open('/my/learning').text
        self.assertIn('Test Onboarding', home)
        self.assertIn('Daily Boost', home)
        # answer a boost card through the form
        card = self.env['zlms.boost.card'].search([('learner_id', '=', learner.id)], limit=1)
        self.assertTrue(card)
        right = card.question_id.answer_ids.filtered('is_correct')
        resp = self.url_open('/my/learning/boost/answer', data={
            'card_id': card.id, 'answer_id': right.id, 'confident': '1', 'csrf_token': Request.csrf_token(self)})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Correct!', resp.text)
        # ask the expert
        resp = self.url_open('/my/learning/ask', data={'question': 'training tee for gym', 'csrf_token': Request.csrf_token(self)})
        self.assertIn('Test Training Tee', resp.text)
        # role-play through the portal
        resp = self.url_open('/my/learning/roleplay/start/%s' % self.scenario.id, data={'csrf_token': Request.csrf_token(self)})
        session = self.env['zlms.roleplay.session'].search([('learner_id', '=', learner.id)], limit=1)
        self.assertTrue(session)
        resp = self.url_open('/my/learning/roleplay/%s' % session.id,
                             data={'message': 'Hello, welcome! What are you looking for?', 'csrf_token': Request.csrf_token(self)})
        self.assertIn('I go to the gym', resp.text)
        resp = self.url_open('/my/learning/roleplay/%s' % session.id, data={'evaluate': '1', 'csrf_token': Request.csrf_token(self)})
        self.assertIn('Score:', resp.text)
        # another learner's session is not reachable
        other = self._new_learner('Other Portal')
        other_session = self.env['zlms.roleplay.session'].create({'scenario_id': self.scenario.id, 'learner_id': other.id})
        resp = self.url_open('/my/learning/roleplay/%s' % other_session.id)
        self.assertNotIn(other.name, resp.text)

    def test_portal_without_profile(self):
        self.authenticate('zlms_portal', 'zlms_portal')
        resp = self.url_open('/my/learning')
        self.assertIn('No learning profile yet', resp.text)


@tagged('post_install', '-at_install', 'zlms')
class TestBackendUi(HttpCase, ZlmsCommon):

    def _wait_for(self, selector):
        return """
            const start = Date.now();
            const check = () => {
                if (document.querySelector('%s')) { console.log('test successful'); }
                else if (Date.now() - start > 15000) { console.error('selector not found: %s'); }
                else { setTimeout(check, 200); }
            };
            check();
        """ % (selector, selector)

    def test_dashboard_client_action(self):
        self._new_learner('JS Learner')
        self.browser_js('/odoo/action-zivoit_retail_lms.zlms_dashboard_action', self._wait_for('.o_zlms_dashboard .o_zlms_tile'),
                        login='admin', timeout=60)

    def test_store_and_learner_views(self):
        self._new_learner('JS Learner 2')
        self.browser_js('/odoo/action-zivoit_retail_lms.zlms_store_action', self._wait_for('.o_kanban_record'),
                        login='admin', timeout=60)
        self.browser_js('/odoo/action-zivoit_retail_lms.zlms_learner_action', self._wait_for('.o_data_row'),
                        login='admin', timeout=60)
