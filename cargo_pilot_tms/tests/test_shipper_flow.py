# -*- coding: utf-8 -*-
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import CpCommon, PIXEL


@tagged('post_install', '-at_install', 'cargo_pilot')
class TestShipperFlow(CpCommon):

    def test_01_auto_allocate_from_contract(self):
        indent = self._indent(count=2)
        indent.action_auto_allocate()
        self.assertEqual(indent.state, 'allocated')
        self.assertEqual(len(indent.trip_ids), 2)
        self.assertTrue(all(t.transporter_id == self.tr_a for t in indent.trip_ids))
        self.assertEqual(indent.trip_ids[0].agreed_freight, 50000)
        self.assertEqual(indent.pending_count, 0)

    def test_02_auction_rules_and_award(self):
        indent = self._indent(count=3)
        indent.action_create_auction()
        auction = indent.auction_ids
        auction.write({'transporter_ids': [(6, 0, (self.tr_a | self.tr_b | self.tr_c).ids)],
                       'ceiling_price': 60000, 'min_decrement_pct': 1.0})
        auction.action_publish()
        self.assertEqual(auction.state, 'live')

        with self.assertRaises(UserError):  # above ceiling
            auction.cp_place_bid(self.tr_b, 61000, 2)
        auction.cp_place_bid(self.tr_b, 55000, 2, channel='portal')
        with self.assertRaises(UserError):  # not a 1% decrement
            auction.cp_place_bid(self.tr_b, 54800, 2)
        auction.cp_place_bid(self.tr_b, 54000, 2)
        auction.cp_place_bid(self.tr_c, 56000, 3)
        outsider = self.env['res.partner'].create({'name': 'Outsider', 'cp_is_transporter': True})
        with self.assertRaises(UserError):
            auction.cp_place_bid(outsider, 50000, 1)

        self.assertEqual(auction.best_rate, 54000)
        self.assertEqual(auction.participant_count, 2)
        self.assertEqual(auction.cp_rank_for(self.tr_b), 1)
        self.assertEqual(len(auction.bid_ids.filtered('is_latest')), 2)

        # Anti-sniping: a bid two minutes before the end extends it
        auction.date_end = fields.Datetime.now() + timedelta(minutes=2)
        before = auction.date_end
        auction.cp_place_bid(self.tr_c, 55000, 3)
        self.assertEqual(auction.date_end, before + timedelta(minutes=5))
        self.assertEqual(auction.extension_count, 1)

        # Award: cheapest first, part-bid fills the rest
        auction.action_close()
        plan = auction._cp_auto_allocation()
        self.assertEqual([(b.transporter_id, q) for b, q in plan], [(self.tr_b, 2), (self.tr_c, 1)])
        wizard_action = auction.action_open_award_wizard()
        wizard = self.env['cp.auction.award.wizard'].browse(wizard_action['res_id'])
        self.assertEqual(wizard.allocated, 3)
        wizard.action_award()
        self.assertEqual(auction.state, 'awarded')
        self.assertEqual(len(auction.trip_ids), 3)
        self.assertEqual(sorted(auction.trip_ids.mapped('agreed_freight')), [54000, 54000, 55000])
        self.assertEqual(auction.winner_ids, self.tr_b | self.tr_c)
        # Savings vs ceiling 60000 x 3
        self.assertAlmostEqual(auction.savings_amount, 180000 - 163000)
        self.assertEqual(indent.state, 'allocated')

    def test_03_pre_bid_and_sealed(self):
        indent = self._indent(count=1)
        auction = self.env['cp.auction'].create({
            'indent_id': indent.id, 'vehicle_count': 1, 'allow_part_bid': False,
            'date_start': fields.Datetime.now() + timedelta(hours=1),
            'date_end': fields.Datetime.now() + timedelta(hours=2),
            'transporter_ids': [(6, 0, self.tr_b.ids)],
        })
        auction.action_publish()
        self.assertEqual(auction.state, 'scheduled')
        auction.cp_place_bid(self.tr_b, 58000, 1)
        self.assertTrue(auction.bid_ids.is_pre_bid)
        with self.assertRaises(UserError):
            auction.cp_place_bid(self.tr_b, 57000, 1)
        self.assertEqual(auction.suggested_ceiling, 58000)

    def test_04_trip_tracking_geofence_and_epod(self):
        trip = self.env['cp.trip'].create({
            'lane_id': self.lane.id, 'transporter_id': self.tr_a.id, 'vehicle_number': 'CG04 ZZ 0001',
            'loaded_weight_ton': 14.0, 'promised_delivery': fields.Datetime.now() + timedelta(hours=40),
        })
        trip.action_assign()
        # Inside origin geofence -> reported; leaving -> auto dispatch
        trip.cp_add_ping(21.2516, 81.6300, speed=0)
        self.assertEqual(trip.state, 'reported')
        trip.cp_add_ping(21.10, 81.50, speed=45)
        self.assertEqual(trip.state, 'in_transit')
        self.assertTrue(trip.eta)
        trip.cp_add_ping(19.50, 79.80, speed=50)
        self.assertGreater(trip.gps_distance_km, 100)
        self.assertGreater(trip.progress_pct, 0)
        # Destination geofence -> arrived
        trip.cp_add_ping(17.3851, 78.4868, speed=5)
        self.assertEqual(trip.state, 'arrived')
        self.assertFalse(trip.is_late)

        # ePOD captured far away -> carrier exception
        far = self.env['cp.epod'].create({'trip_id': trip.id, 'receiver_name': 'X', 'received_weight_ton': 14.0,
                                          'latitude': 18.0, 'longitude': 79.0, 'signature': PIXEL})
        far.action_submit()
        self.assertEqual(far.state, 'exception')
        self.assertEqual(far.fault_party, 'carrier')
        self.assertFalse(far.check_location)
        self.assertFalse(far.check_photo)
        self.assertEqual(trip.epod_state, 'exception')

        good = self.env['cp.epod'].create({
            'trip_id': trip.id, 'receiver_name': 'Stores', 'received_weight_ton': 14.0,
            'latitude': 17.3852, 'longitude': 78.4866, 'signature': PIXEL,
            'photo_ids': [(0, 0, {'name': 'pod.png', 'datas': PIXEL})]})
        good.action_submit()
        self.assertEqual(good.state, 'verified')
        self.assertEqual(trip.state, 'delivered')
        self.assertEqual(trip.epod_state, 'verified')
        trip.action_close()
        self.assertEqual(trip.state, 'closed')
        self.assertTrue(trip.event_ids.filtered(lambda e: e.event_type == 'geofence_in'))

    def test_05_close_requires_epod(self):
        trip = self._delivered_trip(with_epod=False)
        with self.assertRaises(UserError):
            trip.action_close()

    def test_06_stoppage_monitor(self):
        trip = self._delivered_trip(with_epod=False)
        trip.write({'state': 'in_transit', 'last_moved_time': fields.Datetime.now() - timedelta(hours=5)})
        self.env['cp.trip']._cron_trip_monitor()
        self.assertTrue(trip.is_stopped)
        self.assertTrue(trip.event_ids.filtered(lambda e: e.event_type == 'stoppage'))

    def test_07_freight_audit(self):
        good = self._delivered_trip(gps_km=790)
        padded = self._delivered_trip(gps_km=700)
        no_pod = self._delivered_trip(with_epod=False, gps_km=780)
        bill = self.env['cp.freight.bill'].create({'transporter_id': self.tr_a.id, 'bill_number': 'INV-1'})
        bill.action_load_trips()
        self.assertEqual(len(bill.line_ids), 3)
        line_by_trip = {l.trip_id: l for l in bill.line_ids}
        line_by_trip[good].write({'billed_amount': 50000, 'billed_km': 790})
        line_by_trip[padded].write({'billed_amount': 53000, 'billed_km': 860})  # over rate and over km
        line_by_trip[no_pod].write({'billed_amount': 50000, 'billed_km': 780})
        bill.action_submit()
        self.assertEqual(bill.state, 'audited')

        ok, pad, held = line_by_trip[good], line_by_trip[padded], line_by_trip[no_pod]
        self.assertEqual(ok.approved_amount, 50000)
        self.assertEqual(ok.risk, 'low')
        self.assertFalse(pad.check_rate)
        self.assertFalse(pad.check_distance)
        self.assertEqual(pad.approved_amount, 50000)
        self.assertEqual(pad.deduction_amount, 3000)
        self.assertTrue(held.on_hold)
        self.assertEqual(held.approved_amount, 0)
        self.assertEqual(bill.held_count, 1)
        self.assertEqual(bill.approved_total, 100000)

        bill.action_approve()
        self.assertEqual(bill.state, 'approved')
        self.assertEqual(bill.move_id.move_type, 'in_invoice')
        self.assertAlmostEqual(bill.move_id.amount_untaxed, 100000)

        # The same trip on a second bill is flagged as duplicate
        bill2 = self.env['cp.freight.bill'].create({
            'transporter_id': self.tr_a.id, 'bill_number': 'INV-2',
            'line_ids': [(0, 0, {'trip_id': good.id, 'billed_amount': 50000})]})
        bill2.action_submit()
        self.assertFalse(bill2.line_ids.check_duplicate)
        self.assertEqual(bill2.risk_level, 'high')
        self.assertEqual(bill2.approved_total, 0)

    def test_08_rfq_to_contract(self):
        rfq = self.env['cp.rfq'].create({
            'title': 'FY lanes', 'date_deadline': fields.Datetime.now() + timedelta(days=2),
            'contract_start': fields.Date.today(), 'contract_end': fields.Date.today() + timedelta(days=365),
            'line_ids': [(0, 0, {'lane_id': self.lane.id, 'vehicle_type_id': self.vtype.id})],
            'transporter_ids': [(6, 0, (self.tr_b | self.tr_c).ids)],
        })
        rfq.action_publish()
        Quote = self.env['cp.rfq.quote']
        Quote.create({'rfq_id': rfq.id, 'line_id': rfq.line_ids.id, 'transporter_id': self.tr_b.id, 'rate': 48000})
        Quote.create({'rfq_id': rfq.id, 'line_id': rfq.line_ids.id, 'transporter_id': self.tr_c.id, 'rate': 51000})
        self.assertEqual(rfq.line_ids.lowest_rate, 48000)
        self.assertEqual(rfq.line_ids.benchmark_rate, 50000)
        rfq.action_close_quotes()
        rfq.action_select_best()
        self.assertEqual(rfq.quote_ids.filtered('selected').transporter_id, self.tr_b)
        rfq.action_award()
        self.assertEqual(rfq.state, 'awarded')
        contract = rfq.contract_ids
        self.assertEqual(contract.transporter_id, self.tr_b)
        self.assertEqual(contract.state, 'active')
        self.assertEqual(contract.line_ids.rate, 48000)

    def test_09_gate_and_weighbridge(self):
        trip = self.env['cp.trip'].create({'lane_id': self.lane.id, 'transporter_id': self.tr_a.id,
                                           'vehicle_number': 'CG04 GG 2222'})
        trip.action_assign()
        dock = self.env['cp.dock'].create({'name': 'D1', 'location_id': self.origin.id})
        gate = self.env['cp.gate.entry'].create({'location_id': self.origin.id, 'trip_id': trip.id, 'dock_id': dock.id})
        self.assertEqual(gate.vehicle_number, 'CG04 GG 2222')
        gate.action_gate_in()
        self.assertEqual(trip.state, 'reported')
        gate.action_to_dock()
        self.assertEqual(trip.state, 'loading')
        gate.write({'tare_weight_kg': 12000, 'gross_weight_kg': 26500})
        gate.action_weigh()
        self.assertAlmostEqual(trip.loaded_weight_ton, 14.5)
        gate.action_gate_out()
        self.assertEqual(trip.state, 'in_transit')
        # Overlapping dock slot is refused
        start = fields.Datetime.now() + timedelta(hours=3)
        self.env['cp.gate.entry'].create({'location_id': self.origin.id, 'vehicle_number': 'A1', 'dock_id': dock.id,
                                          'slot_start': start, 'slot_end': start + timedelta(hours=1)})
        with self.assertRaises(Exception):
            self.env['cp.gate.entry'].create({'location_id': self.origin.id, 'vehicle_number': 'A2', 'dock_id': dock.id,
                                              'slot_start': start + timedelta(minutes=30),
                                              'slot_end': start + timedelta(hours=2)})

    def test_10_load_plan(self):
        plan = self.env['cp.load.plan'].create({
            'vehicle_type_id': self.vtype.id,
            'item_ids': [(0, 0, {'name': 'Pallet', 'quantity': 10, 'length_m': 1.2, 'width_m': 1.0,
                                 'height_m': 1.2, 'weight_kg': 800}),
                         (0, 0, {'name': 'Heavy', 'quantity': 20, 'length_m': 1.0, 'width_m': 1.0,
                                 'height_m': 1.0, 'weight_kg': 1000})],
        })
        plan.action_compute()
        self.assertEqual(plan.state, 'planned')
        self.assertLessEqual(plan.weight_kg, 15000)
        self.assertGreater(plan.unplaced_count, 0)  # 28 t of cargo cannot fit a 15 t truck
        self.assertIn('<svg', str(plan.preview_svg))
        for box in plan.box_ids:
            self.assertLessEqual(box.x + box.length_m, 9.75 + 1e-6)
            self.assertLessEqual(box.y + box.width_m, 2.4 + 1e-6)
            self.assertLessEqual(box.z + box.height_m, 2.6 + 1e-6)

    def test_11_dashboard(self):
        self._delivered_trip()
        data = self.env['cp.dashboard'].get_dashboard_data()
        self.assertIn('kpis', data)
        self.assertEqual(len(data['daily']), 14)
        self.assertGreaterEqual(data['kpis']['delivered_month'], 1)
