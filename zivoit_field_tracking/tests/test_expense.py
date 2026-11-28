# -*- coding: utf-8 -*-
from datetime import date, timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from .expense_common import ZivoExpenseCommon, jpeg_b64


@tagged('post_install', '-at_install', 'zivoit_field_tracking')
class TestExpensePolicy(ZivoExpenseCommon):

    def test_travel_allowance_rate_and_cap(self):
        self.assertEqual(self.policy._travel_allowance('two_wheeler', 30), (4.0, 50, 30, 120.0))
        rate, cap, km, amount = self.policy._travel_allowance('two_wheeler', 80)
        self.assertEqual((km, amount), (50, 200.0), 'mileage is capped at 50 km per day')
        self.assertEqual(self.policy._travel_allowance('four_wheeler', 80)[3], 800.0, 'no cap on cars')
        self.assertEqual(self.policy._travel_allowance('public', 80)[3], 0.0)
        self.assertEqual(self.policy._travel_allowance('company', 80)[3], 0.0, 'no rate line = no mileage')

    def test_daily_allowance_kinds(self):
        P = self.policy
        self.assertEqual(P._daily_allowance(7, 2, 30), ('full', 400))
        self.assertEqual(P._daily_allowance(4, 2, 30), ('half', 200))
        self.assertEqual(P._daily_allowance(2, 2, 30), ('none', 0.0))
        self.assertEqual(P._daily_allowance(8, 2, 150), ('outstation', 900))
        self.assertEqual(P._daily_allowance(2, 2, 150), ('none', 0.0), 'outstation needs the half day hours')
        P.da_min_visits = 2
        self.assertEqual(P._daily_allowance(8, 1, 30), ('none', 0.0), 'minimum customer visits')
        self.assertEqual(P._daily_allowance(8, 2, 30), ('full', 400))

    def test_policy_constraints(self):
        with self.assertRaises(ValidationError):
            self.policy.da_full_amount = -1
        with self.assertRaises(ValidationError):
            self.env['zivo.expense.policy'].create({'name': 'Second default', 'is_default': True})
        with self.assertRaises(ValidationError):
            self.policy.write({'da_half_min_hours': 8, 'da_full_min_hours': 6})
        with self.assertRaises(Exception), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                self.policy.rate_ids = [(0, 0, {'vehicle_type': 'two_wheeler', 'rate_per_km': 1})]

    def test_user_policy_overrides_default(self):
        special = self.env['zivo.expense.policy'].create({
            'name': 'Metro', 'rate_ids': [(0, 0, {'vehicle_type': 'two_wheeler', 'rate_per_km': 6.0})]})
        self.sales.zivo_expense_policy_id = special
        self.assertEqual(self.env['zivo.expense.policy']._for_user(self.sales), special)
        self.assertEqual(self.env['zivo.expense.policy']._for_user(self.sales2), self.policy)
        special.active = False
        self.assertEqual(self.env['zivo.expense.policy']._for_user(self.sales), self.policy)


@tagged('post_install', '-at_install', 'zivoit_field_tracking')
class TestExpenseClaims(ZivoExpenseCommon):

    def test_session_allowance_from_gps(self):
        session = self.track_day(self.dev, self.day1, km=30, hours=7)
        self.assertAlmostEqual(session.distance_km, 30, delta=0.5)
        self.assertEqual(session.vehicle_type, 'two_wheeler')
        self.assertAlmostEqual(session.ta_amount, round(session.distance_km * 4, 2), places=2)
        self.assertEqual((session.da_kind, session.da_amount), ('full', 400))
        # change of vehicle recomputes the mileage
        session.with_user(self.sales).write({'vehicle_type': 'four_wheeler'})
        self.assertAlmostEqual(session.ta_amount, round(session.distance_km * 10, 2), places=2)
        # policy change recomputes days that are not claimed yet
        self.policy.da_full_amount = 450
        self.assertEqual(session.da_amount, 450)

    def test_claim_build_refresh_and_no_double_claim(self):
        s1 = self.track_day(self.dev, self.day1, km=30, hours=7)
        s2 = self.track_day(self.dev, self.day2, km=80, hours=4)          # capped + half day
        open_day = self.track_day(self.dev, date.today(), km=5, hours=1, close=False)
        food = self.bill(self.sales, self.day1, self.cat_food, 90)
        claim = self.env['zivo.expense.claim'].with_user(self.sales).create({
            'date_from': self.day1 - timedelta(days=1), 'date_to': date.today()})
        claim._fill_from_tracking()
        self.assertEqual(claim.session_ids, s1 | s2, 'unfinished days are not claimed')
        self.assertNotIn(open_day, claim.session_ids)
        self.assertIn(food, claim.line_ids)
        travel = claim.line_ids.filtered(lambda l: l.line_type == 'travel')
        daily = claim.line_ids.filtered(lambda l: l.line_type == 'daily')
        self.assertEqual(len(travel), 2)
        capped = travel.filtered(lambda l: l.session_id == s2)
        self.assertEqual(capped.max_quantity, 50)
        self.assertEqual(capped.amount, 200.0, '80 km capped at 50 km * 4')
        self.assertIn('daily cap', capped.warning_text)
        self.assertEqual(sorted(daily.mapped('amount')), [200.0, 400.0])
        self.assertEqual(claim.total_amount, sum(claim.line_ids.mapped('amount')))
        self.assertEqual(claim.employee_id, self.emp)
        self.assertEqual(claim.approver_id, self.approver)
        self.assertTrue(claim.name.startswith('FEC/'))
        # salesperson corrects claimed km (kept across refresh), and it is flagged
        t1 = travel.filtered(lambda l: l.session_id == s1)
        t1.with_user(self.sales).quantity = round(s1.distance_km * 1.5, 1)
        self.assertIn('GPS tracked', t1.warning_text)
        claim.action_refresh()
        t1 = claim.line_ids.filtered(lambda l: l.line_type == 'travel' and l.session_id == s1)
        self.assertAlmostEqual(t1.quantity, round(s1.distance_km * 1.5, 1))
        # a second claim of the same period cannot take the same days or bills
        other = self.env['zivo.expense.claim']._create_for(self.sales, self.day1, self.day2)
        self.assertFalse(other.session_ids)
        self.assertFalse(other.line_ids)
        # cancelling the first claim releases days and bills
        claim.action_cancel()
        self.assertFalse(s1.claim_id)
        self.assertFalse(food.claim_id)
        self.assertEqual(food.state, 'unclaimed')
        self.assertFalse(claim.line_ids.filtered(lambda l: l.source == 'auto'))
        other._fill_from_tracking()
        self.assertEqual(other.session_ids, s1 | s2)

    def test_warnings(self):
        session = self.track_day(self.dev, self.day1, km=20, hours=7, mock=True)
        photo = jpeg_b64()
        no_photo = self.bill(self.sales, self.day1, self.cat_food, 250)
        self.assertIn('Bill photo missing', no_photo.warning_text)
        no_photo.with_user(self.sales).receipt = photo
        self.assertNotIn('Bill photo missing', no_photo.warning_text or '')
        small = self.bill(self.sales, self.day1, self.cat_food, 80)
        self.assertFalse(small.has_warning, 'below the bill threshold, on a tracked day')
        big = self.bill(self.sales, self.day1, self.cat_food, 300, receipt=photo)
        self.assertIn('Daily limit', big.warning_text)
        dup = self.bill(self.sales, self.day1, self.cat_food, 80)
        self.assertIn('duplicate', dup.warning_text)
        untracked = self.bill(self.sales, self.day1 - timedelta(days=5), self.cat_toll, 20)
        self.assertIn('No GPS tracking', untracked.warning_text)
        far = self.env['zivo.expense.line'].sudo().with_context(zivo_internal=True).create({
            'name': 'Far bill', 'user_id': self.sales.id, 'date': self.day1, 'category_id': self.cat_toll.id,
            'unit_amount': 30, 'latitude': 17.60, 'longitude': 78.80, 'source': 'phone'})
        far._refresh_warnings()
        self.assertIn('away from the tracked route', far.warning_text)
        self.assertGreater(far.route_distance_km, 2)
        near = self.env['zivo.expense.line'].sudo().with_context(zivo_internal=True).create({
            'name': 'Near bill', 'user_id': self.sales.id, 'date': self.day1, 'category_id': self.cat_toll.id,
            'unit_amount': 35, 'latitude': 17.401, 'longitude': 78.4505, 'source': 'phone'})
        near._refresh_warnings()
        self.assertFalse(near.has_warning)
        claim = self.env['zivo.expense.claim']._create_for(self.sales, self.day1, self.day1)
        fuel = self.bill(self.sales, self.day1, self.cat_fuel, 400, receipt=photo, claim_id=claim.id)
        self.assertIn('mileage allowance', fuel.warning_text)
        travel = claim.line_ids.filtered(lambda l: l.line_type == 'travel')
        self.assertEqual(session.mock_point_count, 1)
        self.assertIn('Fake GPS', travel.warning_text)
        self.assertGreaterEqual(claim.warning_count, 4)

    def test_line_amount_validation(self):
        with self.assertRaises(ValidationError):
            self.bill(self.sales, self.day1, self.cat_food, -5)
        with self.assertRaises(ValidationError):
            self.bill(self.sales, self.day1, self.cat_food, 50000000)

    def test_workflow_approve_and_post_to_odoo_expenses(self):
        self.track_day(self.dev, self.day1, km=30, hours=7)
        self.bill(self.sales, self.day1, self.cat_food, 250, receipt=jpeg_b64())
        claim = self.env['zivo.expense.claim'].with_user(self.sales).create(
            {'date_from': self.day1, 'date_to': self.day1})
        claim.with_user(self.sales).action_refresh()
        total = claim.total_amount
        self.assertGreater(total, 600)
        claim.with_user(self.sales).action_submit()
        self.assertEqual(claim.state, 'submitted')
        self.assertTrue(claim.activity_ids.filtered(lambda a: a.user_id == self.approver))
        # locked lines
        with self.assertRaises(UserError):
            claim.line_ids[0].with_user(self.sales).name = 'changed'
        with self.assertRaises(UserError):
            self.bill(self.sales, self.day1, self.cat_food, 20, claim_id=claim.id)
        # no self approval, no approval by an unrelated colleague
        with self.assertRaises(AccessError):
            claim.with_user(self.sales).action_approve()
        with self.assertRaises(AccessError):
            claim.with_user(self.sales2).action_approve()
        self.assertTrue(claim.with_user(self.approver).can_approve)
        claim.with_user(self.approver).action_approve()
        # auto-post (default setting) created approved Odoo Expenses
        self.assertEqual(claim.state, 'posted')
        expenses = claim.hr_expense_ids
        self.assertEqual(len(expenses), len(claim.line_ids))
        self.assertAlmostEqual(sum(expenses.mapped('total_amount_currency')), total, places=2)
        self.assertEqual(set(expenses.mapped('state')), {'approved'})
        self.assertEqual(expenses.employee_id, self.emp)
        self.assertEqual(expenses.manager_id, self.approver)
        bill_expense = expenses.filtered(lambda e: e.product_id == self.cat_food.product_id)
        self.assertEqual(bill_expense.nb_attachment, 1, 'bill photo attached to the Odoo expense')
        self.assertEqual(claim.payment_state, 'to_pay')
        self.assertTrue(all(claim.line_ids.mapped('hr_expense_id')))
        with self.assertRaises(UserError):
            claim.action_reset_draft()
        with self.assertRaises(UserError):
            claim.action_cancel()
        with self.assertRaises(UserError):
            claim.unlink()

    def test_reimbursement_status_follows_accounting(self):
        self.track_day(self.dev, self.day1, km=10, hours=7)
        claim = self.env['zivo.expense.claim']._create_for(self.sales, self.day1, self.day1)
        claim.action_submit()
        claim.with_user(self.manager).action_approve()
        expenses = claim.hr_expense_ids
        self.assertTrue(expenses)
        action = expenses.action_post()
        self.env[action['res_model']].browse(action['res_id']).with_context(
            active_ids=expenses.ids, active_model='hr.expense').action_post_entry()
        self.assertEqual(set(expenses.mapped('state')), {'posted'})
        self.assertEqual(claim.payment_state, 'to_pay')
        moves = expenses.account_move_id
        register = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=moves.ids).create({})
        register.action_create_payments()
        self.assertIn(claim.payment_state, ('in_payment', 'paid'))

    def test_manual_post_and_missing_employee(self):
        self.env['ir.config_parameter'].sudo().set_param('zivoit_field_tracking.expense_auto_post', '0')
        self.track_day(self.dev2, self.day1, km=10, hours=7)
        claim = self.env['zivo.expense.claim']._create_for(self.sales2, self.day1, self.day1)
        claim.action_submit()
        self.assertFalse(claim.approver_id, 'no expense manager / parent on the employee')
        claim.with_user(self.manager).action_approve()
        self.assertEqual(claim.state, 'approved', 'not sent automatically')
        self.emp2.sudo().user_id = False
        claim.invalidate_recordset(['employee_id'])
        claim.employee_id = False
        with self.assertRaises(UserError):
            claim.with_user(self.manager).action_post_expenses()
        claim.employee_id = self.emp2
        claim.with_user(self.manager).action_post_expenses()
        self.assertEqual(claim.state, 'posted')

    def test_reject_and_resubmit(self):
        self.track_day(self.dev, self.day1, km=10, hours=7)
        claim = self.env['zivo.expense.claim']._create_for(self.sales, self.day1, self.day1)
        with self.assertRaises(UserError):
            claim.with_user(self.approver).action_approve()  # not submitted
        claim.with_user(self.sales).action_submit()
        action = claim.with_user(self.approver).action_reject()
        wizard = self.env[action['res_model']].with_user(self.approver).with_context(action['context']).create(
            {'reason': 'Attach the toll bills'})
        wizard.action_reject()
        self.assertEqual(claim.state, 'rejected')
        self.assertEqual(claim.reject_reason, 'Attach the toll bills')
        self.assertFalse(claim.activity_ids)
        # rejected lines can be corrected and the claim resubmitted
        self.bill(self.sales, self.day1, self.cat_toll, 40, claim_id=claim.id)
        claim.with_user(self.sales).action_submit()
        self.assertEqual(claim.state, 'submitted')
        self.assertFalse(claim.reject_reason)
        claim.with_user(self.sales).action_reset_draft()
        self.assertEqual(claim.state, 'draft')

    def test_empty_claim_cannot_be_submitted(self):
        claim = self.env['zivo.expense.claim'].with_user(self.sales).create(
            {'date_from': self.day1, 'date_to': self.day1})
        with self.assertRaises(UserError):
            claim.action_submit()
        with self.assertRaises(ValidationError):
            claim.write({'date_to': self.day1 - timedelta(days=1)})
        with self.assertRaises(ValidationError):
            claim.write({'date_from': self.day1 - timedelta(days=120)})

    def test_claim_wizard(self):
        self.track_day(self.dev, self.day1, km=10, hours=7)
        self.track_day(self.dev2, self.day1, km=12, hours=7)
        wizard = self.env['zivo.expense.claim.wizard'].with_user(self.manager).create({
            'date_from': self.day1, 'date_to': self.day1, 'user_ids': [(6, 0, (self.sales | self.sales2).ids)]})
        action = wizard.action_create()
        claims = self.env['zivo.expense.claim'].search(action['domain'])
        self.assertEqual(claims.user_id, self.sales | self.sales2)
        # running it again refreshes the same draft claims
        action2 = wizard.action_create()
        self.assertEqual(set(self.env['zivo.expense.claim'].search(action2['domain']).ids), set(claims.ids))
        # salesperson can only create own claims
        wiz = self.env['zivo.expense.claim.wizard'].with_user(self.sales).create({
            'date_from': self.day1, 'date_to': self.day1, 'user_ids': [(6, 0, self.sales2.ids)]})
        with self.assertRaises(UserError):
            wiz.action_create()

    def test_monthly_cron(self):
        last_month_day = fields.Date.context_today(self.env['zivo.expense.claim']).replace(day=1) - timedelta(days=3)
        self.track_day(self.dev, last_month_day, km=15, hours=7)
        Claim = self.env['zivo.expense.claim'].with_context(zivo_force_monthly=True)
        Claim._cron_create_monthly_claims()
        self.assertFalse(Claim.search([('user_id', '=', self.sales.id)]), 'disabled by default')
        self.env['ir.config_parameter'].sudo().set_param('zivoit_field_tracking.expense_auto_claim', 'True')
        Claim._cron_create_monthly_claims()
        claim = Claim.search([('user_id', '=', self.sales.id)])
        self.assertEqual(len(claim), 1)
        self.assertEqual(claim.date_from, last_month_day.replace(day=1))
        self.assertTrue(claim.line_ids)
        Claim._cron_create_monthly_claims()
        self.assertEqual(Claim.search_count([('user_id', '=', self.sales.id)]), 1, 'idempotent')

    def test_pdf_report_renders(self):
        self.track_day(self.dev, self.day1, km=10, hours=7)
        self.bill(self.sales, self.day1, self.cat_food, 250, receipt=jpeg_b64())
        claim = self.env['zivo.expense.claim']._create_for(self.sales, self.day1, self.day1)
        html, _type = self.env['ir.actions.report']._render_qweb_html(
            'zivoit_field_tracking.report_expense_claim', claim.ids)
        html = html.decode()
        self.assertIn(claim.name, html)
        self.assertIn('Bill Photos', html)
        self.assertIn('data:image', html)

    def test_settings_default_policy(self):
        other = self.env['zivo.expense.policy'].create({'name': 'Other'})
        settings = self.env['res.config.settings'].create({})
        self.assertEqual(settings.zivo_expense_default_policy_id, self.policy)
        self.assertTrue(settings.zivo_expense_auto_post)
        settings.zivo_expense_default_policy_id = other
        settings.zivo_expense_auto_post = False
        settings.execute()
        self.assertTrue(other.is_default)
        self.assertFalse(self.policy.is_default)
        self.assertFalse(self.env['res.config.settings'].create({}).zivo_expense_auto_post)
