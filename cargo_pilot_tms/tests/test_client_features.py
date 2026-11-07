# -*- coding: utf-8 -*-
"""Shipper features for FTL + LTL movement: rate cards, offers and cascade, freight per order,
serials and GRN on POD, appointments, containers, scorecard, capacity, webhooks, insights."""
import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .common import PIXEL, CpCommon


@tagged('post_install', '-at_install', 'cargo_pilot')
class TestClientFeatures(CpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        india = env.ref('base.in')
        cls.st_cg = env['res.country.state'].search([('country_id', '=', india.id), ('code', '=', 'CT')], limit=1) \
            or env['res.country.state'].create({'name': 'Chhattisgarh', 'code': 'CT', 'country_id': india.id})
        cls.st_tg = env['res.country.state'].search([('country_id', '=', india.id), ('code', '=', 'TS')], limit=1) \
            or env['res.country.state'].create({'name': 'Telangana', 'code': 'TS', 'country_id': india.id})
        cls.origin.state_id = cls.st_cg
        cls.dest.state_id = cls.st_tg
        cls.ltl_contract = env['cp.rate.contract'].create({
            'transporter_id': cls.tr_b.id, 'date_start': fields.Date.today() - timedelta(days=5),
            'date_end': fields.Date.today() + timedelta(days=300),
            'line_ids': [(0, 0, {'dest_state_id': cls.st_tg.id, 'charge_basis': 'kg', 'rate': 8.0,
                                 'min_weight_kg': 100, 'min_charge': 1500, 'docket_charge': 100,
                                 'transit_days': 4})],
        })
        cls.ltl_contract.action_activate()
        cls.contract_b = env['cp.rate.contract'].create({
            'transporter_id': cls.tr_c.id, 'priority': 20,
            'date_start': fields.Date.today() - timedelta(days=5), 'date_end': fields.Date.today() + timedelta(days=300),
            'line_ids': [(0, 0, {'lane_id': cls.lane.id, 'vehicle_type_id': cls.vtype.id, 'charge_basis': 'trip',
                                 'rate': 52000, 'weekly_capacity': 3})],
        })
        cls.contract_b.action_activate()

    # ------------------------------------------------------------------
    def test_lane_is_interstate(self):
        self.assertEqual(self.lane.movement, 'interstate')

    def test_source_document_follows_order_numbers(self):
        indent = self.env['cp.indent'].create({
            'lane_id': self.lane.id, 'vehicle_type_id': self.vtype.id,
            'line_ids': [(0, 0, {'order_ref': 'SO-11', 'weight_ton': 5}), (0, 0, {'order_ref': 'SO-12', 'weight_ton': 5}),
                         (0, 0, {'order_ref': 'SO-11', 'weight_ton': 1})]})
        self.assertEqual(indent.origin, 'SO-11, SO-12')
        indent.line_ids[1].order_ref = 'SO-15'
        self.assertEqual(indent.origin, 'SO-11, SO-15')
        typed = self.env['cp.indent'].create({'lane_id': self.lane.id, 'vehicle_type_id': self.vtype.id,
                                              'origin': 'PO-778',
                                              'line_ids': [(0, 0, {'description': 'Bags', 'weight_ton': 2})]})
        self.assertEqual(typed.origin, 'PO-778', 'a typed source document stays when lines carry no order numbers')

    def test_ltl_rate_card_breakdown(self):
        line = self.ltl_contract.line_ids
        # 60 kg is below the 100 kg minimum: 100 x 8 = 800 + docket 100 = 900 -> topped up to 1500
        small = line.cp_price_breakdown(weight_ton=0.06)
        self.assertEqual(small['total'], 1500.0)
        self.assertTrue(any('minimum' in lbl for lbl, _a in small['lines']))
        line.write({'fov_pct': 0.1, 'fuel_surcharge_pct': 10, 'oda_charge': 400, 'multi_drop_charge': 250})
        big = line.cp_price_breakdown(weight_ton=0.5, goods_value=200000, drops=3, oda=True)
        # base 500 x 8 = 4000, fuel 400, docket 100, ODA 400, 2 drops 500, FOV 200 = 5600
        self.assertAlmostEqual(big['total'], 5600.0)

    def test_diesel_escalation_and_peak_season(self):
        contract = self.contract
        contract.write({'diesel_clause': True, 'base_diesel_price': 90.0, 'fuel_component_pct': 40.0,
                        'diesel_threshold_pct': 2.0,
                        'season_ids': [(0, 0, {'name': 'Festive', 'date_from': fields.Date.today() - timedelta(days=1),
                                               'date_to': fields.Date.today() + timedelta(days=30),
                                               'surcharge_pct': 5.0})]})
        self.env['cp.diesel.price'].create({'date': fields.Date.today() - timedelta(days=1), 'price': 99.0})
        line = contract.line_ids
        # diesel +10% x 40% share = +4% (2000), festive +5% (2500) on 50000
        self.assertAlmostEqual(line.cp_compute_amount(), 54500.0)
        self.env['cp.diesel.price'].create({'date': fields.Date.today(), 'price': 91.0})  # +1.1% < threshold
        self.assertAlmostEqual(line.cp_compute_amount(), 52500.0)

    def test_ftl_needs_truck_type(self):
        with self.assertRaises(ValidationError):
            self.env['cp.indent'].create({'lane_id': self.lane.id, 'shipment_mode': 'ftl', 'vehicle_count': 1})

    def test_book_ltl_and_assign_docket(self):
        booking = self.env['cp.booking'].create({
            'shipment_mode': 'ltl', 'origin_id': self.origin.id, 'destination_id': self.dest.id,
            'channel': 'modern_retail', 'consignee_id': self.customer.id,
            'order_lines': 'SO-1, INV-1, 10, 240, 52000\nSO-2, INV-2, 5, 120, 26000'})
        booking.action_compare()
        option = booking.option_ids.filtered(lambda o: o.kind == 'ltl')
        self.assertEqual(len(option), 1)
        self.assertEqual(option.transporter_id, self.tr_b)
        self.assertEqual(option.price, 360 * 8 + 100)  # 360 kg, docket charge
        self.assertIn('Docket', option.breakdown)
        option.action_choose()
        trip = self.env['cp.trip'].search([('indent_id.origin', '=', False), ('transporter_id', '=', self.tr_b.id),
                                           ('shipment_mode', '=', 'ltl')], order='id desc', limit=1)
        self.assertEqual(trip.order_count, 2)
        self.assertEqual(trip.box_count, 15)
        self.assertEqual(trip.tracking_method, 'carrier')
        self.assertEqual(trip.acceptance_state, 'pending')
        trip.docket_number = 'SFX-44021'
        trip.action_assign()
        self.assertEqual(trip.state, 'assigned')
        self.assertEqual(trip.acceptance_state, 'accepted')

    def test_offer_decline_cascade_then_bidding(self):
        self.company.write({'cp_auction_on_decline': True, 'cp_acceptance_hours': 1})
        indent = self._indent(count=1, weight=14)
        indent.action_auto_allocate()
        trip = indent.trip_ids
        self.assertEqual(trip.transporter_id, self.tr_a)
        offer = trip.offer_ids
        self.assertEqual(offer.state, 'pending')
        self.assertTrue(offer.offer_url.endswith(offer.access_token))
        offer.action_decline('no_vehicle', 'All trucks out')
        self.assertEqual(trip.transporter_id, self.tr_c, 'passed to the next contracted transporter')
        self.assertEqual(trip.agreed_freight, 52000)
        second = trip.offer_ids.filtered(lambda o: o.state == 'pending')
        self.assertEqual(second.transporter_id, self.tr_c)
        second.deadline = fields.Datetime.now() - timedelta(minutes=1)
        self.env['cp.trip.offer']._cron_offer_expiry()
        self.assertEqual(second.state, 'expired')
        issue = self.env['cp.exception'].search([('trip_id', '=', trip.id), ('issue_type', '=', 'offer')])
        self.assertTrue(issue)
        self.assertEqual(trip.state, 'cancel')
        self.assertTrue(indent.auction_ids, 'load opened for bidding')

    def test_freight_cost_per_order(self):
        trip = self.env['cp.trip'].create({
            'lane_id': self.lane.id, 'vehicle_type_id': self.vtype.id, 'transporter_id': self.tr_a.id,
            'agreed_freight': 30000, 'allocation_basis': 'weight',
            'order_ids': [(0, 0, {'order_ref': 'A', 'weight_kg': 6000, 'order_value': 300000, 'quantity': 60}),
                          (0, 0, {'order_ref': 'B', 'weight_kg': 3000, 'order_value': 150000, 'quantity': 30}),
                          (0, 0, {'order_ref': 'C', 'weight_kg': 1000, 'order_value': 50000, 'quantity': 10})]})
        a, b, c = trip.order_ids.sorted('order_ref')
        self.assertEqual((a.freight_cost, b.freight_cost, c.freight_cost), (18000, 9000, 3000))
        self.assertEqual(a.freight_per_unit, 300)
        self.assertAlmostEqual(trip.cost_per_kg, 3.0)
        self.assertAlmostEqual(trip.freight_pct, 6.0)
        trip.allocation_basis = 'equal'
        self.assertEqual(c.freight_cost, 10000)

    def test_freight_cost_follows_approved_bill(self):
        trip = self._delivered_trip()
        trip.order_ids = [(0, 0, {'order_ref': 'X', 'weight_kg': 14000, 'order_value': 700000})]
        bill = self.env['cp.freight.bill'].create({'transporter_id': self.tr_a.id, 'bill_number': 'B-77'})
        bill.action_load_trips()
        bill.line_ids.billed_amount = 53000
        bill.action_submit()
        bill.action_approve()
        self.assertEqual(trip.billed_amount, 50000)
        self.assertEqual(trip.freight_cost_total, 50000)
        self.assertEqual(trip.order_ids.freight_cost, 50000)

    def test_serials_and_grn_on_pod(self):
        rule = self.env['cp.channel.rule'].create({
            'name': 'Retail DCs', 'channel': 'modern_retail', 'promise_hours': 72, 'appointment_required': True,
            'grn_required': True, 'serial_scan_required': True})
        indent = self.env['cp.indent'].create({
            'lane_id': self.lane.id, 'vehicle_type_id': self.vtype.id, 'vehicle_count': 1, 'channel': 'modern_retail',
            'line_ids': [(0, 0, {'order_ref': 'PO-9', 'description': 'Chimney 60cm', 'quantity': 3, 'weight_ton': 0.06,
                                 'serial_numbers': 'CH-001\nCH-002, CH-003'})]})
        indent.action_confirm()
        self.assertEqual(indent.channel_rule_id, rule)
        self.assertEqual(indent.delivery_deadline, indent.required_date + timedelta(hours=72))
        indent.action_auto_allocate()
        trip = indent.trip_ids
        self.assertTrue(trip.grn_required and trip.serial_scan_required and trip.appointment_required)
        self.assertEqual(trip.serial_count, 3)
        self.assertEqual(trip.appointment_status, 'missing')
        trip.vehicle_number = 'CG04 ZZ 9999'
        trip.action_assign()
        trip.action_dispatch()
        base = {'trip_id': trip.id, 'receiver_name': 'DC', 'received_weight_ton': trip.loaded_weight_ton,
                'latitude': self.dest.latitude, 'longitude': self.dest.longitude, 'signature': PIXEL,
                'photo_ids': [(0, 0, {'name': 'p.png', 'datas': PIXEL})]}
        bad = self.env['cp.epod'].create(dict(base, scanned_serials='ch-001\nCH-002\nCH-999'))
        bad.action_submit()
        self.assertEqual(bad.state, 'exception')
        self.assertFalse(bad.check_serials)
        self.assertEqual(bad.missing_serials, 'CH-003')
        self.assertEqual(bad.extra_serials, 'CH-999')
        self.assertFalse(bad.check_grn)
        good = self.env['cp.epod'].create(dict(base, scanned_serials='CH-003 CH-002 CH-001', grn_number='GRN-55'))
        good.action_submit()
        self.assertEqual(good.state, 'verified')
        self.assertEqual(trip.state, 'delivered')
        self.assertEqual(trip.pod_age_bucket, 'done')
        self.assertTrue(trip.epod_tat_hours > 0)

    def test_appointment_status(self):
        trip = self.env['cp.trip'].create({'lane_id': self.lane.id, 'transporter_id': self.tr_a.id,
                                           'vehicle_number': 'CG04 AP 0001', 'appointment_required': True})
        now = fields.Datetime.now()
        trip.write({'appointment_ref': 'APT-1', 'appointment_start': now + timedelta(hours=10),
                    'appointment_end': now + timedelta(hours=12)})
        self.assertEqual(trip.appointment_status, 'booked')
        trip.action_assign()
        trip.action_dispatch()
        trip.eta = now + timedelta(hours=40)
        self.assertEqual(trip.appointment_status, 'at_risk')
        trip._cp_check_late()
        self.assertTrue(self.env['cp.exception'].search([('trip_id', '=', trip.id), ('issue_type', '=', 'appointment')]))
        trip.actual_arrival = now + timedelta(hours=11)
        self.assertEqual(trip.appointment_status, 'met')

    def test_container_free_time(self):
        trip = self.env['cp.trip'].create({'lane_id': self.lane.id, 'channel': 'import', 'container_number': 'MSKU1234567',
                                           'free_days': 1, 'container_picked_at': fields.Datetime.now() - timedelta(hours=30)})
        self.assertTrue(trip.empty_return_due)
        self.assertGreaterEqual(trip.container_detention_days, 1)
        self.env['cp.trip']._cron_trip_followups()
        issue = self.env['cp.exception'].search([('trip_id', '=', trip.id), ('issue_type', '=', 'container')])
        self.assertTrue(issue)
        trip.action_mark_empty_returned()
        self.assertEqual(issue.state, 'resolved')

    def test_scorecard(self):
        trip = self._delivered_trip()  # tr_a: assigned (offer accepted), delivered with clean ePOD
        self.assertEqual(trip.offer_ids.state, 'accepted')
        late = self.env['cp.trip'].create({'lane_id': self.lane.id, 'vehicle_type_id': self.vtype.id,
                                           'transporter_id': self.tr_a.id, 'vehicle_number': 'X1',
                                           'planned_dispatch': fields.Datetime.now() - timedelta(hours=10)})
        late.offer_ids.action_decline('rate')
        self.tr_a.invalidate_recordset()
        self.assertEqual(self.tr_a.cp_offer_count, 2)
        self.assertEqual(self.tr_a.cp_acceptance_rate, 50.0)
        self.assertEqual(self.tr_a.cp_damage_free_rate, 100.0)
        self.assertTrue(self.tr_a.cp_score > 0)
        self.assertIn(self.tr_a.cp_score_grade, ('a', 'b', 'c', 'd'))
        self.assertEqual(self.tr_b.cp_score_grade, 'new')

    def test_capacity_plan(self):
        for days_ago in (3, 10, 17):
            self.env['cp.trip'].create({'lane_id': self.lane.id, 'vehicle_type_id': self.vtype.id,
                                        'planned_dispatch': fields.Datetime.now() - timedelta(days=days_ago)})
        plan = self.env['cp.capacity.plan'].create({'name': 'Festive', 'weeks': 4, 'growth_pct': 100})
        plan.action_suggest()
        lines = plan.line_ids.filtered(lambda l: l.lane_id == self.lane)
        self.assertEqual(len(lines), 4)
        self.assertTrue(all(l.forecast_trucks == 1 for l in lines))  # 3 trips / 8 weeks x 2, rounded up
        self.assertTrue(all(l.committed_trucks == 3 for l in lines))
        # this lane is covered; other lanes (demo shipments in the same weeks) may show their own shortfall
        self.assertTrue(all(l.gap <= 0 for l in lines))
        self.assertEqual(plan.total_gap, sum(g for g in (plan.line_ids - lines).mapped('gap') if g > 0))

    def test_webhooks(self):
        hook = self.env['cp.webhook'].create({'name': 'Test', 'url': 'https://example.com/hook'})
        trip = self.env['cp.trip'].create({'lane_id': self.lane.id, 'transporter_id': self.tr_a.id,
                                           'vehicle_number': 'CG04 WH 0001'})
        trip.action_assign()
        deliveries = hook.delivery_ids
        self.assertEqual(set(deliveries.mapped('event')), {'shipment.created', 'shipment.status_changed'})
        sent = []

        class Resp:
            status_code = 200
            text = 'ok'

        def fake_post(url, data=None, headers=None, timeout=None):
            sent.append((url, data, headers))
            return Resp()
        with patch('odoo.addons.cargo_pilot_tms.models.cp_webhook.requests.post', new=fake_post):
            self.env['cp.webhook.delivery']._cron_send()
        self.assertEqual(len(sent), 2)
        url, body, headers = sent[0]
        expected = hmac.new(hook.secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertEqual(headers['X-CP-Signature'], 'sha256=%s' % expected)
        self.assertEqual(json.loads(body)['data']['name'], trip.name)
        self.assertTrue(all(d.state == 'sent' for d in deliveries))

        class Fail:
            status_code = 500
            text = 'down'
        trip.action_report()
        with patch('odoo.addons.cargo_pilot_tms.models.cp_webhook.requests.post', new=lambda *a, **k: Fail()):
            self.env['cp.webhook.delivery']._cron_send()
        failed = hook.delivery_ids.filtered(lambda d: d.state == 'pending' and d.attempts == 1)
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed.next_attempt > fields.Datetime.now())

    def test_insights(self):
        self._delivered_trip()
        data = self.env['cp.insights'].get_insights('month')
        self.assertGreaterEqual(data['kpis']['shipments'], 1)
        self.assertEqual(len(data['months']), 12)
        self.assertEqual([m['key'] for m in data['modes']], ['ftl', 'ltl', 'courier'])
        self.assertEqual(len(data['pod']), 4)
        for period in ('30d', 'quarter', 'year'):
            self.assertIn('kpis', self.env['cp.insights'].get_insights(period))
