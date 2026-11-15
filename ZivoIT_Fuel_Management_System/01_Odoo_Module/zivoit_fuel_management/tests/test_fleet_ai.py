# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from .common import PumpCommon


@tagged('post_install', '-at_install', 'bpcl')
class TestFleetAi(PumpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        brand = env['fleet.vehicle.model.brand'].create({'name': 'TestBrand'})
        model = env['fleet.vehicle.model'].create({'name': 'Tanker X', 'brand_id': brand.id})
        cls.driver = env['hr.employee'].create({'name': 'Driver T', 'pump_role': 'driver'})
        cls.tanker = env['fleet.vehicle'].create({
            'license_plate': 'TS00TK0001', 'model_id': model.id, 'is_pump_fleet': True, 'pump_role': 'tanker',
            'pump_driver_emp_id': cls.driver.id,
            'pump_compartment_ids': [(0, 0, {'name': 'C1', 'capacity': 6000}), (0, 0, {'name': 'C2', 'capacity': 6000})]})
        cls.bowser = env['fleet.vehicle'].create({
            'license_plate': 'TS00BW0002', 'model_id': model.id, 'is_pump_fleet': True, 'pump_role': 'bowser',
            'pump_compartment_ids': [(0, 0, {'name': 'C1', 'capacity': 4000})]})

    # ---------------------------------------------------------------- fleet
    def test_01_capacity_and_documents(self):
        self.assertEqual(self.tanker.pump_capacity, 12000)
        doc = self.env['pump.vehicle.document'].create({'vehicle_id': self.tanker.id, 'doc_type': 'insurance',
                                                        'expiry_date': self.today + timedelta(days=10)})
        self.assertEqual(doc.state, 'expiring')
        doc2 = self.env['pump.vehicle.document'].create({'vehicle_id': self.tanker.id, 'doc_type': 'puc',
                                                         'expiry_date': self.today + timedelta(days=200)})
        self.assertEqual(doc2.state, 'valid')
        found = self.env['pump.vehicle.document'].search([('state', 'in', ('expiring', 'expired'))])
        self.assertIn(doc, found)
        self.assertNotIn(doc2, found)
        self.assertEqual(self.tanker.pump_doc_alert, 'expiring')
        created = self.env['fleet.vehicle']._cron_pump_fleet_alerts()
        self.assertGreaterEqual(created, 1)
        self.assertTrue(doc.activity_ids)

    def test_02_depot_trip_creates_receipt_and_expenses(self):
        trip = self.env['pump.tanker.trip'].create({
            'trip_type': 'depot', 'vehicle_id': self.tanker.id, 'product_id': self.hsd.id, 'qty': 8000,
            'delivered_qty': 7990, 'dest_tank_id': self.tank.id, 'load_rate': 90.0, 'invoice_no': 'D1',
            'odometer_start': 1000, 'expense_ids': [(0, 0, {'expense_type': 'toll', 'amount': 300})]})
        trip.action_start()
        self.tanker.invalidate_recordset()
        self.assertEqual(self.tanker.pump_status, 'on_trip')
        with self.assertRaises(UserError):     # same tanker cannot start a second trip
            self.env['pump.tanker.trip'].create({'trip_type': 'depot', 'vehicle_id': self.tanker.id,
                                                 'product_id': self.hsd.id, 'qty': 100}).action_start()
        trip.action_deliver()
        self.assertEqual(trip.purchase_id.state, 'posted')
        self.tank.invalidate_recordset()
        self.assertAlmostEqual(self.tank.stock_qty, 10000 + 7990)
        trip.odometer_end = 1140
        trip.action_close()
        self.assertEqual(trip.state, 'closed')
        self.assertAlmostEqual(trip.distance, 140)
        self.assertAlmostEqual(self._balance(self._acc('acc_trip_expense')), 300)
        self.assertEqual(self.tanker.odometer, 1140)

    def test_03_capacity_constraint_and_blocking_documents(self):
        with self.assertRaises(ValidationError):
            self.env['pump.tanker.trip'].create({'trip_type': 'depot', 'vehicle_id': self.tanker.id,
                                                 'product_id': self.hsd.id, 'qty': 15000})
        self.env['pump.vehicle.document'].create({'vehicle_id': self.tanker.id, 'doc_type': 'peso',
                                                  'expiry_date': self.today - timedelta(days=1)})
        trip = self.env['pump.tanker.trip'].create({'trip_type': 'depot', 'vehicle_id': self.tanker.id,
                                                    'product_id': self.hsd.id, 'qty': 1000})
        with self.assertRaises(UserError):
            trip.action_start()

    def test_04_doorstep_delivery(self):
        trip = self.env['pump.tanker.trip'].create({
            'trip_type': 'delivery', 'vehicle_id': self.bowser.id, 'product_id': self.hsd.id, 'qty': 300,
            'partner_id': self.customer.id, 'source_tank_id': self.tank.id})
        trip.action_start()
        trip.action_deliver()
        slip = trip.sale_id
        self.assertEqual(slip.source, 'delivery')
        self.assertEqual(slip.state, 'confirmed')
        self.assertAlmostEqual(slip.amount, 300 * 95.0)
        self.tank.invalidate_recordset()
        self.assertAlmostEqual(self.tank.stock_qty, 9700)
        self.assertAlmostEqual(self._balance(self._acc('acc_credit_clearing')), 28500)
        self.assertAlmostEqual(self._balance(self._acc('acc_sale_hsd')), -28500)
        # shift sheet of the same day must not count the delivery slip as nozzle credit
        sheet = self.env['pump.shift.sheet'].create({'date': trip.date, 'shift_id': self.shift1.id})
        sheet._compute_totals()
        self.assertEqual(sheet.credit_amount, 0.0)
        # billing still clears it
        bill = self.env['pump.bill'].generate_bills(trip.date, trip.date, self.customer)
        self.assertAlmostEqual(bill.amount_total, 28500)
        self.assertAlmostEqual(self._balance(self._acc('acc_credit_clearing')), 0.0)

    def test_05_fleet_fuel_log(self):
        self.env['pump.purchase'].create({'date': self.d0, 'tank_id': self.tank.id, 'qty': 1000, 'rate': 90.0}).action_post()
        l1 = self.env['pump.fleet.fuel.log'].create({'vehicle_id': self.tanker.id, 'tank_id': self.tank.id,
                                                     'qty': 100, 'odometer': 5000})
        l1.action_post()
        self.assertEqual(l1.rate, 90.0, 'cost rate = last purchase rate')
        l2 = self.env['pump.fleet.fuel.log'].create({'vehicle_id': self.tanker.id, 'tank_id': self.tank.id,
                                                     'qty': 100, 'odometer': 5360})
        l2.action_post()
        self.assertAlmostEqual(l2.kmpl, 3.6)
        self.assertAlmostEqual(self._balance(self._acc('acc_fleet_fuel')), 18000)
        self.tank.invalidate_recordset()
        self.assertAlmostEqual(self.tank.stock_qty, 11000 - 200)
        bad = self.env['pump.fleet.fuel.log'].create({'vehicle_id': self.tanker.id, 'tank_id': self.tank.id,
                                                      'qty': 10, 'odometer': 5000})
        with self.assertRaises(UserError):
            bad.action_post()

    def test_06_maintenance_schedule_and_job(self):
        svc = self.env.ref('zivoit_fuel_management.svc_engine_oil')
        self.env['fleet.vehicle.odometer'].create({'vehicle_id': self.tanker.id, 'value': 20000})
        sch = self.env['pump.maintenance.schedule'].create({
            'vehicle_id': self.tanker.id, 'service_type_id': svc.id, 'interval_km': 15000, 'interval_days': 120,
            'last_done_km': 4000, 'last_done_date': self.today - timedelta(days=30)})
        self.assertEqual(sch.next_due_km, 19000)
        self.assertEqual(sch.due_state, 'overdue')
        job = self.env['pump.maintenance.order'].create({
            'vehicle_id': self.tanker.id, 'schedule_id': sch.id, 'service_type_id': svc.id, 'odometer': 20000,
            'paid_from': 'cash', 'line_ids': [(0, 0, {'name': 'Oil', 'qty': 2, 'unit_price': 1500})]})
        job.action_start()
        self.tanker.invalidate_recordset()
        self.assertEqual(self.tanker.pump_status, 'maintenance')
        with self.assertRaises(UserError):  # cannot dispatch a vehicle in the workshop
            self.env['pump.tanker.trip'].create({'trip_type': 'depot', 'vehicle_id': self.tanker.id,
                                                 'product_id': self.hsd.id, 'qty': 100}).action_start()
        job.action_done()
        self.assertEqual(job.amount_total, 3000)
        self.assertEqual(job.service_log_id.amount, 3000)
        self.assertEqual(sch.last_done_km, 20000)
        sch.invalidate_recordset()
        self.assertEqual(sch.due_state, 'ok')
        self.assertAlmostEqual(self._balance(self._acc('acc_fleet_maintenance')), 3000)
        empty = self.env['pump.maintenance.order'].create({'vehicle_id': self.tanker.id, 'service_type_id': svc.id})
        with self.assertRaises(UserError):
            empty.action_done()

    # ---------------------------------------------------------------- billing
    def test_07_bill_payment_status_fifo(self):
        self._slip(100, date=self.d0)
        b1 = self.env['pump.bill'].generate_bills(self.d0, self.d0, self.customer, bill_date=self.d0)
        self._slip(100, date=self.d0 + timedelta(days=1))
        b2 = self.env['pump.bill'].generate_bills(self.d0 + timedelta(days=1), self.d0 + timedelta(days=1), self.customer,
                                                  bill_date=self.d0 + timedelta(days=1))
        self.customer.pump_credit_days = 1
        self.env['pump.quick.voucher.wizard'].create({'voucher_type': 'receipt', 'partner_id': self.customer.id,
                                                      'amount': 12000, 'date': self.d0 + timedelta(days=2)}).action_confirm()
        (b1 | b2).invalidate_recordset()
        self.assertEqual(b1.payment_state, 'paid')
        self.assertAlmostEqual(b2.amount_paid, 2500)
        self.assertAlmostEqual(b2.amount_due, 7000)
        self.assertEqual(b2.payment_state, 'overdue')
        summary = b2.account_summary()
        self.assertAlmostEqual(summary['total'], b2.previous_balance + 9500)
        rows = b2.vehicle_summary()
        self.assertEqual(rows[0]['vehicle'], 'TS01AB1234')
        self.env.company.pump_upi_id = 'test@upi'
        html, _t = self.env['ir.actions.report']._render_qweb_html('zivoit_fuel_management.action_report_bill', b2.ids)
        self.assertIn(b'Amount Payable', html)
        self.assertIn(b'Vehicle-wise Summary', html)
        self.assertIn(b'Scan to pay by UPI', html, 'UPI QR embedded')
        self.assertTrue(b2.upi_qr_data_uri(100).startswith('data:image/png;base64,'))

    # ---------------------------------------------------------------- AI
    def test_08_ai_anomalies_and_risk(self):
        s1 = self._slip(100, date=self.d0)
        s2 = self._slip(50, date=self.d0)
        flags = self.env['pump.ai']._slip_flags(s1 | s2)
        self.assertIn('same day', flags[s1.id])
        big = self._slip(650, date=self.d0 + timedelta(days=1), confirm=False)
        with self.assertRaises(UserError):   # quantity above the vehicle's tank is blocked
            big.action_confirm()
        risk = self.env['pump.ai'].customer_risk(self.customer)
        self.assertIn(risk['grade'], ('A', 'B', 'C', 'D'))
        self.assertTrue(0 <= risk['score'] <= 100)

    def test_09_ai_stock_forecast_and_insights(self):
        sheet = self.env['pump.shift.sheet'].create({'date': self.today, 'shift_id': self.shift1.id})
        sheet.line_ids.filtered(lambda l: l.nozzle_id == self.n1).closing = 1000 + 9000
        sheet.action_post()
        fc = {f['tank'].id: f for f in self.env['pump.ai'].tank_forecast()}
        self.assertLess(fc[self.tank.id]['days_left'], 2)
        self.assertEqual(fc[self.tank.id]['level'], 'danger')
        titles = ' '.join(i['title'] for i in self.env['pump.ai'].station_insights())
        self.assertIn('T-HSD', titles)

    def test_10_ai_assistant_customer(self):
        self._slip(100, date=self.today)
        Ai = self.env['pump.ai']
        r = Ai.ask('What is my balance?', partner=self.customer)
        self.assertEqual(r['source'], 'builtin')
        self.assertIn('outstanding', r['answer'].lower())
        r = Ai.ask('How much diesel did TS01AB1234 take this month?', partner=self.customer)
        self.assertIn('100.00 L', r['answer'])
        r = Ai.ask('book 50 litres diesel for TS01AB1234', partner=self.customer)
        self.assertTrue(any('/my/fuel/indent?' in l['url'] and 'qty=50' in l['url'] for l in r['links']))
        r = Ai.ask("today's diesel rate", partner=self.customer)
        self.assertIn('95.00', r['answer'])
        r = Ai.ask('How much do I owe?', partner=self.customer)
        self.assertIn('outstanding', r['answer'].lower())
        r = Ai.ask('statement for last month', partner=self.customer)
        self.assertTrue(any('/ledger/pdf' in l['url'] for l in r['links']))
        r = Ai.ask('xyzzy', partner=self.customer)
        self.assertIn('help', r['answer'].lower())
        self.assertGreaterEqual(self.env['pump.ai.log'].search_count([('partner_id', '=', self.customer.id)]), 7)
        r = Ai.ask('stock days left', mode='staff')
        self.assertIn('T-HSD', r['answer'])

    def test_11_ai_access_control(self):
        portal = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Test Haulage Co', 'login': 'haul_portal', 'partner_id': self.customer.id,
            'group_ids': [(6, 0, [self.env.ref('base.group_portal').id])]})
        with self.assertRaises(AccessError):
            self.env['pump.ai'].with_user(portal).station_insights()
        with self.assertRaises(AccessError):
            self.env['pump.dashboard'].with_user(portal).get_dashboard_data()
        with self.assertRaises(AccessError):
            self.other.with_user(portal).sudo(False).pump_ledger(self.d0, self.today)
        own = self.customer.with_user(portal).pump_ledger(self.d0, self.today)
        self.assertIn('lines', own)
