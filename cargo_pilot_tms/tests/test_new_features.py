# -*- coding: utf-8 -*-
import json
import re
from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, tagged

from .common import CpCommon, PIXEL


@tagged('post_install', '-at_install', 'cargo_pilot')
class TestBookingAndIssues(CpCommon):

    def test_01_book_a_load_contract(self):
        booking = self.env['cp.booking'].create({
            'origin_id': self.origin.id, 'destination_id': self.dest.id, 'vehicle_type_id': self.vtype.id,
            'vehicle_count': 2, 'weight_ton': 28, 'reference': 'PO-55'})
        booking.action_compare()
        self.assertEqual(booking.step, 'options')
        contract_opt = booking.option_ids.filtered(lambda o: o.kind == 'contract')
        self.assertEqual(contract_opt.transporter_id, self.tr_a)
        self.assertTrue(contract_opt.recommended)
        self.assertTrue(booking.option_ids.filtered(lambda o: o.kind == 'bidding'))
        self.assertGreater(contract_opt.co2_kg, 0)
        contract_opt.action_choose()
        indent = self.env['cp.indent'].search([('origin', '=', 'PO-55')])
        self.assertEqual(indent.source_channel, 'booking')
        self.assertEqual(indent.state, 'allocated')
        self.assertEqual(len(indent.trip_ids), 2)
        self.assertEqual(indent.trip_ids[0].customer_ref, 'PO-55')

    def test_02_book_a_load_bidding(self):
        booking = self.env['cp.booking'].create({
            'origin_id': self.hub_a.id, 'destination_id': self.hub_b.id, 'vehicle_type_id': self.vtype.id,
            'vehicle_count': 1})
        booking.action_compare()
        bidding = booking.option_ids.filtered(lambda o: o.kind == 'bidding')
        self.assertTrue(bidding.recommended)  # no contract on a brand-new lane
        action = bidding.action_choose()
        self.assertEqual(action['res_model'], 'cp.auction')

    def test_03_issue_lifecycle_late_and_stopped(self):
        trip = self.env['cp.trip'].create({
            'lane_id': self.lane.id, 'transporter_id': self.tr_a.id, 'vehicle_number': 'CG04 IS 0001',
            'loaded_weight_ton': 10, 'promised_delivery': fields.Datetime.now() + timedelta(hours=2)})
        trip.action_assign()
        trip.action_dispatch()  # ETA ~20h at 40 km/h -> late
        trip.cp_add_ping(21.10, 81.50, speed=40)
        late = trip.issue_ids.filtered(lambda i: i.issue_type == 'late')
        self.assertTrue(late)
        self.assertEqual(late.state, 'open')
        # Raising again does not duplicate
        trip._cp_check_late()
        self.assertEqual(len(trip.issue_ids.filtered(lambda i: i.issue_type == 'late')), 1)

        trip.write({'last_moved_time': fields.Datetime.now() - timedelta(hours=6)})
        self.env['cp.trip']._cron_trip_monitor()
        stopped = trip.issue_ids.filtered(lambda i: i.issue_type == 'stopped')
        self.assertEqual(stopped.severity, 'high')
        # Moving again closes the stoppage automatically
        trip.cp_add_ping(20.50, 81.00, speed=45)
        self.assertEqual(stopped.state, 'resolved')
        self.assertTrue(stopped.auto_resolved)
        # Delivery closes the late issue
        trip.action_mark_delivered()
        self.assertEqual(late.state, 'resolved')
        self.assertEqual(trip.open_issue_count, 0)

    def test_04_epod_and_audit_issues(self):
        trip = self._delivered_trip(with_epod=False)
        bad = self.env['cp.epod'].create({'trip_id': trip.id, 'receiver_name': 'X', 'received_weight_ton': 12})
        bad.action_submit()
        issue = self.env['cp.exception'].search([('res_model', '=', 'cp.epod'), ('res_id', '=', bad.id)])
        self.assertEqual(issue.issue_type, 'epod')
        bad.action_verify()
        self.assertEqual(issue.state, 'resolved')

        bill = self.env['cp.freight.bill'].create({
            'transporter_id': self.tr_a.id, 'bill_number': 'ISS-1',
            'line_ids': [(0, 0, {'trip_id': trip.id, 'billed_amount': 56000})]})
        bill.action_submit()
        audit_issue = self.env['cp.exception'].search([('res_model', '=', 'cp.freight.bill'), ('res_id', '=', bill.id)])
        self.assertTrue(audit_issue)
        bill.action_approve()
        self.assertEqual(audit_issue.state, 'resolved')

    def test_05_co2(self):
        self.vtype.emission_factor = 100
        trip = self.env['cp.trip'].create({'lane_id': self.lane.id, 'vehicle_type_id': self.vtype.id,
                                           'loaded_weight_ton': 10})
        self.assertAlmostEqual(trip.co2_kg, 10 * 785 * 100 / 1000.0)
        data = self.env['cp.dashboard'].get_dashboard_data()
        for key in ('issues', 'positions', 'co2', 'setup', 'issue_counts'):
            self.assertIn(key, data)


@tagged('post_install', '-at_install', 'cargo_pilot')
class TestNewPortal(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.company.partner_id.tz = 'Asia/Kolkata'
        Loc = env['cp.location']
        cls.origin = Loc.create({'name': 'Portal Plant', 'latitude': 21.25, 'longitude': 81.63})
        cls.dest = Loc.create({'name': 'Portal Depot', 'latitude': 17.38, 'longitude': 78.48})
        cls.dock = env['cp.dock'].create({'name': 'Bay 7', 'location_id': cls.origin.id})
        cls.vtype = env['cp.vehicle.type'].create({'name': 'Portal Truck', 'capacity_ton': 9})
        cls.lane = env['cp.lane'].create({'origin_id': cls.origin.id, 'destination_id': cls.dest.id,
                                          'distance_km': 780})
        portal = env.ref('base.group_portal')
        cls.customer = env['res.partner'].create({'name': 'Portal Customer Co', 'is_company': True})
        env['res.users'].create({'name': 'Customer User', 'login': 'cp_cust', 'password': 'cp_cust',
                                 'parent_id': cls.customer.id, 'tz': 'Asia/Kolkata',
                                 'group_ids': [(6, 0, portal.ids)]})
        cls.transporter = env['res.partner'].create({'name': 'Slot Transport', 'is_company': True,
                                                     'cp_is_transporter': True})
        env['res.users'].create({'name': 'Slot User', 'login': 'cp_slot', 'password': 'cp_slot',
                                 'parent_id': cls.transporter.id, 'tz': 'Asia/Kolkata',
                                 'group_ids': [(6, 0, portal.ids)]})
        cls.trip = env['cp.trip'].create({'lane_id': cls.lane.id, 'transporter_id': cls.transporter.id,
                                          'vehicle_number': 'CG04 SL 0007'})
        cls.trip.action_assign()

    def _csrf(self, url):
        match = re.search(r'name="csrf_token" value="([^"]+)"', self.url_open(url).text)
        return match.group(1) if match else ''

    def test_customer_requests_truck(self):
        self.authenticate('cp_cust', 'cp_cust')
        self.assertIn('Your Shipments', self.url_open('/my/home').text)
        res = self.url_open('/my/cp/shipments/new')
        self.assertEqual(res.status_code, 200)
        pickup = (fields.Datetime.now() + timedelta(days=2)).strftime('%Y-%m-%dT10:00')
        res = self.url_open('/my/cp/shipments/new', data={
            'csrf_token': self._csrf('/my/cp/shipments/new'), 'origin_id': self.origin.id,
            'destination_id': self.dest.id, 'vehicle_type_id': self.vtype.id, 'vehicle_count': 2,
            'weight_ton': 18, 'pickup': pickup, 'reference': 'CUST-PO-1', 'material': 'Tiles'})
        self.assertEqual(res.status_code, 200)
        indent = self.env['cp.indent'].search([('origin', '=', 'CUST-PO-1')])
        self.assertEqual(indent.customer_id, self.customer)
        self.assertEqual(indent.source_channel, 'portal')
        self.assertEqual(indent.vehicle_count, 2)
        self.assertIn('CUST-PO-1', self.url_open('/my/cp/shipments').text)

    def test_transporter_books_dock(self):
        self.authenticate('cp_slot', 'cp_slot')
        page = self.url_open('/my/cp/trips/%s' % self.trip.id).text
        self.assertIn('Loading appointment', page)
        slot = re.search(r'name="slot"[^>]*value="([^"]+)"', page).group(1)
        res = self.url_open('/my/cp/trips/%s/appointment' % self.trip.id,
                            data={'csrf_token': self._csrf('/my/cp/trips/%s' % self.trip.id), 'slot': slot})
        self.assertEqual(res.status_code, 200)
        gate = self.env['cp.gate.entry'].search([('trip_id', '=', self.trip.id)])
        self.assertEqual(gate.dock_id, self.dock)
        self.assertEqual(gate.state, 'expected')
        self.assertIn('Bay 7', self.url_open('/my/cp/trips/%s' % self.trip.id).text)

    def test_driver_manifest_and_tracking_map(self):
        res = self.url_open('/cp/driver/%s/manifest.webmanifest' % self.trip.driver_token)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(json.loads(res.text)['display'], 'standalone')
        page = self.url_open('/cp/driver/%s' % self.trip.driver_token).text
        self.assertIn('manifest.webmanifest', page)
        track = self.url_open('/cp/track/%s' % self.trip.tracking_token).text
        self.assertIn('cp-leaflet', track)
