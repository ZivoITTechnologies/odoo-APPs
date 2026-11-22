# -*- coding: utf-8 -*-
from datetime import datetime

from odoo.tests import BaseCase, tagged

from odoo.addons.zivoit_tender_ai.models import tender_utils as tu


@tagged('post_install', '-at_install', 'zivoit_tender')
class TestTenderUtils(BaseCase):

    def test_indian_amounts(self):
        cases = {
            'Rs. 12.5 Crore': 125000000.0,
            '₹ 3,45,00,000': 34500000.0,
            'INR 45.6 Lakhs': 4560000.0,
            'Estimated cost 1,20,50,000.00': 12050000.0,
            'EMD Rs 2.35 Lakh': 235000.0,
            '12 cr': 120000000.0,
            '2.5 million': 2500000.0,
            'Value 4500000': 4500000.0,
            'Tender No 2026/45': 0.0,
            '': 0.0,
            None: 0.0,
        }
        for text, expected in cases.items():
            self.assertAlmostEqual(tu.parse_indian_amount(text), expected, msg=text)
        self.assertEqual(tu.parse_indian_amount(1500.0), 1500.0)

    def test_dates(self):
        expected = datetime(2026, 12, 15, 15, 0)
        for text in ['15-Dec-2026 03:00 PM', '15/12/2026 15:00', '2026-12-15T15:00:00', '15.12.2026 15:00',
                     'Bid submission end date: 15-Dec-2026 15:00 IST', '2026-12-15T15:00:00+05:30']:
            self.assertEqual(tu.parse_date(text), expected, text)
        self.assertEqual(tu.parse_date('15th December 2026'), datetime(2026, 12, 15))
        # formats seen on real portals / aggregators (Nov 2026 real-data test)
        real = {
            '18-08-2026': datetime(2026, 8, 18),
            '29-Nov-2025, 4:00 pm': datetime(2025, 11, 29, 16, 0),
            '01-08-2026 11:00 AM': datetime(2026, 8, 1, 11, 0),
            '19 June 2026 17:00': datetime(2026, 6, 19, 17, 0),
            '19 June. 2026 17:00': datetime(2026, 6, 19, 17, 0),
            '13/07/2026 16.00 Hrs': datetime(2026, 7, 13, 16, 0),
            'Aug 03, 2026': datetime(2026, 8, 3),
            '13.05.2026 14:30': datetime(2026, 5, 13, 14, 30),
        }
        for text, expected in real.items():
            self.assertEqual(tu.parse_date(text), expected, text)
        self.assertIsNone(tu.parse_date('not a date'))
        self.assertIsNone(tu.parse_date(None))

    def test_duration(self):
        self.assertEqual(tu.parse_duration_months('18 Months'), 18)
        self.assertEqual(tu.parse_duration_months('540 Days'), 18)
        self.assertEqual(tu.parse_duration_months('2 Years'), 24)
        self.assertEqual(tu.parse_duration_months(None), 0)

    def test_detection(self):
        self.assertEqual(tu.detect_zone('Sr. DEN/Co-ord, South Central Railway, Secunderabad'), 'SCR')
        self.assertEqual(tu.detect_zone('Dy CE/Con, SCR, Guntakal'), 'SCR')
        self.assertEqual(tu.detect_zone('Southern Railway Chennai'), 'SR')
        self.assertFalse(tu.detect_zone('Roads and Buildings department'))
        # aliases must match whole words only
        self.assertFalse(tu.detect_state('Mandatory documents and medium voltage podium works'))
        self.assertEqual(tu.detect_state('Kannur International Airport'), 'Kerala')
        self.assertEqual(tu.detect_state('Works at Una town'), 'Himachal Pradesh')
        self.assertEqual(tu.detect_state('EE R&B Division Warangal'), 'Telangana')
        self.assertEqual(tu.detect_state('Works at Vijayawada'), 'Andhra Pradesh')
        self.assertIn('roads', tu.detect_sectors('Widening of NH-44 to four lane'))
        self.assertIn('bridges', tu.detect_sectors('Construction of ROB in lieu of LC 45'))
        self.assertIn('buildings', tu.detect_sectors('Construction of staff quarters G+3'))
        self.assertEqual(tu.detect_work_type('on EPC mode'), 'epc')
        self.assertEqual(tu.detect_work_type('Hybrid Annuity Mode'), 'ham')

    def test_query_scr_railways(self):
        q = tu.parse_query('open tenders in railways in construction in SCR')
        self.assertEqual(q['zones'], ['SCR'])
        self.assertEqual(q['sectors'], ['railways'])
        self.assertEqual(q['keywords'], [])
        self.assertTrue(q['open_only'])

    def test_query_telangana(self):
        q = tu.parse_query('construction tenders in Telengana')  # common misspelling
        self.assertEqual(q['states'], ['Telangana'])
        self.assertEqual(q['keywords'], [])

    def test_query_values_and_deadlines(self):
        q = tu.parse_query('road construction tenders in Telangana above 5 crore closing in 15 days')
        self.assertEqual(q['sectors'], ['roads'])
        self.assertEqual(q['min_value'], 5 * tu.CRORE)
        self.assertEqual(q['deadline_days'], 15)
        q = tu.parse_query('new building tenders GHMC between 2 and 10 crore excluding maintenance')
        self.assertEqual((q['min_value'], q['max_value']), (2 * tu.CRORE, 10 * tu.CRORE))
        self.assertEqual(q['exclude_keywords'], ['maintenance'])
        self.assertIn('GHMC', q['organisations'])
        self.assertEqual(q['published_days'], 7)
        q = tu.parse_query('tenders below 50 lakh')
        self.assertEqual(q['max_value'], 50 * tu.LAKH)
        q = tu.parse_query('awarded tenders RVNL')
        self.assertFalse(q['open_only'])
        self.assertIn('RVNL', q['zones'])

    def test_query_keywords(self):
        q = tu.parse_query('ballastless track slab tenders in SCR')
        self.assertIn('ballastless', q['keywords'])
        self.assertIn('slab', q['keywords'])

    def test_fingerprint(self):
        a = tu.fingerprint('SCR-ENG-2026-145', 'Construction of ROB', 'SCR')
        b = tu.fingerprint('scr eng 2026 145', 'Different title', 'Other')
        self.assertEqual(a, b, 'Reference number drives the fingerprint')
        c = tu.fingerprint(False, 'Construction of ROB at Kazipet', 'South Central Railway')
        d = tu.fingerprint('', 'Construction  of ROB at  Kazipet.', 'South Central Railway')
        self.assertEqual(c, d)
