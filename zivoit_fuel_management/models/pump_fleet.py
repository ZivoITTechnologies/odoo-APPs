# -*- coding: utf-8 -*-
"""Own fleet integration (Odoo Fleet): fuel tankers, delivery bowsers and
service vehicles — compartments, statutory documents, trips, internal fuel
issues, preventive maintenance schedules and maintenance job cards."""
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

DOC_TYPES = [
    ('insurance', 'Insurance'), ('permit', 'National / State Permit'), ('fitness', 'Fitness Certificate'),
    ('puc', 'PUC (Pollution)'), ('peso', 'PESO Explosive Licence'), ('calibration', 'Tank Calibration (W&M)'),
    ('road_tax', 'Road Tax'), ('rc', 'Registration (RC)'), ('other', 'Other'),
]


class FleetVehicle(models.Model):
    _inherit = 'fleet.vehicle'

    is_pump_fleet = fields.Boolean('Station Fleet', default=False,
                                   help='Vehicle operated by the fuel station (tanker, bowser, service vehicle).')
    pump_role = fields.Selection([('tanker', 'Fuel Tanker (depot loading)'),
                                  ('bowser', 'Delivery Bowser (doorstep diesel)'),
                                  ('service', 'Service / Utility Vehicle')], string='Fleet Role', default='tanker')
    pump_capacity = fields.Float('Tank Capacity (Ltr)', compute='_compute_pump_capacity', store=True)
    pump_compartment_ids = fields.One2many('pump.tanker.compartment', 'vehicle_id', string='Compartments')
    pump_driver_emp_id = fields.Many2one('hr.employee', string='Assigned Driver')
    pump_status = fields.Selection([('available', 'Available'), ('on_trip', 'On Trip'),
                                    ('maintenance', 'In Maintenance')], compute='_compute_pump_status',
                                   string='Live Status')
    pump_document_ids = fields.One2many('pump.vehicle.document', 'vehicle_id', string='Documents')
    pump_doc_alert = fields.Selection([('ok', 'All valid'), ('expiring', 'Expiring soon'), ('expired', 'Expired')],
                                      compute='_compute_doc_alert', string='Document Status')
    pump_schedule_ids = fields.One2many('pump.maintenance.schedule', 'vehicle_id', string='Maintenance Plan')
    pump_job_ids = fields.One2many('pump.maintenance.order', 'vehicle_id', string='Job Cards')
    pump_trip_ids = fields.One2many('pump.tanker.trip', 'vehicle_id', string='Trips')
    pump_fuel_log_ids = fields.One2many('pump.fleet.fuel.log', 'vehicle_id', string='Fuel Logs')
    pump_avg_kmpl = fields.Float('Avg. km / Ltr', compute='_compute_pump_kpis', digits=(16, 2))
    pump_month_cost = fields.Float('Cost this month', compute='_compute_pump_kpis', digits=(16, 2))
    pump_trip_count = fields.Integer(compute='_compute_pump_kpis')
    pump_job_count = fields.Integer(compute='_compute_pump_kpis')

    @api.depends('pump_compartment_ids.capacity')
    def _compute_pump_capacity(self):
        for v in self:
            v.pump_capacity = sum(v.pump_compartment_ids.mapped('capacity'))

    def _compute_pump_status(self):
        Trip = self.env['pump.tanker.trip']
        Job = self.env['pump.maintenance.order']
        for v in self:
            if Job.search_count([('vehicle_id', '=', v.id), ('state', '=', 'in_progress')]):
                v.pump_status = 'maintenance'
            elif Trip.search_count([('vehicle_id', '=', v.id), ('state', '=', 'in_transit')]):
                v.pump_status = 'on_trip'
            else:
                v.pump_status = 'available'

    def _compute_doc_alert(self):
        for v in self:
            states = v.pump_document_ids.mapped('state')
            v.pump_doc_alert = 'expired' if 'expired' in states else ('expiring' if 'expiring' in states else 'ok')

    def _compute_pump_kpis(self):
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)
        for v in self:
            logs = v.pump_fuel_log_ids.filtered(lambda l: l.state == 'posted' and l.kmpl > 0)
            dist = sum(logs.mapped('distance'))
            qty = sum(logs.mapped('qty'))
            v.pump_avg_kmpl = dist / qty if qty else 0.0
            cost = sum(v.pump_fuel_log_ids.filtered(lambda l: l.state == 'posted' and l.date >= month_start).mapped('amount'))
            cost += sum(v.pump_job_ids.filtered(lambda j: j.state == 'done' and j.date >= month_start).mapped('amount_total'))
            cost += sum(v.pump_trip_ids.filtered(lambda t: t.state == 'closed' and t.date >= month_start).mapped('expense_total'))
            v.pump_month_cost = cost
            v.pump_trip_count = len(v.pump_trip_ids)
            v.pump_job_count = len(v.pump_job_ids)

    def action_pump_trips(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': self.env._('Trips'), 'res_model': 'pump.tanker.trip',
                'view_mode': 'list,form', 'domain': [('vehicle_id', '=', self.id)],
                'context': {'default_vehicle_id': self.id}}

    def action_pump_jobs(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': self.env._('Job Cards'), 'res_model': 'pump.maintenance.order',
                'view_mode': 'list,form', 'domain': [('vehicle_id', '=', self.id)],
                'context': {'default_vehicle_id': self.id}}

    def _pump_record_odometer(self, value, date):
        for v in self:
            if value and value > (v.odometer or 0):
                self.env['fleet.vehicle.odometer'].create({'vehicle_id': v.id, 'value': value, 'date': date})
                v.invalidate_recordset(['odometer'])

    @api.model
    def _cron_pump_fleet_alerts(self):
        """Daily: schedule activities for expiring documents and due maintenance."""
        activity_type = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        user = self.env.ref('base.user_admin', raise_if_not_found=False) or self.env.user
        created = 0
        for doc in self.env['pump.vehicle.document'].search([('state', 'in', ('expiring', 'expired'))]):
            summary = self.env._('%(doc)s of %(veh)s expires on %(d)s', doc=doc.display_name, veh=doc.vehicle_id.display_name,
                        d=doc.expiry_date)
            if not doc.activity_ids and activity_type:
                doc.activity_schedule(activity_type_id=activity_type.id, summary=summary,
                                      date_deadline=doc.expiry_date, user_id=user.id)
                created += 1
        for sch in self.env['pump.maintenance.schedule'].search([('active', '=', True)]):
            if sch.due_state in ('due', 'overdue') and not sch.activity_ids and activity_type:
                sch.activity_schedule(activity_type_id=activity_type.id, user_id=user.id,
                                      summary=self.env._('Maintenance due: %s', sch.display_name),
                                      date_deadline=sch.next_due_date or fields.Date.context_today(self))
                created += 1
        return created


class PumpTankerCompartment(models.Model):
    _name = 'pump.tanker.compartment'
    _description = 'Tanker Compartment'
    _order = 'vehicle_id, sequence'

    vehicle_id = fields.Many2one('fleet.vehicle', required=True, ondelete='cascade')
    sequence = fields.Integer(default=1)
    name = fields.Char(required=True, default='C1')
    capacity = fields.Float('Capacity (Ltr)', required=True)
    product_id = fields.Many2one('pump.product', string='Dedicated Product', domain=[('product_type', '=', 'fuel')])


class PumpVehicleDocument(models.Model):
    _name = 'pump.vehicle.document'
    _description = 'Vehicle Statutory Document'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'expiry_date'

    vehicle_id = fields.Many2one('fleet.vehicle', required=True, ondelete='cascade', index=True)
    doc_type = fields.Selection(DOC_TYPES, required=True, string='Document')
    number = fields.Char('Document No.')
    issue_date = fields.Date()
    expiry_date = fields.Date(required=True, tracking=True)
    remind_days = fields.Integer('Remind Before (days)', default=30)
    attachment = fields.Binary(attachment=True)
    attachment_name = fields.Char()
    days_left = fields.Integer(compute='_compute_state')
    state = fields.Selection([('valid', 'Valid'), ('expiring', 'Expiring Soon'), ('expired', 'Expired')],
                             compute='_compute_state', search='_search_state')

    @api.depends('doc_type', 'vehicle_id')
    def _compute_display_name(self):
        labels = dict(DOC_TYPES)
        for d in self:
            d.display_name = '%s - %s' % (labels.get(d.doc_type, ''), d.vehicle_id.license_plate or d.vehicle_id.name or '')

    @api.depends('expiry_date', 'remind_days')
    def _compute_state(self):
        today = fields.Date.context_today(self)
        for d in self:
            left = (d.expiry_date - today).days if d.expiry_date else 9999
            d.days_left = left
            d.state = 'expired' if left < 0 else ('expiring' if left <= (d.remind_days or 30) else 'valid')

    def _search_state(self, operator, value):
        values = [value] if isinstance(value, str) else list(value)
        ids = self.search([]).filtered(lambda d: d.state in values).ids
        return [('id', 'in' if operator in ('=', 'in') else 'not in', ids)]

    def action_renew(self):
        """Open a copy with the next validity window prefilled."""
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'view_mode': 'form', 'target': 'new',
                'context': {'default_vehicle_id': self.vehicle_id.id, 'default_doc_type': self.doc_type,
                            'default_issue_date': fields.Date.context_today(self),
                            'default_expiry_date': fields.Date.add(self.expiry_date, years=1)}}


class PumpFleetFuelLog(models.Model):
    _name = 'pump.fleet.fuel.log'
    _description = 'Fleet Fuel Log (own vehicles)'
    _order = 'date desc, id desc'

    name = fields.Char(readonly=True, default='/', copy=False)
    date = fields.Date(required=True, default=fields.Date.context_today)
    vehicle_id = fields.Many2one('fleet.vehicle', required=True, domain=[('is_pump_fleet', '=', True)])
    source = fields.Selection([('station', 'From Station Tank'), ('outside', 'Outside Bunk (paid)')],
                              default='station', required=True)
    tank_id = fields.Many2one('pump.tank', string='Issued From Tank')
    qty = fields.Float('Qty (Ltr)', required=True, digits=(16, 2))
    rate = fields.Float('Cost Rate', digits=(16, 2), help='Defaults to last tanker purchase rate for station issues.')
    amount = fields.Float(compute='_compute_amount', store=True, digits=(16, 2))
    odometer = fields.Float('Odometer (km)')
    prev_odometer = fields.Float(readonly=True)
    distance = fields.Float(compute='_compute_kmpl', store=True)
    kmpl = fields.Float('km / Ltr', compute='_compute_kmpl', store=True, digits=(16, 2))
    driver_id = fields.Many2one('hr.employee')
    voucher_id = fields.Many2one('pump.voucher', readonly=True, copy=False)
    state = fields.Selection([('draft', 'Draft'), ('posted', 'Posted'), ('cancel', 'Cancelled')], default='draft')

    @api.depends('qty', 'rate')
    def _compute_amount(self):
        for l in self:
            l.amount = round(l.qty * l.rate, 2)

    @api.depends('odometer', 'prev_odometer', 'qty')
    def _compute_kmpl(self):
        for l in self:
            l.distance = (l.odometer - l.prev_odometer) if l.odometer and l.prev_odometer and l.odometer > l.prev_odometer else 0.0
            l.kmpl = l.distance / l.qty if l.distance and l.qty else 0.0

    @api.onchange('tank_id')
    def _onchange_tank(self):
        if self.tank_id and not self.rate:
            self.rate = self._last_cost(self.tank_id)

    def _last_cost(self, tank):
        last = self.env['pump.purchase'].search([('product_id', '=', tank.product_id.id), ('state', '=', 'posted')],
                                                order='date desc, id desc', limit=1)
        return last.rate if last else tank.product_id.get_rate(self.date or fields.Date.context_today(self))

    def action_post(self):
        for l in self.filtered(lambda x: x.state == 'draft'):
            if l.qty <= 0:
                raise UserError(self.env._('Quantity must be positive.'))
            prev = self.search([('vehicle_id', '=', l.vehicle_id.id), ('state', '=', 'posted'), ('id', '!=', l.id),
                                ('odometer', '>', 0)], order='date desc, id desc', limit=1)
            if l.odometer and prev.odometer and l.odometer < prev.odometer:
                raise UserError(self.env._('Odometer %(o).0f is lower than the previous log (%(p).0f).', o=l.odometer, p=prev.odometer))
            l.prev_odometer = prev.odometer or 0.0
            if l.source == 'station':
                if not l.tank_id:
                    raise UserError(self.env._('Select the tank the fuel was issued from.'))
                if not l.rate:
                    l.rate = l._last_cost(l.tank_id)
                if l.tank_id.stock_qty < l.qty:
                    raise UserError(self.env._('Not enough stock in %s.', l.tank_id.name))
            if l.name in (False, '/'):
                l.name = self.env['ir.sequence'].next_by_code('pump.fleet.fuel.log') or '/'
            label = self.env._('Fleet fuel %(v)s %(q).2f L', v=l.vehicle_id.license_plate or l.vehicle_id.name, q=l.qty)
            if l.source == 'station':
                self.env['pump.stock.move'].create({
                    'date': l.date, 'tank_id': l.tank_id.id, 'qty': -l.qty, 'move_type': 'internal',
                    'reference': '%s %s' % (l.name, l.vehicle_id.license_plate or ''), 'res_model': l._name, 'res_id': l.id})
                credit_acc = l.tank_id.product_id.purchase_account_id
            else:
                credit_acc = self.env.ref('zivoit_fuel_management.acc_cash')
            l._compute_amount()
            if l.amount:
                l.voucher_id = self.env['pump.voucher']._create_posted([
                    {'account_id': self.env.ref('zivoit_fuel_management.acc_fleet_fuel').id, 'debit': l.amount, 'name': label},
                    {'account_id': credit_acc.id, 'credit': l.amount, 'name': label},
                ], voucher_type='journal', date=l.date, narration=label, origin_model=l._name, origin_id=l.id)
            l.vehicle_id._pump_record_odometer(l.odometer, l.date)
            l.state = 'posted'
        return True

    def action_cancel(self):
        for l in self:
            if l.voucher_id:
                l.voucher_id.with_context(pump_from_origin=True).action_cancel()
            self.env['pump.stock.move'].search([('res_model', '=', l._name), ('res_id', '=', l.id)]).unlink()
            l.state = 'cancel'


class PumpTankerTrip(models.Model):
    _name = 'pump.tanker.trip'
    _description = 'Tanker / Bowser Trip'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc, id desc'

    name = fields.Char(readonly=True, default='/', copy=False)
    trip_type = fields.Selection([('depot', 'Depot Loading (oil company terminal → station)'),
                                  ('delivery', 'Doorstep / Bulk Delivery to customer')],
                                 required=True, default='depot', tracking=True)
    date = fields.Date(required=True, default=fields.Date.context_today)
    vehicle_id = fields.Many2one('fleet.vehicle', required=True, domain=[('is_pump_fleet', '=', True)], tracking=True)
    driver_id = fields.Many2one('hr.employee', string='Driver')
    origin = fields.Char('From', default=lambda s: s.env._('Station'))
    destination = fields.Char('To')
    product_id = fields.Many2one('pump.product', required=True, domain=[('product_type', '=', 'fuel')])
    qty = fields.Float('Planned Qty (Ltr)', digits=(16, 2))
    delivered_qty = fields.Float('Delivered Qty (Ltr)', digits=(16, 2))
    odometer_start = fields.Float()
    odometer_end = fields.Float()
    distance = fields.Float(compute='_compute_distance', store=True)
    departure = fields.Datetime()
    arrival = fields.Datetime()
    # depot loading
    purchase_id = fields.Many2one('pump.purchase', string='Tanker Receipt', readonly=True, copy=False)
    invoice_no = fields.Char('Depot Invoice No.')
    load_rate = fields.Float('Invoice Rate / Ltr', digits=(16, 4))
    dest_tank_id = fields.Many2one('pump.tank', string='Decant Into Tank')
    # delivery
    partner_id = fields.Many2one('res.partner', string='Customer', domain=[('is_pump_customer', '=', True)])
    customer_vehicle_id = fields.Many2one('pump.vehicle', domain="[('partner_id', '=', partner_id)]")
    source_tank_id = fields.Many2one('pump.tank', string='Loaded From Tank')
    sale_id = fields.Many2one('pump.credit.sale', string='Delivery Slip', readonly=True, copy=False)
    sale_voucher_id = fields.Many2one('pump.voucher', readonly=True, copy=False)
    # expenses
    expense_ids = fields.One2many('pump.trip.expense', 'trip_id', string='Trip Expenses')
    expense_total = fields.Float(compute='_compute_expense', store=True, digits=(16, 2))
    expense_voucher_id = fields.Many2one('pump.voucher', readonly=True, copy=False)
    cost_per_km = fields.Float(compute='_compute_cost_per_km', digits=(16, 2))
    state = fields.Selection([('planned', 'Planned'), ('in_transit', 'In Transit'), ('delivered', 'Delivered / Unloaded'),
                              ('closed', 'Closed'), ('cancel', 'Cancelled')], default='planned', tracking=True)

    @api.depends('odometer_start', 'odometer_end')
    def _compute_distance(self):
        for t in self:
            t.distance = max(t.odometer_end - t.odometer_start, 0.0) if t.odometer_end else 0.0

    @api.depends('expense_ids.amount')
    def _compute_expense(self):
        for t in self:
            t.expense_total = sum(t.expense_ids.mapped('amount'))

    @api.depends('expense_total', 'distance')
    def _compute_cost_per_km(self):
        for t in self:
            t.cost_per_km = t.expense_total / t.distance if t.distance else 0.0

    @api.onchange('vehicle_id')
    def _onchange_vehicle(self):
        if self.vehicle_id:
            self.driver_id = self.vehicle_id.pump_driver_emp_id
            self.odometer_start = self.vehicle_id.odometer

    @api.constrains('qty', 'vehicle_id')
    def _check_capacity(self):
        for t in self:
            if t.vehicle_id.pump_capacity and t.qty > t.vehicle_id.pump_capacity:
                raise ValidationError(self.env._('Planned quantity exceeds the tanker capacity (%.0f L).', t.vehicle_id.pump_capacity))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code('pump.tanker.trip') or '/'
        return super().create(vals_list)

    def action_start(self):
        for t in self.filtered(lambda x: x.state == 'planned'):
            busy = self.search([('vehicle_id', '=', t.vehicle_id.id), ('state', '=', 'in_transit'), ('id', '!=', t.id)], limit=1)
            if busy:
                raise UserError(self.env._('%(v)s is already on trip %(t)s.', v=t.vehicle_id.display_name, t=busy.name))
            if self.env['pump.maintenance.order'].search_count([('vehicle_id', '=', t.vehicle_id.id), ('state', '=', 'in_progress')]):
                raise UserError(self.env._('%s is under maintenance.', t.vehicle_id.display_name))
            expired = t.vehicle_id.pump_document_ids.filtered(lambda d: d.state == 'expired' and d.doc_type in ('insurance', 'peso', 'fitness', 'permit'))
            if expired:
                raise UserError(self.env._('Cannot dispatch: %s expired.', ', '.join(expired.mapped('display_name'))))
            if t.trip_type == 'delivery':
                if not (t.partner_id and t.source_tank_id and t.qty > 0):
                    raise UserError(self.env._('Customer, source tank and quantity are required for a delivery trip.'))
                if t.source_tank_id.product_id != t.product_id:
                    raise UserError(self.env._('Source tank holds %s.', t.source_tank_id.product_id.name))
                if t.source_tank_id.stock_qty < t.qty:
                    raise UserError(self.env._('Not enough stock in %s.', t.source_tank_id.name))
                rate = t.product_id.get_rate(t.date)
                t.partner_id._pump_check_credit(t.qty * rate)
            t.write({'state': 'in_transit', 'departure': fields.Datetime.now(),
                     'odometer_start': t.odometer_start or t.vehicle_id.odometer})
        return True

    def action_deliver(self):
        Sale = self.env['pump.credit.sale']
        for t in self.filtered(lambda x: x.state == 'in_transit'):
            qty = t.delivered_qty or t.qty
            if qty <= 0:
                raise UserError(self.env._('Enter the delivered quantity.'))
            if t.trip_type == 'depot':
                if not (t.dest_tank_id and t.load_rate):
                    raise UserError(self.env._('Destination tank and invoice rate are required to unload.'))
                pur = self.env['pump.purchase'].create({
                    'date': t.date, 'tank_id': t.dest_tank_id.id, 'qty': t.qty or qty, 'received_qty': qty,
                    'rate': t.load_rate, 'invoice_no': t.invoice_no, 'paid_from': 'bank',
                    'truck_no': t.vehicle_id.license_plate})
                pur.action_post()
                t.purchase_id = pur
            else:
                rate = t.product_id.get_rate(t.date)
                sale = Sale.create({
                    'date': t.date, 'partner_id': t.partner_id.id, 'product_id': t.product_id.id, 'qty': qty,
                    'rate': rate, 'vehicle_id': t.customer_vehicle_id.id, 'source': 'delivery',
                    'delivery_trip_id': t.id, 'note': self.env._('Doorstep delivery %s', t.name)})
                sale.with_context(pump_override_limit=True).action_confirm()
                self.env['pump.stock.move'].create({
                    'date': t.date, 'tank_id': t.source_tank_id.id, 'qty': -qty, 'move_type': 'delivery',
                    'reference': '%s %s' % (t.name, t.partner_id.name), 'res_model': t._name, 'res_id': t.id})
                label = self.env._('Doorstep delivery %(t)s - %(p)s', t=t.name, p=t.partner_id.name)
                voucher = self.env['pump.voucher']._create_posted([
                    {'account_id': self.env.ref('zivoit_fuel_management.acc_credit_clearing').id, 'debit': sale.amount, 'name': label},
                    {'account_id': t.product_id.income_account_id.id, 'credit': sale.amount, 'name': label},
                ], voucher_type='journal', date=t.date, partner_id=t.partner_id.id, narration=label,
                    origin_model=t._name, origin_id=t.id)
                t.write({'sale_id': sale.id, 'sale_voucher_id': voucher.id})
            t.write({'state': 'delivered', 'delivered_qty': qty, 'arrival': fields.Datetime.now()})
        return True

    def action_close(self):
        for t in self.filtered(lambda x: x.state == 'delivered'):
            if t.odometer_end and t.odometer_end < t.odometer_start:
                raise UserError(self.env._('Closing odometer is below the starting odometer.'))
            t.vehicle_id._pump_record_odometer(t.odometer_end, t.date)
            if t.expense_total:
                label = self.env._('Trip expenses %s', t.name)
                t.expense_voucher_id = self.env['pump.voucher']._create_posted([
                    {'account_id': self.env.ref('zivoit_fuel_management.acc_trip_expense').id, 'debit': t.expense_total, 'name': label},
                    {'account_id': self.env.ref('zivoit_fuel_management.acc_cash').id, 'credit': t.expense_total, 'name': label},
                ], voucher_type='payment', date=t.date, narration=label, origin_model=t._name, origin_id=t.id)
            t.state = 'closed'
        return True

    def action_cancel(self):
        for t in self:
            if t.state in ('delivered', 'closed'):
                raise UserError(self.env._('Delivered trips cannot be cancelled; reverse the slip / receipt instead.'))
            t.state = 'cancel'


class PumpTripExpense(models.Model):
    _name = 'pump.trip.expense'
    _description = 'Trip Expense'

    trip_id = fields.Many2one('pump.tanker.trip', required=True, ondelete='cascade')
    expense_type = fields.Selection([('toll', 'Toll / FASTag'), ('allowance', 'Driver Allowance'),
                                     ('loading', 'Loading / Unloading'), ('parking', 'Parking'),
                                     ('repair', 'On-road Repair'), ('other', 'Other')], required=True, default='toll')
    description = fields.Char()
    amount = fields.Float(required=True, digits=(16, 2))


class PumpMaintenanceSchedule(models.Model):
    _name = 'pump.maintenance.schedule'
    _description = 'Preventive Maintenance Plan'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'next_due_date'

    vehicle_id = fields.Many2one('fleet.vehicle', required=True, ondelete='cascade', domain=[('is_pump_fleet', '=', True)])
    service_type_id = fields.Many2one('fleet.service.type', required=True, domain=[('category', '=', 'service')])
    interval_km = fields.Float('Every (km)')
    interval_days = fields.Integer('Every (days)')
    last_done_date = fields.Date()
    last_done_km = fields.Float()
    next_due_date = fields.Date(compute='_compute_due', store=True)
    next_due_km = fields.Float(compute='_compute_due', store=True)
    km_left = fields.Float(compute='_compute_due_state')
    due_state = fields.Selection([('ok', 'OK'), ('due', 'Due Soon'), ('overdue', 'Overdue')], compute='_compute_due_state')
    active = fields.Boolean(default=True)

    @api.depends('vehicle_id', 'service_type_id')
    def _compute_display_name(self):
        for s in self:
            s.display_name = '%s - %s' % (s.vehicle_id.license_plate or s.vehicle_id.name or '', s.service_type_id.name or '')

    @api.depends('interval_km', 'interval_days', 'last_done_date', 'last_done_km')
    def _compute_due(self):
        for s in self:
            s.next_due_date = fields.Date.add(s.last_done_date, days=s.interval_days) if s.last_done_date and s.interval_days else False
            s.next_due_km = (s.last_done_km + s.interval_km) if s.interval_km else 0.0

    def _compute_due_state(self):
        today = fields.Date.context_today(self)
        for s in self:
            odo = s.vehicle_id.odometer or 0.0
            s.km_left = (s.next_due_km - odo) if s.next_due_km else 0.0
            overdue = (s.next_due_date and s.next_due_date < today) or (s.next_due_km and odo >= s.next_due_km)
            soon = (s.next_due_date and s.next_due_date <= today + timedelta(days=7)) or \
                   (s.next_due_km and s.next_due_km - odo <= max(s.interval_km * 0.1, 500))
            s.due_state = 'overdue' if overdue else ('due' if soon else 'ok')

    def action_create_job(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'pump.maintenance.order', 'view_mode': 'form',
                'context': {'default_vehicle_id': self.vehicle_id.id, 'default_schedule_id': self.id,
                            'default_service_type_id': self.service_type_id.id, 'default_maintenance_type': 'preventive',
                            'default_odometer': self.vehicle_id.odometer}}


class PumpMaintenanceOrder(models.Model):
    _name = 'pump.maintenance.order'
    _description = 'Maintenance Job Card'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc, id desc'

    name = fields.Char(readonly=True, default='/', copy=False)
    vehicle_id = fields.Many2one('fleet.vehicle', required=True, domain=[('is_pump_fleet', '=', True)], tracking=True)
    schedule_id = fields.Many2one('pump.maintenance.schedule', domain="[('vehicle_id', '=', vehicle_id)]")
    service_type_id = fields.Many2one('fleet.service.type', required=True, domain=[('category', '=', 'service')])
    maintenance_type = fields.Selection([('preventive', 'Preventive (scheduled)'), ('breakdown', 'Breakdown'),
                                         ('accident', 'Accident'), ('tyre', 'Tyre'), ('statutory', 'Statutory / Calibration')],
                                        default='preventive', required=True)
    date = fields.Date(required=True, default=fields.Date.context_today)
    odometer = fields.Float()
    vendor_id = fields.Many2one('res.partner', string='Workshop / Vendor')
    vendor_ref = fields.Char('Vendor Invoice No.')
    complaint = fields.Text('Complaint / Reason')
    work_done = fields.Text()
    line_ids = fields.One2many('pump.maintenance.line', 'order_id', string='Parts & Labour')
    amount_total = fields.Float(compute='_compute_total', store=True, digits=(16, 2))
    paid_from = fields.Selection([('cash', 'Cash'), ('bank', 'Bank'), ('credit', 'Vendor Credit')], default='bank', required=True)
    downtime_start = fields.Datetime()
    downtime_end = fields.Datetime()
    downtime_hours = fields.Float(compute='_compute_total', store=True, digits=(16, 1))
    service_log_id = fields.Many2one('fleet.vehicle.log.services', readonly=True, copy=False)
    voucher_id = fields.Many2one('pump.voucher', readonly=True, copy=False)
    state = fields.Selection([('draft', 'Draft'), ('in_progress', 'In Workshop'), ('done', 'Done'), ('cancel', 'Cancelled')],
                             default='draft', tracking=True)

    @api.depends('line_ids.subtotal', 'downtime_start', 'downtime_end')
    def _compute_total(self):
        for o in self:
            o.amount_total = sum(o.line_ids.mapped('subtotal'))
            o.downtime_hours = ((o.downtime_end - o.downtime_start).total_seconds() / 3600.0
                                if o.downtime_start and o.downtime_end and o.downtime_end > o.downtime_start else 0.0)

    @api.onchange('schedule_id')
    def _onchange_schedule(self):
        if self.schedule_id:
            self.service_type_id = self.schedule_id.service_type_id

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code('pump.maintenance.order') or '/'
        return super().create(vals_list)

    def action_start(self):
        for o in self.filtered(lambda x: x.state == 'draft'):
            if self.env['pump.tanker.trip'].search_count([('vehicle_id', '=', o.vehicle_id.id), ('state', '=', 'in_transit')]):
                raise UserError(self.env._('%s is on a trip. Close the trip first.', o.vehicle_id.display_name))
            o.write({'state': 'in_progress', 'downtime_start': o.downtime_start or fields.Datetime.now()})

    def action_done(self):
        for o in self.filtered(lambda x: x.state in ('draft', 'in_progress')):
            if not o.line_ids:
                raise UserError(self.env._('Add the parts / labour lines before completing the job card.'))
            o.downtime_end = o.downtime_end or fields.Datetime.now()
            o.service_log_id = self.env['fleet.vehicle.log.services'].create({
                'vehicle_id': o.vehicle_id.id, 'service_type_id': o.service_type_id.id, 'date': o.date,
                'amount': o.amount_total, 'vendor_id': o.vendor_id.id, 'inv_ref': o.vendor_ref,
                'description': o.name, 'notes': o.work_done or o.complaint, 'state': 'done',
                'odometer': o.odometer or o.vehicle_id.odometer})
            label = self.env._('Job card %(n)s - %(v)s', n=o.name, v=o.vehicle_id.license_plate or o.vehicle_id.name)
            credit = {'cash': 'acc_cash', 'bank': 'acc_bank', 'credit': 'acc_creditors'}[o.paid_from]
            if o.amount_total:
                if o.paid_from == 'credit' and not o.vendor_id:
                    raise UserError(self.env._('Select the vendor for a credit purchase.'))
                o.voucher_id = self.env['pump.voucher']._create_posted([
                    {'account_id': self.env.ref('zivoit_fuel_management.acc_fleet_maintenance').id, 'debit': o.amount_total, 'name': label},
                    {'account_id': self.env.ref('zivoit_fuel_management.%s' % credit).id, 'credit': o.amount_total, 'name': label,
                     'partner_id': o.vendor_id.id if o.paid_from == 'credit' else False},
                ], voucher_type='payment', date=o.date, partner_id=o.vendor_id.id, narration=label,
                    origin_model=o._name, origin_id=o.id)
            if o.schedule_id:
                o.schedule_id.write({'last_done_date': o.date, 'last_done_km': o.odometer or o.vehicle_id.odometer})
                o.schedule_id.activity_ids.action_feedback(feedback=self.env._('Done via %s', o.name))
            o.state = 'done'
        return True

    def action_cancel(self):
        for o in self:
            if o.voucher_id:
                o.voucher_id.with_context(pump_from_origin=True).action_cancel()
            if o.service_log_id:
                o.service_log_id.state = 'cancelled'
            o.state = 'cancel'


class PumpMaintenanceLine(models.Model):
    _name = 'pump.maintenance.line'
    _description = 'Job Card Line'

    order_id = fields.Many2one('pump.maintenance.order', required=True, ondelete='cascade')
    line_type = fields.Selection([('part', 'Spare Part'), ('labour', 'Labour'), ('tyre', 'Tyre'),
                                  ('oil', 'Oil / Lubricant'), ('other', 'Other')], default='part', required=True)
    name = fields.Char('Description', required=True)
    qty = fields.Float(default=1.0)
    unit_price = fields.Float(digits=(16, 2))
    subtotal = fields.Float(compute='_compute_subtotal', store=True, digits=(16, 2))

    @api.depends('qty', 'unit_price')
    def _compute_subtotal(self):
        for l in self:
            l.subtotal = round(l.qty * l.unit_price, 2)
