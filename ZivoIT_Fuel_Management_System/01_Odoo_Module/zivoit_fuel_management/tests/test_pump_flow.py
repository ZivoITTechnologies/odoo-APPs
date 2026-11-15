# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from .common import PumpCommon


@tagged('post_install', '-at_install', 'bpcl')
class TestPumpFlow(PumpCommon):

    def test_01_rate_lookup_and_slip_amount(self):
        self.env['pump.rate'].create({'product_id': self.hsd.id, 'date': self.d0 + timedelta(days=3), 'rate': 96.5})
        self.assertEqual(self.hsd.get_rate(self.d0), 95.0)
        self.assertEqual(self.hsd.get_rate(self.d0 + timedelta(days=5)), 96.5)
        slip = self._slip(100)
        self.assertEqual(slip.rate, 95.0)
        self.assertEqual(slip.amount, 9500.0)
        self.assertEqual(slip.state, 'confirmed')
        self.assertTrue(slip.name.startswith('FS/'))
        self.assertTrue(self.customer.pump_code.startswith('CUS'))

    def test_02_credit_limit_enforced(self):
        self._slip(500)                     # 47,500 of 50,000
        with self.assertRaises(UserError):
            self._slip(100)                 # 9,500 more -> over limit
        self.customer.invalidate_recordset()
        self.assertAlmostEqual(self.customer.pump_unbilled, 47500.0)
        self.assertAlmostEqual(self.customer.pump_available_credit, 2500.0)
        # manager override
        s = self._slip(100, confirm=False)
        s.with_context(pump_override_limit=True).action_confirm()
        self.assertEqual(s.state, 'confirmed')
        self.customer.invalidate_recordset()
        self.assertTrue(self.customer.pump_over_limit)

    def test_03_shift_sheet_posts_stock_and_cash(self):
        self._slip(50)   # 4,750 credit in shift 1
        sheet = self.env['pump.shift.sheet'].create({'date': self.d0, 'shift_id': self.shift1.id})
        self.assertEqual(len(sheet.line_ids), 2, 'running nozzles auto-loaded')
        l1 = sheet.line_ids.filtered(lambda l: l.nozzle_id == self.n1)
        l2 = sheet.line_ids.filtered(lambda l: l.nozzle_id == self.n2)
        self.assertEqual(l1.opening, 1000.0)
        l1.write({'closing': 1405.0, 'testing_qty': 5.0})   # 400 L HSD
        l2.write({'closing': 700.0})                         # 200 L MS
        sheet.write({'card_amount': 10000.0, 'upi_amount': 5000.0})
        sheet._compute_totals()
        self.assertAlmostEqual(sheet.total_amount, 400 * 95 + 200 * 105)
        self.assertAlmostEqual(sheet.credit_amount, 4750.0)
        expected_cash = 59000 - 4750 - 15000
        self.assertAlmostEqual(sheet.expected_cash, expected_cash)
        sheet.cash_collected = expected_cash - 100
        cash_before = self._balance(self._acc('acc_cash'))
        sheet.action_post()
        self.assertEqual(sheet.state, 'posted')
        self.assertEqual(sheet.voucher_id.state, 'posted')
        self.assertAlmostEqual(self._balance(self._acc('acc_cash')) - cash_before, expected_cash - 100)
        self.assertAlmostEqual(self._balance(self._acc('acc_short_excess')), 100.0)
        self.assertAlmostEqual(self._balance(self._acc('acc_credit_clearing')), 4750.0)
        self.tank.invalidate_recordset()
        self.assertAlmostEqual(self.tank.stock_qty, 10000 - 400)
        self.n1.invalidate_recordset()
        self.assertAlmostEqual(self.n1.last_reading, 1405.0)
        # next sheet starts from last closing; mismatched opening blocked
        sheet2 = self.env['pump.shift.sheet'].create({'date': self.d0, 'shift_id': self.shift2.id})
        self.assertEqual(sheet2.line_ids.filtered(lambda l: l.nozzle_id == self.n1).opening, 1405.0)
        sheet2.line_ids.filtered(lambda l: l.nozzle_id == self.n1).write({'opening': 1400.0, 'closing': 1500.0})
        with self.assertRaises(UserError):
            sheet2.action_post()
        # cancelling restores stock
        sheet.action_cancel()
        self.tank.invalidate_recordset()
        self.assertAlmostEqual(self.tank.stock_qty, 10000.0)

    def test_04_reading_validation(self):
        sheet = self.env['pump.shift.sheet'].create({'date': self.d0, 'shift_id': self.shift1.id})
        with self.assertRaises(ValidationError):
            sheet.line_ids[0].write({'closing': sheet.line_ids[0].opening - 1})
        with self.assertRaises(ValidationError):
            sheet.line_ids[0].write({'closing': sheet.line_ids[0].opening + 3, 'testing_qty': 5})

    def test_05_bill_receipt_and_ledger(self):
        s1 = self._slip(100, date=self.d0)
        s2 = self._slip(50, date=self.d0 + timedelta(days=1))
        bills = self.env['pump.bill'].generate_bills(self.d0, self.d0 + timedelta(days=2), self.customer,
                                                     bill_date=self.d0 + timedelta(days=3))
        self.assertEqual(len(bills), 1)
        bill = bills
        self.assertEqual(bill.state, 'posted')
        self.assertAlmostEqual(bill.amount_total, 14250.0)
        self.assertEqual((s1 | s2).mapped('state'), ['billed', 'billed'])
        self.customer.invalidate_recordset()
        self.assertAlmostEqual(self.customer.pump_balance, 14250.0)
        self.assertAlmostEqual(self.customer.pump_unbilled, 0.0)
        # billed slips are locked
        with self.assertRaises(UserError):
            s1.qty = 10
        # receipt
        self.env['pump.quick.voucher.wizard'].create({
            'voucher_type': 'receipt', 'date': self.d0 + timedelta(days=5), 'partner_id': self.customer.id,
            'amount': 10000.0, 'via': 'bank', 'payment_mode': 'neft', 'cheque_no': 'UTR123'}).action_confirm()
        self.customer.invalidate_recordset()
        self.assertAlmostEqual(self.customer.pump_balance, 4250.0)
        led = self.customer.pump_ledger(self.d0, self.today)
        self.assertEqual(led['opening'], 0.0)
        self.assertEqual([l['vtype'] for l in led['lines']], ['Period Bill', 'Receipt'])
        self.assertAlmostEqual(led['closing'], 4250.0)
        self.assertEqual(led['lines'][1]['cheque'], 'UTR123')
        # ledger opening carries forward
        led2 = self.customer.pump_ledger(self.d0 + timedelta(days=4), self.today)
        self.assertAlmostEqual(led2['opening'], 14250.0)
        # monthly view totals agree
        fy = self.customer.pump_current_fy(self.d0)
        mon = self.customer.pump_monthly(fy)
        self.assertAlmostEqual(mon['total_debit'] - mon['total_credit'], 4250.0)
        # cancel bill releases slips
        bill.action_cancel()
        self.assertEqual((s1 | s2).mapped('state'), ['confirmed', 'confirmed'])
        self.customer.invalidate_recordset()
        self.assertAlmostEqual(self.customer.pump_balance, -10000.0)

    def test_06_vouchers_balanced(self):
        V = self.env['pump.voucher']
        bad = V.create({'voucher_type': 'journal', 'line_ids': [
            (0, 0, {'account_id': self._acc('acc_cash').id, 'debit': 100}),
            (0, 0, {'account_id': self._acc('acc_capital').id, 'credit': 90})]})
        with self.assertRaises(UserError):
            bad.action_post()
        no_party = V.create({'voucher_type': 'journal', 'line_ids': [
            (0, 0, {'account_id': self._acc('acc_debtors').id, 'debit': 100}),
            (0, 0, {'account_id': self._acc('acc_capital').id, 'credit': 100})]})
        with self.assertRaises(UserError):
            no_party.action_post()
        ok = V.create({'voucher_type': 'journal', 'line_ids': [
            (0, 0, {'account_id': self._acc('acc_cash').id, 'debit': 100}),
            (0, 0, {'account_id': self._acc('acc_capital').id, 'credit': 100})]})
        ok.action_post()
        self.assertTrue(ok.name.startswith('JV/'))
        with self.assertRaises(UserError):
            ok.unlink()
        rows = self.env['pump.trial.balance.wizard'].compute_rows('2000-01-01', '2100-01-01')
        self.assertAlmostEqual(sum(r['debit'] for r in rows), sum(r['credit'] for r in rows))

    def test_07_purchase_and_dip(self):
        pur = self.env['pump.purchase'].create({'date': self.d0, 'tank_id': self.tank.id, 'qty': 8000,
                                                'received_qty': 7990, 'rate': 90.0, 'invoice_no': 'INV1'})
        bank_before = self._balance(self._acc('acc_bank'))
        pur.action_post()
        self.tank.invalidate_recordset()
        self.assertAlmostEqual(self.tank.stock_qty, 17990.0)
        self.assertAlmostEqual(self._balance(self._acc('acc_bank')) - bank_before, -720000.0)
        over = self.env['pump.purchase'].create({'date': self.d0, 'tank_id': self.tank.id, 'qty': 8000, 'rate': 90.0})
        with self.assertRaises(UserError):
            over.action_post()
        dip = self.env['pump.dip.reading'].create({'date': self.d0, 'tank_id': self.tank.id, 'physical_qty': 17950})
        dip.action_post()
        self.assertAlmostEqual(dip.variance, -40.0)
        self.tank.invalidate_recordset()
        self.assertAlmostEqual(self.tank.stock_qty, 17950.0)

    def test_08_dip_chart_interpolation(self):
        self.tank.dip_chart_ids = [(0, 0, {'dip_mm': 100, 'volume': 500}), (0, 0, {'dip_mm': 200, 'volume': 1500})]
        self.assertAlmostEqual(self.tank.volume_from_dip(150), 1000.0)
        self.assertAlmostEqual(self.tank.volume_from_dip(50), 250.0)
        self.assertAlmostEqual(self.tank.volume_from_dip(999), 1500.0)

    def test_09_indent_to_slip(self):
        ind = self.env['pump.indent'].create({'partner_id': self.customer.id, 'vehicle_id': self.vehicle.id,
                                              'product_id': self.hsd.id, 'indent_type': 'amount', 'amount': 1900})
        action = ind.action_create_slip()
        self.assertAlmostEqual(action['context']['default_qty'], 20.0)
        slip = self.env['pump.credit.sale'].with_context(**action['context']).create({})
        slip.action_confirm()
        self.assertEqual(ind.state, 'delivered')

    def test_10_salary_and_settlement(self):
        emp = self.env['hr.employee'].create({'name': 'DSM One', 'pump_role': 'dsm', 'pump_basic': 9000,
                                              'pump_da': 600, 'pump_hra': 400})
        sheet = self.env['pump.salary.sheet'].create({'month': '2', 'year': 2026})
        self.env['pump.attendance'].create([{'employee_id': emp.id, 'date': '2026-02-%02d' % d,
                                             'status': 'absent' if d <= 4 else 'present'} for d in range(1, 29)])
        sheet.action_compute()
        line = sheet.line_ids.filtered(lambda l: l.employee_id == emp)
        self.assertEqual(line.paid_days, 24)
        self.assertAlmostEqual(line.earned, round(10000 * 24 / 28, 2))
        sheet.action_post()
        self.assertEqual(sheet.state, 'posted')
        w = self.env['pump.quick.voucher.wizard'].create({
            'voucher_type': 'contra', 'contra_direction': 'settle', 'amount': 1000, 'charges': 6,
            'settle_account_id': self._acc('acc_card').id})
        w.action_confirm()
        self.assertAlmostEqual(self._balance(self._acc('acc_bank_charges')), 6.0)

    def test_11_dashboard_and_reports(self):
        self._slip(10)
        data = self.env['pump.dashboard'].get_dashboard_data(str(self.d0))
        self.assertIn('kpi', data)
        self.assertEqual(len(data['tanks']), self.env['pump.tank'].search_count([]))
        report = self.env['ir.actions.report']
        html, _t = report._render_qweb_html('zivoit_fuel_management.action_report_statement', self.customer.ids,
                                            data={'date_from': str(self.d0), 'date_to': str(self.today)})
        self.assertIn(b'Account Statement', html)
        bill = self.env['pump.bill'].generate_bills(self.d0, self.today, self.customer)
        html, _t = report._render_qweb_html('zivoit_fuel_management.action_report_bill', bill.ids)
        self.assertIn(bill.name.encode(), html)
        from odoo.addons.zivoit_fuel_management.controllers.excel import ledger_xlsx
        content = ledger_xlsx(self.env, self.customer, self.d0, self.today)
        self.assertTrue(content.startswith(b'PK'))

    def test_12_only_manager_changes_limit(self):
        user = self.env['res.users'].create({
            'name': 'Cashier', 'login': 'cashier_t', 'group_ids': [(6, 0, [self.env.ref('zivoit_fuel_management.group_pump_user').id])]})
        with self.assertRaises(UserError):
            self.customer.with_user(user).write({'pump_credit_limit': 999999})
