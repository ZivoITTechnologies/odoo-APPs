# -*- coding: utf-8 -*-
import json

from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestCourierHttp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        india = cls.env.ref('base.in')
        cls.sender = cls.env['res.partner'].create({'name': 'WH', 'street': 'Plot 1', 'city': 'Raipur',
                                                    'zip': '492003', 'country_id': india.id, 'phone': '9826000001'})
        cls.receiver = cls.env['res.partner'].create({'name': 'Asha', 'street': 'FC Road', 'city': 'Pune',
                                                      'zip': '411005', 'country_id': india.id, 'phone': '9822045671'})
        cls.account = cls.env['cp.courier.account'].create({
            'name': 'SR', 'provider': 'shiprocket', 'username': 'a@b.c', 'password': 'p', 'pickup_partner_id': cls.sender.id,
            'pickup_location_name': 'Primary', 'notify_customer': False})
        cls.parcel = cls.env['cp.parcel'].create({
            'account_id': cls.account.id, 'sender_partner_id': cls.sender.id, 'recipient_partner_id': cls.receiver.id,
            'awb': '1432600001', 'state': 'in_transit', 'courier_name': 'Delhivery Air', 'declared_value': 100})

    def test_webhook_updates_parcel(self):
        url = '/cp/courier/webhook/%s/%s' % (self.account.id, self.account.sudo().webhook_token)
        payload = {'awb': '1432600001', 'current_status': 'DELIVERED', 'current_status_id': 7,
                   'shipment_status_id': 7, 'etd': '2026-10-27 18:00:00', 'scans': [
                       {'date': '2026-10-27 12:31:00', 'activity': 'Delivered to consignee', 'location': 'Pune',
                        'sr-status': '7', 'sr-status-label': 'DELIVERED'}]}
        response = self.url_open(url, data=json.dumps(payload), headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'ok': True, 'updated': 1})
        self.parcel.invalidate_recordset()
        self.assertEqual(self.parcel.state, 'delivered')
        self.assertEqual(self.parcel.event_ids[:1].source, 'webhook')

    def test_webhook_wrong_token(self):
        response = self.url_open('/cp/courier/webhook/%s/not-the-token' % self.account.id, data='{}',
                                 headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 404)

    def test_public_tracking_page(self):
        response = self.url_open('/cp/parcel/%s' % self.parcel.access_token)
        self.assertEqual(response.status_code, 200)
        self.assertIn('1432600001', response.text)
        self.assertIn('On the way', response.text)
        self.assertEqual(self.url_open('/cp/parcel/nope-nope-nope').status_code, 404)
