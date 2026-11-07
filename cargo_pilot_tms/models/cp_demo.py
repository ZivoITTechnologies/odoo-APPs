# -*- coding: utf-8 -*-
"""Demo data for the shipper features (FTL + LTL, channels, orders, diesel, offers).

Built in Python so Indian states are reused when the India localisation already created them."""
from datetime import timedelta

from odoo import api, fields, models


class CpDemo(models.AbstractModel):
    _name = 'cp.demo'
    _description = 'Cargo Pilot demo data'

    @api.model
    def _cp_state(self, code, name):
        india = self.env.ref('base.in')
        State = self.env['res.country.state']
        return State.search([('country_id', '=', india.id), ('code', '=', code)], limit=1) or \
            State.create({'country_id': india.id, 'code': code, 'name': name})

    @api.model
    def _cp_load_client_demo(self):
        ref = self.env.ref
        today = fields.Date.context_today(self)
        now = fields.Datetime.now()
        states = {'CT': self._cp_state('CT', 'Chhattisgarh'), 'TS': self._cp_state('TS', 'Telangana'),
                  'MH': self._cp_state('MH', 'Maharashtra'), 'TN': self._cp_state('TN', 'Tamil Nadu')}
        for xmlid, code, pin in (('demo_loc_raipur', 'CT', '492001'), ('demo_loc_nagpur', 'MH', '440001'),
                                 ('demo_loc_hyderabad', 'TS', '500032'), ('demo_loc_chennai', 'TN', '600001'),
                                 ('demo_loc_pune', 'MH', '411001'), ('demo_loc_mumbai', 'MH', '400072')):
            ref('cargo_pilot_tms.%s' % xmlid).write({'state_id': states[code].id, 'pincode': pin})

        # Diesel escalation on the Vega contract and a festive peak
        Diesel = self.env['cp.diesel.price']
        for days, price in ((75, 89.6), (45, 90.4), (15, 92.1), (1, 93.8)):
            Diesel.create({'date': today - timedelta(days=days), 'price': price})
        vega = ref('cargo_pilot_tms.demo_contract_vega')
        vega.write({'diesel_clause': True, 'base_diesel_price': 89.6, 'fuel_component_pct': 40,
                    'diesel_threshold_pct': 2})
        self.env['cp.rate.season'].create({'contract_id': vega.id, 'name': 'Diwali peak',
                                           'date_from': today + timedelta(days=10),
                                           'date_to': today + timedelta(days=30), 'surcharge_pct': 8})

        # Part-load (LTL) rate card by destination state
        ltl = self.env['cp.rate.contract'].create({
            'transporter_id': ref('cargo_pilot_tms.demo_tr_deccan').id,
            'date_start': today - timedelta(days=30), 'date_end': today + timedelta(days=330), 'priority': 5,
            'line_ids': [
                (0, 0, {'mode': 'ltl', 'dest_state_id': states['TS'].id, 'charge_basis': 'kg', 'rate': 7.5,
                        'min_weight_kg': 100, 'min_charge': 1200, 'docket_charge': 150, 'fov_pct': 0.1,
                        'fuel_surcharge_pct': 12, 'oda_charge': 750, 'transit_days': 3, 'weekly_capacity': 40}),
                (0, 0, {'mode': 'ltl', 'dest_state_id': states['MH'].id, 'charge_basis': 'kg', 'rate': 8.5,
                        'min_weight_kg': 100, 'min_charge': 1400, 'docket_charge': 150, 'fov_pct': 0.1,
                        'fuel_surcharge_pct': 12, 'oda_charge': 750, 'transit_days': 4}),
                (0, 0, {'mode': 'ltl', 'dest_state_id': states['TN'].id, 'charge_basis': 'box', 'rate': 260,
                        'min_charge': 1500, 'docket_charge': 150, 'transit_days': 5}),
            ]})
        ltl.action_activate()

        # Channel rules
        Rule = self.env['cp.channel.rule']
        Rule.create({'name': 'Modern retail DCs', 'channel': 'modern_retail', 'promise_hours': 72,
                     'appointment_required': True, 'grn_required': True, 'serial_scan_required': True,
                     'note': 'Book the DC slot 24 h ahead. Pallets with ASN copy.'})
        Rule.create({'name': 'Marketplace FCs', 'channel': 'marketplace', 'promise_hours': 48,
                     'appointment_required': True, 'serial_scan_required': True})
        Rule.create({'name': 'Distributors', 'channel': 'offline', 'promise_hours': 96})

        # Orders on the running and delivered trucks
        trip1 = ref('cargo_pilot_tms.demo_trip_1')
        kaveri = ref('cargo_pilot_tms.demo_cust_kaveri')
        trip1.write({
            'channel': 'modern_retail', 'appointment_required': True, 'grn_required': True,
            'appointment_ref': 'RRL-HYD-DC-4471', 'appointment_start': now + timedelta(hours=9),
            'appointment_end': now + timedelta(hours=11),
            'order_ids': [
                (0, 0, {'order_ref': 'SO/2026/0412', 'invoice_ref': 'INV/26-27/1182', 'customer_id': kaveri.id,
                        'description': 'Solar pump 3 HP', 'quantity': 40, 'weight_kg': 7200, 'order_value': 1240000,
                        'serial_numbers': '\n'.join('SP3-%04d' % i for i in range(101, 111)),
                        'eway_bill_no': '351044712290'}),
                (0, 0, {'order_ref': 'SO/2026/0415', 'invoice_ref': 'INV/26-27/1186', 'customer_id': kaveri.id,
                        'description': 'Controller units', 'quantity': 120, 'weight_kg': 4800, 'order_value': 610000,
                        'eway_bill_no': '351044712291'}),
                (0, 0, {'order_ref': 'SO/2026/0419', 'invoice_ref': 'INV/26-27/1190', 'customer_id': kaveri.id,
                        'description': 'Mounting structures', 'quantity': 60, 'weight_kg': 2000, 'order_value': 180000}),
            ]})
        ref('cargo_pilot_tms.demo_trip_2').write({
            'channel': 'offline',
            'order_ids': [(0, 0, {'order_ref': 'SO/2026/0388', 'invoice_ref': 'INV/26-27/1101',
                                  'customer_id': kaveri.id, 'quantity': 280, 'weight_kg': 14000,
                                  'order_value': 980000})]})

        # One part load on the road with a docket
        lane = ref('cargo_pilot_tms.demo_lane_rpr_hyd')
        line = ltl.line_ids.filtered(lambda l: l.dest_state_id == states['TS'])
        ltl_trip = self.env['cp.trip'].with_context(cp_no_offer=True).create({
            'lane_id': lane.id, 'shipment_mode': 'ltl', 'channel': 'marketplace',
            'transporter_id': ref('cargo_pilot_tms.demo_tr_deccan').id, 'contract_line_id': line.id,
            'docket_number': 'DKN-58812', 'tracking_method': 'carrier',
            'planned_dispatch': now - timedelta(hours=26), 'promised_delivery': now + timedelta(hours=40),
            'agreed_freight': line.cp_compute_amount(weight_ton=0.62, goods_value=410000),
            'loaded_weight_ton': 0.62,
            'order_ids': [(0, 0, {'order_ref': 'AMZ-402-7781120', 'invoice_ref': 'INV/26-27/1201', 'quantity': 14,
                                  'weight_kg': 380, 'order_value': 260000, 'channel': 'marketplace'}),
                          (0, 0, {'order_ref': 'FK-OD32118890', 'invoice_ref': 'INV/26-27/1203', 'quantity': 9,
                                  'weight_kg': 240, 'order_value': 150000, 'channel': 'marketplace'})]})
        ltl_trip.action_assign()
        ltl_trip.action_dispatch()

        # Capacity plan for the next quarter
        plan = self.env['cp.capacity.plan'].create({'name': 'Q3 truck plan', 'weeks': 12, 'growth_pct': 15})
        plan.action_suggest()
        return True
