# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from .common_dispatch import DispatchCommon


@tagged('post_install', '-at_install', 'cargo_pilot')
class TestDispatchPlan(DispatchCommon):

    def test_fr01_plan_from_deliveries_and_capacity(self):
        plan = self._plan(qty=10)
        self.assertTrue(plan.name.startswith('DSP/'))
        self.assertEqual(plan.gross_weight_kg, 180.0)
        self.assertAlmostEqual(plan.volume_cbm, 1.2)
        self.assertEqual(plan.package_count, 10)
        self.assertAlmostEqual(plan.goods_value, 140000)
        self.assertEqual(plan.sale_order_ids, plan.picking_ids.sale_id)
        self.assertEqual(plan.movement, 'interstate')
        self.assertTrue(plan.eway_required)
        self.assertEqual(plan.dest_pincode, '500032')
        self.assertEqual(plan.suggested_vehicle_type_id, self.vt_small)
        # an undersized truck is refused
        big = self._plan(qty=60)
        big.vehicle_type_id = self.vt_small
        self.assertFalse(big.capacity_ok)
        with self.assertRaises(UserError):
            big.action_ready()
        big.vehicle_type_id = self.vt_14ft
        big.action_ready()
        self.assertEqual(big.state, 'ready')
        plan.action_ready()
        self.assertEqual(plan.vehicle_type_id, self.vt_small, 'suggested truck type applied')
        # one delivery order cannot sit on two plans
        with self.assertRaises(ValidationError):
            self.env['cp.dispatch.plan'].create({'picking_ids': [(6, 0, plan.picking_ids.ids)],
                                                 'warehouse_id': self.warehouse.id, 'partner_id': self.customer.id})

    def test_fr02_regular_partner_first_and_booking(self):
        plan = self._plan()
        plan.action_ready()
        with self.assertRaises(UserError):
            plan.action_escalate_bid()
        plan.cp_log_check(self.dhl, 'no_vehicle')
        plan.cp_log_check(self.fedex, 'no_service')
        self.assertFalse(plan.regulars_exhausted)
        self.assertEqual(plan.regulars_pending, 'Delhivery')
        with self.assertRaises(UserError):
            plan.action_escalate_bid()
        # serviceability pre-check fills the wizard
        self.dhl.cp_max_weight_kg = 100
        action = plan.action_open_carrier_check()
        wizard = self.env['cp.carrier.check.wizard'].browse(action['res_id'])
        dhl_line = wizard.line_ids.filtered(lambda l: l.carrier_id == self.dhl)
        self.assertEqual(dhl_line.status, 'no_vehicle', 'the latest logged answer wins over the pre-check')
        line = wizard.line_ids.filtered(lambda l: l.carrier_id == self.delhivery)
        line.write({'status': 'confirmed', 'quoted_rate': 4500, 'booking_ref': 'DLV998877', 'book': True})
        wizard.action_book()
        self.assertEqual(plan.state, 'assigned')
        self.assertEqual(plan.carrier_type, 'regular')
        self.assertEqual(plan.vendor_id, self.delhivery)
        self.assertEqual(plan.booking_ref, 'DLV998877')
        self.assertEqual(plan.tracking_url, 'https://www.delhivery.com/track-v2/package/DLV998877')
        self.assertEqual(plan.trip_id.shipment_mode, 'ltl')
        self.assertEqual(plan.trip_id.docket_number, 'DLV998877')
        self.assertEqual(plan.trip_id.state, 'assigned')
        self.assertEqual(plan.trip_id.order_ids.sale_order_id, plan.sale_order_ids)
        po = plan.purchase_order_id
        self.assertEqual(po.state, 'purchase')
        self.assertEqual(po.partner_id, self.delhivery)
        self.assertEqual(po.amount_untaxed, 4500)
        log = plan.check_log_ids.filtered(lambda l: l.carrier_id == self.delhivery)
        self.assertTrue(log.booked)
        with self.assertRaises(UserError):
            log.status = 'no_vehicle'
        with self.assertRaises(UserError):
            log.unlink()
        # pickup, customer alert with the carrier link, delivery
        plan.eway_bill_no = '391200000001'
        plan.action_mark_picked_up()
        self.assertEqual(plan.state, 'in_transit')
        self.assertEqual(plan.trip_id.state, 'in_transit')
        sent = plan.message_ids.filtered(lambda m: self.customer in m.partner_ids)
        self.assertTrue(any('DLV998877' in (m.body or '') and 'delhivery.com' in (m.body or '') for m in sent))
        plan.action_out_for_delivery()
        self.assertEqual(plan.board_status, 'out_for_delivery')
        plan.action_mark_delivered()
        self.assertEqual(plan.state, 'delivered')
        self.assertEqual(plan.trip_id.state, 'delivered')

    def test_fr02_precheck_limits(self):
        plan = self._plan(qty=40)
        self.dhl.cp_max_weight_kg = 500
        self.fedex.cp_service_pincodes = '4000, 4110'
        self.assertEqual(self.dhl.cp_precheck(plan), 'over_capacity')
        self.assertEqual(self.fedex.cp_precheck(plan), 'no_service')
        self.assertFalse(self.delhivery.cp_precheck(plan))
        plan.action_ready()
        action = plan.action_open_carrier_check()
        wizard = self.env['cp.carrier.check.wizard'].browse(action['res_id'])
        self.assertEqual(wizard.line_ids.filtered(lambda l: l.carrier_id == self.fedex).status, 'no_service')

    def test_fr03_fr04_reverse_auction(self):
        plan = self._plan()
        plan.action_ready()
        self._log_all_unavailable(plan)
        self.assertTrue(plan.regulars_exhausted)
        self.assertEqual(plan._cp_eligible_vendors(self.vt_14ft), self.v1 | self.v2)
        with self.assertRaises(UserError):
            plan.cp_launch_bid(self.v1 | self.v2, 25, vehicle_type=self.vt_14ft)
        action = plan.action_escalate_bid()
        wizard = self.env['cp.bid.launch.wizard'].browse(action['res_id'])
        self.assertEqual(wizard.duration_minutes, 15)
        wizard.write({'vehicle_type_id': self.vt_14ft.id, 'target_price': 9500,
                      'vendor_ids': [(6, 0, (self.v1 | self.v2).ids)]})
        wizard.action_launch()
        auction = plan.auction_id
        self.assertEqual(plan.state, 'bidding')
        self.assertEqual(auction.state, 'live')
        self.assertAlmostEqual((auction.date_end - auction.date_start).total_seconds(), 900, delta=2)
        self.assertTrue(plan.message_ids.filtered(lambda m: self.v1 in m.partner_ids))
        auction.cp_place_bid(self.v1, 9000)
        auction.cp_place_bid(self.v2, 8500)
        auction.cp_place_bid(self.v1, 8200)
        with self.assertRaises(UserError):
            auction.cp_place_bid(self.v3, 7000)
        status = auction.cp_live_status(self.v1)
        self.assertEqual(status['state'], 'live')
        self.assertEqual(status['best_rate'], 8200)
        self.assertEqual(status['my_rank'], 1)
        self.assertGreater(status['seconds_left'], 800)
        # the deadline locks the bid: late offers are refused, the next check closes it
        auction.write({'date_start': fields.Datetime.now() - timedelta(minutes=30), 'date_end': fields.Datetime.now() - timedelta(seconds=1)})
        with self.assertRaises(UserError):
            auction.cp_place_bid(self.v2, 8000)
        auction.cp_close_if_expired()
        self.assertEqual(auction.state, 'closed')
        self.assertEqual(auction.proposed_bid_id.transporter_id, self.v1)
        self.assertEqual(auction.proposed_bid_id.rate, 8200)
        # choosing L2 needs a justification
        l2 = auction.bid_ids.filtered(lambda b: b.transporter_id == self.v2 and b.is_latest)
        with self.assertRaises(UserError):
            plan.cp_award(l2)
        plan.cp_award(l2, justification='Reports 3 hours earlier')
        self.assertEqual(plan.state, 'assigned')
        self.assertEqual(plan.vendor_id, self.v2)
        self.assertEqual(plan.freight_amount, 8500)
        self.assertEqual(auction.override_reason, 'Reports 3 hours earlier')
        self.assertEqual(auction.state, 'awarded')
        self.assertEqual(plan.trip_id.dispatch_plan_id, plan)
        self.assertEqual(plan.trip_id.transporter_id, self.v2)
        self.assertEqual(plan.purchase_order_id.amount_untaxed, 8500)
        self.assertEqual(plan.purchase_order_id.cp_dispatch_id, plan)
        self.assertTrue(plan.message_ids.filtered(lambda m: self.v1 in m.partner_ids and 'awarded' in (m.body or '')))
        self.assertTrue(plan.message_ids.filtered(lambda m: self.v2 in m.partner_ids and 'You won' in (m.body or '')))
        self.assertEqual(plan.trip_id.offer_ids.filtered(lambda o: o.state == 'pending'), self.env['cp.trip.offer'])

    def test_fr03_target_price_first_acceptance(self):
        plan = self._plan()
        plan.action_ready()
        self._log_all_unavailable(plan)
        with self.assertRaises(UserError):
            plan.cp_launch_bid(self.v1 | self.v2, 12, mode='target_price', vehicle_type=self.vt_14ft)
        auction = plan.cp_launch_bid(self.v1 | self.v2, 12, mode='target_price', target_price=9000,
                                     vehicle_type=self.vt_14ft)
        bid = auction.cp_place_bid(self.v2, 1)
        self.assertEqual(bid.rate, 9000, 'target price mode always books at the target')
        self.assertTrue(bid.accepted_target)
        self.assertEqual(auction.state, 'closed')
        self.assertEqual(auction.proposed_bid_id, bid)
        with self.assertRaises(UserError):
            auction.cp_place_bid(self.v1, 9000)
        plan.cp_award(bid)
        self.assertEqual(plan.vendor_id, self.v2)

    def test_fr03_no_bids_goes_back_to_ready(self):
        plan = self._plan()
        plan.action_ready()
        self._log_all_unavailable(plan)
        auction = plan.cp_launch_bid(self.v1, 10, vehicle_type=self.vt_14ft)
        auction.write({'date_start': fields.Datetime.now() - timedelta(minutes=30), 'date_end': fields.Datetime.now() - timedelta(minutes=1)})
        self.env['cp.auction']._cron_auction_lifecycle()
        self.assertEqual(auction.state, 'closed')
        self.assertEqual(plan.state, 'ready')
        self.assertTrue(self.env['cp.exception'].search([('res_model', '=', 'cp.dispatch.plan'),
                                                         ('res_id', '=', plan.id), ('issue_type', '=', 'no_bids')]))

    def test_fr05_fr06_gate_and_hourly_detention(self):
        plan = self._plan()
        plan.action_ready()
        self._bid_and_award(plan, vendor=self.v1, rate=8200)
        action = plan.action_new_gate_entry()
        entry = self.env['cp.gate.entry'].browse(action['res_id'])
        with self.assertRaises(UserError):
            entry.action_gate_in()
        entry.write({'vehicle_number': 'CG04 AB 1234', 'driver_name': 'Ramesh', 'driver_phone': '9000000001',
                     'driver_license': 'CG0420190001234'})
        entry.action_gate_in()
        self.assertEqual(plan.trip_id.vehicle_number, 'CG04 AB 1234')
        self.assertEqual(plan.trip_id.state, 'reported')
        self.assertTrue(plan.gate_in_time)
        self.assertTrue(plan.reported_on_time)
        with self.assertRaises(UserError):
            entry.action_to_dock()
        entry.write({'check_license': True, 'check_fitness': True, 'check_clean': True, 'check_seal': True})
        entry.action_to_dock()
        self.assertEqual(plan.state, 'loading')
        entry.action_loading_done()
        with self.assertRaises(UserError):
            entry.action_gate_out()  # interstate: e-way bill required
        entry.eway_bill_no = '391234567890'
        entry.gate_in_time = fields.Datetime.now() - timedelta(hours=6)
        entry.action_gate_out()
        self.assertEqual(plan.state, 'in_transit')
        self.assertEqual(plan.trip_id.state, 'in_transit')
        self.assertEqual(plan.trip_id.eway_bill_no, '391234567890')
        alert = plan.message_ids.filtered(lambda m: self.customer in m.partner_ids)
        self.assertTrue(any('/cp/track/' in (m.body or '') for m in alert), 'customer gets the GPS tracking link')
        detention = entry.detention_id
        self.assertTrue(detention)
        self.assertAlmostEqual(detention.tat_hours, 6.0, places=1)
        self.assertEqual(detention.free_hours, 2.0)
        self.assertAlmostEqual(detention.gross_waiting_hours, 4.0, places=1)
        self.assertAlmostEqual(detention.calculated_amount, 1200, delta=15)
        with self.assertRaises(UserError):
            detention.action_submit()
        detention.delay_reason = 'dock_busy'
        detention.action_submit()
        self.assertEqual(detention.state, 'submitted')
        with self.assertRaises(UserError):
            detention.with_user(self.ops_head).action_approve_ops()
        detention.action_approve_logistics()
        self.assertEqual(detention.state, 'logistics_approved')
        with self.assertRaises(UserError):
            detention.action_approve_ops()  # same person cannot approve twice
        detention.with_user(self.ops_head).action_approve_ops()
        self.assertEqual(detention.state, 'approved')
        self.assertAlmostEqual(plan.detention_approved, detention.calculated_amount)

    def test_fr06_daily_slab_and_transporter_fault(self):
        Detention = self.env['cp.detention.record']
        plan = self._plan()
        plan.action_ready()
        self._bid_and_award(plan, vendor=self.v2, rate=8500)
        now = fields.Datetime.now()
        daily = Detention.create({'plan_id': plan.id, 'gate_in_time': now - timedelta(hours=34), 'gate_out_time': now})
        daily._cp_apply_terms()
        daily.write({'gate_in_time': now - timedelta(hours=34), 'gate_out_time': now})
        self.assertEqual(daily.method, 'daily')
        self.assertAlmostEqual(daily.gross_waiting_hours, 30.0, places=1)
        self.assertEqual(daily.chargeable_days, 2)
        self.assertEqual(daily.calculated_amount, 5000)
        daily.partial_day = 'prorata'
        self.assertAlmostEqual(daily.calculated_amount, 2500 * 30 / 24, places=0)
        fault = Detention.create({'plan_id': plan.id, 'gate_in_time': now - timedelta(hours=10), 'gate_out_time': now,
                                  'free_hours': 2, 'method': 'hourly', 'hourly_rate': 500,
                                  'delay_reason': 'transporter_fault'})
        self.assertEqual(fault.chargeable_hours, 0)
        self.assertEqual(fault.calculated_amount, 0)
        fault.action_submit()
        self.assertEqual(fault.state, 'approved')
        self.assertEqual(fault.approved_amount, 0)
        # an adjusted amount needs a reason
        daily.delay_reason = 'goods_not_ready'
        daily.action_submit()
        daily.approved_amount = 3000
        with self.assertRaises(ValidationError):
            daily.action_approve_logistics()
        daily.adjust_reason = 'Agreed with vendor on call'
        daily.action_approve_logistics()
        daily.with_user(self.ops_head).action_approve_ops()
        self.assertEqual(plan.detention_approved, 3000)

    def test_fr08_fr09_pod_gate_and_three_way_match(self):
        plan = self._plan()
        plan.action_ready()
        self._bid_and_award(plan, vendor=self.v1, rate=8200)
        entry = self._through_gate(plan, waited_hours=4)
        detention = entry.detention_id
        detention.delay_reason = 'goods_not_ready'
        detention.action_submit()
        detention.action_approve_logistics()
        detention.with_user(self.ops_head).action_approve_ops()
        plan.action_mark_delivered()
        self.assertEqual(plan.pod_status, 'missing')
        move = plan.cp_create_vendor_bill(bill_ref='VEGA/101')
        freight_line = move.invoice_line_ids.filtered(lambda l: l.purchase_line_id)
        self.assertEqual(freight_line.price_unit, 8200)
        self.assertEqual(len(move.invoice_line_ids), 2)
        self.assertAlmostEqual(move.cp_detention_billed, detention.approved_amount, places=2)
        self.assertEqual(move.cp_match_state, 'ok')
        with self.assertRaises(ValidationError):
            move.action_post()
        # a rejected POD goes back to the vendor
        attachment = self.env['ir.attachment'].create({'name': 'blurry.jpg', 'datas': b'aGVsbG8='})
        pod = self.env['cp.dispatch.pod'].create({'plan_id': plan.id, 'attachment_ids': [(6, 0, attachment.ids)]})
        self.assertEqual(plan.pod_status, 'pending')
        with self.assertRaises(UserError):
            pod.action_verify()
        with self.assertRaises(UserError):
            pod.action_reject()
        pod.reject_reason = 'Stamp not visible'
        pod.action_reject()
        self.assertEqual(plan.pod_status, 'rejected')
        self.assertTrue(plan.message_ids.filtered(lambda m: self.v1 in m.partner_ids and 'Stamp not visible' in (m.body or '')))
        with self.assertRaises(ValidationError):
            move.action_post()
        self._verified_pod(plan)
        self.assertEqual(plan.pod_status, 'verified')
        self.assertEqual(plan.state, 'pod_verified')
        with self.assertRaises(UserError):
            plan.action_close()  # bill not posted yet
        move.action_post()
        self.assertEqual(move.state, 'posted')
        plan.action_close()
        self.assertEqual(plan.state, 'closed')
        self.assertEqual(plan.trip_id.state, 'closed')
        self.assertTrue(plan.pod_tat_hours >= 0)

    def test_fr09_mismatch_needs_accounting_approval(self):
        plan = self._plan()
        plan.action_ready()
        self._bid_and_award(plan, vendor=self.v1, rate=8200)
        self._through_gate(plan)
        plan.action_mark_delivered()
        self._verified_pod(plan)
        move = plan.cp_create_vendor_bill(bill_ref='VEGA/102', freight_billed=8600, detention_billed=500)
        self.assertEqual(move.cp_match_state, 'exception')
        self.assertIn('8,600.00', move.cp_match_note)
        self.assertIn('Detention', move.cp_match_note)
        with self.assertRaises(ValidationError):
            move.action_post()
        with self.assertRaises(UserError):
            move.action_cp_approve_mismatch()  # not an accounting manager
        approver = move.with_user(self.acc_manager)
        with self.assertRaises(UserError):
            approver.action_cp_approve_mismatch()  # reason missing
        move.cp_exception_reason = 'Toll increase agreed'
        approver.action_cp_approve_mismatch()
        self.assertEqual(move.cp_match_state, 'approved')
        # changing the bill after approval re-opens the exception
        move.invoice_line_ids.filtered(lambda l: l.purchase_line_id).price_unit = 9000
        self.assertEqual(move.cp_match_state, 'exception')
        move.invoice_line_ids.filtered(lambda l: l.purchase_line_id).price_unit = 8600
        self.assertEqual(move.cp_match_state, 'approved')
        move.action_post()
        self.assertEqual(move.state, 'posted')

    def test_fr09_standard_po_bill_is_gated(self):
        plan = self._plan()
        plan.action_ready()
        self._bid_and_award(plan, vendor=self.v1, rate=8200)
        po = plan.purchase_order_id
        po.action_create_invoice()
        move = po.invoice_ids
        self.assertEqual(move.cp_dispatch_id, plan)
        move.invoice_date = fields.Date.today()
        with self.assertRaises(ValidationError):
            move.action_post()

    def test_epod_from_driver_creates_pod_and_board(self):
        plan = self._plan()
        plan.action_ready()
        self._bid_and_award(plan, vendor=self.v1, rate=8200)
        self._through_gate(plan)
        self.assertEqual(plan.board_status, 'in_transit')
        plan.target_date = fields.Datetime.now() - timedelta(hours=1)
        self.assertEqual(plan.board_status, 'delayed')
        self.assertTrue(plan.is_delayed)
        self.assertIn(plan, self.env['cp.dispatch.plan'].search([('is_delayed', '=', True)]))
        from odoo.addons.cargo_pilot_tms.tests.common import PIXEL
        trip = plan.trip_id
        epod = self.env['cp.epod'].create({
            'trip_id': trip.id, 'receiver_name': 'Stores', 'received_weight_ton': trip.loaded_weight_ton,
            'latitude': trip.destination_id.latitude, 'longitude': trip.destination_id.longitude, 'signature': PIXEL,
            'photo_ids': [(0, 0, {'name': 'pod.png', 'datas': PIXEL})]})
        epod.action_submit()
        pod = plan.pod_ids.filtered(lambda p: p.source == 'driver')
        self.assertTrue(pod)
        self.assertEqual(len(pod.attachment_ids), 2)
        self.assertEqual(plan.state, 'delivered', 'the driver app delivery moves the plan')
        self.assertEqual(plan.pod_status, 'pending', 'the driver photo still needs the logistics audit')

    def test_fr10_dashboard_and_cancel(self):
        regular = self._plan()
        regular.action_ready()
        regular.cp_log_check(self.delhivery, 'confirmed', rate=3000)
        regular.cp_book_regular(self.delhivery, booking_ref='D1')
        third = self._plan()
        third.action_ready()
        self._bid_and_award(third, vendor=self.v1, rate=8200, mode='reverse_auction')
        third.target_rate = 9000
        data = self.env['cp.dispatch.dashboard'].get_data('month')
        self.assertEqual(data['kpis']['regular_share'], 50.0)
        self.assertEqual(data['kpis']['savings'], 800)
        self.assertEqual(data['kpis']['bids'], 1)
        self.assertEqual({v['name'] for v in data['vendors']}, {'Delhivery', 'Vega 3PL'})
        self.assertTrue(data['board'])
        draft = self._plan()
        draft.action_ready()
        draft.action_cancel()
        self.assertEqual(draft.state, 'cancel')
        self.assertFalse(draft.picking_ids.cp_indent_id)
        third.action_cancel()
        self.assertEqual(third.purchase_order_id.state, 'cancel')
        self.assertEqual(third.trip_id.state, 'cancel')


@tagged('post_install', '-at_install', 'cargo_pilot')
class TestDispatchRoles(DispatchCommon):

    def test_role_matrix(self):
        from odoo.exceptions import AccessError
        from odoo.tests.common import new_test_user
        sales = new_test_user(self.env, 'cp_sales_coord',
                              groups='sales_team.group_sale_salesman,cargo_pilot_tms.group_dispatch_sales')
        gate = new_test_user(self.env, 'cp_gate_guard', groups='base.group_user,cargo_pilot_tms.group_dispatch_gate')
        order = self._sale(5)
        plan = self.env['cp.dispatch.plan'].with_user(sales).create({
            'picking_ids': [(6, 0, order.picking_ids.ids)], 'warehouse_id': self.warehouse.id,
            'partner_id': self.customer.id})
        self.assertEqual(plan.gross_weight_kg, 90.0)
        plan.with_user(sales).action_ready()
        with self.assertRaises(AccessError):
            self.env['cp.auction'].with_user(sales).search([])
        plan = plan.with_env(self.env)  # the logistics team takes over
        self._bid_and_award(plan, vendor=self.v1, rate=8000)
        # the gate guard runs the whole gate without touching anything else
        entry = self.env['cp.gate.entry'].browse(plan.action_new_gate_entry()['res_id']).with_user(gate)
        entry.write({'vehicle_number': 'CG07 XY 9999', 'driver_name': 'Mohan', 'driver_phone': '9000000002',
                     'driver_license': 'DL-77', 'check_license': True, 'check_fitness': True, 'check_clean': True,
                     'check_seal': True, 'eway_bill_no': '391299999999'})
        entry.action_gate_in()
        entry.action_to_dock()
        entry.action_loading_done()
        entry.action_gate_out()
        self.assertEqual(plan.state, 'in_transit')
        with self.assertRaises(AccessError):
            plan.with_user(gate).write({'freight_amount': 1})
        # finance can read but not approve a detention claim
        self.assertTrue(plan.with_user(self.acc_manager).read(['name', 'freight_amount']))
