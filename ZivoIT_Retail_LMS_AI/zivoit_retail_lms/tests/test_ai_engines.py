# -*- coding: utf-8 -*-
import json
from unittest.mock import patch

from odoo.tests import tagged

from odoo.addons.zivoit_retail_lms.models.ai_service import ZlmsAiService

from .common import ZlmsCommon


@tagged('post_install', '-at_install', 'zlms')
class TestAiEngines(ZlmsCommon):

    def _enable_ai(self, provider='openai'):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('zivoit_retail_lms.ai_provider', provider)
        icp.set_param('zivoit_retail_lms.ai_api_key', 'test-key')

    def test_01_offline_by_default(self):
        self.assertFalse(self.env['zlms.ai.service'].is_enabled())

    def test_02_offline_course_builder_from_cards(self):
        wizard = self.env['zlms.ai.course.wizard'].create({
            'source': 'product_card', 'product_card_ids': [(6, 0, (self.card | self.card2).ids)],
            'topic': 'Offline PK', 'question_count': 5})
        action = wizard.action_generate()
        channel = self.env['slide.channel'].browse(action['res_id'])
        self.assertEqual(channel.zlms_retail_category, 'product')
        self.assertFalse(channel.zlms_ai_generated)
        self.assertEqual(len(channel.slide_ids), 2)
        quiz = channel.slide_ids.filtered(lambda s: s.slide_category == 'quiz')
        self.assertTrue(1 <= len(quiz.question_ids) <= 5)
        for question in quiz.question_ids:
            self.assertEqual(len(question.answer_ids.filtered('is_correct')), 1)
            self.assertGreaterEqual(len(question.answer_ids), 2)
            self.assertFalse(question.answers_validation_error)
        self.assertEqual(self.card.channel_id, channel)

    def test_03_offline_course_builder_from_text(self):
        text = ('Every customer must be greeted within ten seconds of entering the store. '
                'Exchanges are accepted within fifteen days with the original receipt. '
                'Loyalty enrolment should be offered during every billing transaction.')
        wizard = self.env['zlms.ai.course.wizard'].create({'source': 'text', 'source_text': text,
                                                           'topic': 'SOP', 'retail_category': 'compliance'})
        channel = self.env['slide.channel'].browse(wizard.action_generate()['res_id'])
        questions = channel.slide_ids.question_ids
        self.assertEqual(len(questions), 3)
        self.assertTrue(all('_____' in q.question for q in questions))

    def test_04_ai_course_builder_with_mocked_provider(self):
        self._enable_ai()
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('zivoit_retail_lms.brand_name', 'Acme Electronics')
        icp.set_param('zivoit_retail_lms.retail_sector', 'electronics')
        icp.set_param('zivoit_retail_lms.market', 'UAE')
        payload = {
            'summary': 'AI summary',
            'lesson_html': '<h3>Lesson</h3><p>Dry fabric</p>',
            'questions': [
                {'question': 'Q1?', 'answers': [{'text': 'A', 'correct': True}, {'text': 'B', 'correct': False}]},
                {'question': 'Bad question without correct answer', 'answers': [{'text': 'x'}, {'text': 'y'}]},
            ],
        }
        with patch.object(ZlmsAiService, '_call_provider', return_value=json.dumps(payload)) as mocked:
            wizard = self.env['zlms.ai.course.wizard'].create({
                'source': 'product_card', 'product_card_ids': [(6, 0, self.card.ids)], 'topic': 'AI PK',
                'language': 'Telugu'})
            self.assertTrue(wizard.use_ai)
            channel = self.env['slide.channel'].browse(wizard.action_generate()['res_id'])
            prompt = mocked.call_args[0][1][0]['content']
            self.assertIn('Telugu', prompt)
            self.assertIn('Test Training Tee', prompt)
            self.assertIn('Acme Electronics is a consumer electronics retailer', prompt)
            self.assertIn('UAE', prompt)
        self.assertTrue(channel.zlms_ai_generated)
        self.assertEqual(len(channel.slide_ids.question_ids), 1, 'Invalid AI questions are dropped')
        log = self.env['zlms.ai.log'].search([('feature', '=', 'course_builder')], limit=1)
        self.assertTrue(log.success)

    def test_05_ai_failure_falls_back_to_offline(self):
        self._enable_ai('anthropic')
        with patch.object(ZlmsAiService, '_call_provider', side_effect=ConnectionError('boom')):
            wizard = self.env['zlms.ai.course.wizard'].create({
                'source': 'product_card', 'product_card_ids': [(6, 0, self.card.ids)], 'topic': 'Fallback'})
            channel = self.env['slide.channel'].browse(wizard.action_generate()['res_id'])
        self.assertFalse(channel.zlms_ai_generated)
        self.assertTrue(channel.slide_ids.question_ids)
        log = self.env['zlms.ai.log'].search([('feature', '=', 'course_builder')], limit=1)
        self.assertFalse(log.success)

    def test_06_ai_roleplay_customer_and_evaluation(self):
        self._enable_ai('gemini')
        learner = self._new_learner('AI Roleplay')
        evaluation = {'criteria': [{'name': 'Greeting', 'score': 8, 'comment': 'Warm'},
                                   {'name': 'Discovery', 'score': 6, 'comment': 'Ask more'}],
                      'overall': 72, 'strengths': ['Warm greeting'], 'improvements': ['Ask about frequency'],
                      'model_answer': 'How often do you train?'}
        responses = ['Mostly evenings, I sweat a lot.', 'Okay I will take it [END]', json.dumps(evaluation)]
        with patch.object(ZlmsAiService, '_call_provider', side_effect=responses):
            session = self.env['zlms.roleplay.session'].create({'scenario_id': self.scenario.id,
                                                                'learner_id': learner.id})
            session.action_start()
            self.assertEqual(session.engine, 'ai')
            self.assertEqual(session.send_message('Hi! What brings you in today?'), 'Mostly evenings, I sweat a lot.')
            last = session.send_message('This tee keeps you dry. Want to try it?')
            self.assertEqual(last, 'Okay I will take it')
        self.assertEqual(session.state, 'evaluated', '[END] token closes the conversation')
        self.assertEqual(session.score, 72)
        self.assertEqual(len(session.criterion_ids), 2)
        self.assertIn('How often do you train?', session.feedback_html)

    def test_07_insights_offline_and_ai(self):
        learner = self._new_learner('Insight')
        insight = self.env['zlms.ai.insight'].generate_insight('learner', learner=learner)
        self.assertEqual(insight.engine, 'offline')
        self.assertIn('Insight', insight.content_html)
        store_insight = self.env['zlms.ai.insight'].generate_insight('store', store=self.store)
        self.assertTrue(store_insight.content_html)
        self._enable_ai()
        ai_result = {'summary': 'Network is improving', 'gaps': ['Gap A'], 'actions': ['Do B'],
                     'recommended_path_codes': ['T-ONB'], 'manager_tip': 'Tip C'}
        with patch.object(ZlmsAiService, '_call_provider', return_value=json.dumps(ai_result)):
            net = self.env['zlms.ai.insight'].generate_insight('network')
        self.assertEqual(net.engine, 'ai')
        self.assertIn('Network is improving', net.content_html)
        self.assertEqual(net.recommended_path_ids, self.path)

    def test_08_ask_the_expert(self):
        res = self.env['zlms.ask.expert.wizard'].answer_question('Is the training tee good for the gym?')
        self.assertEqual(res['engine'], 'offline')
        self.assertIn(self.card.id, res['card_ids'])
        self.assertIn('You stay dry', res['html'])
        res = self.env['zlms.ask.expert.wizard'].answer_question('zzzz qqqq')
        self.assertFalse(res['card_ids'])
        self._enable_ai()
        with patch.object(ZlmsAiService, '_call_provider', return_value='- It keeps you dry\nSay: try it on!') as m:
            res = self.env['zlms.ask.expert.wizard'].answer_question('Is the training tee good for the gym?', 'Hindi')
            self.assertIn('Hindi', m.call_args[0][2])
        self.assertEqual(res['engine'], 'ai')
        self.assertIn('It keeps you dry', res['html'])

    def test_09_json_parsing_and_provider_payloads(self):
        service = self.env['zlms.ai.service']
        self.assertEqual(service.parse_json('```json\n{"a": 1}\n```'), {'a': 1})
        self.assertEqual(service.parse_json('Here you go: {"b": 2} thanks'), {'b': 2})

        class FakeResp:
            def __init__(self, data):
                self.data = data

            def raise_for_status(self):
                return None

            def json(self):
                return self.data

        cfg = {'api_key': 'k', 'model': 'm', 'base_url': '', 'temperature': 0.2, 'timeout': 5}
        msgs = [{'role': 'user', 'content': 'hi'}]
        with patch('odoo.addons.zivoit_retail_lms.models.ai_service.requests.post',
                   return_value=FakeResp({'choices': [{'message': {'content': 'ok-openai'}}]})) as post:
            self.assertEqual(service._call_provider(dict(cfg, provider='openai'), msgs, 'sys', True, 10), 'ok-openai')
            self.assertIn('api.openai.com', post.call_args[0][0])
            self.assertEqual(post.call_args[1]['json']['response_format'], {'type': 'json_object'})
        with patch('odoo.addons.zivoit_retail_lms.models.ai_service.requests.post',
                   return_value=FakeResp({'content': [{'type': 'text', 'text': 'ok-claude'}]})) as post:
            self.assertEqual(service._call_provider(dict(cfg, provider='anthropic'), msgs, 'sys', False, 10), 'ok-claude')
            self.assertEqual(post.call_args[1]['headers']['x-api-key'], 'k')
            self.assertEqual(post.call_args[1]['json']['system'], 'sys')
        with patch('odoo.addons.zivoit_retail_lms.models.ai_service.requests.post',
                   return_value=FakeResp({'candidates': [{'content': {'parts': [{'text': 'ok-gemini'}]}}]})) as post:
            self.assertEqual(service._call_provider(dict(cfg, provider='gemini'), msgs, 'sys', True, 10), 'ok-gemini')
            self.assertIn(':generateContent', post.call_args[0][0])
