# -*- coding: utf-8 -*-
"""Regression test on REAL tender notices.

tests/data/real_tenders_2026.json holds 22 genuine public tender notices collected from portals and
aggregators in November 2026 (source URL kept on every row): ERP / software tenders in Kerala and
Himachal Pradesh, manpower and telecom tenders in Arunachal Pradesh, plus real decoys (BSNL telecom
outsourcing in Odisha and Telangana, a BSNL transformer repair, RBI security guards, laptop supply).
The rows keep the exact text formats the portals publish (dates, Lakh / Crore values, fees).
"""
import json
import os

from odoo import fields
from odoo.tests import tagged

from .common import TenderCommon

DATA = os.path.join(os.path.dirname(__file__), 'data', 'real_tenders_2026.json')


@tagged('post_install', '-at_install', 'zivoit_tender')
class TestRealTenders(TenderCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(DATA, encoding='utf-8') as handle:
            cls.rows = json.load(handle)
        raw = [{k: v for k, v in row.items() if k != 'expect'} for row in cls.rows]
        cls.tenders, cls.created, cls.updated = cls.Tender._ingest_rows(raw)
        cls.by_title = {t.name: t for t in cls.tenders}

    def _t(self, starts):
        return next(t for name, t in self.by_title.items() if name.startswith(starts))

    def _ids(self, query):
        return set(r['id'] for r in self.Tender.agent_search(query, limit=200)['results'])

    def test_ingestion_of_real_notices(self):
        self.assertEqual((self.created, self.updated), (len(self.rows), 0))
        now = fields.Datetime.now()
        for row, tender in zip(self.rows, self.tenders):
            exp = row['expect']
            with self.subTest(tender=row['title'][:60]):
                self.assertEqual(tender.state_id.name, exp['state'])
                self.assertIn(exp['sector'], tender.sector_ids.mapped('code'))
                if 'value' in exp:
                    self.assertAlmostEqual(tender.estimated_value, exp['value'], delta=1)
                if 'emd' in exp:
                    self.assertAlmostEqual(tender.emd_amount, exp['emd'], delta=1)
                if 'fee' in exp:
                    self.assertAlmostEqual(tender.tender_fee, exp['fee'], delta=1)
                if 'months' in exp:
                    self.assertAlmostEqual(tender.completion_months, exp['months'], delta=0.05)
                if row.get('closing_date'):
                    self.assertTrue(tender.date_closing, 'closing date "%s" must be parsed' % row['closing_date'])
                    self.assertEqual(tender.is_open, tender.date_closing > now)
                self.assertEqual(tender.url, row['url'])

    def test_real_date_formats(self):
        # portal times are IST; Odoo stores UTC (IST - 5:30)
        self.assertEqual(str(self._t('Development, Customization and Implementation of Odoo ERP').date_closing),
                         '2025-11-29 10:30:00')  # 29-Nov-2025, 4:00 pm IST
        self.assertEqual(str(self._t('Manpower Outsourcing Services - Minimum Wage - Unskilled').date_closing),
                         '2026-08-18 18:29:00')  # "18-08-2026" (no time) = end of that day, 23:59 IST
        self.assertEqual(str(self._t('Request for Proposal for Selection of IT Consultant').date_closing),
                         '2026-06-19 11:30:00')  # 19 June 2026 17:00 IST
        self.assertEqual(str(self._t('Purchase of energy meters').date_closing), '2026-08-01 05:30:00')  # 11:00 AM IST
        self.assertEqual(str(self._t('Purchase of energy meters').date_published), '2026-07-03 05:33:00')

    def test_no_false_sector_tags(self):
        self.assertNotIn('media_events', self._t('EOI for ICT').sector_ids.mapped('code'),
                         '"media converters" is networking hardware, not media')
        self.assertNotIn('transport', self._t('Repairing and Rewinding').sector_ids.mapped('code'),
                         '"including transportation" inside a works tender is not a transport contract')
        self.assertNotIn('goods_supply', self._t('Hiring of manpower outsourcing agency').sector_ids.mapped('code'),
                         '"supply of manpower" is a manpower service, not goods')

    def test_query_erp_kerala(self):
        erp_kerala = {self._t(p).id for p in ('Tender for ERP software', 'Supply, Design, Installation, Customization',
                                              'Request for Proposal for Selection of IT Consultant',
                                              'Development, Customization and Implementation of Odoo ERP')}
        self.assertEqual(self._ids('ERP tenders in Kerala including closed'), erp_kerala)
        self.assertNotIn(self._t('Supply, Installation, Commissioning & Maintenance of Laptop').id,
                         self._ids('ERP tenders in Kerala including closed'), 'Laptop supply is IT but not ERP')
        open_ids = self._ids('ERP tenders in Kerala')
        self.assertTrue(open_ids <= erp_kerala)
        self.assertNotIn(self._t('Development, Customization and Implementation of Odoo ERP').id, open_ids,
                         'Closed in Nov 2025, so it is not an open tender')

    def test_query_software_himachal(self):
        ids = self._ids('custom software development projects in Himachal pradesh including closed')
        for title in ('Design, Development, Implementation and Support of a Blockchain', 'Tender to Establish & Implement',
                      'Corrigendum - Empanelment of IT agencies', 'Request for proposal - selection of Technical Support'):
            self.assertIn(self._t(title).id, ids, title)
        self.assertNotIn(self._t('Selection of System Integrator for operation').id, ids, 'Network O&M is not development')
        self.assertNotIn(self._t('Purchase of energy meters').id, ids, 'Goods purchase is not development')
        self.assertFalse(ids & {t.id for t in self.tenders if t.state_id.name != 'Himachal Pradesh'})

    def test_query_manpower_telecom_arunachal(self):
        ids = self._ids('man power supply for telecom arunachal pradesh including closed')
        arunachal_manpower = {t.id for t in self.tenders
                              if t.state_id.name == 'Arunachal Pradesh' and 'manpower' in t.sector_ids.mapped('code')}
        self.assertEqual(len(arunachal_manpower), 5)  # Lohit, Dirang, NIELIT, NYKS, CIHCS
        self.assertTrue(arunachal_manpower <= ids)
        self.assertIn(self._t('EOI for ICT').id, ids, 'Arunachal telecom tender is included')
        for decoy in ('Outsourcing of allied OFC works', 'Outsourcing of Maintenance of BSNL BTSs',
                      'Annual Service Contract for providing Security Guards'):
            self.assertNotIn(self._t(decoy).id, ids, decoy)

    def test_local_time_in_texts(self):
        tender = self._t('Development, Customization and Implementation of Odoo ERP')
        self.env.user.tz = 'Asia/Kolkata'
        tender.invalidate_recordset(['date_closing_local'])
        self.assertEqual(tender.date_closing_local, '29-Nov-2025 04:00 PM', 'Shown exactly as the portal published it')
        tender.action_analyse()
        self.assertIn('29-Nov-2025 04:00 PM', tender.ai_summary)

    def test_ingested_tenders_have_no_bot_owner(self):
        self.assertFalse(self.tenders.filtered('user_id'), 'Connector tenders wait for a person or an agent to own them')

    def test_portal_timezone_setting(self):
        self.env['ir.config_parameter'].sudo().set_param('zivoit_tender_ai.portal_tz', 'UTC')
        tenders, created, _u = self.Tender._ingest_rows([{'title': 'UTC portal test tender', 'reference': 'TZ-UTC-1',
                                                         'closing_date': '01-12-2030 15:00'}])
        self.assertEqual(str(tenders.date_closing), '2030-12-01 15:00:00')

    def test_reingest_is_deduplicated(self):
        raw = [{k: v for k, v in row.items() if k != 'expect'} for row in self.rows]
        _tenders, created, updated = self.Tender._ingest_rows(raw)
        self.assertEqual((created, updated), (0, len(self.rows)))
