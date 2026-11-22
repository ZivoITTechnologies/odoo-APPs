# -*- coding: utf-8 -*-
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from .common import TenderCommon


@tagged('post_install', '-at_install', 'zivoit_tender')
class TestRaBills(TenderCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tender = cls._tender(margin_pct=0, overhead_pct=0, contingency_pct=0)
        cls.tender.boq_line_ids = [
            (0, 0, {'item_code': 'A', 'name': 'Earthwork', 'quantity': 1000, 'cost_rate': 300}),
            (0, 0, {'item_code': 'B', 'name': 'RCC', 'quantity': 100, 'cost_rate': 8000}),
        ]
        cls.tender.stage_id = cls.tender._stage('won')
        cls.boq_a = cls.tender.boq_line_ids.filtered(lambda l: l.item_code == 'A')
        cls.boq_b = cls.tender.boq_line_ids - cls.boq_a

    def _bill(self, qty_a, qty_b):
        bill = self.env['zivoit.tender.ra.bill'].create({'tender_id': self.tender.id})
        bill.line_ids.filtered(lambda l: l.boq_line_id == self.boq_a).current_qty = qty_a
        bill.line_ids.filtered(lambda l: l.boq_line_id == self.boq_b).current_qty = qty_b
        return bill

    def test_ra_bill_cycle(self):
        self.assertTrue(self.tender.project_id, 'Won tender created the project')
        bill1 = self._bill(400, 20)
        self.assertEqual(bill1.bill_no, 1)
        self.assertEqual(bill1.name, '%s/RA-01' % self.tender.code)
        self.assertEqual(len(bill1.line_ids), 2, 'BOQ loaded automatically')
        self.assertEqual(bill1.line_ids.filtered(lambda l: l.boq_line_id == self.boq_b).rate, 8000)
        gross = 400 * 300 + 20 * 8000  # 280,000
        self.assertAlmostEqual(bill1.gross_amount, gross)
        self.assertAlmostEqual(bill1.retention_amount, gross * 0.05)
        self.assertAlmostEqual(bill1.gst_amount, gross * 0.18)
        self.assertAlmostEqual(bill1.tds_amount, gross * 0.02)
        bill1.mobilisation_recovery = 10000
        expected = gross * (1 + 0.18 - 0.05 - 0.02 - 0.02 - 0.01) - 10000
        self.assertAlmostEqual(bill1.net_payable, expected, places=2)
        bill1.action_submit()
        bill1.action_certify()
        self.assertAlmostEqual(bill1.certified_amount, expected, places=2)
        bill1.action_mark_paid()
        self.assertEqual(bill1.state, 'paid')
        self.assertTrue(bill1.payment_date)

        bill2 = self._bill(700, 10)
        self.assertEqual(bill2.bill_no, 2)
        line_a = bill2.line_ids.filtered(lambda l: l.boq_line_id == self.boq_a)
        self.assertEqual(line_a.previous_qty, 400)
        self.assertEqual(line_a.cumulative_qty, 1100)
        self.assertTrue(line_a.excess_qty, 'Cumulative exceeds contract quantity')
        bill2.action_submit()
        self.assertEqual(self.boq_a.executed_qty, 1100)
        self.assertAlmostEqual(self.boq_b.progress, 30.0)
        self.assertEqual(self.tender.ra_bill_count, 2)
        project = self.tender.project_id
        self.assertEqual(project.ra_bill_count, 2)
        self.assertAlmostEqual(project.ra_billed_amount, gross + 700 * 300 + 10 * 8000)
        self.assertGreater(project.physical_progress, 0)
        action = project.action_view_ra_bills()
        self.assertEqual(action['domain'], [('tender_id', '=', self.tender.id)])

    def test_ra_bill_validation(self):
        bill = self.env['zivoit.tender.ra.bill'].create({'tender_id': self.tender.id})
        with self.assertRaises(UserError):
            bill.action_submit()  # no quantities
        with self.assertRaises(ValidationError):
            bill.line_ids[0].current_qty = -5
        bill.line_ids[0].current_qty = 10
        bill.action_submit()
        with self.assertRaises(UserError):
            bill.action_load_boq()
        nxt = self._bill(5, 0)
        line = nxt.line_ids.filtered(lambda l: l.boq_line_id == self.boq_a)
        self.assertEqual(line.previous_qty, 10)
        bill.action_cancel()
        self.assertEqual(line.previous_qty, 0, 'Cancelled earlier bills no longer count')
