# -*- coding: utf-8 -*-
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import CpCommon


@tagged('post_install', '-at_install', 'cargo_pilot')
class TestTransporterFlow(CpCommon):

    def _lr(self, partner, to_loc, packages=10, weight=1000.0, rate=5.0, basis='kg'):
        lr = self.env['cp.consignment'].create({
            'consignor_id': partner.id, 'consignee_id': self.customer.id, 'billing_partner_id': partner.id,
            'load_type': 'ptl', 'booking_hub_id': self.hub_a.id,
            'from_location_id': self.hub_a.id, 'to_location_id': to_loc.id,
            'freight_basis': basis, 'freight_rate': rate, 'loading_charge': 100,
            'line_ids': [(0, 0, {'description': 'Boxes', 'packages': packages,
                                 'actual_weight_kg': weight, 'charged_weight_kg': weight})],
        })
        lr.action_book()
        return lr

    def test_01_consignment_amounts_and_invoice(self):
        party = self.env['res.partner'].create({'name': 'Shipper One'})
        lr1 = self._lr(party, self.hub_b, weight=1000, rate=5.0)
        lr2 = self._lr(party, self.hub_b, packages=4, rate=200.0, basis='package')
        self.assertEqual(lr1.freight_amount, 5000)
        self.assertEqual(lr1.total_amount, 5100)
        self.assertEqual(lr2.freight_amount, 800)
        self.assertEqual(lr1.current_hub_id, self.hub_a)
        (lr1 | lr2).action_create_invoice()
        self.assertEqual(lr1.invoice_id, lr2.invoice_id)
        self.assertEqual(lr1.state, 'invoiced')
        self.assertEqual(len(lr1.invoice_id.invoice_line_ids), 2)
        self.assertAlmostEqual(lr1.invoice_id.amount_untaxed, 5100 + 900)

    def test_02_challan_moves_hub_stock(self):
        party = self.env['res.partner'].create({'name': 'Shipper Two'})
        lr1 = self._lr(party, self.hub_b)
        lr2 = self._lr(party, self.hub_b)
        challan = self.env['cp.challan'].create({'from_hub_id': self.hub_a.id, 'to_hub_id': self.hub_b.id})
        challan.action_load_pending()
        self.assertEqual(challan.consignment_ids, lr1 | lr2)
        challan.action_dispatch()
        self.assertEqual(lr1.state, 'in_transit')
        self.assertFalse(lr1.current_hub_id)
        challan.action_receive()
        self.assertEqual(lr1.state, 'at_hub')
        self.assertEqual(lr1.current_hub_id, self.hub_b)
        # Last-mile run delivers them
        last_mile = self.env['cp.challan'].create({'from_hub_id': self.hub_b.id, 'to_hub_id': self.hub_b.id,
                                                   'is_last_mile': True})
        last_mile.action_load_pending()
        last_mile.action_dispatch()
        last_mile.action_receive()
        self.assertEqual(lr1.state, 'delivered')

    def test_03_quick_lr_parser(self):
        party = self.env['res.partner'].create({'name': 'Acme Steel'})
        quick = self.env['cp.consignment.quick'].create({
            'instruction': 'LR for Acme Steel, 10 tons of steel coils, 12 packages, from Hub A to Hub B, rate 2200 per ton'})
        quick.action_parse()
        self.assertEqual(quick.billing_partner_id, party)
        self.assertEqual(quick.from_location_id, self.hub_a)
        self.assertEqual(quick.to_location_id, self.hub_b)
        self.assertEqual(quick.weight_kg, 10000)
        self.assertEqual(quick.packages, 12)
        self.assertEqual(quick.freight_basis, 'ton')
        self.assertEqual(quick.freight_rate, 2200)
        action = quick.action_create()
        lr = self.env['cp.consignment'].browse(action['res_id'])
        self.assertEqual(lr.freight_amount, 22000)

    def test_04_hire_slip_and_trip_pnl(self):
        owner = self.env['res.partner'].create({'name': 'Owner', 'cp_is_vehicle_owner': True})
        trip = self.env['cp.trip'].create({'lane_id': self.lane.id, 'vehicle_number': 'MH12 XX 1', 'agreed_freight': 60000})
        slip = self.env['cp.hire.slip'].create({'trip_id': trip.id, 'owner_id': owner.id, 'hire_amount': 45000,
                                                'detention_amount': 2000, 'deduction_amount': 500})
        self.assertTrue(trip.is_market_vehicle)
        self.assertEqual(slip.vehicle_number, 'MH12 XX 1')
        self.assertEqual(slip.total_hire, 46500)
        slip.action_confirm()
        slip.write({'payment_ids': [(0, 0, {'payment_type': 'advance', 'amount': 30000})]})
        self.assertEqual(slip.balance_due, 16500)
        with self.assertRaises(UserError):
            slip.action_settle()
        slip.write({'payment_ids': [(0, 0, {'payment_type': 'balance', 'amount': 16500})]})
        slip.action_settle()
        self.assertEqual(slip.state, 'settled')
        slip.action_create_bill()
        self.assertEqual(slip.move_id.amount_untaxed, 46500)
        self.env['cp.trip.expense'].create({'trip_id': trip.id, 'expense_type': 'toll', 'amount': 3500})
        self.env['cp.trip.expense'].create({'trip_id': trip.id, 'expense_type': 'driver_advance', 'amount': 5000})
        self.assertEqual(trip.revenue_amount, 60000)
        self.assertEqual(trip.profit_amount, 60000 - 46500 - 3500)

    def test_05_fuel_anomaly_and_maintenance(self):
        brand = self.env['fleet.vehicle.model.brand'].create({'name': 'B'})
        model = self.env['fleet.vehicle.model'].create({'name': 'M', 'brand_id': brand.id})
        vehicle = self.env['fleet.vehicle'].create({'model_id': model.id, 'license_plate': 'T1',
                                                    'cp_mileage_benchmark': 4.0})
        Fuel = self.env['cp.fuel.log']
        Fuel.create({'vehicle_id': vehicle.id, 'odometer': 1000, 'litres': 100})
        ok = Fuel.create({'vehicle_id': vehicle.id, 'odometer': 1400, 'litres': 100})
        bad = Fuel.create({'vehicle_id': vehicle.id, 'odometer': 1700, 'litres': 100})
        self.assertAlmostEqual(ok.mileage_kmpl, 4.0)
        self.assertFalse(ok.is_anomaly)
        self.assertAlmostEqual(bad.mileage_kmpl, 3.0)
        self.assertTrue(bad.is_anomaly)
        vehicle.invalidate_recordset(['odometer'])
        self.assertEqual(vehicle.odometer, 1700)

        plan = self.env['cp.maintenance.plan'].create({'vehicle_id': vehicle.id, 'name': 'Oil', 'interval_km': 500,
                                                       'last_done_odometer': 1000})
        self.assertEqual(plan.due_state, 'overdue')
        self.env['cp.maintenance.plan']._cron_maintenance_due()
        self.assertTrue(vehicle.activity_ids)
        plan.action_mark_done()
        self.assertEqual(plan.last_done_odometer, 1700)
        self.assertTrue(self.env['fleet.vehicle.log.services'].search([('vehicle_id', '=', vehicle.id)]))

    def test_06_tyre_and_documents(self):
        brand = self.env['fleet.vehicle.model.brand'].create({'name': 'B2'})
        model = self.env['fleet.vehicle.model'].create({'name': 'M2', 'brand_id': brand.id})
        vehicle = self.env['fleet.vehicle'].create({'model_id': model.id, 'license_plate': 'T2'})
        self.env['fleet.vehicle.odometer'].create({'vehicle_id': vehicle.id, 'value': 5000})
        tyre = self.env['cp.tyre'].create({'name': 'SER-1', 'vehicle_id': vehicle.id, 'position': 'fl',
                                           'expected_life_km': 10000, 'cost': 20000})
        vehicle.invalidate_recordset(['odometer'])
        tyre.action_fit()
        self.env['fleet.vehicle.odometer'].create({'vehicle_id': vehicle.id, 'value': 7500})
        vehicle.invalidate_recordset(['odometer'])
        tyre.invalidate_recordset()
        self.assertEqual(tyre.km_run, 2500)
        self.assertEqual(tyre.wear_pct, 25)
        doc = self.env['cp.document'].create({'vehicle_id': vehicle.id, 'doc_type': 'insurance',
                                              'expiry_date': '2000-01-01'})
        self.assertEqual(doc.status, 'expired')
        self.env['cp.document']._cron_document_expiry()
        self.assertTrue(vehicle.activity_ids)

    def test_07_safety_score(self):
        driver = self.env['res.partner'].create({'name': 'Driver', 'cp_is_driver': True})
        brand = self.env['fleet.vehicle.model.brand'].create({'name': 'B3'})
        model = self.env['fleet.vehicle.model'].create({'name': 'M3', 'brand_id': brand.id})
        vehicle = self.env['fleet.vehicle'].create({'model_id': model.id, 'license_plate': 'T3', 'driver_id': driver.id})
        self.env['cp.driving.event'].create({'vehicle_id': vehicle.id, 'event_type': 'collision', 'severity': 'high'})
        self.assertEqual(driver.cp_safety_score, 80)
