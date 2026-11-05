from datetime import date

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged


@tagged("post_install", "-at_install", "zivoit_epc")
class TestRaBill(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids = [(4, cls.env.ref("zivoit_epc_ai.group_epc_manager").id)]
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True, epc_ai_force_fallback=True))
        cls.project = cls.env["project.project"].create({
            "name": "Billing test", "is_epc": True, "partner_id": cls.partner_a.id,
            "date_start": date(2026, 1, 1), "epc_retention_pct": 5.0, "epc_advance_recovery_pct": 10.0,
            "company_id": cls.env.company.id,
        })
        cls.boq = cls.env["epc.boq.line"].create({
            "project_id": cls.project.id, "code": "B1", "name": "Foundations", "quantity": 100, "rate": 1000})
        cls.activity = cls.env["epc.activity"].create({
            "project_id": cls.project.id, "code": "A", "name": "Foundations", "duration": 10,
            "boq_line_id": cls.boq.id, "planned_qty": 100})

    def _dpr(self, day, qty):
        dpr = self.env["epc.dpr"].create({
            "project_id": self.project.id, "date": day,
            "line_ids": [(0, 0, {"activity_id": self.activity.id, "quantity": qty})]})
        dpr.action_approve()
        return dpr

    def test_full_cycle(self):
        self._dpr(date(2026, 1, 5), 30)
        bill = self.env["epc.ra.bill"].create({"project_id": self.project.id})
        self.assertEqual(bill.partner_id, self.partner_a)
        self.assertEqual(bill.retention_pct, 5.0)
        self.assertEqual(bill.bill_no, 1)
        bill.action_generate_lines()
        self.assertEqual(bill.line_ids.claimed_qty, 30)
        self.assertEqual(bill.gross_amount, 30000)
        self.assertEqual(bill.retention_amount, 1500)
        self.assertEqual(bill.advance_recovery_amount, 3000)
        self.assertEqual(bill.net_amount, 25500)
        bill.action_submit()
        bill.action_certify()
        self.assertEqual(self.boq.billed_qty, 30)
        self.assertEqual(self.boq.unbilled_qty, 0)
        bill.action_create_invoice()
        move = bill.invoice_id
        self.assertEqual(bill.state, "invoiced")
        self.assertEqual(move.move_type, "out_invoice")
        self.assertEqual(move.partner_id, self.partner_a)
        self.assertAlmostEqual(move.amount_untaxed, 25500)
        move.action_post()
        self.assertEqual(move.state, "posted")

        # Second bill only claims the new quantity
        self._dpr(date(2026, 1, 12), 20)
        bill2 = self.env["epc.ra.bill"].create({"project_id": self.project.id})
        self.assertEqual(bill2.bill_no, 2)
        bill2.action_generate_lines()
        self.assertEqual(bill2.line_ids.previous_qty, 30)
        self.assertEqual(bill2.line_ids.claimed_qty, 20)
        bill2.line_ids.certified_qty = 15  # PMC certifies less
        bill2.action_certify()
        self.assertEqual(self.boq.billed_qty, 45)
        self.assertEqual(self.boq.unbilled_qty, 5)

    def test_over_billing_blocked(self):
        self._dpr(date(2026, 1, 5), 10)
        bill_a = self.env["epc.ra.bill"].create({"project_id": self.project.id})
        bill_b = self.env["epc.ra.bill"].create({"project_id": self.project.id})
        bill_a.action_generate_lines()
        bill_b.action_generate_lines()
        bill_a.action_certify()
        with self.assertRaises(ValidationError):
            bill_b.action_certify()

    def test_guards(self):
        bill = self.env["epc.ra.bill"].create({"project_id": self.project.id})
        with self.assertRaises(UserError):
            bill.action_generate_lines()  # nothing executed yet
        with self.assertRaises(UserError):
            bill.action_create_invoice()
        with self.assertRaises(UserError):
            bill.action_submit()
        self._dpr(date(2026, 1, 5), 10)
        bill.action_generate_lines()
        bill.action_certify()
        bill.action_create_invoice()
        with self.assertRaises(UserError):
            bill.unlink()
        with self.assertRaises(UserError):
            bill.action_reset_draft()
        with self.assertRaises(UserError):
            bill.action_cancel()
