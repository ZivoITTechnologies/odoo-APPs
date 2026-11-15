# -*- coding: utf-8 -*-
"""Demo data generator: ~75 days of realistic activity for a BPCL outlet.

All customer, employee and vehicle names are fictional.
"""
import base64
import math
import random
from datetime import timedelta

from odoo import api, fields, models
from odoo.tools.misc import file_open

DEMO_DAYS = 75


class PumpDemo(models.AbstractModel):
    _name = 'pump.demo'
    _description = 'Fuel Station Demo Data Loader'

    # ------------------------------------------------------------------ helpers
    @api.model
    def _dip_chart(self, capacity, diameter_mm=2400, step=100):
        r = diameter_mm / 200.0                      # radius in dm
        length = capacity / (math.pi * r * r)        # dm  (1 dm3 = 1 L)
        rows = []
        h_mm = step
        while h_mm <= diameter_mm:
            h = h_mm / 100.0
            area = r * r * math.acos((r - h) / r) - (r - h) * math.sqrt(max(2 * r * h - h * h, 0))
            rows.append((0, 0, {'dip_mm': h_mm, 'volume': round(area * length, 1)}))
            h_mm += step
        return rows

    @api.model
    def load_demo(self):
        if self.env['pump.tank'].search_count([]):
            return True
        rnd = random.Random(2026)
        env = self.env
        ref = lambda x: env.ref('zivoit_fuel_management.%s' % x)
        today = fields.Date.context_today(self)
        start = today - timedelta(days=DEMO_DAYS - 1)

        # ---------------- company
        company = env.company
        vals = {
            'name': 'BPCL - Sri Lakshmi Narasimha Fuels',
            'pump_outlet_name': 'Sri Lakshmi Narasimha Fuels',
            'pump_dealer_code': 'RO-HYD-24817',
            'street': 'Plot 12, Survey No. 44, NH-163', 'city': 'Hyderabad', 'zip': '500039',
            'state_id': env.ref('base.state_in_ts', raise_if_not_found=False).id if env.ref('base.state_in_ts', raise_if_not_found=False) else False,
            'country_id': env.ref('base.in').id, 'phone': '+91 40 2720 0000', 'email': 'office@lakshmifuels.example.com',
            'pump_bank_name': 'State Bank of India', 'pump_bank_account': '3900 0000 4521',
            'pump_bank_ifsc': 'SBIN0012345', 'pump_bank_branch': 'Uppal Branch, Hyderabad',
            'pump_upi_id': 'lakshmifuels@sbi',
        }
        try:
            with file_open('zivoit_fuel_management/static/src/img/bpcl_logo.png', 'rb') as f:
                vals['logo'] = base64.b64encode(f.read())
        except OSError:
            pass
        company.write(vals)
        ref('partner_bpcl_supplier').name = 'Bharat Petroleum Corporation Ltd.'
        inr = env.ref('base.INR')
        inr.active = True
        if 'account.move' not in env and company.currency_id != inr:
            company.currency_id = inr

        # ---------------- rates
        ms, hsd, spd = ref('product_ms'), ref('product_hsd'), ref('product_speed')
        lube1, lube2 = ref('product_lube_4t'), ref('product_lube_15w40')
        Rate = env['pump.rate']
        for prod, rate in ((ms, 107.46), (hsd, 95.65), (spd, 115.10), (lube1, 425.0), (lube2, 1650.0)):
            Rate.create({'product_id': prod.id, 'date': start, 'rate': rate})
        Rate.create({'product_id': spd.id, 'date': start + timedelta(days=21), 'rate': 115.35, 'note': 'Revision'})
        Rate.create({'product_id': hsd.id, 'date': start + timedelta(days=44), 'rate': 95.70, 'note': 'Revision'})
        Rate.create({'product_id': ms.id, 'date': today - timedelta(days=6), 'rate': 107.41, 'note': 'Revision'})

        # ---------------- tanks & nozzles
        Tank = env['pump.tank']
        t_ms = Tank.create({'name': 'Tank 1 - MS', 'product_id': ms.id, 'capacity': 20000, 'dead_stock': 600,
                            'reorder_level': 6000, 'opening_stock': 15200, 'opening_date': start,
                            'dip_chart_ids': self._dip_chart(20000)})
        t_hsd1 = Tank.create({'name': 'Tank 2 - HSD', 'product_id': hsd.id, 'capacity': 20000, 'dead_stock': 600,
                              'reorder_level': 6000, 'opening_stock': 16400, 'opening_date': start,
                              'dip_chart_ids': self._dip_chart(20000)})
        t_hsd2 = Tank.create({'name': 'Tank 3 - HSD', 'product_id': hsd.id, 'capacity': 15000, 'dead_stock': 500,
                              'reorder_level': 4500, 'opening_stock': 11800, 'opening_date': start,
                              'dip_chart_ids': self._dip_chart(15000, 2100)})
        t_spd = Tank.create({'name': 'Tank 4 - Speed', 'product_id': spd.id, 'capacity': 9000, 'dead_stock': 300,
                             'reorder_level': 2500, 'opening_stock': 6900, 'opening_date': start,
                             'dip_chart_ids': self._dip_chart(9000, 1800)})
        (t_ms | t_hsd1 | t_hsd2 | t_spd).action_post_opening()
        Nozzle = env['pump.nozzle']
        nozzles = Nozzle.browse()
        spec = [('MPD-1 / N1 MS', 'MPD-1', t_ms, 184520.0), ('MPD-1 / N2 HSD', 'MPD-1', t_hsd1, 402115.5),
                ('MPD-2 / N3 MS', 'MPD-2', t_ms, 166044.2), ('MPD-2 / N4 HSD', 'MPD-2', t_hsd2, 388710.0),
                ('MPD-3 / N5 Speed', 'MPD-3', t_spd, 52380.4), ('MPD-3 / N6 HSD', 'MPD-3', t_hsd1, 275903.8)]
        for seq, (name, mpd, tank, meter) in enumerate(spec, 1):
            nozzles |= Nozzle.create({'name': name, 'dispenser': mpd, 'tank_id': tank.id,
                                      'opening_meter': meter, 'sequence': seq})
        # typical litres per shift per nozzle
        base_qty = {nozzles[0].id: 520, nozzles[1].id: 900, nozzles[2].id: 470,
                    nozzles[3].id: 760, nozzles[4].id: 120, nozzles[5].id: 680}

        # ---------------- staff
        Emp = env['hr.employee']
        staff = [('Suresh Reddy', 'manager', 18000, 4000, 3000), ('Anil Goud', 'cashier', 13000, 2500, 2000),
                 ('Mahesh Yadav', 'dsm', 10500, 1500, 1500), ('Prakash Naik', 'dsm', 10500, 1500, 1500),
                 ('Venkatesh Rao', 'dsm', 10500, 1500, 1500), ('Imran Shaikh', 'dsm', 10500, 1500, 1500),
                 ('Ramesh Kumar', 'cleaner', 8500, 1000, 1000)]
        employees = Emp.browse()
        for name, role, basic, da, hra in staff:
            employees |= Emp.create({'name': name, 'pump_role': role, 'pump_basic': basic, 'pump_da': da,
                                     'pump_hra': hra, 'mobile_phone': '+91 9%09d' % rnd.randint(0, 999999999)})
        manager = employees[0]
        dsms = employees.filtered(lambda e: e.pump_role == 'dsm')
        drivers = Emp.browse()
        for name in ('Narsimha Chary', 'Srikanth Goud', 'Bhaskar Rao'):
            drivers |= Emp.create({'name': name, 'pump_role': 'driver', 'pump_basic': 14000, 'pump_da': 2000, 'pump_hra': 1500})
        employees |= drivers

        # ---------------- own fleet (Odoo Fleet)
        Brand, Model, Veh = env['fleet.vehicle.model.brand'], env['fleet.vehicle.model'], env['fleet.vehicle']
        tata = Brand.create({'name': 'Tata Motors'})
        al = Brand.create({'name': 'Ashok Leyland'})
        mah = Brand.create({'name': 'Mahindra'})
        m_tanker = Model.create({'name': 'Signa 2825.TK Tanker', 'brand_id': tata.id})
        m_bowser = Model.create({'name': 'Ecomet 1215 Bowser', 'brand_id': al.id})
        m_pick = Model.create({'name': 'Bolero Pik-Up', 'brand_id': mah.id})
        fleet_spec = [
            ('TS07UA1101', m_tanker, 'tanker', [('C1', 4000, ms), ('C2', 4000, hsd), ('C3', 4000, hsd)], drivers[0], 84210),
            ('TS07UA1102', m_tanker, 'tanker', [('C1', 6000, hsd), ('C2', 6000, hsd)], drivers[1], 121450),
            ('TS08UB2201', m_bowser, 'bowser', [('C1', 4000, hsd)], drivers[2], 46320),
            ('TS08EC3301', m_pick, 'service', [], drivers[2], 38900),
        ]
        own_fleet = Veh.browse()
        fleet_odo = {}
        for plate, model, role, comps, drv, odo in fleet_spec:
            v = Veh.create({'license_plate': plate, 'model_id': model.id, 'is_pump_fleet': True, 'pump_role': role,
                            'pump_driver_emp_id': drv.id, 'acquisition_date': start - timedelta(days=900),
                            'pump_compartment_ids': [(0, 0, {'name': n, 'capacity': c, 'product_id': pr.id, 'sequence': i})
                                                     for i, (n, c, pr) in enumerate(comps, 1)]})
            env['fleet.vehicle.odometer'].create({'vehicle_id': v.id, 'value': odo, 'date': start})
            own_fleet |= v
            fleet_odo[v.id] = odo
            for dtype, days in (('insurance', 210), ('permit', 300), ('fitness', 160), ('puc', 75), ('road_tax', 400)):
                env['pump.vehicle.document'].create({'vehicle_id': v.id, 'doc_type': dtype, 'number': '%s/%s' % (dtype.upper(), plate[-4:]),
                                                     'issue_date': today - timedelta(days=365 - days), 'expiry_date': today + timedelta(days=days)})
            if role in ('tanker', 'bowser'):
                env['pump.vehicle.document'].create({'vehicle_id': v.id, 'doc_type': 'peso', 'number': 'P/HQ/TS/15/%s' % plate[-4:],
                                                     'expiry_date': today + timedelta(days=12 if plate.endswith('1102') else 240)})
                env['pump.vehicle.document'].create({'vehicle_id': v.id, 'doc_type': 'calibration', 'number': 'WM/%s' % plate[-4:],
                                                     'expiry_date': today + timedelta(days=95)})
            env['pump.maintenance.schedule'].create([
                {'vehicle_id': v.id, 'service_type_id': ref('svc_engine_oil').id, 'interval_km': 15000, 'interval_days': 120,
                 'last_done_date': start - timedelta(days=rnd.randint(40, 100)), 'last_done_km': odo - rnd.randint(4000, 13500)},
                {'vehicle_id': v.id, 'service_type_id': ref('svc_tyre').id, 'interval_km': 20000,
                 'last_done_date': start - timedelta(days=60), 'last_done_km': odo - rnd.randint(5000, 12000)},
            ])
        # one expired PUC on the pick-up (non-blocking document)
        env['pump.vehicle.document'].search([('vehicle_id', '=', own_fleet[3].id), ('doc_type', '=', 'puc')]).expiry_date = today - timedelta(days=3)
        tankers = own_fleet.filtered(lambda v: v.pump_role == 'tanker')
        bowser = own_fleet.filtered(lambda v: v.pump_role == 'bowser')

        # ---------------- customers (fictional)
        Partner = env['res.partner']
        cust_spec = [
            ('Konark Roadlines Pvt. Ltd.', 800000, 'fortnightly', 'Kukatpally, Hyderabad', 'konark.roadlines@example.com',
             [('TS09UB4521', 'truck', hsd, 300), ('TS09UB4522', 'truck', hsd, 300), ('TS08HK7310', 'truck', hsd, 400)], 185000, 1.0),
            ('Deccan Agro Transport', 500000, 'fortnightly', 'Medchal, Telangana', 'accounts@deccanagro.example.com',
             [('TS07UC1188', 'truck', hsd, 250), ('TS07UC1190', 'tractor', hsd, 80)], 96500, 0.95),
            ('Godavari Infra Projects', 1200000, 'monthly', 'Ghatkesar, Hyderabad', 'stores@godavariinfra.example.com',
             [('TS10EX0042', 'genset', hsd, 500), ('TS10JC7781', 'other', hsd, 150), ('TS10TP2290', 'truck', hsd, 350)], 342000, 0.9),
            ('Vayu Cabs & Travels', 200000, 'weekly', 'Secunderabad', 'vayucabs@example.com',
             [('TS11EA3301', 'car', ms, 45), ('TS11EA3302', 'car', ms, 45), ('TS11EB0915', 'car', spd, 50)], 18400, 1.0),
            ('Nilgiri Construction Co.', 600000, 'fortnightly', 'LB Nagar, Hyderabad', 'nilgiri.cc@example.com',
             [('TS12TM6610', 'truck', hsd, 300), ('TS12RM0821', 'other', hsd, 120)], 154000, 0.85),
            ('Sahyadri Milk Dairy', 300000, 'fortnightly', 'Bhongir, Telangana', 'logistics@sahyadridairy.example.com',
             [('TS30TA5150', 'truck', hsd, 180), ('TS30TA5151', 'truck', hsd, 180)], 62000, 1.0),
            ('Shree Ganesh Logistics', 400000, 'fortnightly', 'Nacharam, Hyderabad', 'sgl.accounts@example.com',
             [('AP29TB9932', 'truck', hsd, 350), ('AP29TB9933', 'truck', hsd, 350)], 318000, 0.45),
            ('Pragathi School Transport', 150000, 'monthly', 'Boduppal, Hyderabad', 'pragathi.transport@example.com',
             [('TS08BS1101', 'bus', hsd, 120), ('TS08BS1102', 'bus', hsd, 120)], 41200, 1.0),
        ]
        cust_odo = {}
        customers = []
        for name, limit, cycle, city, email, vehicles, opening, pay_factor in cust_spec:
            p = Partner.create({
                'name': name, 'is_company': True, 'is_pump_customer': True, 'pump_credit_limit': limit,
                'pump_billing_cycle': cycle, 'city': city, 'email': email, 'country_id': env.ref('base.in').id,
                'phone': '+91 9%09d' % rnd.randint(0, 999999999),
                'pump_credit_days': 7 if cycle == 'weekly' else 15,
                'pump_vehicle_ids': [(0, 0, {'name': v, 'vehicle_type': vt, 'product_id': pr.id, 'tank_capacity': cap,
                                             'driver_name': rnd.choice(['Raju', 'Srinu', 'Babu', 'Yadagiri', 'Kiran', 'Naresh', 'Salim'])})
                                     for v, vt, pr, cap in vehicles],
            })
            customers.append({'partner': p, 'pay': pay_factor, 'cycle': cycle})
            env['pump.opening.wizard'].create({'partner_id': p.id, 'date': start, 'amount': opening}).action_confirm()

        # opening cash / bank / capital
        env['pump.voucher']._create_posted([
            {'account_id': ref('acc_cash').id, 'debit': 45000, 'name': 'Opening cash'},
            {'account_id': ref('acc_bank').id, 'debit': 1250000, 'name': 'Opening bank'},
            {'account_id': ref('acc_security_deposit').id, 'debit': 500000, 'name': 'Deposit with BPCL'},
            {'account_id': ref('acc_capital').id, 'credit': 1795000, 'name': 'Capital'},
        ], voucher_type='opening', date=start, narration='Opening balances')

        shifts = ref('shift_1') | ref('shift_2') | ref('shift_3')
        Sale, Sheet = env['pump.credit.sale'], env['pump.shift.sheet']
        pending_pay = {}   # date -> list of (partner, amount, mode)
        slip_counter = 5101
        shift_factor = {1: 1.05, 2: 1.15, 3: 0.55}

        day = start
        while day <= today:
            weekday_factor = 1.12 if day.weekday() in (4, 5) else 1.0
            used_today = set()
            is_today = day == today
            for shift in shifts:
                if is_today and shift.sequence == 3:
                    continue
                # credit slips
                n_slips = rnd.randint(1, 3) if shift.sequence != 3 else rnd.randint(0, 1)
                for _i in range(n_slips):
                    c = rnd.choice(customers)
                    p = c['partner']
                    free = p.pump_vehicle_ids.filtered(lambda x: x.id not in used_today)
                    if not free:
                        continue
                    veh = rnd.choice(free)
                    used_today.add(veh.id)
                    qty = round(veh.tank_capacity * rnd.uniform(0.35, 0.85), 2)
                    true_kmpl = {'truck': 4.2, 'bus': 5.0, 'car': 14.0, 'tractor': 3.0, 'genset': 0, 'other': 3.5}.get(veh.vehicle_type, 4)
                    odo_val = 0.0
                    if true_kmpl:
                        last = cust_odo.get(veh.id, rnd.randint(40000, 180000))
                        k = true_kmpl * rnd.uniform(0.92, 1.08)
                        if veh.name == 'AP29TB9933' and day > today - timedelta(days=12):
                            k = true_kmpl * 0.45   # planted: sudden mileage drop for the AI to catch
                        odo_val = round(last + qty * k, 0)
                        cust_odo[veh.id] = odo_val
                    s = Sale.create({'date': day, 'shift_id': shift.id, 'partner_id': p.id, 'vehicle_id': veh.id,
                                     'product_id': veh.product_id.id, 'qty': qty, 'slip_no': str(slip_counter),
                                     'employee_id': rnd.choice(dsms).id, 'odometer': odo_val})
                    slip_counter += 1
                    s.with_context(pump_override_limit=True).action_confirm()
                if rnd.random() < 0.08:
                    c = rnd.choice(customers)
                    lube = rnd.choice([lube1, lube2])
                    Sale.create({'date': day, 'shift_id': shift.id, 'partner_id': c['partner'].id,
                                 'vehicle_id': c['partner'].pump_vehicle_ids[:1].id, 'product_id': lube.id,
                                 'qty': rnd.randint(1, 4), 'slip_no': str(slip_counter)
                                 }).with_context(pump_override_limit=True).action_confirm()
                    slip_counter += 1
                if is_today and shift.sequence == 2:
                    Sheet.create({'date': day, 'shift_id': shift.id, 'manager_id': manager.id})  # left in draft
                    continue
                sheet = Sheet.create({'date': day, 'shift_id': shift.id, 'manager_id': manager.id})
                for line in sheet.line_ids:
                    qty = base_qty[line.nozzle_id.id] * shift_factor[shift.sequence] * weekday_factor * rnd.uniform(0.82, 1.18)
                    testing = 5.0 if shift.sequence == 1 and line.product_id != spd else 0.0
                    line.write({'closing': round(line.opening + qty + testing, 2), 'testing_qty': testing,
                                'employee_id': rnd.choice(dsms).id})
                sheet._compute_totals()
                non_credit = sheet.total_amount - sheet.credit_amount
                card = round(non_credit * rnd.uniform(0.10, 0.16), 0)
                upi = round(non_credit * rnd.uniform(0.15, 0.24), 0)
                fleet = round(non_credit * rnd.uniform(0.0, 0.05), 0) if rnd.random() < 0.5 else 0.0
                sheet.write({'card_amount': card, 'upi_amount': upi, 'fleet_card_amount': fleet})
                sheet._compute_totals()
                diff = rnd.choice([0, 0, 0, 0, -50, -120, 20, -200])
                sheet.cash_collected = round(sheet.expected_cash + diff, 2)
                sheet.action_post()

            # tanker receipts (morning after) when below 40%
            for tank in (t_ms | t_hsd1 | t_hsd2 | t_spd):
                if tank.fill_percent < 40 and not is_today:
                    qty = 12000 if tank.capacity >= 15000 else 4000
                    if tank.ullage > qty + 200:
                        rate = {ms.id: 101.85, hsd.id: 90.62, spd.id: 109.40}[tank.product_id.id]
                        tk = tankers.filtered(lambda v: v.pump_capacity >= qty)
                        if tk and rnd.random() < 0.7:
                            tk = tk[day.toordinal() % len(tk)]
                            dist = rnd.randint(128, 150)
                            trip = env['pump.tanker.trip'].create({
                                'trip_type': 'depot', 'date': day, 'vehicle_id': tk.id, 'driver_id': tk.pump_driver_emp_id.id,
                                'origin': 'BPCL Cherlapally Terminal', 'destination': 'Station', 'product_id': tank.product_id.id,
                                'qty': qty, 'delivered_qty': qty - rnd.choice([0, 4, 8]), 'dest_tank_id': tank.id, 'load_rate': rate,
                                'invoice_no': 'BPC/%s/%04d' % (day.strftime('%y%m'), rnd.randint(1000, 9999)),
                                'odometer_start': fleet_odo[tk.id],
                                'expense_ids': [(0, 0, {'expense_type': 'toll', 'amount': 285}),
                                                (0, 0, {'expense_type': 'allowance', 'amount': 400}),
                                                (0, 0, {'expense_type': 'loading', 'amount': rnd.choice([0, 150])})]})
                            trip.action_start()
                            trip.action_deliver()
                            fleet_odo[tk.id] += dist
                            trip.odometer_end = fleet_odo[tk.id]
                            trip.action_close()
                            continue
                        pur = env['pump.purchase'].create({
                            'date': day, 'tank_id': tank.id, 'qty': qty, 'received_qty': qty - rnd.choice([0, 5, 10, 12]),
                            'rate': rate, 'invoice_no': 'BPC/%s/%04d' % (day.strftime('%y%m'), rnd.randint(1000, 9999)),
                            'truck_no': rnd.choice(['TS07TC4411', 'TS07TC4415', 'TS15TB2020']), 'paid_from': 'bank'})
                        pur.action_post()

            # doorstep diesel delivery by bowser every ~4 days (Godavari Infra gensets)
            if not is_today and day.toordinal() % 4 == 0:
                cust = customers[2]['partner']
                qty = float(rnd.choice([1200, 1500, 1800, 2200]))
                if t_hsd1.stock_qty - qty > 3000:
                    dist = rnd.randint(36, 52)
                    trip = env['pump.tanker.trip'].create({
                        'trip_type': 'delivery', 'date': day, 'vehicle_id': bowser.id, 'driver_id': bowser.pump_driver_emp_id.id,
                        'origin': 'Station', 'destination': 'Godavari Infra site, Ghatkesar', 'product_id': hsd.id,
                        'qty': qty, 'partner_id': cust.id, 'customer_vehicle_id': cust.pump_vehicle_ids[0].id,
                        'source_tank_id': t_hsd1.id, 'odometer_start': fleet_odo[bowser.id],
                        'expense_ids': [(0, 0, {'expense_type': 'allowance', 'amount': 250})]})
                    trip.with_context(pump_override_limit=True).action_start()
                    trip.action_deliver()
                    fleet_odo[bowser.id] += dist
                    trip.odometer_end = fleet_odo[bowser.id]
                    trip.action_close()
            # own fleet refuels from the station HSD tank every 3 days
            if not is_today and day.toordinal() % 3 == 0:
                for v in own_fleet:
                    fleet_odo[v.id] += rnd.randint(60, 160)
                    kmpl_v = {'service': 11.0, 'tanker': 3.6, 'bowser': 4.4}[v.pump_role]
                    prev_logs = v.pump_fuel_log_ids.filtered(lambda l: l.state == 'posted').sorted('odometer', reverse=True)
                    km = fleet_odo[v.id] - (prev_logs[:1].odometer or fleet_odo[v.id] - 300)
                    qty = round(max(km, 60) / (kmpl_v * rnd.uniform(0.93, 1.07)), 2)
                    log = env['pump.fleet.fuel.log'].create({'date': day, 'vehicle_id': v.id, 'source': 'station',
                                                             'tank_id': t_hsd2.id, 'qty': qty, 'odometer': fleet_odo[v.id],
                                                             'driver_id': v.pump_driver_emp_id.id})
                    if t_hsd2.stock_qty > qty + 500:
                        log.action_post()
                    else:
                        log.unlink()

            # weekly dip reading
            if day.weekday() == 6:
                for tank in (t_ms | t_hsd1 | t_hsd2 | t_spd):
                    book = tank.stock_qty
                    dip = env['pump.dip.reading'].create({
                        'date': day, 'tank_id': tank.id,
                        'physical_qty': round(book * (1 + rnd.uniform(-0.0025, 0.0012)), 2), 'book_qty': book})
                    dip.action_post()

            # customer payments due today
            for partner, amount, mode in pending_pay.pop(day, []):
                env['pump.quick.voucher.wizard'].create({
                    'voucher_type': 'receipt', 'date': day, 'partner_id': partner.id, 'amount': amount,
                    'via': 'cash' if mode == 'cash' else 'bank', 'payment_mode': mode,
                    'cheque_no': '' if mode == 'cash' else ('UTR%s%06d' % (day.strftime('%y%m%d'), rnd.randint(0, 999999)))
                }).action_confirm()

            # billing: weekly (Mon), fortnightly (1st & 16th), monthly (1st)
            def bill(cycle, d_from, d_to):
                parts = env['res.partner'].browse([c['partner'].id for c in customers if c['cycle'] == cycle])
                bills = env['pump.bill'].generate_bills(d_from, d_to, parts, bill_date=day)
                for b in bills:
                    c = next(x for x in customers if x['partner'] == b.partner_id)
                    amount = round(b.amount_total * c['pay'] * rnd.uniform(0.9, 1.0), -2)
                    if amount > 0:
                        pay_day = day + timedelta(days=rnd.randint(3, 11))
                        if pay_day < today:
                            pending_pay.setdefault(pay_day, []).append(
                                (b.partner_id, amount, rnd.choice(['neft', 'neft', 'cheque', 'upi'])))
            if day > start:
                prev = day - timedelta(days=1)
                if day.weekday() == 0:
                    bill('weekly', day - timedelta(days=7), prev)
                if day.day == 16:
                    bill('fortnightly', day.replace(day=1), prev)
                if day.day == 1:
                    bill('fortnightly', prev.replace(day=16), prev)
                    bill('monthly', prev.replace(day=1), prev)

            # cash deposit (keep ~25k float), expenses, attendance
            if not is_today:
                env['pump.account'].invalidate_model(['balance'])
                cash = ref('acc_cash').balance
                if cash > 40000:
                    env['pump.quick.voucher.wizard'].create({
                        'voucher_type': 'contra', 'date': day, 'contra_direction': 'deposit',
                        'amount': round(cash - 25000, -2), 'narration': 'Daily cash deposit'}).action_confirm()
            # T+1 settlement of yesterday's card / UPI / fleet sales
            if day > start:
                y_sheets = Sheet.search([('date', '=', day - timedelta(days=1)), ('state', '=', 'posted')])
                for acc_x, fld, mdr in (('acc_card', 'card_amount', 0.006), ('acc_upi', 'upi_amount', 0.0),
                                        ('acc_fleet', 'fleet_card_amount', 0.0)):
                    amt = sum(y_sheets.mapped(fld))
                    if amt > 0:
                        env['pump.quick.voucher.wizard'].create({
                            'voucher_type': 'contra', 'date': day, 'contra_direction': 'settle',
                            'settle_account_id': ref(acc_x).id, 'amount': amt, 'charges': round(amt * mdr, 2),
                        }).action_confirm()
            if day.day == 5 and day != start:
                env['pump.quick.voucher.wizard'].create({
                    'voucher_type': 'payment', 'date': day, 'via': 'bank', 'payment_mode': 'neft',
                    'account_id': ref('acc_electricity').id, 'amount': rnd.randint(38000, 46000),
                    'narration': 'TSSPDCL electricity bill'}).action_confirm()
            if rnd.random() < 0.12:
                env['pump.quick.voucher.wizard'].create({
                    'voucher_type': 'payment', 'date': day, 'via': 'cash', 'payment_mode': 'cash',
                    'account_id': rnd.choice([ref('acc_maintenance'), ref('acc_general_exp')]).id,
                    'amount': rnd.choice([350, 600, 1200, 1800, 2500]),
                    'narration': rnd.choice(['Air compressor service', 'Stationery & slip books', 'Tea & refreshments',
                                             'Washroom maintenance', 'Nozzle hose replacement'])}).action_confirm()
            for e in employees:
                status = 'off' if (day.weekday() == (e.id % 7)) else rnd.choices(
                    ['present', 'half', 'absent'], weights=[92, 4, 4])[0]
                env['pump.attendance'].create({'date': day, 'employee_id': e.id, 'status': status,
                                               'shift_id': shifts[rnd.randint(0, 1)].id})
            day += timedelta(days=1)

        # maintenance job cards
        sch = env['pump.maintenance.schedule'].search([('vehicle_id', '=', tankers[0].id), ('service_type_id', '=', ref('svc_engine_oil').id)])
        job = env['pump.maintenance.order'].create({
            'vehicle_id': tankers[0].id, 'schedule_id': sch.id, 'service_type_id': sch.service_type_id.id,
            'maintenance_type': 'preventive', 'date': today - timedelta(days=20), 'odometer': fleet_odo[tankers[0].id] - 900,
            'vendor_id': Partner.create({'name': 'Sri Sai Auto Works', 'is_company': True}).id, 'vendor_ref': 'SSA/2211',
            'paid_from': 'bank', 'complaint': 'Scheduled 15,000 km service', 'work_done': 'Engine oil, oil filter, air filter, greasing',
            'line_ids': [(0, 0, {'line_type': 'oil', 'name': 'Engine oil 15W40 (18 L)', 'qty': 1, 'unit_price': 6480}),
                         (0, 0, {'line_type': 'part', 'name': 'Oil filter', 'qty': 1, 'unit_price': 850}),
                         (0, 0, {'line_type': 'part', 'name': 'Air filter', 'qty': 1, 'unit_price': 1650}),
                         (0, 0, {'line_type': 'labour', 'name': 'Service labour', 'qty': 1, 'unit_price': 1200})]})
        job.action_start()
        job.action_done()
        job2 = env['pump.maintenance.order'].create({
            'vehicle_id': bowser.id, 'service_type_id': ref('svc_breakdown').id, 'maintenance_type': 'breakdown',
            'date': today - timedelta(days=9), 'paid_from': 'cash', 'complaint': 'Delivery pump not priming',
            'work_done': 'Replaced pump seal kit and suction hose',
            'line_ids': [(0, 0, {'line_type': 'part', 'name': 'Pump seal kit', 'qty': 1, 'unit_price': 2350}),
                         (0, 0, {'line_type': 'part', 'name': 'Suction hose 2"', 'qty': 1, 'unit_price': 1900}),
                         (0, 0, {'line_type': 'labour', 'name': 'Mechanic', 'qty': 1, 'unit_price': 800})]})
        job2.action_done()
        job3 = env['pump.maintenance.order'].create({
            'vehicle_id': own_fleet[3].id, 'service_type_id': ref('svc_brake').id, 'maintenance_type': 'preventive',
            'date': today, 'complaint': 'Brake noise', 'paid_from': 'cash',
            'line_ids': [(0, 0, {'line_type': 'part', 'name': 'Brake pads (front)', 'qty': 1, 'unit_price': 2200})]})
        job3.action_start()   # stays in the workshop today
        # live trips today
        live = env['pump.tanker.trip'].create({
            'trip_type': 'depot', 'date': today, 'vehicle_id': tankers[1].id, 'driver_id': tankers[1].pump_driver_emp_id.id,
            'origin': 'BPCL Cherlapally Terminal', 'destination': 'Station', 'product_id': hsd.id, 'qty': 12000,
            'dest_tank_id': t_hsd2.id, 'load_rate': 90.62, 'odometer_start': fleet_odo[tankers[1].id]})
        live.action_start()
        env['pump.tanker.trip'].create({
            'trip_type': 'delivery', 'date': today, 'vehicle_id': bowser.id, 'driver_id': bowser.pump_driver_emp_id.id,
            'origin': 'Station', 'destination': 'Nilgiri Construction site, LB Nagar', 'product_id': hsd.id, 'qty': 1500,
            'partner_id': customers[4]['partner'].id, 'source_tank_id': t_hsd1.id})
        # planted double-fill anomaly (same truck twice today)
        sg = customers[6]['partner']
        v2 = sg.pump_vehicle_ids.filtered(lambda v: v.name == 'AP29TB9932')
        for q in (210.0, 180.0):
            Sale.create({'date': today, 'shift_id': shifts[0].id, 'partner_id': sg.id, 'vehicle_id': v2.id,
                         'product_id': hsd.id, 'qty': q, 'slip_no': str(slip_counter)}).with_context(pump_override_limit=True).action_confirm()
            slip_counter += 1
        env['fleet.vehicle']._cron_pump_fleet_alerts()

        # salary sheets for completed months
        month_cursor = start.replace(day=1)
        while month_cursor < today.replace(day=1):
            sheet = env['pump.salary.sheet'].create({'month': str(month_cursor.month), 'year': month_cursor.year,
                                                    'pay_account': 'bank'})
            sheet.action_compute()
            sheet.action_post()
            month_cursor = (month_cursor + timedelta(days=32)).replace(day=1)

        # open indents (one from the portal)
        konark = customers[0]['partner']
        env['pump.indent'].create({'partner_id': konark.id, 'vehicle_id': konark.pump_vehicle_ids[0].id,
                                   'product_id': hsd.id, 'indent_type': 'qty', 'qty': 220, 'source': 'portal',
                                   'comment': 'Hyderabad - Nagpur trip'})
        env['pump.indent'].create({'partner_id': customers[2]['partner'].id, 'product_id': hsd.id,
                                   'indent_type': 'full', 'source': 'office',
                                   'vehicle_id': customers[2]['partner'].pump_vehicle_ids[0].id})

        # portal logins for two demo customers
        portal = env.ref('base.group_portal')
        for c, login, pwd in ((customers[0]['partner'], 'konark', 'Konark@123'),
                              (customers[3]['partner'], 'vayu', 'Vayu@123')):
            env['res.users'].with_context(no_reset_password=True).create({
                'name': c.name, 'login': login, 'password': pwd, 'partner_id': c.id,
                'group_ids': [(6, 0, [portal.id])]})
        for q in ('What is my balance?', 'How much diesel did TS09UB4521 take last month?', "Today's rates"):
            env['pump.ai'].ask(q, partner=customers[0]['partner'])
        return True
