# -*- coding: utf-8 -*-
import base64
import json
from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, new_test_user, tagged

from .expense_common import jpeg_b64


@tagged('post_install', '-at_install', 'zivoit_field_tracking')
class TestExpenseHttp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sales = new_test_user(cls.env, login='zx_http_sales', password='zx_http_sales_pw',
                                  name='HTTP Expense Sales', tz='Asia/Kolkata',
                                  groups='zivoit_field_tracking.group_field_user')
        cls.device = cls.env['zivo.track.device'].create({'user_id': cls.sales.id, 'source': 'pwa'})
        cls.food = cls.env.ref('zivoit_field_tracking.expense_category_food')
        cls.ta = cls.env.ref('zivoit_field_tracking.expense_category_ta')
        cls.customer = cls.env['res.partner'].create({'name': 'Bill Customer', 'is_company': True,
                                                      'user_id': cls.sales.id})

    def _sync(self, payload):
        return self.url_open('/zivoit_tracking/api/sync', data=json.dumps(payload), headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer %s' % self.device.token})

    def _ts(self, minutes_ago=30):
        return (fields.Datetime.now() - timedelta(minutes=minutes_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')

    def test_expense_upload_with_photo_offline(self):
        photo = 'data:image/jpeg;base64,' + jpeg_b64().decode()
        payload = {
            'points': [{'uid': 'xp1', 'lat': 17.44, 'lon': 78.44, 'ts': self._ts(40)},
                       {'uid': 'xp2', 'lat': 17.45, 'lon': 78.44, 'ts': self._ts(20)}],
            'events': [{'uid': 'xe1', 'type': 'day_start', 'ts': self._ts(45), 'vehicle': 'four_wheeler'}],
            'expenses': [
                {'uid': 'x1', 'category_id': self.food.id, 'amount': 320, 'note': 'Lunch with buyer',
                 'ts': self._ts(25), 'lat': 17.445, 'lon': 78.44, 'accuracy': 9, 'offline': True,
                 'receipt': photo, 'partner_id': self.customer.id},
                {'uid': 'x2', 'category_id': self.ta.id, 'amount': 50, 'ts': self._ts(25)},
                {'uid': 'x3', 'category_id': self.food.id, 'amount': -4, 'ts': self._ts(25)},
                {'uid': 'x4', 'category_id': self.food.id, 'amount': 40, 'receipt': 'not-base64!!'},
                {'uid': 'x5', 'category_id': self.food.id, 'amount': 40,
                 'receipt': base64.b64encode(b'this is not an image').decode()},
                {'uid': '', 'category_id': self.food.id, 'amount': 40},
                {'uid': 'x6', 'category_id': 'abc', 'amount': 40},
            ],
        }
        resp = self._sync(payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['expenses']['accepted'], ['x1'])
        reasons = {r['uid']: r['reason'] for r in data['expenses']['rejected']}
        self.assertEqual(reasons, {'x2': 'unknown_category', 'x3': 'invalid_amount', 'x4': 'invalid_receipt',
                                   'x5': 'invalid_receipt', '': 'missing_uid', 'x6': 'unknown_category'})
        line = self.env['zivo.expense.line'].search([('user_id', '=', self.sales.id)])
        self.assertEqual(len(line), 1)
        self.assertEqual(line.amount, 320)
        self.assertEqual(line.name, 'Lunch with buyer')
        self.assertTrue(line.receipt)
        self.assertTrue(line.captured_offline)
        self.assertEqual(line.source, 'phone')
        self.assertEqual(line.partner_id, self.customer)
        self.assertTrue(line.session_id)
        self.assertEqual(line.session_id.vehicle_type, 'four_wheeler', 'vehicle chosen at day start')
        self.assertFalse(line.has_warning, line.warning_text)
        # summary of the day and policy config for the phone
        self.assertIn('ta', data['today'])
        self.assertAlmostEqual(data['today']['bills'], 320)
        cfg = data['config']['expense']
        self.assertTrue(cfg['enabled'])
        self.assertIn(self.food.id, [c['id'] for c in cfg['categories']])
        self.assertNotIn(self.ta.id, [c['id'] for c in cfg['categories']], 'allowances are automatic')
        self.assertEqual({v['key'] for v in cfg['vehicles']}, {'two_wheeler', 'four_wheeler', 'public', 'company'})
        # re-sent after a lost reply -> duplicate, nothing created
        again = self._sync({'expenses': [payload['expenses'][0]]}).json()
        self.assertEqual(again['expenses']['duplicates'], ['x1'])
        self.assertEqual(self.env['zivo.expense.line'].search_count([('user_id', '=', self.sales.id)]), 1)

    def test_expense_goes_to_open_draft_claim(self):
        today = fields.Date.context_today(self.env['zivo.expense.claim'])
        claim = self.env['zivo.expense.claim'].create({'user_id': self.sales.id, 'date_from': today - timedelta(days=5),
                                                       'date_to': today + timedelta(days=5)})
        self._sync({'expenses': [{'uid': 'dc1', 'category_id': self.food.id, 'amount': 60, 'ts': self._ts(1)}]})
        self.assertEqual(claim.line_ids.client_uid, 'dc1')

    def test_bad_expense_payload_types(self):
        for bad in ({'expenses': 'nope'}, {'expenses': {'a': 1}}, {'expenses': [1, 'x', None]},
                    {'events': 'x', 'points': 5}):
            resp = self._sync(bad)
            self.assertEqual(resp.status_code, 200, bad)
            self.assertTrue(resp.json()['ok'])

    def test_pwa_page_has_expense_card(self):
        self.authenticate('zx_http_sales', 'zx_http_sales_pw')
        resp = self.url_open('/zivoit_tracking/app')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('zv-exp-card', resp.text)
        self.assertIn('zv-vehicle', resp.text)
        boot = resp.text.split('data-boot="', 1)[1].split('"', 1)[0]
        self.assertIn('categories', boot)
