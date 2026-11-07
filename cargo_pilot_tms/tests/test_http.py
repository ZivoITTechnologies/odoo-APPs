# -*- coding: utf-8 -*-
import json

from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install', 'cargo_pilot')
class TestCargoPilotHttp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.company = env.company
        cls.company.cp_api_key = 'test-key-0123456789abcdef'
        Loc = env['cp.location']
        cls.origin = Loc.create({'name': 'HTTP Plant', 'code': 'HP1', 'latitude': 21.25, 'longitude': 81.63})
        cls.dest = Loc.create({'name': 'HTTP Depot', 'code': 'HD1', 'latitude': 17.38, 'longitude': 78.48})
        cls.vtype = env['cp.vehicle.type'].create({'name': 'HTTP Truck', 'capacity_ton': 9})
        cls.lane = env['cp.lane'].create({'origin_id': cls.origin.id, 'destination_id': cls.dest.id,
                                          'distance_km': 780})
        cls.transporter = env['res.partner'].create({'name': 'Portal Transport', 'is_company': True,
                                                     'cp_is_transporter': True, 'email': 'pt@example.com'})
        cls.portal_user = env['res.users'].create({
            'name': 'Portal Transport User', 'login': 'cp_portal_user', 'password': 'cp_portal_user',
            'email': 'ptu@example.com', 'parent_id': cls.transporter.id,
            'group_ids': [(6, 0, [env.ref('base.group_portal').id])],
        })
        cls.trip = env['cp.trip'].create({'lane_id': cls.lane.id, 'transporter_id': cls.transporter.id,
                                          'vehicle_number': 'CG04 HT 0001', 'loaded_weight_ton': 9})
        cls.trip.action_assign()
        cls.trip.action_dispatch()
        indent = env['cp.indent'].create({'lane_id': cls.lane.id, 'vehicle_type_id': cls.vtype.id,
                                          'vehicle_count': 2})
        indent.action_confirm()
        cls.auction = env['cp.auction'].create({'indent_id': indent.id, 'vehicle_count': 2,
                                                'transporter_ids': [(6, 0, cls.transporter.ids)]})
        cls.auction.action_publish()

    def test_public_tracking_and_driver_pages(self):
        res = self.url_open('/cp/track/%s' % self.trip.tracking_token)
        self.assertEqual(res.status_code, 200)
        self.assertIn('CG04 HT 0001', res.text)
        self.assertEqual(self.url_open('/cp/track/not-a-real-token-123456').status_code, 404)
        res = self.url_open('/cp/driver/%s' % self.trip.driver_token)
        self.assertEqual(res.status_code, 200)
        self.assertIn('Delivery proof', res.text)

    def test_driver_ping_json(self):
        res = self.url_open('/cp/driver/%s/ping' % self.trip.driver_token, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'latitude': 19.5, 'longitude': 79.8, 'speed': 12.5}}),
            headers={'Content-Type': 'application/json'})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['result']['ok'])
        self.assertEqual(self.trip.ping_ids[:1].source, 'driver')

    def test_api(self):
        headers = {'Content-Type': 'application/json', 'X-CP-API-Key': 'test-key-0123456789abcdef'}
        bad = self.url_open('/cp/api/v1/ping', data='{}', headers={'X-CP-API-Key': 'wrong-key-000000000'})
        self.assertEqual(bad.status_code, 401)
        res = self.url_open('/cp/api/v1/ping', data=json.dumps({
            'vehicle_number': 'cg04 ht 0001', 'latitude': 17.3801, 'longitude': 78.4801, 'speed': 20}), headers=headers)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['matched'])
        self.assertEqual(self.trip.state, 'arrived')

        res = self.url_open('/cp/api/v1/indent', data=json.dumps({
            'origin_code': 'HP1', 'destination_code': 'HD1', 'vehicle_type': 'HTTP Truck',
            'vehicle_count': 2, 'reference': 'SO-API-1', 'confirm': True,
            'lines': [{'description': 'Goods', 'quantity': 10, 'weight_ton': 12}]}), headers=headers)
        self.assertEqual(res.status_code, 201, res.text)
        indent = self.env['cp.indent'].search([('origin', '=', 'SO-API-1')])
        self.assertEqual(indent.state, 'confirmed')
        self.assertEqual(indent.source_channel, 'api')

        res = self.url_open('/cp/api/v1/trip/%s' % self.trip.name, headers=headers)
        self.assertEqual(res.json()['state'], 'arrived')

    def test_portal_pages_and_bid(self):
        self.authenticate('cp_portal_user', 'cp_portal_user')
        res = self.url_open('/my/home')
        self.assertEqual(res.status_code, 200)
        self.assertIn('Load Auctions', res.text)
        res = self.url_open('/my/cp/auctions')
        self.assertIn(self.auction.name, res.text)
        res = self.url_open('/my/cp/auctions/%s' % self.auction.id)
        self.assertEqual(res.status_code, 200)
        res = self.url_open('/my/cp/auctions/%s/bid' % self.auction.id,
                            data={'rate': '45000', 'vehicles': '2', 'csrf_token': self._csrf()})
        self.assertEqual(res.status_code, 200)
        bid = self.auction.bid_ids
        self.assertEqual(bid.rate, 45000)
        self.assertEqual(bid.channel, 'portal')
        for url in ('/my/cp/trips', '/my/cp/trips/%s' % self.trip.id, '/my/cp/rfqs', '/my/cp/bills'):
            self.assertEqual(self.url_open(url).status_code, 200, url)

    def test_portal_isolation(self):
        other = self.env['res.partner'].create({'name': 'Other', 'cp_is_transporter': True, 'is_company': True})
        other_trip = self.env['cp.trip'].create({'lane_id': self.lane.id, 'transporter_id': other.id})
        self.authenticate('cp_portal_user', 'cp_portal_user')
        self.assertEqual(self.url_open('/my/cp/trips/%s' % other_trip.id).status_code, 404)

    def _csrf(self):
        res = self.url_open('/my/cp/auctions/%s' % self.auction.id)
        import re
        match = re.search(r'name="csrf_token" value="([^"]+)"', res.text)
        return match.group(1) if match else ''
