# -*- coding: utf-8 -*-
from odoo.exceptions import AccessError
from odoo.tests import tagged

from .expense_common import ZivoExpenseCommon


@tagged('post_install', '-at_install', 'zivoit_field_tracking')
class TestExpenseSecurity(ZivoExpenseCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.session = cls.track_day(cls.dev, cls.day1, km=20, hours=7)
        cls.track_day(cls.dev2, cls.day1, km=20, hours=7)
        cls.claim = cls.env['zivo.expense.claim']._create_for(cls.sales, cls.day1, cls.day1)
        cls.claim2 = cls.env['zivo.expense.claim']._create_for(cls.sales2, cls.day1, cls.day1)

    def test_salesperson_sees_only_own(self):
        Claim = self.env['zivo.expense.claim'].with_user(self.sales)
        self.assertEqual(Claim.search([]), self.claim)
        Line = self.env['zivo.expense.line'].with_user(self.sales)
        self.assertEqual(Line.search([]).user_id, self.sales)
        with self.assertRaises(AccessError):
            self.claim2.with_user(self.sales).read(['total_amount'])

    def test_approver_sees_team_claims(self):
        self.assertEqual(self.env['zivo.expense.claim'].with_user(self.approver).search([]), self.claim)
        self.assertEqual(self.env['zivo.expense.claim'].with_user(self.manager).search(
            [('id', 'in', (self.claim | self.claim2).ids)]), self.claim | self.claim2)

    def test_salesperson_cannot_bypass_workflow(self):
        for vals in ({'state': 'approved'}, {'approved_by_id': self.sales.id}, {'policy_id': False}):
            with self.assertRaises(AccessError):
                self.claim.with_user(self.sales).write(vals)
        with self.assertRaises(AccessError):
            self.claim.with_user(self.sales).write({'approver_id': self.sales2.id})
        with self.assertRaises(AccessError):
            self.env['zivo.expense.claim'].with_user(self.sales).create(
                {'date_from': self.day1, 'date_to': self.day1, 'state': 'approved'})
        with self.assertRaises(AccessError):
            self.env['zivo.expense.claim'].with_user(self.sales).create(
                {'date_from': self.day1, 'date_to': self.day1, 'user_id': self.sales2.id})

    def test_salesperson_cannot_change_rates_or_gps(self):
        travel = self.claim.line_ids.filtered(lambda l: l.line_type == 'travel')
        with self.assertRaises(AccessError):
            travel.with_user(self.sales).write({'unit_amount': 100})
        with self.assertRaises(AccessError):
            travel.with_user(self.sales).write({'tracked_km': 500})
        travel.with_user(self.sales).write({'quantity': 21})  # allowed, but flagged when too high
        with self.assertRaises(AccessError):
            self.session.with_user(self.sales).write({'distance_km': 500})
        with self.assertRaises(AccessError):
            self.session.with_user(self.sales).write({'ta_amount': 5000})
        with self.assertRaises(AccessError):
            self.env['zivo.expense.line'].with_user(self.sales).create({
                'name': 'Fake mileage', 'date': self.day1, 'category_id': self.cat_ta.id, 'quantity': 300,
                'unit_amount': 4})
        with self.assertRaises(AccessError):
            self.env['zivo.expense.line'].with_user(self.sales).create({
                'name': 'x', 'date': self.day1, 'category_id': self.cat_food.id, 'unit_amount': 4,
                'source': 'auto'})
        with self.assertRaises(AccessError):
            self.env['zivo.expense.line'].with_user(self.sales).create({
                'name': 'x', 'date': self.day1, 'category_id': self.cat_food.id, 'unit_amount': 4,
                'user_id': self.sales2.id})

    def test_manager_can_correct(self):
        travel = self.claim.line_ids.filtered(lambda l: l.line_type == 'travel')
        travel.with_user(self.manager).write({'unit_amount': 5})
        self.assertEqual(travel.unit_amount, 5)
        self.session.with_user(self.manager).write({'vehicle_type': 'four_wheeler'})

    def test_policy_and_category_read_only_for_salesperson(self):
        with self.assertRaises(AccessError):
            self.policy.with_user(self.sales).write({'da_full_amount': 9999})
        with self.assertRaises(AccessError):
            self.cat_food.with_user(self.sales).write({'daily_limit': 0})
        self.assertTrue(self.policy.with_user(self.sales).name)
