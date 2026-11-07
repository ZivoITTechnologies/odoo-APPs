# -*- coding: utf-8 -*-
"""Regression tests for the issues found in the live test on zunaxit-tms.odoo.com (October 2026)."""
from datetime import datetime, timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import new_test_user

from .common_dispatch import DispatchCommon


@tagged('post_install', '-at_install', 'cargo_pilot')
class TestLiveFixes(DispatchCommon):

    def _approve(self, detention, reason='dock_busy'):
        detention.delay_reason = reason
        detention.action_submit()
        detention.action_approve_logistics()
        detention.with_user(self.ops_head).action_approve_ops()

    # Bug 1 -------------------------------------------------------------
    def test_bid_invite_uses_planner_time_zone(self):
        planner = new_test_user(self.env, 'cp_planner_ist', groups='base.group_user,cargo_pilot_tms.group_cp_manager',
                                tz='Asia/Kolkata')
        plan = self._plan(user_id=planner.id)
        plan.action_ready()
        self._log_all_unavailable(plan)
        slot = datetime(2026, 11, 2, 6, 30)  # 06:30 UTC = 12:00 IST
        plan.cp_launch_bid(self.v1 | self.v2, 15, vehicle_type=self.vt_14ft, pickup_slot=slot)
        invite = plan.message_ids.filtered(lambda m: self.v1 in m.partner_ids and 'pickup' in (m.body or ''))
        self.assertTrue(invite)
        self.assertIn('12:00', invite[0].body)
        self.assertNotIn('06:30', invite[0].body)

    # Bug 2 -------------------------------------------------------------
    def test_detention_approved_after_bill_is_billed_separately(self):
        plan = self._plan()
        plan.action_ready()
        self._bid_and_award(plan, vendor=self.v1, rate=8200)
        detention = self._through_gate(plan, waited_hours=6).detention_id
        detention.delay_reason = 'dock_busy'
        detention.action_submit()  # still waiting for approval when the vendor bills
        plan.action_mark_delivered()
        self._verified_pod(plan)
        first = plan.cp_create_vendor_bill(bill_ref='VEGA/201')
        self.assertEqual(len(first.invoice_line_ids), 1, 'only freight: detention is not approved yet')
        self.assertTrue(plan.message_ids.filtered(lambda m: 'still awaiting approval' in (m.body or '')))
        first.action_post()
        detention.action_approve_logistics()
        detention.with_user(self.ops_head).action_approve_ops()
        self.assertAlmostEqual(plan.detention_unbilled, detention.approved_amount, places=2)
        self.assertGreater(plan.detention_unbilled, 0)
        with self.assertRaises(UserError):
            plan.action_close()  # approved detention is not billed yet
        second = plan.cp_create_vendor_bill(bill_ref='VEGA/201-D')
        self.assertEqual(len(second.invoice_line_ids), 1)
        self.assertEqual(second.invoice_line_ids.product_id, self.env.ref('cargo_pilot_tms.product_cp_detention'))
        self.assertAlmostEqual(second.amount_untaxed, detention.approved_amount, places=2)
        self.assertEqual(second.cp_freight_expected, 0.0, 'freight is already on the first bill')
        self.assertEqual(second.cp_match_state, 'ok')
        self.assertEqual(detention.move_id, second)
        self.assertEqual(plan.detention_unbilled, 0)
        with self.assertRaises(UserError):
            plan.cp_create_vendor_bill()  # nothing left to bill
        with self.assertRaises(UserError):
            plan.action_close()  # draft detention bill
        second.action_post()
        plan.action_close()
        self.assertEqual(plan.state, 'closed')

    def test_second_bill_cannot_repeat_freight(self):
        plan = self._plan()
        plan.action_ready()
        self._bid_and_award(plan, vendor=self.v1, rate=8200)
        detention = self._through_gate(plan, waited_hours=6).detention_id
        plan.action_mark_delivered()
        self._verified_pod(plan)
        plan.cp_create_vendor_bill(bill_ref='VEGA/301').action_post()
        self._approve(detention)
        product = self.env.ref('cargo_pilot_tms.product_cp_freight')
        repeat = self.env['account.move'].create({
            'move_type': 'in_invoice', 'partner_id': self.v1.id, 'cp_dispatch_id': plan.id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': [(0, 0, {'product_id': product.id, 'quantity': 1, 'price_unit': 8200})]})
        self.assertEqual(repeat.cp_match_state, 'exception', 'freight billed twice is a mismatch')

    # Bug 3 -------------------------------------------------------------
    def test_target_date_is_not_now(self):
        self.company.cp_dispatch_transit_days = 2.0
        plan = self._plan(target_date=False)
        self.assertTrue(plan.target_date)
        self.assertGreater(plan.target_date, fields.Datetime.now() + timedelta(days=1, hours=20))
        plan.action_ready()
        self._bid_and_award(plan, vendor=self.v1, rate=8200)
        self.assertFalse(plan.is_delayed)
        self._through_gate(plan)
        self.assertEqual(plan.board_status, 'in_transit')
        plan.action_mark_delivered()
        self.assertTrue(plan.on_time_delivery)

    def test_target_date_follows_delivery_deadline_and_manual_plans(self):
        order = self._sale(5)
        deadline = fields.Datetime.now() + timedelta(days=5)
        order.picking_ids.date_deadline = deadline
        plan = self.env['cp.dispatch.plan'].create({'picking_ids': [(6, 0, order.picking_ids.ids)],
                                                    'warehouse_id': self.warehouse.id, 'partner_id': self.customer.id})
        self.assertEqual(plan.target_date, deadline)
        promised = self._sale(3)
        promised.commitment_date = fields.Datetime.now() + timedelta(days=4)
        plan2 = self.env['cp.dispatch.plan'].create({'picking_ids': [(6, 0, promised.picking_ids.ids)],
                                                     'warehouse_id': self.warehouse.id, 'partner_id': self.customer.id})
        self.assertEqual(plan2.target_date, promised.commitment_date)
        manual = self.env['cp.dispatch.plan'].create({'warehouse_id': self.warehouse.id, 'partner_id': self.customer.id,
                                                      'gross_weight_kg': 200})
        self.assertFalse(manual.target_date)
        manual.action_ready()
        self.assertGreater(manual.target_date, fields.Datetime.now() + timedelta(days=1))

    # Bug 4 -------------------------------------------------------------
    def test_reset_asks_regular_partners_again(self):
        plan = self._plan()
        plan.action_ready()
        self._log_all_unavailable(plan)
        self.assertTrue(plan.regulars_exhausted)
        plan.action_cancel()
        plan.action_reset_draft()
        plan.action_ready()
        self.assertFalse(plan.regulars_exhausted)
        self.assertIn(self.dhl.name, plan.regulars_pending)
        self.assertEqual(len(plan.check_log_ids), 3, 'old checks stay in the audit trail')
        self.assertTrue(all(plan.check_log_ids.mapped('superseded')))
        with self.assertRaises(UserError):
            plan.action_escalate_bid()
        with self.assertRaises(UserError):
            plan.check_log_ids[:1].write({'status': 'confirmed'})  # still immutable
        self._log_all_unavailable(plan)
        self.assertTrue(plan.regulars_exhausted)

    # Bug 5 -------------------------------------------------------------
    def test_detention_term_display_name(self):
        term = self.env['cp.detention.term'].cp_for(self.v1)
        self.assertIn('Vega 3PL', term.display_name)
        self.assertNotIn('cp.detention.term', term.display_name)

    # Bug 6 -------------------------------------------------------------
    def test_amounts_are_formatted(self):
        plan = self._plan()
        plan.action_ready()
        auction = self._bid_and_award(plan, vendor=self.v1, rate=8200)
        bid = auction.bid_ids.filtered(lambda b: b.transporter_id == self.v1)[:1]
        self.assertIn('8,200.00', bid.display_name)
        self._through_gate(plan)
        plan.action_mark_delivered()
        self._verified_pod(plan)
        move = plan.cp_create_vendor_bill(bill_ref='VEGA/401', freight_billed=8600)
        self.assertEqual(move.cp_match_state, 'exception')
        self.assertIn('8,600.00', move.cp_match_note)
        self.assertIn('8,200.00', move.cp_match_note)
        self.assertNotIn('8600.0 ', move.cp_match_note + ' ')

    # Bug 7 -------------------------------------------------------------
    def test_dispatch_bid_savings_against_target(self):
        plan = self._plan()
        plan.action_ready()
        self._log_all_unavailable(plan)
        auction = plan.cp_launch_bid(self.v1 | self.v2, 15, target_price=9000, vehicle_type=self.vt_14ft)
        auction.cp_place_bid(self.v1, 8200)
        auction.write({'date_start': fields.Datetime.now() - timedelta(minutes=30),
                       'date_end': fields.Datetime.now() - timedelta(seconds=1)})
        self.env['cp.auction']._cron_auction_lifecycle()
        plan.cp_award(auction.proposed_bid_id)
        self.assertEqual(auction.awarded_amount, 8200)
        self.assertEqual(auction.savings_amount, 800)
        self.assertAlmostEqual(auction.savings_pct, 800 / 9000 * 100, places=2)
        data = self.env['cp.dispatch.dashboard'].get_data('month')
        self.assertGreaterEqual(data['kpis']['savings'], 800)

    # Bug 8 -------------------------------------------------------------
    def test_record_phone_bid_for_vendor(self):
        plan = self._plan()
        plan.action_ready()
        self._log_all_unavailable(plan)
        auction = plan.cp_launch_bid(self.v1 | self.v2, 15, vehicle_type=self.vt_14ft)
        action = plan.action_record_bid()
        wizard = self.env['cp.bid.record.wizard'].browse(action['res_id'])
        wizard.write({'vendor_id': self.v2.id, 'rate': 7900, 'channel': 'phone', 'note': 'Spoke to Suresh'})
        wizard.action_record()
        bid = auction.bid_ids.filtered(lambda b: b.transporter_id == self.v2)
        self.assertEqual(bid.rate, 7900)
        self.assertIn('Phone call', bid.note)
        self.assertIn('Spoke to Suresh', bid.note)
        self.assertTrue(plan.message_ids.filtered(lambda m: 'Orbit 3PL' in (m.body or '') and 'Phone call' in (m.body or '')))
        other = self.env['cp.bid.record.wizard'].create({'plan_id': plan.id, 'vendor_id': self.v3.id, 'rate': 7000})
        with self.assertRaises(UserError):
            other.action_record()  # not invited
        zero = self.env['cp.bid.record.wizard'].create({'plan_id': plan.id, 'vendor_id': self.v1.id, 'rate': 0})
        with self.assertRaises(UserError):
            zero.action_record()
        auction.write({'date_start': fields.Datetime.now() - timedelta(minutes=30),
                       'date_end': fields.Datetime.now() - timedelta(seconds=1)})
        with self.assertRaises(UserError):
            plan.action_record_bid()  # closed
        auction.cp_close_if_expired()
        self.assertEqual(auction.proposed_bid_id, bid, 'the phoned-in offer is L1')

    def test_record_target_price_acceptance(self):
        plan = self._plan()
        plan.action_ready()
        self._log_all_unavailable(plan)
        auction = plan.cp_launch_bid(self.v1 | self.v2, 15, mode='target_price', target_price=8800,
                                     vehicle_type=self.vt_14ft)
        wizard = self.env['cp.bid.record.wizard'].browse(plan.action_record_bid()['res_id'])
        wizard.write({'vendor_id': self.v1.id, 'channel': 'whatsapp'})
        wizard.action_record()
        self.assertEqual(auction.state, 'closed')
        self.assertEqual(auction.proposed_bid_id.transporter_id, self.v1)
        self.assertEqual(auction.proposed_bid_id.rate, 8800)
        self.assertEqual(auction.proposed_bid_id.channel, 'whatsapp')

    # Bug 9 -------------------------------------------------------------
    def test_carrier_wizard_book_keeps_checks(self):
        plan = self._plan()
        plan.action_ready()
        wizard = self.env['cp.carrier.check.wizard'].browse(plan.action_open_carrier_check()['res_id'])
        for line, status in zip(wizard.line_ids, ('no_vehicle', 'over_capacity', 'no_service')):
            line.status = status
        result = wizard.action_book()
        self.assertEqual(result['tag'], 'display_notification')
        self.assertEqual(result['params']['type'], 'warning')
        self.assertIn('3PL', result['params']['message'])
        self.assertEqual(len(plan.check_log_ids), 3, 'the checks typed in are saved')
        self.assertTrue(plan.regulars_exhausted)
        self.assertFalse(plan.trip_id)
        empty = self.env['cp.carrier.check.wizard'].create({'plan_id': plan.id, 'line_ids': [
            (0, 0, {'carrier_id': self.dhl.id})]})
        result = empty.action_book()
        self.assertFalse(result['params']['next'], 'nothing entered: the wizard stays open')
