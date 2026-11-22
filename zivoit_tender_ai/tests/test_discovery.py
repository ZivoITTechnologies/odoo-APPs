# -*- coding: utf-8 -*-
import json
from datetime import timedelta
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import TenderCommon, fake_response

SRC = 'odoo.addons.zivoit_tender_ai.models.tender_source.requests'
AI = 'odoo.addons.zivoit_tender_ai.models.tender_ai.requests'

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Active tenders</title>
<item><title>Construction of FOB at Kacheguda station</title>
<link>https://www.ireps.gov.in/tender/1001</link>
<guid>SCR-HYB-2026-55</guid>
<pubDate>Mon, 16 Nov 2026 10:00:00 +0530</pubDate>
<description>Organisation: South Central Railway  Estimated Cost: Rs. 4.85 Crore  EMD: Rs 3.9 Lakh  Bid Submission End Date: 15-Dec-2030 15:00  Completion Period: 12 Months</description>
</item>
<item><title>Annual maintenance of street lights in Warangal</title>
<link>https://tender.telangana.gov.in/2</link>
<description>Tender value: 45 Lakhs. Last date of submission: 10/12/2030 17:00</description>
</item>
</channel></rss>"""

HTML = b"""<html><body><table id="t">
<tr><th>#</th><th>Work</th><th>Tender ID</th><th>Value</th><th>Closing</th></tr>
<tr><td>1</td><td><a href="/view/77">Widening of Medak - Siddipet road to two lane with paved shoulders</a></td>
    <td>2026_RB_77</td><td>Rs 23.4 Cr</td><td>20-Dec-2030 03:00 PM</td></tr>
<tr><td>2</td><td><a href="/view/78">Construction of Gram Panchayat building at Jangaon</a></td>
    <td>2026_PR_78</td><td>85,00,000</td><td>22/12/2030 15:00</td></tr>
</table></body></html>"""


@tagged('post_install', '-at_install', 'zivoit_tender')
class TestDiscovery(TenderCommon):

    def test_create_autoclassify(self):
        tender = self._tender(name='Construction of Road Over Bridge in lieu of LC 12 near Kazipet',
                              organisation='South Central Railway', department='Sr DEN Secunderabad Division',
                              location='Warangal', estimated_value=0)
        self.assertTrue(tender.code.startswith('TND/'))
        self.assertEqual(tender.zone_id, self.scr)
        self.assertEqual(tender.state_id, self.ts)
        self.assertIn('bridges', tender.sector_ids.mapped('code'))
        self.assertIn('railways', tender.sector_ids.mapped('code'))
        self.assertEqual(tender.value_band, 'unknown')
        self.assertEqual(tender.stage_type, 'new')
        self.assertTrue(tender.is_open)
        self.assertTrue(tender.fingerprint)

    def test_bands_and_buckets(self):
        t = self._tender(estimated_value=40 * 1e5, date_closing=self.now + timedelta(hours=30), completion_months=5)
        self.assertEqual(t.value_band, 'upto_50l')
        self.assertEqual(t.deadline_bucket, '3d')
        self.assertEqual(t.duration_band, 'upto_6m')
        t.write({'estimated_value': 150 * 1e7, 'date_closing': self.now + timedelta(days=45), 'completion_months': 30})
        self.assertEqual(t.value_band, 'above_100cr')
        self.assertEqual(t.deadline_bucket, 'later')
        self.assertEqual(t.duration_band, '24_36m')
        t.date_closing = self.now - timedelta(days=1)
        self.assertEqual(t.deadline_bucket, 'closed')
        self.assertFalse(t.is_open)
        self.assertEqual(t.days_left_label, 'Closed')

    def test_ingest_and_dedup(self):
        rows = [{
            'title': 'Doubling between Kazipet and Vijayawada - earthwork', 'reference': 'SCR-BZA-77',
            'organisation': 'South Central Railway', 'estimated_value': 'Rs. 42.3 Crore',
            'emd': '26 Lakh', 'closing_date': '20-Dec-2030 15:00', 'completion_period': '24 Months',
            'state': 'Andhra Pradesh', 'url': 'https://www.ireps.gov.in/x',
        }]
        tenders, created, updated = self.Tender._ingest_rows(rows)
        self.assertEqual((created, updated), (1, 0))
        self.assertAlmostEqual(tenders.estimated_value, 42.3e7)
        self.assertAlmostEqual(tenders.emd_amount, 26e5)
        self.assertEqual(tenders.completion_months, 24)
        self.assertEqual(tenders.state_id, self.ap)
        self.assertEqual(tenders.zone_id, self.scr)
        # same reference from another portal with an extended deadline -> merged, not duplicated
        rows2 = [dict(rows[0], title='DOUBLING KAZIPET-VIJAYAWADA', reference='scr bza 77',
                      closing_date='27-Dec-2030 15:00', tender_fee='5900')]
        again, created, updated = self.Tender._ingest_rows(rows2)
        self.assertEqual((created, updated), (0, 1))
        self.assertEqual(again, tenders)
        self.assertEqual(tenders.seen_count, 2)
        self.assertEqual(tenders.date_closing.day, 27, 'Extended deadline (corrigendum) is applied')
        self.assertEqual(tenders.tender_fee, 5900)
        self.assertIn('extended', tenders.message_ids[0].body)
        # duplicates inside a single batch are ignored
        _t, created, _u = self.Tender._ingest_rows([dict(rows[0], reference='NEW-1'), dict(rows[0], reference='NEW-1')])
        self.assertEqual(created, 1)
        # rows without a title are skipped
        _t, created, _u = self.Tender._ingest_rows([{'reference': 'X'}])
        self.assertEqual(created, 0)

    def test_ingest_never_overwrites_user_edits(self):
        tender = self._tender(reference='REF-9001', estimated_value=1e7)
        self.Tender._ingest_rows([{'title': 'x', 'reference': 'REF-9001', 'estimated_value': '5 Cr'}])
        self.assertEqual(tender.estimated_value, 1e7)

    def test_rss_connector(self):
        source = self.Source.create({'name': 'IREPS feed', 'source_type': 'rss', 'url': 'https://example.gov.in/rss?q={keyword}',
                                     'portal': 'ireps'})
        with patch(SRC + '.get', return_value=fake_response(RSS)) as get:
            tenders = source._run(query='FOB', filters={'keywords': ['fob']})
        self.assertIn('q=fob', get.call_args[0][0])
        self.assertEqual(len(tenders), 2)
        fob = tenders.filtered(lambda t: 'FOB' in t.name)
        self.assertAlmostEqual(fob.estimated_value, 4.85e7)
        self.assertAlmostEqual(fob.emd_amount, 3.9e5)
        self.assertEqual(fob.date_closing.year, 2030)
        self.assertEqual(fob.url, 'https://www.ireps.gov.in/tender/1001')
        self.assertEqual(fob.source_id, source)
        self.assertEqual(fob.portal, 'IREPS - Indian Railways')
        self.assertEqual(source.last_status, 'ok')
        self.assertEqual(source.last_count, 2)
        self.assertEqual(source.log_ids[:1].created, 2)
        lights = tenders - fob
        self.assertAlmostEqual(lights.estimated_value, 45e5)
        self.assertIn('maintenance', lights.sector_ids.mapped('code'))

    def test_html_connector(self):
        source = self.Source.create({
            'name': 'Telangana listing', 'source_type': 'html', 'url': 'https://tender.example.gov.in/list',
            'row_xpath': '//table[@id="t"]//tr[td]',
            'field_mapping': json.dumps({'title': './td[2]//text()', 'reference': './td[3]//text()',
                                         'estimated_value': './td[4]//text()', 'closing_date': './td[5]//text()',
                                         'url': './td[2]/a/@href'}),
            'default_state_id': self.ts.id,
        })
        with patch(SRC + '.get', return_value=fake_response(HTML, url='https://tender.example.gov.in/list')):
            tenders = source._run()
        self.assertEqual(len(tenders), 2)
        road = tenders.filtered(lambda t: 'Widening' in t.name)
        self.assertAlmostEqual(road.estimated_value, 23.4e7)
        self.assertEqual(road.url, 'https://tender.example.gov.in/view/77')
        self.assertEqual(road.state_id, self.ts)
        self.assertIn('roads', road.sector_ids.mapped('code'))
        gp = tenders - road
        self.assertAlmostEqual(gp.estimated_value, 85e5)
        self.assertEqual(gp.date_closing.day, 22)

    def test_json_connector(self):
        payload = {'data': {'items': [
            {'workName': 'Construction of 2 lane bridge across Musi river', 'tenderNo': 'GHMC/2026/88',
             'dept': {'name': 'GHMC'}, 'ecv': 125000000, 'lastDate': '2030-12-20T15:00:00', 'link': 'https://x/88'},
            {'workName': '', 'tenderNo': 'empty'},
        ]}}
        source = self.Source.create({
            'name': 'Aggregator API', 'source_type': 'json_api', 'url': 'https://api.example.com/tenders?q={query}',
            'http_method': 'POST', 'request_body': '{"q": "{query}"}',
            'request_headers': '{"X-Api-Key": "secret"}', 'results_path': 'data.items',
            'field_mapping': json.dumps({'title': 'workName', 'reference': 'tenderNo', 'organisation': 'dept.name',
                                         'estimated_value': 'ecv', 'closing_date': 'lastDate', 'url': 'link'}),
        })
        with patch(SRC + '.post', return_value=fake_response(json_data=payload)) as post:
            tenders = source._run(query='bridge')
        self.assertEqual(post.call_args.kwargs['headers']['X-Api-Key'], 'secret')
        self.assertEqual(len(tenders), 1)
        self.assertEqual(tenders.organisation, 'GHMC')
        self.assertEqual(tenders.estimated_value, 125000000)
        self.assertEqual(tenders.state_id, self.ts)

    def test_source_errors_are_logged_not_raised(self):
        bad = self.Source.create({'name': 'Broken', 'source_type': 'json_api', 'url': 'https://down.example.com'})
        good = self.Source.create({'name': 'Feed', 'source_type': 'rss', 'url': 'https://ok.example.com'})

        def side_effect(url, **kw):
            if 'down' in url:
                raise ConnectionError('portal down')
            return fake_response(RSS)
        with patch(SRC + '.get', side_effect=side_effect):
            tenders = (bad | good)._run()
        self.assertEqual(bad.last_status, 'error')
        self.assertIn('portal down', bad.last_message)
        self.assertEqual(good.last_status, 'ok')
        self.assertEqual(len(tenders), 2)

    def test_invalid_json_mapping(self):
        source = self.Source.create({'name': 'Bad map', 'source_type': 'html', 'url': 'https://x', 'field_mapping': '{oops'})
        with patch(SRC + '.get', return_value=fake_response(HTML)):
            with self.assertRaises(UserError):
                source._fetch()

    def test_ai_web_search_requires_provider(self):
        source = self.env.ref('zivoit_tender_ai.source_ai_ireps')
        with self.assertRaises(UserError):
            source._fetch('railway tenders SCR')
        source._run('railway tenders SCR')
        self.assertEqual(source.last_status, 'error')

    def _enable_ai(self, provider='anthropic'):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('zivoit_tender_ai.ai_provider', provider)
        icp.set_param('zivoit_tender_ai.ai_api_key', 'test-key')

    def test_ai_web_search_anthropic(self):
        self._enable_ai('anthropic')
        found = [{'title': 'Construction of ROB at Kazipet in lieu of LC 45', 'reference': 'IREPS-SCR-145',
                  'organisation': 'South Central Railway', 'state': 'Telangana', 'estimated_value': 185000000,
                  'emd': 1075000, 'closing_date': '2030-12-15T15:00:00', 'completion_period': '18 Months',
                  'url': 'https://www.ireps.gov.in/145', 'portal': 'IREPS'}]
        answer = {'content': [{'type': 'server_tool_use', 'name': 'web_search'},
                              {'type': 'text', 'text': 'Here are the tenders:\n```json\n%s\n```' % json.dumps(found)}]}
        source = self.env.ref('zivoit_tender_ai.source_ai_ireps')
        with patch(AI + '.post', return_value=fake_response(json_data=answer)) as post:
            tenders = source._run('open railway construction tenders SCR', filters={'zones': ['SCR']})
        payload = post.call_args.kwargs['json']
        self.assertEqual(post.call_args.args[0], 'https://api.anthropic.com/v1/messages')
        self.assertEqual(post.call_args.kwargs['headers']['x-api-key'], 'test-key')
        self.assertEqual(payload['tools'][0]['name'], 'web_search')
        self.assertIn('ireps.gov.in', payload['tools'][0]['allowed_domains'])
        self.assertIn('SCR', payload['messages'][0]['content'])
        self.assertEqual(len(tenders), 1)
        self.assertEqual(tenders.zone_id, self.scr)
        self.assertIn('railways', tenders.sector_ids.mapped('code'))  # source default sector
        self.assertEqual(tenders.estimated_value, 185000000)

    def test_ai_web_search_openai(self):
        self._enable_ai('openai')
        found = [{'title': 'Four laning of NH-163 Hyderabad - Bhupalpatnam', 'organisation': 'NHAI',
                  'estimated_value': '650 Crore', 'closing_date': '2030-12-30'}]
        answer = {'output': [{'type': 'web_search_call'},
                             {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(found)}]}]}
        with patch(AI + '.post', return_value=fake_response(json_data=answer)) as post:
            rows = self.env['zivoit.tender.ai'].search_tenders_web('NHAI road tenders Telangana', ['nhai.gov.in'])
        self.assertEqual(post.call_args.args[0], 'https://api.openai.com/v1/responses')
        self.assertEqual(post.call_args.kwargs['json']['tools'][0]['type'], 'web_search')
        self.assertEqual(rows[0]['organisation'], 'NHAI')

    def test_ai_api_error(self):
        self._enable_ai('anthropic')
        with patch(AI + '.post', return_value=fake_response(b'{"error": "bad key"}', status=401)):
            with self.assertRaises(UserError):
                self.env['zivoit.tender.ai']._call_llm('s', 'p')

    def test_ai_query_parse_merges_with_rules(self):
        self._enable_ai('anthropic')
        answer = {'content': [{'type': 'text', 'text': json.dumps({
            'keywords': ['rob'], 'zones': ['scr'], 'sectors': ['bridges'], 'min_value': 20000000, 'open_only': True})}]}
        with patch(AI + '.post', return_value=fake_response(json_data=answer)):
            result = self.env['zivoit.tender.ai'].parse_query('ROB tenders in SCR above 2 crore')
        self.assertEqual(result['engine'], 'ai')
        self.assertEqual(result['zones'], ['SCR'])
        self.assertIn('bridges', result['sectors'])
        self.assertIn('railways', result['sectors'])
        self.assertEqual(result['min_value'], 20000000)
        # AI failure falls back silently to rules
        with patch(AI + '.post', side_effect=TimeoutError('slow')):
            result = self.env['zivoit.tender.ai'].parse_query('ROB tenders in SCR')
        self.assertEqual(result['engine'], 'rules')
        self.assertEqual(result['zones'], ['SCR'])

    def test_ai_analysis(self):
        tender = self._tender()
        tender.action_analyse()
        self.assertEqual(tender.ai_engine, 'rules')
        self.assertIn('Buyer: South Central Railway', tender.ai_summary)
        self.assertIn(tender.ai_recommendation, ('go', 'review', 'no_go'))
        self._enable_ai('anthropic')
        answer = {'content': [{'type': 'text', 'text': json.dumps({
            'summary': '- ROB 18.5 Cr', 'scope': 'ROB', 'eligibility': 'Similar work 35%', 'risks': 'Block',
            'recommendation': 'Go', 'score': 140, 'reason': 'Fits'})}]}
        with patch(AI + '.post', return_value=fake_response(json_data=answer)):
            tender.action_analyse()
        self.assertEqual((tender.ai_engine, tender.ai_recommendation, tender.ai_score), ('ai', 'go', 100))
        self.assertEqual(tender.ai_eligibility, 'Similar work 35%')

    def test_email_alert_creates_tender(self):
        profile = self.Profile.create({'name': 'Telangana buildings', 'state_ids': [(6, 0, self.ts.ids)],
                                       'sector_ids': [(6, 0, self.sector_bldg.ids)], 'notify_mode': 'none'})
        body = """<p>Dear Bidder, a new tender matching your profile has been published.</p>
        <p>Tender ID: 2026_TSEWIDC_99812  Name of Work: Construction of Government Degree College building at Jangaon
        Organisation Name: TS Education and Welfare Infrastructure Development Corporation  Location: Jangaon, Telangana
        Tender Value: Rs. 7.25 Crore  EMD Amount: Rs. 7,25,000  Bid Submission End Date: 18-Dec-2030 15:00
        Period of Completion: 12 Months</p><p>https://tender.telangana.gov.in/view?id=99812</p>"""
        msg = {'subject': 'Tender alert: new tender published', 'body': body, 'from': 'alerts@portal.gov.in',
               'email_from': 'alerts@portal.gov.in', 'to': 'tenders@example.com', 'message_id': '<t1@portal>',
               'date': '2026-11-20 10:00:00', 'partner_ids': [], 'attachments': [], 'cc': ''}
        tender = self.Tender.message_new(msg)
        self.assertIn('Degree College', tender.name)
        self.assertEqual(tender.reference, '2026_TSEWIDC_99812')
        self.assertAlmostEqual(tender.estimated_value, 7.25e7)
        self.assertAlmostEqual(tender.emd_amount, 7.25e5)
        self.assertEqual(tender.date_closing.year, 2030)
        self.assertEqual(tender.completion_months, 12)
        self.assertEqual(tender.state_id, self.ts)
        self.assertIn('buildings', tender.sector_ids.mapped('code'))
        self.assertEqual(tender.source_id, self.env.ref('zivoit_tender_ai.source_email'))
        self.assertIn(tender, profile.tender_ids, 'E-mail tenders are matched against agents immediately')

    def test_feed_cron(self):
        self.Source.search([]).write({'active': False})
        self.Source.create({'name': 'Feed', 'source_type': 'rss', 'url': 'https://ok.example.com'})
        with patch(SRC + '.get', return_value=fake_response(RSS)):
            self.Source._cron_fetch_all()
        self.assertEqual(self.Tender.search_count([('name', 'ilike', 'Kacheguda')]), 1)
