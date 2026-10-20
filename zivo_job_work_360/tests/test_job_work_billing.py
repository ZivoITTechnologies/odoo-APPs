# -*- coding: utf-8 -*-
from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import JobWorkCommon


@tagged('post_install', '-at_install', 'job_work')
class TestJobWorkBilling(JobWorkCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.job_type.quality_required = False
        if not cls.env['account.journal'].search([('type', '=', 'purchase'), ('company_id', '=', cls.company.id)]):
            cls.skipTest(cls, "No purchase journal (no chart of accounts installed)")

    def _job_with_po(self, rate=12.0, qty=100.0):
        job = self._create_job(qty=qty, material_qty=qty, rate=rate)
        job.material_ids.scrap_pct = 0.0
        self._issue_all(job)
        action = job.action_create_purchase_order()
        po = self.env['purchase.order'].browse(action['res_id'])
        self.assertEqual(po.order_line.price_unit, rate)
        self.assertEqual(po.order_line.jw_order_id, job)
        self.assertEqual(job.purchase_count, 1)
        po.button_confirm()
        return job, po

    def _bill(self, po, qty=None, price=None, extra=0.0):
        po_line = po.order_line
        lines = [Command.create({
            'product_id': po_line.product_id.id,
            'name': po_line.name,
            'quantity': po_line.qty_received - po_line.qty_invoiced if qty is None else qty,
            'price_unit': po_line.price_unit if price is None else price,
            'purchase_line_id': po_line.id,
            'tax_ids': [],
        })]
        if extra:
            lines.append(Command.create({'name': 'Express freight', 'quantity': 1, 'price_unit': extra,
                                         'tax_ids': []}))
        return self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': po.partner_id.id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': lines,
        })

    def test_01_po_received_follows_accepted_qty(self):
        job, po = self._job_with_po()
        self.assertEqual(po.order_line.qty_received, 0.0)
        self._receive(job, 60.0, consumed=60.0)
        self.assertEqual(po.order_line.qty_received, 60.0, "PO received quantity = accepted job quantity")
        self.assertEqual(job.cost_processing_source, 'accrual')
        self.assertAlmostEqual(job.cost_act_processing, 720.0)

    def test_02_bill_quantity_variance_blocks_posting(self):
        job, po = self._job_with_po()
        self._receive(job, 96.0, consumed=96.0)
        bill = self._bill(po, qty=98.0)
        self.assertTrue(bill.jw_has_variance)
        self.assertAlmostEqual(bill.jw_qty_variance, 2.0)
        self.assertAlmostEqual(bill.jw_value_variance, 24.0)
        with self.assertRaises(UserError):
            bill.action_post()
        job._jw_check_bill_variance()
        self.assertTrue(job.exception_ids.filtered(lambda e: e.exception_type == 'bill_variance' and e.state == 'open'))
        with self.assertRaises(AccessError):
            bill.with_user(self.user).action_jw_approve_variance()
        bill.with_user(self.manager).action_jw_approve_variance()
        bill.action_post()
        self.assertEqual(bill.state, 'posted')
        self.assertFalse(job.exception_ids.filtered(lambda e: e.exception_type == 'bill_variance' and e.state == 'open'))
        self.assertEqual(job.cost_processing_source, 'bill')
        self.assertAlmostEqual(job.cost_act_processing, 98 * 12.0)

    def test_03_bill_rate_variance_and_dispute(self):
        job, po = self._job_with_po(rate=10.0)
        self._receive(job, 100.0, consumed=100.0)
        bill = self._bill(po, price=11.0)
        self.assertAlmostEqual(bill.jw_price_variance, 1.0)
        bill.with_user(self.manager).action_jw_dispute()
        self.assertTrue(job.is_disputed)
        with self.assertRaises(UserError):
            bill.action_post()
        bill.with_user(self.manager).action_jw_reset_review()
        bill.invoice_line_ids.filtered('purchase_line_id').price_unit = 10.0
        self.assertFalse(bill.jw_has_variance)
        bill.action_post()
        self.assertEqual(bill.state, 'posted')
        self.assertFalse(job.is_disputed)

    def test_04_unapproved_extra_charges(self):
        job, po = self._job_with_po()
        self._receive(job, 100.0, consumed=100.0)
        bill = self._bill(po, extra=500.0)
        self.assertAlmostEqual(bill.jw_extra_amount, 500.0)
        with self.assertRaises(UserError):
            bill.action_post()
        self.company.jw_block_bill_variance = False
        bill.action_post()
        self.assertEqual(bill.state, 'posted')

    def test_05_second_bill_counts_previous(self):
        job, po = self._job_with_po()
        self._receive(job, 100.0, consumed=100.0)
        bill1 = self._bill(po, qty=100.0)
        bill1.action_post()
        bill2 = self._bill(po, qty=5.0)
        self.assertTrue(bill2.jw_has_variance, "previously billed quantity is taken into account")
        with self.assertRaises(UserError):
            bill2.action_post()

    def test_06_cost_lines_multi_currency(self):
        eur = self.env.ref('base.EUR')
        eur.active = True
        self.env['res.currency.rate'].create({
            'name': fields.Date.today(), 'currency_id': eur.id, 'rate': 0.5, 'company_id': self.company.id})
        if self.company.currency_id == eur:
            self.skipTest("Company currency is EUR")
        job = self._create_job(qty=10.0, material_qty=10.0)
        job.write({'cost_line_ids': [Command.create({
            'cost_type': 'freight', 'name': 'Truck', 'currency_id': eur.id,
            'expected_amount': 100.0, 'actual_amount': 150.0})]})
        self.assertAlmostEqual(job.cost_exp_freight, 200.0)
        self.assertAlmostEqual(job.cost_act_freight, 300.0)
        job.currency_id = eur
        job.rate = 5.0
        self.assertAlmostEqual(job.cost_exp_processing, 100.0, msg="10 units * 5 EUR = 100 company currency")

    def test_07_users_without_job_work_rights(self):
        """Inventory and accounting users outside Job Work keep working on job work documents."""
        from odoo.tests import Form, new_test_user
        stock_user = new_test_user(self.env, login='plain_stock', groups='stock.group_stock_user')
        accountant = new_test_user(self.env, login='plain_acc',
                                   groups='account.group_account_invoice,purchase.group_purchase_user')
        job = self._create_job(qty=10.0, material_qty=10.0, rate=5.0)
        job.material_ids.scrap_pct = 0.0
        job.action_plan()
        job.action_issue_material()
        picking = job.picking_ids.with_user(stock_user)
        Form(picking).save()
        picking.button_validate()
        self.assertEqual(job.state, 'issued')
        self._receive(job, 10.0, consumed=10.0)
        action = job.action_create_purchase_order()
        po = self.env['purchase.order'].browse(action['res_id'])
        po.button_confirm()
        Form(po.with_user(accountant)).save()
        bill = self._bill(po, qty=12.0).with_user(accountant)
        bill_form = Form(bill)
        bill_form.save()
        self.assertTrue(bill.jw_is_job_work_bill)
        with self.assertRaises(UserError):
            bill.action_post()
        bill.invoice_line_ids.filtered('purchase_line_id').quantity = 10.0
        bill.action_post()
        self.assertEqual(bill.state, 'posted')
        self.vendor.with_user(accountant).read(['jw_job_count', 'jw_grade', 'name'])
