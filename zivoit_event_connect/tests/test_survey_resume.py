# -*- coding: utf-8 -*-
import re
from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install', 'zivo_event_connect')
class TestSurveySaveLater(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Survey = cls.env['survey.survey']
        cls.survey = Survey.create({
            'title': 'ZEC Long Application',
            'access_mode': 'public',
            'users_login_required': False,
            'questions_layout': 'page_per_section',
            'users_can_go_back': True,
            'zivo_allow_save_later': True,
        })
        Q = cls.env['survey.question']
        cls.page1 = Q.create({'survey_id': cls.survey.id, 'title': 'About you', 'is_page': True, 'sequence': 1})
        cls.q_name = Q.create({'survey_id': cls.survey.id, 'title': 'Your name', 'question_type': 'char_box',
                               'sequence': 2, 'constr_mandatory': True})
        cls.q_bio = Q.create({'survey_id': cls.survey.id, 'title': 'Short bio', 'question_type': 'text_box',
                              'sequence': 3, 'constr_mandatory': True})
        cls.page2 = Q.create({'survey_id': cls.survey.id, 'title': 'Project details', 'is_page': True, 'sequence': 10})
        cls.q_budget = Q.create({'survey_id': cls.survey.id, 'title': 'Budget', 'question_type': 'numerical_box',
                                 'sequence': 11, 'constr_mandatory': True})

    def _new_answer(self):
        answer = self.survey.sudo()._create_answer(check_attempts=False)
        answer._mark_in_progress()
        return answer

    def _save_later(self, answer, params):
        return self.make_jsonrpc_request(
            '/survey/zivo/save_later/%s/%s' % (self.survey.access_token, answer.access_token), params)

    def test_save_partial_page_without_validation(self):
        answer = self._new_answer()
        # q_bio is mandatory but left empty: save later must still work
        result = self._save_later(answer, {'page_id': self.page1.id, str(self.q_name.id): 'Asha'})
        self.assertEqual(result['saved_questions'], 1)
        answer.invalidate_recordset()
        self.assertTrue(answer.zivo_saved_later)
        self.assertRegex(answer.zivo_resume_code, r'^[A-Z2-9]{4}-[A-Z2-9]{4}$')
        self.assertFalse(answer.last_displayed_page_id, 'resume on first page')
        line = answer.user_input_line_ids.filtered(lambda l: l.question_id == self.q_name)
        self.assertEqual(line.value_char_box, 'Asha')
        # resume link displays page 1 with the saved value
        res = self.url_open('/survey/%s/%s' % (self.survey.access_token, answer.access_token))
        self.assertEqual(res.status_code, 200)
        self.assertIn('About you', res.text)
        self.assertIn('Asha', res.text)
        self.assertIn('o_zivo_survey_save_later', res.text)

    def test_resume_on_second_page(self):
        answer = self._new_answer()
        self._save_later(answer, {'page_id': self.page1.id, str(self.q_name.id): 'Asha', str(self.q_bio.id): 'Dev'})
        self._save_later(answer, {'page_id': self.page2.id, str(self.q_budget.id): '1500'})
        answer.invalidate_recordset()
        self.assertEqual(answer.last_displayed_page_id, self.page1)
        self.assertEqual(answer.zivo_save_count, 2)
        res = self.url_open('/survey/%s/%s' % (self.survey.access_token, answer.access_token))
        self.assertIn('Project details', res.text)
        self.assertIn('1500', res.text)

    def test_saved_page_email_and_code(self):
        answer = self._new_answer()
        result = self._save_later(answer, {'page_id': self.page1.id, str(self.q_name.id): 'Asha'})
        answer.invalidate_recordset()
        page = self.url_open(result['url'])
        self.assertEqual(page.status_code, 200)
        self.assertIn(answer.zivo_resume_code, page.text)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        res = self.url_open('/survey/zivo/send_resume/%s/%s' % (self.survey.access_token, answer.access_token),
                            data={'email': 'asha@example.com', 'csrf_token': csrf})
        self.assertEqual(res.status_code, 200)
        self.assertIn('sent=1', res.url)
        answer.invalidate_recordset()
        self.assertEqual(answer.zivo_resume_email, 'asha@example.com')
        mail = self.env['mail.mail'].sudo().search([('email_to', 'ilike', 'asha@example.com')], limit=1)
        self.assertTrue(mail or self.env['mail.message'].sudo().search(
            [('model', '=', 'survey.user_input'), ('res_id', '=', answer.id)]))
        # resume by code (case/format insensitive)
        code = answer.zivo_resume_code.replace('-', '').lower()
        res = self.url_open('/survey-resume?code=%s' % code, allow_redirects=False)
        self.assertIn(res.status_code, (302, 303))
        self.assertIn('/survey/%s/%s' % (self.survey.access_token, answer.access_token), res.headers['Location'])
        res = self.url_open('/survey-resume?code=ZZZZ-9999')
        self.assertIn('unknown, expired', res.text)

    def test_code_expiry_and_disabled(self):
        answer = self._new_answer()
        self._save_later(answer, {'page_id': self.page1.id})
        answer.invalidate_recordset()
        UI = self.env['survey.user_input']
        self.assertEqual(UI._zivo_find_by_code(answer.zivo_resume_code), answer)
        answer.zivo_saved_date = fields.Datetime.now() - timedelta(days=31)
        self.assertFalse(UI._zivo_find_by_code(answer.zivo_resume_code))
        self.survey.zivo_allow_save_later = False
        other = self._new_answer()
        self.assertEqual(self._save_later(other, {'page_id': self.page1.id}), {'error': 'unauthorized'})
        res = self.url_open('/survey/%s/%s' % (self.survey.access_token, other.access_token))
        self.assertNotIn('o_zivo_survey_save_later', res.text)

    def test_saved_counter(self):
        answer = self._new_answer()
        self._save_later(answer, {'page_id': self.page1.id})
        self.survey.invalidate_recordset()
        self.assertEqual(self.survey.zivo_saved_count, 1)
        answer._mark_done()
        self.survey.invalidate_recordset()
        self.assertEqual(self.survey.zivo_saved_count, 0)
