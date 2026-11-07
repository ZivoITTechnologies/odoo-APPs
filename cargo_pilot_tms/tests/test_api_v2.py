# -*- coding: utf-8 -*-
import json

from odoo.tests import HttpCase, tagged

from .common import PIXEL


@tagged('post_install', '-at_install', 'cargo_pilot')
class TestApiV2(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.company.cp_api_key = 'v2-key-0123456789abcdef'
        Loc = env['cp.location']
        cls.origin = Loc.create({'name': 'V2 Plant', 'code': 'V2P', 'latitude': 21.25, 'longitude': 81.63})
        cls.dest = Loc.create({'name': 'V2 Depot', 'code': 'V2D', 'latitude': 17.38, 'longitude': 78.48})
        cls.vtype = env['cp.vehicle.type'].create({'name': 'V2 32ft', 'capacity_ton': 15})
        cls.transporter = env['res.partner'].create({'name': 'V2 Transport', 'is_company': True,
                                                     'cp_is_transporter': True})
        lane = env['cp.lane'].create({'origin_id': cls.origin.id, 'destination_id': cls.dest.id, 'distance_km': 780})
        contract = env['cp.rate.contract'].create({
            'transporter_id': cls.transporter.id, 'date_start': '2020-01-01', 'date_end': '2099-12-31',
            'line_ids': [(0, 0, {'lane_id': lane.id, 'vehicle_type_id': cls.vtype.id, 'rate': 41000})]})
        contract.action_activate()
        cls.headers = {'Content-Type': 'application/json', 'X-CP-API-Key': 'v2-key-0123456789abcdef'}

    def _post(self, url, payload):
        return self.url_open(url, data=json.dumps(payload), headers=self.headers)

    def test_full_flow(self):
        self.assertEqual(self.url_open('/cp/api/v2/shipments', headers={'X-CP-API-Key': 'nope-nope-nope-nope'}).status_code, 401)
        res = self._post('/cp/api/v2/shipments', {
            'origin': 'V2P', 'destination': 'V2D', 'mode': 'ftl', 'vehicle_type': 'V2 32ft', 'channel': 'marketplace',
            'reference': 'EE-BATCH-7', 'auto_allocate': True,
            'orders': [{'order_ref': 'AMZ-1', 'invoice_ref': 'INV-1', 'quantity': 20, 'weight_kg': 400,
                        'order_value': 90000, 'serial_numbers': ['SN-1', 'SN-2']},
                       {'order_ref': 'AMZ-2', 'quantity': 10, 'weight_kg': 200, 'order_value': 45000}]})
        self.assertEqual(res.status_code, 201, res.text)
        body = res.json()
        self.assertEqual(body['state'], 'allocated')
        shipment = body['shipments'][0]
        self.assertEqual(shipment['transporter'], 'V2 Transport')
        self.assertEqual(shipment['acceptance'], 'pending')
        self.assertEqual(len(shipment['orders']), 2)
        name = shipment['name']

        res = self.url_open('/cp/api/v2/shipments?mode=ftl&channel=marketplace', headers=self.headers)
        self.assertGreaterEqual(res.json()['count'], 1)
        res = self._post('/cp/api/v2/shipments/%s/events' % name, {'action': 'accept'})
        self.assertEqual(res.json()['acceptance'], 'accepted')
        res = self._post('/cp/api/v2/shipments/%s/events' % name,
                         {'action': 'assign', 'vehicle_number': 'CG04 API 1', 'driver_phone': '9000000001'})
        self.assertEqual(res.json()['state'], 'assigned', res.text)
        res = self._post('/cp/api/v2/shipments/%s/events' % name, {'action': 'dispatch', 'eway_bill_no': '391234567890'})
        self.assertEqual(res.json()['state'], 'in_transit')
        res = self._post('/cp/api/v2/shipments/%s/events' % name, {'action': 'fly'})
        self.assertEqual(res.status_code, 422)

        res = self._post('/cp/api/v2/shipments/%s/pod' % name, {
            'receiver_name': 'Warehouse', 'serial_numbers': ['SN-1', 'SN-2'], 'latitude': 17.38, 'longitude': 78.48,
            'signature': PIXEL.decode(), 'photos': [PIXEL.decode()]})
        self.assertEqual(res.status_code, 201, res.text)
        self.assertEqual(res.json()['state'], 'verified', res.text)

        res = self.url_open('/cp/api/v2/shipments/%s' % name, headers=self.headers)
        data = res.json()
        self.assertEqual(data['state'], 'delivered')
        self.assertTrue(data['timeline'])
        res = self.url_open('/cp/api/v2/orders/AMZ-1/freight', headers=self.headers)
        self.assertAlmostEqual(res.json()['freight_cost'], 41000 * 2 / 3, places=0)
        self.assertEqual(self.url_open('/cp/api/v2/transporters/scorecard', headers=self.headers).status_code, 200)
        self.assertEqual(self.url_open('/cp/api/v2/freight-bills', headers=self.headers).status_code, 200)

    def test_openapi_and_offer_link(self):
        spec = self.url_open('/cp/api/v2/openapi.json').json()
        self.assertEqual(spec['openapi'], '3.0.3')
        self.assertIn('/cp/api/v2/shipments', spec['paths'])
        lane = self.env['cp.lane'].search([('origin_id', '=', self.origin.id)], limit=1)
        trip = self.env['cp.trip'].create({'lane_id': lane.id, 'transporter_id': self.transporter.id,
                                           'vehicle_type_id': self.vtype.id, 'agreed_freight': 41000})
        offer = trip.offer_ids
        self.authenticate(None, None)
        page = self.url_open('/cp/offer/%s' % offer.access_token)
        self.assertEqual(page.status_code, 200)
        self.assertIn('Accept booking', page.text)
        self.assertEqual(self.url_open('/cp/offer/not-a-token-at-all-123').status_code, 404)
        csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]
        res = self.url_open('/cp/offer/%s' % offer.access_token, data={'csrf_token': csrf, 'decision': 'accept'})
        self.assertEqual(res.status_code, 200)
        self.assertIn('confirmed', res.text)
        offer.invalidate_recordset()
        self.assertEqual(offer.state, 'accepted')
        self.assertEqual(offer.channel, 'link')
