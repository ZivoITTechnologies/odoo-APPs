# -*- coding: utf-8 -*-
import json
from unittest.mock import patch

from odoo.tests import tagged

from odoo.addons.zivoit_tender_ai.models import tender_utils as tu

from .common import TenderCommon, fake_response

AI = 'odoo.addons.zivoit_tender_ai.models.tender_ai.requests'


@tagged('post_install', '-at_install', 'zivoit_tender')
class TestAllIndustries(TenderCommon):
    """The agent must work for any industry, not only construction."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        st = lambda code: cls.env['res.country.state'].search(  # noqa: E731
            [('country_id.code', '=', 'IN'), ('code', '=', code)], limit=1)
        cls.kl, cls.hp, cls.ar, cls.tn, cls.dl = st('KL'), st('HP'), st('AR'), st('TN'), st('DL')
        T = cls._tender
        # targets
        cls.kl_erp = T(name='Supply, implementation and maintenance of ERP solution for Kerala State Co-operative Federation',
                       organisation='Kerala State Co-operative Federation', location='Thiruvananthapuram',
                       estimated_value=3.4e7)
        cls.hp_soft = T(name='Design, development and maintenance of custom web-based application for HP Tourism',
                        organisation='HP Tourism Development Corporation', location='Shimla', estimated_value=1.25e7)
        cls.ar_manpower = T(name='Hiring of skilled and unskilled manpower for OFC and telecom tower maintenance',
                            organisation='BSNL Arunachal Pradesh Circle', location='Itanagar', estimated_value=2.1e7)
        # decoys
        cls.tn_erp = T(name='Implementation of ERP for Tamil Nadu Civil Supplies Corporation',
                       organisation='TNCSC', location='Chennai', estimated_value=5e7)
        cls.kl_road = T(name='Improvement of Kottayam - Kumily road', organisation='PWD Kerala',
                        location='Kottayam, Kerala', estimated_value=9e7)
        cls.kl_crm = T(name='Development of CRM portal for Kerala Tourism', organisation='Kerala Tourism',
                       location='Kochi', estimated_value=8e6)
        cls.hp_road = T(name='Construction of link road in Mandi district', organisation='HPPWD',
                        location='Mandi, Himachal Pradesh', estimated_value=3e7)
        cls.ar_tower = T(name='Construction of telecom tower foundations at 40 sites', organisation='BSNL Arunachal',
                         location='Itanagar', estimated_value=6e7)
        cls.ar_staff = T(name='Outsourcing of data entry operators for Directorate of Health',
                         organisation='Government of Arunachal Pradesh', location='Itanagar', estimated_value=4e6)
        cls.as_manpower = T(name='Manpower supply for telecom O&M in Assam', organisation='BSNL Assam Circle',
                            location='Guwahati', estimated_value=2e7)

    # ------------------------------------------------------------------
    def test_every_state_and_ut_exists_in_odoo(self):
        """Every state the parser can return must resolve to a res.country.state record."""
        State = self.env['res.country.state']
        names = set(tu.INDIAN_STATES) | set(tu.STATE_ABBREVIATIONS.values())
        missing = [n for n in names if not State.search([('country_id.code', '=', 'IN'), ('name', '=ilike', n)])]
        self.assertFalse(missing, 'States not found in Odoo: %s' % missing)
        self.assertGreaterEqual(len(tu.INDIAN_STATES), 36)
        for alias, expected in (('kerela', 'Kerala'), ('himachal', 'Himachal Pradesh'), ('arunachal', 'Arunachal Pradesh'),
                                ('itanagar', 'Arunachal Pradesh'), ('shimla', 'Himachal Pradesh'), ('leh', 'Jammu and Kashmir'),
                                ('pondicherry', 'Puducherry'), ('raipur', 'Chattisgarh'), ('shillong', 'Meghalaya')):
            self.assertEqual(tu.detect_state('Works at %s district' % alias), expected, alias)

    def test_every_sector_exists(self):
        Sector = self.env['zivoit.tender.sector']
        for code in tu.SECTOR_KEYWORDS:
            sector = Sector.search([('code', '=', code)])
            self.assertTrue(sector, code)
            self.assertNotEqual(sector.industry_group, 'other', code)
        groups = set(Sector.search([]).mapped('industry_group'))
        self.assertTrue({'works', 'it', 'services', 'goods', 'consulting'} <= groups)

    def test_parse_user_examples(self):
        q = tu.parse_query('ERP tenders in Kerala')
        self.assertEqual((q['states'], q['sectors'], q['keywords']), (['Kerala'], ['it_software'], ['erp']))
        q = tu.parse_query('custom software development projects in Himachal pradesh')
        self.assertEqual(q['states'], ['Himachal Pradesh'])
        self.assertEqual(q['sectors'], ['it_software'])
        self.assertIn('development', q['keywords'])
        q = tu.parse_query('man power supply for telecom arunachal pradesh')
        self.assertEqual(q['states'], ['Arunachal Pradesh'])
        self.assertEqual(set(q['sectors']), {'manpower', 'telecom'})
        self.assertEqual(q['keywords'], [], '"supply" / "man power" are not left as restrictive keywords')
        q = tu.parse_query('supply of laptops to schools in Chhattisgarh')
        self.assertEqual(q['states'], ['Chattisgarh'])
        self.assertIn('laptop', q['keywords'])
        self.assertEqual(tu.parse_query('IT tenders published in the last 3 days')['sectors'], ['it_software'])
        self.assertEqual(tu.parse_query('it tenders in kerala')['sectors'], ['it_software'])
        self.assertNotIn('it_software', tu.parse_query('find it in Kerala roads')['sectors'])
        q = tu.parse_query('housekeeping and security services in Delhi excluding canteen')
        self.assertEqual(q['exclude_keywords'], ['canteen'])
        q = tu.parse_query('housekeeping and security guards tenders in Delhi')
        self.assertTrue({'facility_mgmt', 'security_services'} <= set(q['sectors']))

    def test_classification_beyond_construction(self):
        expectations = {
            self.kl_erp: 'it_software',
            self.hp_soft: 'it_software',
            self.ar_manpower: 'manpower',
            self.ar_staff: 'manpower',
            self.kl_crm: 'it_software',
        }
        for tender, code in expectations.items():
            self.assertIn(code, tender.sector_ids.mapped('code'), tender.name)
        self.assertIn('telecom', self.ar_manpower.sector_ids.mapped('code'))
        self.assertEqual(self.kl_erp.state_id, self.kl)
        self.assertEqual(self.hp_soft.state_id, self.hp)
        self.assertEqual(self.ar_manpower.state_id, self.ar)
        self.assertEqual(self.ar_manpower.work_type, 'service')
        self.assertIn('it', self.kl_erp.sector_ids.mapped('industry_group'))
        # IT wording must not leak into construction sectors
        soft = self._tender(name='Supply and installation of software platform for e-office', organisation='NIC')
        self.assertNotIn('railways', soft.sector_ids.mapped('code'))
        self.assertIn('it_software', soft.sector_ids.mapped('code'))

    def _ids(self, query):
        return [r['id'] for r in self.Tender.agent_search(query)['results']]

    def test_agent_erp_kerala(self):
        ids = self._ids('ERP tenders in Kerala')
        self.assertEqual(ids, [self.kl_erp.id], 'Only the Kerala ERP tender: not Tamil Nadu ERP, Kerala roads or Kerala CRM')

    def test_agent_software_himachal(self):
        ids = self._ids('custom software development projects in Himachal pradesh')
        self.assertEqual(ids, [self.hp_soft.id])

    def test_agent_manpower_telecom_arunachal(self):
        result = self.Tender.agent_search('man power supply for telecom arunachal pradesh')
        ids = [r['id'] for r in result['results']]
        self.assertEqual(ids[0], self.ar_manpower.id, 'Tender matching BOTH manpower and telecom ranks first')
        self.assertIn(self.ar_tower.id, ids)
        self.assertIn(self.ar_staff.id, ids)
        self.assertNotIn(self.as_manpower.id, ids, 'Assam is not Arunachal Pradesh')
        scores = {r['id']: r['score'] for r in result['results']}
        self.assertGreater(scores[self.ar_manpower.id], scores[self.ar_tower.id])
        self.assertGreater(scores[self.ar_manpower.id], scores[self.ar_staff.id])

    def test_saved_agent_any_industry(self):
        profile = self.Profile.create({'name': 'Kerala ERP', 'nl_query': 'ERP tenders in Kerala', 'live_search': False,
                                       'notify_mode': 'instant', 'notify_user_ids': [(6, 0, [self.manager.id])]})
        profile.action_parse_query()
        self.assertEqual(profile.state_ids, self.kl)
        self.assertEqual(profile.sector_ids.code, 'it_software')
        self.assertEqual(profile.keywords, 'erp')
        profile.action_run()
        self.assertEqual(profile.tender_ids, self.kl_erp)
        self.assertTrue(profile.message_ids.filtered(lambda m: 'ERP solution' in (m.body or '')))

    def test_checklist_by_industry(self):
        self.kl_erp.action_shortlist()
        self.assertTrue(any('ISO 27001' in n for n in self.kl_erp.checklist_line_ids.mapped('name')))
        self.ar_staff.action_shortlist()
        self.assertTrue(any('EPF, ESIC' in n for n in self.ar_staff.checklist_line_ids.mapped('name')))
        media = self._tender(name='Event management for state tourism festival', organisation='Tourism Dept')
        self.assertEqual(media.sector_ids.code, 'media_events')
        media.action_shortlist()
        self.assertTrue(any('similar supply / service / work' in n for n in media.checklist_line_ids.mapped('name')),
                        'Tenders without a specific template get the general checklist')
        self.kl_road.action_shortlist()
        self.assertTrue(any('Bid capacity' in n for n in self.kl_road.checklist_line_ids.mapped('name')),
                        'Construction tenders still get the civil works checklist')

    def test_full_bid_cycle_for_service_tender(self):
        tender = self.ar_manpower
        tender.action_shortlist()
        tender.checklist_line_ids.write({'status': 'yes'})
        tender.action_prepare_bid()
        tender.boq_line_ids = [
            (0, 0, {'name': 'Skilled technician (per man-month)', 'uom': 'Man-month', 'quantity': 480, 'cost_rate': 24000}),
            (0, 0, {'name': 'Unskilled helper (per man-month)', 'uom': 'Man-month', 'quantity': 480, 'cost_rate': 16000}),
        ]
        self.assertAlmostEqual(tender.boq_cost, 480 * 40000)
        tender.action_submit_bid()
        wizard = self.env['zivoit.tender.result.wizard'].create({'tender_id': tender.id, 'outcome': 'won'})
        wizard._onchange_outcome()
        wizard.action_confirm()
        self.assertEqual(tender.stage_type, 'won')
        self.assertEqual(len(tender.project_id.task_ids), 2)

    def test_ai_prompt_is_industry_neutral(self):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('zivoit_tender_ai.ai_provider', 'anthropic')
        icp.set_param('zivoit_tender_ai.ai_api_key', 'k')
        rows = [{'title': 'Hiring of manpower for BSNL OFC maintenance, Arunachal', 'organisation': 'BSNL',
                 'state': 'Arunachal Pradesh', 'closing_date': '2030-12-01'}]
        answer = {'content': [{'type': 'text', 'text': json.dumps(rows)}]}
        source = self.env.ref('zivoit_tender_ai.source_ai_state_portals')
        with patch(AI + '.post', return_value=fake_response(json_data=answer)) as post:
            tenders = source._run('man power supply for telecom arunachal pradesh',
                                  filters=tu.parse_query('man power supply for telecom arunachal pradesh'))
        payload = post.call_args.kwargs['json']
        prompt = payload['messages'][0]['content']
        self.assertIn('any category', prompt)
        self.assertNotIn('construction company', payload['system'])
        self.assertEqual(payload['tools'][0]['allowed_domains'], ['gov.in', 'nic.in'])
        self.assertIn('Arunachal Pradesh', prompt)
        self.assertEqual(tenders.state_id, self.ar)
        self.assertTrue({'manpower', 'telecom'} <= set(tenders.sector_ids.mapped('code')))

    def test_gem_style_email_for_services(self):
        body = """<p>Bid Number: GEM/2026/B/5551234  Item Category: Manpower Outsourcing Services - Minimum wage - Skilled
        Organisation Name: Bharat Sanchar Nigam Limited  Location: Itanagar, Arunachal Pradesh
        Estimated Bid Value: 2,10,00,000  EMD Amount: 4,20,000  Bid End Date: 05-12-2030 15:00</p>"""
        msg = {'subject': 'GeM bid alert', 'body': body, 'from': 'noreply@gem.gov.in', 'email_from': 'noreply@gem.gov.in',
               'to': 'bids@example.com', 'message_id': '<gem1@gem>', 'date': '2026-11-20 10:00:00', 'partner_ids': [],
               'attachments': [], 'cc': ''}
        tender = self.Tender.message_new(msg)
        self.assertEqual(tender.state_id, self.ar)
        self.assertIn('manpower', tender.sector_ids.mapped('code'))
        self.assertAlmostEqual(tender.estimated_value, 2.1e7)
        self.assertEqual(tender.date_closing.year, 2030)
