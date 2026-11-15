# -*- coding: utf-8 -*-
import re

from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install', 'bpcl')
class TestPortal(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.hsd = env.ref('zivoit_fuel_management.product_hsd')
        env['pump.rate'].create({'product_id': cls.hsd.id, 'date': '2026-01-01', 'rate': 95.0})
        portal = env.ref('base.group_portal')
        cls.cust_a = env['res.partner'].create({'name': 'Portal Fleet A', 'is_company': True,
                                                'is_pump_customer': True, 'pump_credit_limit': 100000})
        cls.cust_b = env['res.partner'].create({'name': 'Portal Fleet B', 'is_company': True,
                                                'is_pump_customer': True, 'pump_credit_limit': 100000})
        env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Portal Fleet A', 'login': 'fleet_a', 'password': 'fleet_a_pw', 'partner_id': cls.cust_a.id,
            'group_ids': [(6, 0, [portal.id])]})
        cls.veh_a = env['pump.vehicle'].create({'name': 'TS01AA0001', 'partner_id': cls.cust_a.id})
        for partner in (cls.cust_a, cls.cust_b):
            env['pump.credit.sale'].create({'partner_id': partner.id, 'product_id': cls.hsd.id, 'qty': 100,
                                            'date': '2026-03-05'}).action_confirm()
        bills = env['pump.bill'].generate_bills('2026-03-01', '2026-03-15')
        cls.bill_a = bills.filtered(lambda b: b.partner_id == cls.cust_a)
        cls.bill_b = bills.filtered(lambda b: b.partner_id == cls.cust_b)

    def test_portal_pages(self):
        self.authenticate('fleet_a', 'fleet_a_pw')
        for url in ['/my/fuel', '/my/fuel/ledger?date_from=2026-03-01&date_to=2026-03-31', '/my/fuel/monthly?fy=2025',
                    '/my/fuel/statement?date_from=2026-03-01&date_to=2026-03-31', '/my/fuel/bills?date_from=2026-01-01',
                    '/my/fuel/slips?date_from=2026-03-01&date_to=2026-03-31', '/my/fuel/indent', '/my/fuel/rates']:
            r = self.url_open(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn('Portal Fleet A', r.text, url)
        r = self.url_open('/my/fuel/ledger?date_from=2026-03-01&date_to=2026-03-31')
        self.assertIn(self.bill_a.name, r.text)
        self.assertNotIn(self.bill_b.name, r.text)

    def test_portal_downloads_and_isolation(self):
        self.authenticate('fleet_a', 'fleet_a_pw')
        r = self.url_open('/my/fuel/ledger/xlsx?date_from=2026-03-01&date_to=2026-03-31')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b'PK'))
        r = self.url_open('/my/fuel/slips/xlsx?date_from=2026-03-01&date_to=2026-03-31')
        self.assertTrue(r.content.startswith(b'PK'))
        r = self.url_open('/my/fuel/bill/%d/pdf' % self.bill_b.id)
        self.assertEqual(r.status_code, 404, "customer A must not see customer B's bill")
        slip_b = self.bill_b.sale_ids[:1]
        r = self.url_open('/my/fuel/slip/%d/pdf' % slip_b.id)
        self.assertEqual(r.status_code, 404)

    def test_portal_indent(self):
        self.authenticate('fleet_a', 'fleet_a_pw')
        page = self.url_open('/my/fuel/indent').text
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        r = self.url_open('/my/fuel/indent', data={
            'csrf_token': token,
            'vehicle_id': self.veh_a.id, 'product_id': self.hsd.id, 'indent_type': 'qty', 'qty': '40',
            'comment': 'test', 'date': '2026-03-20'})
        self.assertEqual(r.status_code, 200)
        ind = self.env['pump.indent'].search([('partner_id', '=', self.cust_a.id)])
        self.assertEqual(len(ind), 1)
        self.assertEqual(ind.source, 'portal')

    def test_login_redirect_and_unlinked(self):
        r = self.url_open('/web/login')
        self.assertIn('Fuel Station Management', r.text)
        self.authenticate('admin', 'admin')
        r = self.url_open('/my/fuel')
        self.assertIn('No fuel credit account linked', r.text)

    def test_portal_v2_pages_and_ai(self):
        self.authenticate('fleet_a', 'fleet_a_pw')
        r = self.url_open('/my/fuel/bill/%d' % self.bill_a.id)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Balance due on this bill', r.text)
        self.assertEqual(self.url_open('/my/fuel/bill/%d' % self.bill_b.id).status_code, 404)
        r = self.url_open('/my/fuel/vehicles')
        self.assertIn('TS01AA0001', r.text)
        r = self.url_open('/my/fuel/indent?vehicle=%d&product=%d&qty=40' % (self.veh_a.id, self.hsd.id))
        self.assertIn('Prepared by the AI assistant', r.text)
        page = self.url_open('/my/fuel').text
        self.assertIn('bpf-ai-panel', page)
        token = re.search(r'csrf_token: "([^"]+)"', page).group(1)
        r = self.url_open('/my/fuel/ai/ask', data={'question': 'What is my balance?', 'csrf_token': token})
        data = r.json()
        self.assertIn('outstanding', data['answer'].lower())
        self.assertEqual(data['source'], 'builtin')
