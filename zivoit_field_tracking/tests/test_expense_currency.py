# -*- coding: utf-8 -*-
import json

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import HttpCase, new_test_user, tagged

from .expense_common import ZivoExpenseCommon

PARAM = 'zivoit_field_tracking.expense_currency'


@tagged('post_install', '-at_install', 'zivoit_field_tracking')
class TestExpenseCurrency(ZivoExpenseCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Currency = cls.env['res.currency'].with_context(active_test=False)
        cls.usd = Currency.search([('name', '=', 'USD')])
        cls.inr = Currency.search([('name', '=', 'INR')])
        # company in USD, no INR rate yet: a clean starting point whatever the database
        cls.env.cr.execute("UPDATE res_company SET currency_id = %s WHERE id = %s", [cls.usd.id, cls.company.id])
        cls.env['res.currency.rate'].search([('currency_id', '=', cls.inr.id)]).unlink()
        cls.env['ir.config_parameter'].sudo().set_param(PARAM, '')
        cls.env.invalidate_all()

    def toggle(self, code):
        settings = self.env['res.config.settings'].create({})
        settings.zivo_expense_currency = code
        settings.execute()
        return settings

    def test_default_follows_company_currency(self):
        self.assertEqual(self.policy.currency_id, self.usd)
        self.assertEqual(self.env['res.config.settings'].create({}).zivo_expense_currency, 'USD')
        self.track_day(self.dev, self.day1, km=10, hours=7)
        claim = self.env['zivo.expense.claim']._create_for(self.sales, self.day1, self.day1)
        self.assertEqual(claim.currency_id, self.usd)
        self.assertEqual(set(claim.line_ids.mapped('currency_id')), {self.usd})

    def test_toggle_to_inr(self):
        session = self.track_day(self.dev, self.day1, km=10, hours=7)
        self.inr.active = False
        self.toggle('INR')
        self.assertEqual(self.env['ir.config_parameter'].sudo().get_param(PARAM), 'INR')
        self.assertTrue(self.inr.active, 'INR is activated by the toggle')
        self.assertEqual(self.env['res.config.settings'].create({}).zivo_expense_currency, 'INR')
        self.assertEqual(self.policy.currency_id, self.inr)
        self.assertEqual(self.policy.rate_ids[0].currency_id, self.inr)
        self.assertEqual(self.cat_food.currency_id, self.inr)
        self.assertEqual(session.currency_id, self.inr)
        bill = self.bill(self.sales, self.day1, self.cat_food, 90)
        self.assertEqual(bill.currency_id, self.inr)
        claim = self.env['zivo.expense.claim'].with_user(self.sales).create(
            {'date_from': self.day1, 'date_to': self.day1})
        self.assertEqual(claim.currency_id, self.inr)
        claim._fill_from_tracking()
        self.assertIn(bill, claim.line_ids)
        self.assertEqual(set(claim.line_ids.mapped('currency_id')), {self.inr})
        html = self.env['ir.actions.report']._render_qweb_html(
            'zivoit_field_tracking.report_expense_claim', claim.ids)[0].decode()
        self.assertIn('₹', html)
        # and back to USD
        self.toggle('USD')
        self.assertEqual(self.policy.currency_id, self.usd)
        self.assertEqual(claim.currency_id, self.inr, 'existing claims keep their currency')

    def test_claims_never_mix_currencies(self):
        self.track_day(self.dev, self.day1, km=10, hours=7)
        usd_bill = self.bill(self.sales, self.day1, self.cat_toll, 20)
        usd_claim = self.env['zivo.expense.claim']._create_for(self.sales, self.day1, self.day1)
        self.assertIn(usd_bill, usd_claim.line_ids)
        usd_claim.action_cancel()
        self.toggle('INR')
        with self.assertRaises(UserError):
            usd_claim.action_reset_draft()  # refresh of a USD claim while expenses use INR
        inr_claim = self.env['zivo.expense.claim']._create_for(self.sales, self.day1, self.day1)
        self.assertNotIn(usd_bill, inr_claim.line_ids, 'a USD bill is not pulled into an INR claim')
        self.assertTrue(inr_claim.line_ids, 'the GPS allowances are, in INR')
        with self.assertRaises(ValidationError):
            usd_bill.with_user(self.sales).write({'claim_id': inr_claim.id})
        # salesperson cannot change currencies
        with self.assertRaises(AccessError):
            inr_claim.with_user(self.sales).write({'currency_id': self.usd.id})
        with self.assertRaises(AccessError):
            usd_bill.with_user(self.sales).write({'currency_id': self.inr.id})
        # a currency sent by the client on create is ignored: the server decides
        line = self.env['zivo.expense.line'].with_user(self.sales).create({
            'name': 'Tea', 'date': self.day1, 'category_id': self.cat_food.id, 'unit_amount': 30,
            'currency_id': self.usd.id})
        self.assertEqual(line.currency_id, self.inr)

    def test_inr_claim_posted_in_usd_company(self):
        self.toggle('INR')
        self.track_day(self.dev, self.day1, km=20, hours=7)
        claim = self.env['zivo.expense.claim']._create_for(self.sales, self.day1, self.day1)
        claim.action_submit()
        self.env['ir.config_parameter'].sudo().set_param('zivoit_field_tracking.expense_auto_post', '0')
        claim.with_user(self.approver).action_approve()
        with self.assertRaises(UserError):
            claim.with_user(self.approver).action_post_expenses()  # no INR exchange rate yet
        self.env['res.currency.rate'].create({'currency_id': self.inr.id, 'rate': 80.0,
                                              'company_id': self.company.id, 'name': '2020-01-01'})
        claim.with_user(self.approver).action_post_expenses()
        expenses = claim.hr_expense_ids
        self.assertEqual(len(expenses), len(claim.line_ids))
        self.assertEqual(set(expenses.mapped('currency_id')), {self.inr})
        self.assertAlmostEqual(sum(expenses.mapped('total_amount_currency')), claim.total_amount, places=2)
        self.assertAlmostEqual(sum(expenses.mapped('total_amount')), claim.total_amount / 80.0, delta=0.05)
        self.assertEqual(set(expenses.mapped('state')), {'approved'})

    def test_same_currency_as_company_needs_no_rate(self):
        self.toggle('USD')
        self.track_day(self.dev, self.day1, km=20, hours=7)
        claim = self.env['zivo.expense.claim']._create_for(self.sales, self.day1, self.day1)
        claim.action_submit()
        claim.with_user(self.approver).action_approve()
        self.assertEqual(claim.state, 'posted')
        self.assertAlmostEqual(sum(claim.hr_expense_ids.mapped('total_amount')), claim.total_amount, places=2)


@tagged('post_install', '-at_install', 'zivoit_field_tracking')
class TestExpenseCurrencyHttp(HttpCase):

    def test_phone_gets_toggled_currency(self):
        user = new_test_user(self.env, login='zx_cur_sales', name='Currency Sales', tz='Asia/Kolkata',
                             groups='zivoit_field_tracking.group_field_user')
        device = self.env['zivo.track.device'].create({'user_id': user.id})
        self.env['res.currency'].with_context(active_test=False).search([('name', '=', 'INR')]).active = True
        for code, symbol in (('INR', '₹'), ('USD', '$')):
            self.env['ir.config_parameter'].sudo().set_param(PARAM, code)
            resp = self.url_open('/zivoit_tracking/api/sync', data=json.dumps({}), headers={
                'Content-Type': 'application/json', 'Authorization': 'Bearer %s' % device.token})
            currency = resp.json()['config']['expense']['currency']
            self.assertEqual(currency['code'], code)
            self.assertEqual(currency['symbol'], symbol)
