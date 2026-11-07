# -*- coding: utf-8 -*-
import json
from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, tagged

from .test_easyecom import ee_order


@tagged('post_install', '-at_install', 'cargo_pilot')
class TestEasyEcomCarrier(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env['cp.rate.contract'].search([('state', '=', 'active')]).write({'state': 'expired'})
        env['cp.channel.rule'].search([]).write({'active': False})
        india = env.ref('base.in')
        State = env['res.country.state']
        st_tg = State.search([('country_id', '=', india.id), ('code', '=', 'TS')], limit=1) or \
            State.create({'name': 'Telangana', 'code': 'TS', 'country_id': india.id})
        origin = env['cp.location'].create({'name': 'EE Plant', 'code': 'EEP', 'latitude': 21.25, 'longitude': 81.63})
        carrier = env['res.partner'].create({'name': 'EE Part Load Co', 'is_company': True, 'cp_is_transporter': True})
        contract = env['cp.rate.contract'].create({
            'transporter_id': carrier.id, 'date_start': fields.Date.today() - timedelta(days=1),
            'date_end': fields.Date.today() + timedelta(days=100),
            'line_ids': [(0, 0, {'dest_state_id': st_tg.id, 'charge_basis': 'kg', 'rate': 10.0})]})
        contract.action_activate()
        cls.account = env['cp.easyecom.account'].create({
            'name': 'EE carrier', 'provider': 'simulator', 'push_tracking': False,
            'warehouse_ids': [(0, 0, {'name': 'Plant', 'location_key': 'WH-RPR', 'location_id': origin.id})]})
        cls.token = cls.account.sudo().inbound_token

    def _post(self, path, payload, token=None):
        return self.url_open(path, data=json.dumps(payload),
                             headers={'Content-Type': 'application/json', 'X-CP-Token': token or self.token})

    def test_carrier_create_and_cancel(self):
        self.assertEqual(self._post('/cp/easyecom/carrier/create_shipment', ee_order(41, 'AMZ-41'),
                                    token='wrong-token-wrong-token').status_code, 401)
        res = self._post('/cp/easyecom/carrier/create_shipment', ee_order(41, 'AMZ-41'))
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['carrier_name'], 'EE Part Load Co')
        self.assertTrue(data['awb'].startswith(data['shipment']))
        self.assertGreater(data['freight'], 0)
        again = self._post('/cp/easyecom/carrier/create_shipment', ee_order(41, 'AMZ-41')).json()
        self.assertEqual(again['awb'], data['awb'], 'same order, same shipment')
        res = self._post('/cp/easyecom/carrier/cancel_shipment', {'awb': data['awb']})
        self.assertEqual(res.json()['status'], 'cancelled', res.text)
        order = self.env['cp.channel.order'].search([('external_id', '=', '41')])
        self.assertEqual(order.state, 'cancel')
        trip = self.env['cp.trip'].search([('name', '=', data['shipment'])])
        self.assertEqual(trip.state, 'cancel')

    def test_webhooks(self):
        res = self._post('/cp/easyecom/webhook/order', {'orders': [ee_order(51, 'W-51'), ee_order(52, 'W-52')]})
        self.assertEqual(res.json()['orders'], 2)
        res = self.url_open('/cp/easyecom/webhook/inventory?token=%s' % self.token,
                            data=json.dumps([{'sku': 'PUMP', 'location_key': 'WH-RPR', 'availableInventory': 7}]),
                            headers={'Content-Type': 'application/json'})
        self.assertEqual(res.json()['rows'], 1)
        self.assertEqual(self.env['cp.channel.stock'].search([('sku', '=', 'PUMP')]).available_qty, 7)
