# -*- coding: utf-8 -*-
import base64
import io
import json
import zipfile
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..models.zivo_ai_document import extract_text
from .common import ZivoCommon

SERVICE = 'odoo.addons.zivoit_event_connect.models.zivo_ai_service.ZivoAiService._zivo_call_provider'
POST = 'odoo.addons.zivoit_event_connect.models.zivo_ai_service.requests.post'


class FakeResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.text = json.dumps(data)

    def json(self):
        return self._data


class AiMixin:

    def _enable_ai(self, provider='openai', key='sk-test', **extra):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('zivoit_event_connect.ai_enabled', 'True')
        icp.set_param('zivoit_event_connect.ai_provider', provider)
        icp.set_param('zivoit_event_connect.ai_api_key', key)
        for feature in ('writer', 'search', 'booking', 'survey', 'document', 'anomaly'):
            icp.set_param('zivoit_event_connect.ai_feature_%s' % feature, 'True')
        for k, v in extra.items():
            icp.set_param('zivoit_event_connect.%s' % k, v)

    @contextmanager
    def _ai(self, answer):
        """Mock the provider: ``answer`` is a dict/str or a callable(system, prompt)."""
        calls = []

        def fake(_self, cfg, system, prompt, as_json):
            calls.append({'system': system, 'prompt': prompt, 'as_json': as_json})
            result = answer(system, prompt) if callable(answer) else answer
            text = json.dumps(result) if isinstance(result, (dict, list)) else result
            return text, {'input': 100, 'output': 50}
        with patch(SERVICE, autospec=True, side_effect=fake):
            yield calls


@tagged('post_install', '-at_install', 'zivo_event_connect')
class TestAiService(AiMixin, TransactionCase):

    def test_disabled_by_default(self):
        self.assertFalse(self.env['zivo.ai.service']._feature_enabled('writer'))
        with self.assertRaises(UserError):
            self.env['zivo.ai.service']._zivo_ai_complete('writer', 's', 'p')

    def test_feature_toggle(self):
        self._enable_ai()
        self.env['ir.config_parameter'].sudo().set_param('zivoit_event_connect.ai_feature_writer', False)
        self.assertFalse(self.env['zivo.ai.service']._feature_enabled('writer'))
        self.assertTrue(self.env['zivo.ai.service']._feature_enabled('booking'))

    def test_missing_key(self):
        self._enable_ai(key='')
        with self.assertRaises(UserError):
            self.env['zivo.ai.service']._zivo_ai_complete('writer', 's', 'p')

    def test_openai_payload(self):
        self._enable_ai('openai', ai_model='gpt-test')
        with patch(POST, return_value=FakeResp({'choices': [{'message': {'content': '{"a": 1}'}}],
                                                'usage': {'prompt_tokens': 12, 'completion_tokens': 3}})) as post:
            result = self.env['zivo.ai.service']._zivo_ai_complete('writer', 'sys', 'hello', as_json=True)
        self.assertEqual(result, {'a': 1})
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://api.openai.com/v1/chat/completions')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer sk-test')
        self.assertEqual(kwargs['json']['model'], 'gpt-test')
        self.assertEqual(kwargs['json']['response_format'], {'type': 'json_object'})
        self.assertEqual(kwargs['json']['messages'][1]['content'], 'hello')
        log = self.env['zivo.ai.log'].search([], limit=1)
        self.assertEqual((log.state, log.input_tokens, log.output_tokens, log.feature), ('ok', 12, 3, 'writer'))

    def test_anthropic_payload(self):
        self._enable_ai('anthropic')
        resp = FakeResp({'content': [{'type': 'text', 'text': 'pong'}], 'usage': {'input_tokens': 5, 'output_tokens': 1}})
        with patch(POST, return_value=resp) as post:
            result = self.env['zivo.ai.service']._zivo_ai_complete('test', 'sys', 'ping')
        self.assertEqual(result, 'pong')
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://api.anthropic.com/v1/messages')
        self.assertEqual(kwargs['headers']['x-api-key'], 'sk-test')
        self.assertEqual(kwargs['headers']['anthropic-version'], '2023-06-01')
        self.assertEqual(kwargs['json']['system'], 'sys')
        self.assertEqual(kwargs['json']['model'], 'claude-sonnet-4-5')

    def test_gemini_payload(self):
        self._enable_ai('gemini')
        resp = FakeResp({'candidates': [{'content': {'parts': [{'text': '{"x": "y"}'}]}}],
                         'usageMetadata': {'promptTokenCount': 9, 'candidatesTokenCount': 4}})
        with patch(POST, return_value=resp) as post:
            result = self.env['zivo.ai.service']._zivo_ai_complete('writer', 'sys', 'go', as_json=True)
        self.assertEqual(result, {'x': 'y'})
        args, kwargs = post.call_args
        self.assertIn('/v1beta/models/gemini-2.0-flash:generateContent', args[0])
        self.assertEqual(kwargs['headers']['x-goog-api-key'], 'sk-test')
        self.assertEqual(kwargs['json']['generationConfig']['responseMimeType'], 'application/json')

    def test_openai_compatible_without_key(self):
        self._enable_ai('openai_compatible', key='', ai_base_url='http://localhost:11434/v1/', ai_model='llama3.1')
        with patch(POST, return_value=FakeResp({'choices': [{'message': {'content': 'ok'}}]})) as post:
            self.env['zivo.ai.service']._zivo_ai_complete('writer', 's', 'p')
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'http://localhost:11434/v1/chat/completions')
        self.assertNotIn('Authorization', kwargs['headers'])

    def test_http_error_logged(self):
        self._enable_ai()
        with patch(POST, return_value=FakeResp({'error': {'message': 'Invalid key'}}, status=401)):
            with self.assertRaises(UserError) as ctx:
                self.env['zivo.ai.service']._zivo_ai_complete('writer', 's', 'p')
        self.assertIn('Invalid key', str(ctx.exception))
        # error logs are committed in a separate transaction in production; in tests check the log values
        with patch.object(type(self.env['zivo.ai.service']), '_zivo_log') as log:
            with patch(POST, return_value=FakeResp({'error': {'message': 'Invalid key'}}, status=401)):
                with self.assertRaises(UserError):
                    self.env['zivo.ai.service']._zivo_ai_complete('writer', 's', 'p')
        vals = log.call_args.args[0]
        self.assertEqual(vals['state'], 'error')
        self.assertTrue(log.call_args.kwargs.get('separate'))

    def test_network_error(self):
        import requests
        self._enable_ai()
        with patch(POST, side_effect=requests.ConnectionError('boom')):
            with self.assertRaises(UserError):
                self.env['zivo.ai.service']._zivo_ai_complete('writer', 's', 'p')

    def test_json_parsing(self):
        parse = self.env['zivo.ai.service']._zivo_parse_json
        self.assertEqual(parse('```json\n{"a": 2}\n```'), {'a': 2})
        self.assertEqual(parse('Sure! Here it is: {"b": [1, 2]} hope it helps'), {'b': [1, 2]})
        with self.assertRaises(UserError):
            parse('no json at all')

    def test_connection_test_action(self):
        self._enable_ai()
        with self._ai('pong'):
            action = self.env['zivo.ai.service'].action_test_connection()
        self.assertEqual(action['params']['type'], 'success')


@tagged('post_install', '-at_install', 'zivo_event_connect')
class TestAiFeatures(AiMixin, ZivoCommon, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._zivo_setup()

    def setUp(self):
        super().setUp()
        self._enable_ai()
        self.env['event.event']._zivo_ai_search_cache.clear()

    # ------------------------------------------------------------ writer
    def test_event_writer(self):
        answer = {
            'subtitle': 'Shape the next three years together',
            'description_html': '<h3>Why attend</h3><p>Two days of strategy.</p><script>alert(1)</script>',
            'seo_title': 'Board Dinner 2027 | Invitation only',
            'seo_description': 'Private strategy dinner for the board.',
            'invite_subject': 'Your personal invitation to the Board Dinner',
            'invite_body': 'Dear guest,\nWe would be delighted…',
            'social_post': 'Big things ahead #strategy',
        }
        wizard = self.env['zivo.ai.event.writer'].with_user(self.user_event).create({
            'event_id': self.ev_invite.id, 'brief': '- strategy\n- dinner', 'tone': 'formal'})
        with self._ai(answer) as calls:
            wizard.action_generate()
        self.assertIn('ZEC Board Dinner', calls[0]['prompt'])
        self.assertIn('formal', calls[0]['prompt'].lower())
        self.assertIn('Invitation Only', calls[0]['prompt'])
        self.assertEqual(wizard.state, 'done')
        self.assertNotIn('<script', str(wizard.description))
        wizard.action_apply()
        ev = self.ev_invite
        self.assertEqual(ev.subtitle, 'Shape the next three years together')
        self.assertIn('Why attend', str(ev.description))
        self.assertEqual(ev.website_meta_title, 'Board Dinner 2027 | Invitation only')
        self.assertEqual(ev.zivo_invite_subject, 'Your personal invitation to the Board Dinner')
        self.assertTrue(ev.message_ids.filtered(lambda m: 'social post' in (m.body or '')))
        self.assertTrue(self.env['zivo.audit.log'].search([('model', '=', 'event.event'), ('res_id', '=', ev.id),
                                                           ('action', '=', 'ai')]))
        # invitation e-mail uses the AI subject
        template = self.env.ref('zivoit_event_connect.mail_template_event_invitation')
        rendered = template._render_field('subject', self.invitation.ids)[self.invitation.id]
        self.assertEqual(rendered, 'Your personal invitation to the Board Dinner')

    def test_writer_apply_requires_generation(self):
        wizard = self.env['zivo.ai.event.writer'].create({'event_id': self.ev_open.id})
        with self.assertRaises(UserError):
            wizard.action_apply()

    def test_writer_disabled(self):
        self.env['ir.config_parameter'].sudo().set_param('zivoit_event_connect.ai_feature_writer', False)
        with self.assertRaises(UserError):
            self.ev_open.action_zivo_ai_writer()

    # ------------------------------------------------------------ NL search
    def test_interpret_query(self):
        tag = self.env['event.tag'].search([], limit=1)
        answer = {'keywords': None, 'departments': ['hrx', 'ghost'], 'tags': [tag.name.upper()] if tag else [],
                  'event_type': None, 'date_from': '2027-01-01', 'date_to': 'next week', 'city': 'Hyderabad',
                  'online': False}
        with self._ai(answer) as calls:
            result = self.env['event.event']._zivo_ai_interpret_query('hr workshops in hyderabad from january')
        self.assertIn('hrx (Human Resources)', calls[0]['prompt'])
        self.assertEqual(result['source'], 'ai')
        self.assertEqual(result['department'], 'hrx')
        self.assertEqual(result['from'], '2027-01-01')
        self.assertNotIn('to', result, 'invalid dates are dropped')
        self.assertEqual(result['city'], 'Hyderabad')
        self.assertEqual(result['online'], '0')
        if tag:
            self.assertEqual(result['tag'], str(tag.id))
        # cached
        with self._ai(answer) as calls:
            self.env['event.event']._zivo_ai_interpret_query('hr workshops in hyderabad from january')
        self.assertFalse(calls)

    def test_interpret_query_fallbacks(self):
        self.env['ir.config_parameter'].sudo().set_param('zivoit_event_connect.ai_feature_search', False)
        self.assertEqual(self.env['event.event']._zivo_ai_interpret_query('payroll'),
                         {'q': 'payroll', 'source': 'keyword'})
        self.env['ir.config_parameter'].sudo().set_param('zivoit_event_connect.ai_feature_search', 'True')
        with self._ai('not json'):
            result = self.env['event.event']._zivo_ai_interpret_query('payroll clinic')
        self.assertEqual(result['source'], 'keyword')

    # ------------------------------------------------------------ booking assistant
    def _setup_equipment(self):
        cat = self.env['zivo.equipment.category'].create({'name': 'AV'})
        proj = self.env['zivo.equipment'].create({
            'name': 'Projector', 'quantity': 2, 'tz': 'UTC', 'category_id': cat.id, 'open_sat': True, 'open_sun': True,
            'approver_ids': [(6, 0, self.user_approver.ids)]})
        other = self.env['zivo.equipment'].create({'name': 'Beamer', 'quantity': 1, 'tz': 'UTC', 'category_id': cat.id,
                                                   'open_sat': True, 'open_sun': True})
        day = datetime.utcnow().date() + timedelta(days=3)
        return proj, other, day

    def test_booking_facts_and_clamp(self):
        proj, other, day = self._setup_equipment()
        start, stop = datetime.combine(day, time(10)), datetime.combine(day, time(11))
        first = self.env['zivo.equipment.booking'].create({'equipment_id': proj.id, 'start': start, 'stop': stop,
                                                            'quantity': 1, 'user_id': self.user_requester.id})

        with self._ai({'decision': 'approve', 'confidence': 90, 'suggested_quantity': 1, 'reason': 'ok'}):
            first.action_submit()
        draft = self.env['zivo.equipment.booking'].create({'equipment_id': proj.id, 'start': start, 'stop': stop,
                                                            'quantity': 2, 'user_id': self.user_portal.id,
                                                            'purpose': 'demo'})
        facts = draft._zivo_booking_facts()
        self.assertEqual(facts['units_free_for_this_interval'], 1)
        self.assertEqual(facts['competing_pending_requests'], 1)
        self.assertTrue(any('Beamer' in a for a in facts['alternatives']))
        self.assertTrue(any('Projector, ' in a for a in facts['alternatives']), 'same equipment other slot')
        # AI wrongly says approve 2 units -> clamped to partial 1
        with self._ai({'decision': 'approve', 'confidence': 140, 'suggested_quantity': 2,
                       'reason': 'Looks fine', 'alternative': None}):
            draft.with_user(self.user_approver).action_zivo_ai_assess()
        self.assertEqual(draft.zivo_ai_decision, 'partial')
        self.assertEqual(draft.zivo_ai_suggested_qty, 1)
        self.assertEqual(draft.zivo_ai_confidence, 100)
        self.assertIn('Beamer', draft.zivo_ai_alternatives)
        self.assertTrue(draft.message_ids.filtered(lambda m: 'AI recommendation' in (m.body or '')))

    def test_booking_auto_assess_and_partial_approve(self):
        proj, other, day = self._setup_equipment()
        b = self.env['zivo.equipment.booking'].with_user(self.user_requester).create({
            'equipment_id': proj.id, 'start': datetime.combine(day, time(9)),
            'stop': datetime.combine(day, time(11)), 'quantity': 2})
        with self._ai({'decision': 'partial', 'confidence': 80, 'suggested_quantity': 1,
                       'reason': 'Second unit reserved for the town hall.'}) as calls:
            b.action_submit()
        self.assertEqual(len(calls), 1)
        self.assertEqual(b.state, 'submitted')
        self.assertEqual(b.zivo_ai_decision, 'partial')
        b.with_user(self.user_approver).action_zivo_approve_suggested()
        self.assertEqual(b.state, 'approved')
        self.assertEqual(b.quantity, 1)

    def test_booking_ai_failure_never_blocks(self):
        proj, other, day = self._setup_equipment()
        b = self.env['zivo.equipment.booking'].create({
            'equipment_id': proj.id, 'start': datetime.combine(day, time(9)),
            'stop': datetime.combine(day, time(10)), 'user_id': self.user_requester.id})
        with self._ai('garbage'):
            b.action_submit()
        self.assertEqual(b.state, 'submitted')
        self.assertFalse(b.zivo_ai_decision)

    def test_booking_auto_disabled(self):
        self.env['ir.config_parameter'].sudo().set_param('zivoit_event_connect.ai_booking_auto', False)
        proj, other, day = self._setup_equipment()
        b = self.env['zivo.equipment.booking'].create({
            'equipment_id': proj.id, 'start': datetime.combine(day, time(9)),
            'stop': datetime.combine(day, time(10)), 'user_id': self.user_requester.id})
        with self._ai({'decision': 'approve'}) as calls:
            b.action_submit()
        self.assertFalse(calls)

    # ------------------------------------------------------------ survey insights
    def _survey_with_answers(self):
        survey = self.env['survey.survey'].create({'title': 'ZEC Feedback', 'access_mode': 'public',
                                                   'zivo_allow_save_later': True})
        q = self.env['survey.question'].create({'survey_id': survey.id, 'title': 'What can we improve?',
                                                'question_type': 'text_box'})
        texts = ['Coffee was cold', 'Great speakers but rooms too small', 'Loved the networking']
        for i, text in enumerate(texts):
            ui = survey._create_answer(check_attempts=False, partner=self.partner_guest if i == 0 else False)
            ui._save_lines(q, text)
            ui._mark_done()
        return survey

    def test_survey_insights(self):
        survey = self._survey_with_answers()
        answer = {'summary': 'Attendees enjoyed content.', 'sentiment': {'positive': 60, 'neutral': 10, 'negative': 30},
                  'themes': [{'name': 'Venue', 'sentiment': 'negative', 'mentions': 2, 'example': 'rooms too small'},
                             {'name': 'Networking', 'sentiment': 'bogus', 'mentions': 'x'}, 'junk'],
                  'recommendations': ['Book a larger room', 'Fix coffee']}
        with self._ai(answer) as calls:
            action = survey.action_zivo_ai_insights()
        self.assertIn('Coffee was cold', calls[0]['prompt'])
        insight = self.env['zivo.survey.insight'].browse(action['res_id'])
        self.assertEqual(insight.responses_count, 3)
        self.assertEqual(insight.answers_count, 3)
        self.assertEqual(insight.sentiment_negative, 30)
        self.assertEqual(len(insight.theme_ids), 2)
        self.assertEqual(insight.theme_ids.filtered(lambda t: t.name == 'Networking').sentiment, 'neutral')
        self.assertIn('• Book a larger room', insight.recommendations)
        self.assertEqual(survey.zivo_insight_count, 1)

    def test_survey_insights_exclude_anonymous(self):
        survey = self._survey_with_answers()
        self.env['ir.config_parameter'].sudo().set_param('zivoit_event_connect.ai_survey_exclude_anonymous', 'True')
        with self._ai({'summary': 'ok', 'sentiment': {}, 'themes': []}) as calls:
            survey.action_zivo_ai_insights()
        self.assertIn('Coffee was cold', calls[0]['prompt'])
        self.assertNotIn('Loved the networking', calls[0]['prompt'])

    def test_survey_no_answers(self):
        survey = self.env['survey.survey'].create({'title': 'Empty'})
        with self.assertRaises(UserError):
            survey.action_zivo_ai_insights()

    def test_survey_at_risk_reminder(self):
        survey = self._survey_with_answers()
        ui = survey._create_answer(check_attempts=False, email='late@example.com')
        ui._mark_in_progress()
        ui._zivo_mark_saved()
        self.assertEqual(survey.zivo_at_risk_count, 0)
        ui.zivo_saved_date = fields.Datetime.now() - timedelta(days=5)
        survey.invalidate_recordset(['zivo_at_risk_count'])
        self.assertEqual(survey.zivo_at_risk_count, 1)
        action = survey.action_zivo_remind_at_risk()
        self.assertIn('1 reminder', action['params']['message'])
        self.assertTrue(self.env['mail.mail'].search([('email_to', 'ilike', 'late@example.com')]))

    # ------------------------------------------------------------ documents
    def test_text_extraction(self):
        self.assertEqual(extract_text(b'hello', 'a.txt'), 'hello')
        self.assertIn('Title', extract_text(b'<h1>Title</h1>', 'a.html'))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('word/document.xml', '<w:document><w:body><w:p><w:r><w:t>Deposit due</w:t></w:r></w:p>'
                                             '</w:body></w:document>')
        self.assertIn('Deposit due', extract_text(buf.getvalue(), 'c.docx'))
        from reportlab.pdfgen import canvas
        pdf = io.BytesIO()
        c = canvas.Canvas(pdf)
        c.drawString(100, 750, 'Venue contract signed 2027-01-15')
        c.save()
        self.assertIn('Venue contract', extract_text(pdf.getvalue(), 'v.pdf'))
        self.assertEqual(extract_text(b'\x89PNG', 'img.png'), '')

    def test_document_assistant(self):
        content = 'Venue agreement. Deposit of 50% due 2027-01-10. Final headcount by 2027-02-01.'
        doc = self.env['zivo.event.document'].create({
            'name': 'Venue', 'event_id': self.ev_open.id, 'filename': 'venue.txt',
            'datas': base64.b64encode(content.encode()).decode()})
        answer = {'summary': 'Venue agreement with deposit and headcount deadlines.', 'category': 'contract',
                  'key_dates': [{'date': '2027-01-10', 'what': 'Pay 50% deposit'},
                                {'date': 'soon', 'what': 'invalid'},
                                {'date': '2027-02-01', 'what': 'Confirm final headcount'}]}
        with self._ai(answer) as calls:
            doc.action_zivo_ai_analyse()
        self.assertIn('Deposit of 50%', calls[0]['prompt'])
        self.assertEqual(doc.zivo_ai_category, 'contract')
        self.assertEqual(len(doc.zivo_ai_date_ids), 2)
        doc.action_zivo_ai_apply_category()
        self.assertEqual(doc.category, 'contract')
        doc.action_zivo_ai_create_activities()
        self.assertEqual(len(self.ev_open.activity_ids.filtered(lambda a: 'deposit' in (a.summary or '').lower())), 1)
        self.assertTrue(all(doc.zivo_ai_date_ids.mapped('activity_created')))
        doc.action_zivo_ai_create_activities()  # idempotent
        self.assertEqual(len(self.ev_open.activity_ids.filtered(lambda a: 'deposit' in (a.summary or '').lower())), 1)

    def test_document_unreadable(self):
        doc = self.env['zivo.event.document'].create({
            'name': 'Logo', 'event_id': self.ev_open.id, 'filename': 'logo.png',
            'datas': base64.b64encode(b'\x89PNG....').decode()})
        with self.assertRaises(UserError):
            doc.action_zivo_ai_analyse()

    def test_document_explain_changes(self):
        doc = self.env['zivo.event.document'].create({
            'name': 'Agenda', 'event_id': self.ev_open.id, 'filename': 'agenda.txt',
            'datas': base64.b64encode(b'09:00 Welcome\n10:00 Keynote').decode()})
        doc._zivo_add_version(base64.b64encode(b'09:00 Welcome\n10:30 Keynote by CEO').decode(), 'agenda.txt')
        v1 = doc.version_ids.filtered(lambda v: v.version_number == 1)
        v2 = doc.version_ids.filtered(lambda v: v.version_number == 2)
        with self.assertRaises(UserError):
            v1.action_zivo_ai_explain_changes()
        with self._ai('- Keynote moved to 10:30 and now given by the CEO') as calls:
            action = v2.action_zivo_ai_explain_changes()
        self.assertIn('+10:30 Keynote by CEO', calls[0]['prompt'])
        self.assertIn('10:30', action['params']['message'])
        self.assertTrue(doc.message_ids.filtered(lambda m: 'changes v1' in (m.body or '')))

    def test_document_auto_analyse(self):
        self.env['ir.config_parameter'].sudo().set_param('zivoit_event_connect.ai_document_auto', 'True')
        with self._ai({'summary': 'Auto', 'category': 'agenda', 'key_dates': []}) as calls:
            doc = self.env['zivo.event.document'].create({
                'name': 'Plan', 'event_id': self.ev_open.id, 'filename': 'plan.txt',
                'datas': base64.b64encode(b'Day plan').decode()})
        self.assertEqual(len(calls), 1)
        self.assertEqual(doc.zivo_ai_summary, 'Auto')

    # ------------------------------------------------------------ anomalies
    def test_anomaly_rules(self):
        Alert = self.env['zivo.ai.alert']
        self.env['ir.config_parameter'].sudo().set_param('zivoit_event_connect.ai_feature_anomaly', False)
        docs = self.env['zivo.event.document'].create([{'name': 'D%s' % i, 'event_id': self.ev_open.id}
                                                       for i in range(5)])
        admin = self.env.ref('base.user_admin')
        docs.with_user(admin).unlink()
        self.ev_invite.with_user(self.user_event).zivo_access_mode = 'open'
        self.ev_ip.with_user(self.user_event).zivo_ip_ranges = '0.0.0.0/0'
        alerts = Alert._zivo_detect()
        rules = set(alerts.mapped('rule'))
        self.assertEqual(rules, {'mass_delete', 'access_loosened', 'ip_widened'})
        mass = alerts.filtered(lambda a: a.rule == 'mass_delete')
        self.assertEqual(mass.user_id, admin)
        self.assertEqual(mass.log_count, 5)
        self.assertEqual(alerts.filtered(lambda a: a.rule == 'ip_widened').severity, 'high')
        self.assertTrue(mass.activity_ids)
        # de-duplicated on next run
        self.assertFalse(Alert._zivo_detect())
        mass.action_ack()
        self.assertEqual(mass.state, 'ack')

    def test_anomaly_ai_assessment_and_cron(self):
        self.ev_invite.with_user(self.user_event).zivo_access_mode = 'open'
        with self._ai('Likely a mistake: the event was private. Revert and ask the user.'):
            alerts = self.env['zivo.ai.alert']._cron_detect()
        self.assertTrue(alerts)
        self.assertIn('Revert', alerts[0].ai_assessment)
        self.assertTrue(self.env['ir.config_parameter'].sudo().get_param('zivoit_event_connect.ai_anomaly_last_run'))

    def test_no_false_positive(self):
        self.ev_open.zivo_ip_ranges = '10.0.0.0/8'
        self.ev_open.name = 'Renamed'
        self.assertFalse(self.env['zivo.ai.alert']._zivo_detect())


@tagged('post_install', '-at_install', 'zivo_event_connect')
class TestAiSearchApi(AiMixin, ZivoCommon, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._zivo_setup()
        cls.env['zivo.api.client'].create({'name': 'AI WP', 'api_key': 'zec_ai_key_123'})

    def test_api_search(self):
        self._enable_ai()
        self.env['event.event']._zivo_ai_search_cache.clear()
        with self._ai({'keywords': None, 'departments': ['hrx']}):
            res = self.url_open('/api/zivo/v1/search?query=anything+for+hr+people',
                                headers={'X-API-Key': 'zec_ai_key_123'})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body['interpreted']['department'], 'hrx')
        self.assertEqual({e['name'] for e in body['data']}, {'ZEC Payroll Clinic'})

    def test_api_search_keyword_fallback(self):
        res = self.url_open('/api/zivo/v1/search?query=Payroll', headers={'X-API-Key': 'zec_ai_key_123'})
        body = res.json()
        self.assertEqual(body['interpreted']['source'], 'keyword')
        self.assertEqual({e['name'] for e in body['data']}, {'ZEC Payroll Clinic'})
        self.assertEqual(self.url_open('/api/zivo/v1/search', headers={'X-API-Key': 'zec_ai_key_123'}).status_code, 400)

    def test_portal_smart_alternatives(self):
        cat = self.env['zivo.equipment.category'].create({'name': 'Cams'})
        cam = self.env['zivo.equipment'].create({'name': 'ZEC Cam A', 'quantity': 1, 'tz': 'UTC', 'category_id': cat.id,
                                                 'open_sat': True, 'open_sun': True})
        self.env['zivo.equipment'].create({'name': 'ZEC Cam B', 'quantity': 1, 'tz': 'UTC', 'category_id': cat.id,
                                           'open_sat': True, 'open_sun': True})
        day = datetime.utcnow().date() + timedelta(days=4)
        b = self.env['zivo.equipment.booking'].create({'equipment_id': cam.id, 'user_id': self.user_requester.id,
                                                       'start': datetime.combine(day, time(9)),
                                                       'stop': datetime.combine(day, time(10))})
        b.action_submit()
        self.authenticate('zec_portal', 'zec_portal')
        res = self.url_open('/equipment/%s?date=%s' % (cam.id, day))
        self.assertIn('Smart suggestion', res.text)
        self.assertIn('ZEC Cam B', res.text)
