# -*- coding: utf-8 -*-
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.cargo_pilot_tms.models.providers import guess_status, parse_dt

from .common_courier import CourierCase


@tagged('post_install', '-at_install')
class TestParcelFlow(CourierCase):

    def test_weights_and_rate_card(self):
        parcel = self.make_parcel(package_ids=[(0, 0, {'quantity': 2, 'weight_kg': 1.0, 'length_cm': 50,
                                                      'width_cm': 40, 'height_cm': 30})])
        # the default box added on create is not added when boxes are given
        self.assertEqual(parcel.piece_count, 2)
        self.assertAlmostEqual(parcel.total_weight_kg, 2.0)
        self.assertAlmostEqual(parcel.volumetric_weight_kg, 24.0)  # 50*40*30/5000 * 2
        self.assertAlmostEqual(parcel.chargeable_weight_kg, 24.0)
        acc = self.demo
        acc.write({'rate_slab_kg': 0.5, 'rate_first_slab': 40, 'rate_additional_slab': 30, 'rate_fuel_percent': 10})
        # 24 kg = 48 slabs -> 40 + 47*30 = 1450, +10% fuel
        self.assertAlmostEqual(acc._cp_rate_card_price(parcel), 1595.0)
        parcel.write({'payment_mode': 'cod', 'cod_amount': 10000})
        acc.write({'rate_cod_fixed': 35, 'rate_cod_percent': 2})
        self.assertAlmostEqual(acc._cp_rate_card_price(parcel), 1795.0)

    def test_cod_needs_amount(self):
        with self.assertRaises(ValidationError):
            self.make_parcel(payment_mode='cod', cod_amount=0)

    def test_compare_and_book_then_track_to_delivery(self):
        manual = self.make_account('manual', courier_display_name='Shree Maruti Courier',
                                   awb_sequence_id=self.env['ir.sequence'].create(
                                       {'name': 'SM', 'prefix': 'SM', 'padding': 6}).id)
        parcel = self.make_parcel(payment_mode='cod', cod_amount=1500.0)
        action = parcel.action_compare_rates()
        wizard = self.env['cp.courier.rate.wizard'].browse(action['res_id'])
        names = set(wizard.line_ids.mapped('courier_name'))
        self.assertIn('Swift', names)
        self.assertIn('Shree Maruti Courier', names)
        self.assertEqual(len(wizard.line_ids.filtered('recommended')), 1)
        self.assertTrue(wizard.line_ids.filtered('is_cheapest'))
        best = wizard.line_ids.filtered('recommended')
        best.action_choose()
        self.assertEqual(parcel.state, 'booked')
        self.assertTrue(parcel.awb)
        self.assertEqual(parcel.shipping_cost, best.price)
        self.assertTrue(parcel.expected_delivery)
        self.assertEqual(parcel.event_ids[:1].status, 'booked')
        self.assertEqual(parcel.cod_state, 'pending')
        self.assertTrue(manual)
        # label falls back to our own 4x6 report
        report = parcel.action_print_label()
        self.assertEqual(report['type'], 'ir.actions.report')
        self.assertIn('report_parcel_label', str(report))
        # simulator moves one step per sync
        seen = []
        for _i in range(6):
            parcel.action_track_now()
            seen.append(parcel.state)
        self.assertEqual(seen[:5], ['picked_up', 'in_transit', 'in_transit', 'out_for_delivery', 'delivered'])
        self.assertTrue(parcel.delivered_at)
        self.assertTrue(parcel.picked_up_at)
        self.assertEqual(parcel.cod_state, 'collected')
        parcel.action_mark_cod_remitted()
        self.assertEqual(parcel.cod_state, 'remitted')
        # dashboard summary
        data = self.env['cp.dashboard'].get_dashboard_data()
        self.assertIn('parcels', data)
        self.assertGreaterEqual(data['parcels']['delivered_month'], 1)

    def test_failed_delivery_raises_issue_and_reattempt(self):
        parcel = self.make_parcel(receiver=self.unlucky)
        parcel.action_book({'ref': 'Swift Surface', 'price': 99.0, 'days': 3})
        for _i in range(4):
            parcel.action_track_now()
        self.assertEqual(parcel.state, 'out_for_delivery')
        parcel.action_track_now()
        self.assertEqual(parcel.state, 'ndr')
        self.assertEqual(parcel.ndr_attempts, 1)
        issue = self.env['cp.exception'].search([('res_model', '=', 'cp.parcel'), ('res_id', '=', parcel.id),
                                                 ('issue_type', '=', 'ndr')])
        self.assertEqual(issue.state, 'open')
        self.assertEqual(issue.severity, 'high')
        wizard = self.env['cp.parcel.ndr.wizard'].create({'parcel_id': parcel.id, 'action': 'reattempt',
                                                          'note': 'Deliver after 6 pm', 'new_phone': '+91 98400 77771'})
        wizard.action_confirm()
        self.assertEqual(parcel.ndr_action, 'reattempt')
        self.assertEqual(issue.state, 'working')
        parcel.action_track_now()
        self.assertEqual(parcel.state, 'out_for_delivery')
        self.assertEqual(issue.state, 'resolved')
        parcel.action_track_now()
        self.assertEqual(parcel.state, 'delivered')

    def test_return_to_sender(self):
        parcel = self.make_parcel(receiver=self.unlucky)
        parcel.action_book()
        for _i in range(5):
            parcel.action_track_now()
        self.assertEqual(parcel.state, 'ndr')
        self.env['cp.parcel.ndr.wizard'].create({'parcel_id': parcel.id, 'action': 'rto'}).action_confirm()
        parcel.action_track_now()
        self.assertEqual(parcel.state, 'rto')
        types = self.env['cp.exception'].search([('res_model', '=', 'cp.parcel'), ('res_id', '=', parcel.id),
                                                 ('state', '!=', 'resolved')]).mapped('issue_type')
        self.assertEqual(types, ['rto'])
        parcel.action_track_now()
        self.assertEqual(parcel.state, 'returned')
        self.assertFalse(self.env['cp.exception'].search_count([
            ('res_model', '=', 'cp.parcel'), ('res_id', '=', parcel.id), ('state', '!=', 'resolved')]))
        # a returned parcel can be reshipped as a new draft
        action = parcel.action_duplicate_to_draft()
        self.assertEqual(self.env['cp.parcel'].browse(action['res_id']).state, 'draft')

    def test_manual_courier_and_updates(self):
        seq = self.env['ir.sequence'].create({'name': 'Docket', 'prefix': 'SM', 'padding': 6, 'number_next': 77})
        manual = self.make_account('manual', courier_display_name='Shree Maruti Courier', awb_sequence_id=seq.id)
        parcel = self.make_parcel(account=manual)
        parcel.action_book()
        self.assertEqual(parcel.awb, 'SM000077')
        self.assertEqual(parcel.courier_name, 'Shree Maruti Courier')
        with self.assertRaises(UserError):
            parcel.action_track_now()  # no tracking API
        self.env['cp.parcel.update.wizard'].create({
            'parcel_id': parcel.id, 'status': 'delivered', 'description': 'Delivered, POD signed by Asha',
            'location': 'Pune'}).action_confirm()
        self.assertEqual(parcel.state, 'delivered')
        self.assertTrue(parcel.event_ids.filtered(lambda e: e.source == 'manual' and e.status == 'delivered'))

    def test_cannot_go_backwards_and_cancel(self):
        parcel = self.make_parcel()
        parcel.action_book()
        parcel._cp_apply_tracking({'status': 'in_transit', 'events': []})
        parcel._cp_apply_tracking({'status': 'booked', 'events': []})
        self.assertEqual(parcel.state, 'in_transit')
        with self.assertRaises(UserError):
            parcel.action_cancel()
        other = self.make_parcel()
        other.action_book()
        other.action_cancel()
        self.assertEqual(other.state, 'cancelled')
        other.action_reset_draft()
        self.assertEqual(other.state, 'draft')
        self.assertFalse(other.awb)

    def test_events_are_not_duplicated(self):
        parcel = self.make_parcel()
        parcel.action_book()
        when = parse_dt('2026-10-20 10:15:00')
        info = {'status': 'in_transit', 'events': [{'time': when, 'description': 'Arrived at hub', 'location': 'Nagpur',
                                                    'status': 'in_transit'}]}
        parcel._cp_apply_tracking(info)
        parcel._cp_apply_tracking(info)
        self.assertEqual(len(parcel.event_ids.filtered(lambda e: e.description == 'Arrived at hub')), 1)
        self.assertEqual(parcel.last_location, 'Nagpur')

    def test_booking_checks(self):
        no_phone = self.receiver.copy({'phone': False})
        parcel = self.make_parcel(receiver=no_phone)
        with self.assertRaisesRegex(UserError, 'phone'):
            parcel.action_book()
        self.demo.max_weight_kg = 0.5
        heavy = self.make_parcel()
        with self.assertRaisesRegex(UserError, 'limit'):
            heavy.action_book()

    def test_delay_issue(self):
        parcel = self.make_parcel()
        parcel.action_book()
        parcel.expected_delivery = '2020-01-01'
        self.assertTrue(parcel.is_delayed)
        parcel._cp_check_delay()
        issue = self.env['cp.exception'].search([('res_model', '=', 'cp.parcel'), ('res_id', '=', parcel.id),
                                                 ('issue_type', '=', 'parcel_delay')])
        self.assertEqual(issue.state, 'open')
        parcel._cp_apply_tracking({'status': 'delivered', 'events': []})
        self.assertEqual(issue.state, 'resolved')

    def test_consignment_to_parcel(self):
        loc_a = self.env['cp.location'].create({'name': 'Raipur Hub', 'partner_id': self.sender.id})
        loc_b = self.env['cp.location'].create({'name': 'Pune Depot'})
        lr = self.env['cp.consignment'].create({
            'consignor_id': self.sender.id, 'consignee_id': self.receiver.id, 'billing_partner_id': self.sender.id,
            'from_location_id': loc_a.id, 'to_location_id': loc_b.id, 'goods_value': 9000, 'payment_type': 'tbb',
            'line_ids': [(0, 0, {'description': 'Bearings', 'packages': 3, 'actual_weight_kg': 12})]})
        action = lr.action_ship_by_courier()
        parcel = self.env['cp.parcel'].browse(action['res_id'])
        self.assertEqual(parcel.consignment_id, lr)
        self.assertEqual(parcel.piece_count, 3)
        self.assertAlmostEqual(parcel.total_weight_kg, 12.0)
        self.assertEqual(parcel.recipient_partner_id, self.receiver)
        self.assertEqual(lr.parcel_count, 1)
        # opening it again reuses the draft
        self.assertEqual(lr.action_ship_by_courier()['res_id'], parcel.id)

    def test_pickup_wizard(self):
        parcels = self.make_parcel() | self.make_parcel()
        parcels.action_book()
        action = parcels.action_request_pickup()
        wizard = self.env['cp.courier.pickup.wizard'].with_context(action['context']).create({})
        wizard.action_confirm()
        self.assertTrue(all(parcels.mapped('pickup_ref')))
        self.assertEqual(len(set(parcels.mapped('pickup_ref'))), 1)

    def test_guess_status(self):
        cases = {
            'Out for Delivery': 'out_for_delivery', 'Shipment Delivered': 'delivered',
            'Consignee not available': 'ndr', 'RTO Initiated': 'rto', 'RTO Delivered': 'returned',
            'In Transit to Mumbai Hub': 'in_transit', 'Picked Up': 'picked_up', 'Manifested': 'booked',
            'Shipment Cancelled': 'cancelled', 'Undelivered - door locked': 'ndr',
        }
        for text, expected in cases.items():
            self.assertEqual(guess_status(text), expected, text)
        self.assertEqual(parse_dt('/Date(1761206400000)/').year, 2025)
        self.assertEqual(parse_dt('24102026 1030', formats=('%d%m%Y %H%M',)).hour, 10)
        self.assertEqual(parse_dt('24-Oct-2026 14:05').month, 10)

    def test_test_connection_demo(self):
        self.demo.action_test_connection()
        self.assertEqual(self.demo.state, 'ok')

    def test_track_draft_parcel_says_book_first(self):
        """Live test bug 11: tracking a draft parcel used to say "False has no tracking API"."""
        parcel = self.make_parcel()
        with self.assertRaises(UserError) as err:
            parcel.action_track_now()
        self.assertIn('not booked yet', str(err.exception))
        self.assertNotIn('False', str(err.exception))
