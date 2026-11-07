# -*- coding: utf-8 -*-
from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class FleetVehicle(models.Model):
    _inherit = 'fleet.vehicle'

    cp_vehicle_type_id = fields.Many2one('cp.vehicle.type', string='TMS Vehicle Type')
    cp_device_id = fields.Char(string='GPS Device ID / IMEI', copy=False,
                               help='Used by the tracking API to match pings to this vehicle.')
    cp_mileage_benchmark = fields.Float(string='Mileage Benchmark (km/l)', default=4.0)
    cp_current_trip_id = fields.Many2one('cp.trip', compute='_compute_cp_trip', string='Current Trip')
    cp_trip_count = fields.Integer(compute='_compute_cp_trip')
    cp_fuel_log_ids = fields.One2many('cp.fuel.log', 'vehicle_id', string='Fuel Logs')
    cp_document_ids = fields.One2many('cp.document', 'vehicle_id', string='Documents')
    cp_tyre_ids = fields.One2many('cp.tyre', 'vehicle_id', string='Tyres')
    cp_maintenance_plan_ids = fields.One2many('cp.maintenance.plan', 'vehicle_id', string='Maintenance Plans')
    cp_avg_mileage = fields.Float(compute='_compute_cp_fuel', string='Avg. Mileage (km/l)')
    cp_fuel_anomaly_count = fields.Integer(compute='_compute_cp_fuel', string='Fuel Anomalies')

    _cp_device_uniq = models.Constraint(
        'unique(cp_device_id)',
        'This GPS device is already linked to another vehicle.',
    )

    def _compute_cp_trip(self):
        Trip = self.env['cp.trip']
        active_states = ('assigned', 'reported', 'loading', 'in_transit', 'arrived')
        for vehicle in self:
            vehicle.cp_current_trip_id = Trip.search([('vehicle_id', '=', vehicle.id),
                                                      ('state', 'in', active_states)], limit=1)
            vehicle.cp_trip_count = Trip.search_count([('vehicle_id', '=', vehicle.id)])

    def _compute_cp_fuel(self):
        for vehicle in self:
            logs = vehicle.cp_fuel_log_ids.filtered(lambda l: l.mileage_kmpl)
            vehicle.cp_avg_mileage = (sum(logs.mapped('mileage_kmpl')) / len(logs)) if logs else 0.0
            vehicle.cp_fuel_anomaly_count = len(vehicle.cp_fuel_log_ids.filtered('is_anomaly'))

    def action_cp_trips(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('cargo_pilot_tms.action_cp_trip')
        action['domain'] = [('vehicle_id', '=', self.id)]
        action['context'] = {'default_vehicle_id': self.id}
        return action


class CpFuelLog(models.Model):
    _name = 'cp.fuel.log'
    _description = 'Fuel Log'
    _order = 'date desc, odometer desc'

    vehicle_id = fields.Many2one('fleet.vehicle', required=True, index=True)
    driver_id = fields.Many2one('res.partner', domain=[('cp_is_driver', '=', True)])
    trip_id = fields.Many2one('cp.trip')
    date = fields.Datetime(default=fields.Datetime.now, required=True)
    odometer = fields.Float(required=True)
    litres = fields.Float(required=True)
    price_per_litre = fields.Monetary()
    amount = fields.Monetary(compute='_compute_amount', store=True, readonly=False)
    full_tank = fields.Boolean(default=True, help='Mileage is measured between two full-tank fills.')
    station = fields.Char(string='Fuel Station')
    payment_mode = fields.Selection([('cash', 'Cash'), ('card', 'Fuel Card'), ('credit', 'Credit Account')],
                                    default='card')
    mileage_kmpl = fields.Float(string='Mileage (km/l)', compute='_compute_mileage', store=True, digits=(6, 2))
    is_anomaly = fields.Boolean(compute='_compute_mileage', store=True, string='Anomaly',
                                help='Mileage well below the vehicle benchmark: check for theft, leakage or misuse.')
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    @api.depends('litres', 'price_per_litre')
    def _compute_amount(self):
        for log in self:
            if log.price_per_litre:
                log.amount = log.litres * log.price_per_litre

    @api.depends('odometer', 'litres', 'full_tank', 'vehicle_id.cp_mileage_benchmark')
    def _compute_mileage(self):
        for log in self:
            log.mileage_kmpl, log.is_anomaly = 0.0, False
            if not (log.full_tank and log.vehicle_id and log.odometer):
                continue
            previous_full = self.search([
                ('vehicle_id', '=', log.vehicle_id.id), ('full_tank', '=', True),
                ('odometer', '<', log.odometer), ('id', '!=', log.id or 0),
            ], order='odometer desc', limit=1)
            if not previous_full:
                continue
            between = self.search([
                ('vehicle_id', '=', log.vehicle_id.id), ('odometer', '>', previous_full.odometer),
                ('odometer', '<', log.odometer), ('id', '!=', log.id or 0),
            ])
            litres = log.litres + sum(between.mapped('litres'))
            if litres:
                log.mileage_kmpl = (log.odometer - previous_full.odometer) / litres
                bench = log.vehicle_id.cp_mileage_benchmark
                threshold = (log.company_id or self.env.company).cp_fuel_anomaly_percent or 15.0
                log.is_anomaly = bool(bench and log.mileage_kmpl < bench * (1 - threshold / 100.0))

    @api.constrains('litres', 'odometer')
    def _check_values(self):
        for log in self:
            if log.litres <= 0:
                raise ValidationError(_('Litres must be positive.'))

    @api.model_create_multi
    def create(self, vals_list):
        logs = super().create(vals_list)
        Odometer = self.env['fleet.vehicle.odometer']
        for log in logs:
            if log.odometer and log.odometer > log.vehicle_id.odometer:
                Odometer.create({'vehicle_id': log.vehicle_id.id, 'value': log.odometer,
                                 'date': fields.Date.to_date(log.date)})
            if log.trip_id:
                self.env['cp.trip.expense'].create({
                    'trip_id': log.trip_id.id, 'expense_type': 'fuel', 'amount': log.amount,
                    'payment_mode': 'fuel_card' if log.payment_mode == 'card' else 'cash',
                    'paid_to': log.station, 'date': fields.Date.to_date(log.date),
                })
            if log.is_anomaly:
                self.env['cp.exception'].cp_raise(
                    'fuel', log, _('Low mileage on %s', log.vehicle_id.license_plate or log.vehicle_id.name),
                    severity='medium', vehicle=log.vehicle_id,
                    detail=_('%(m).2f km/l vs benchmark %(b).2f km/l', m=log.mileage_kmpl,
                             b=log.vehicle_id.cp_mileage_benchmark),
                    suggestion=_('Check the fill receipt, tank seal and route for diversions.'))
                log.vehicle_id.message_post(body=_(
                    'Fuel anomaly: %(m).2f km/l against a %(b).2f km/l benchmark at odometer %(o)s.',
                    m=log.mileage_kmpl, b=log.vehicle_id.cp_mileage_benchmark, o=log.odometer))
        return logs


class CpMaintenancePlan(models.Model):
    _name = 'cp.maintenance.plan'
    _description = 'Preventive Maintenance Plan'
    _order = 'next_due_date'

    name = fields.Char(required=True, help='e.g. Engine oil change')
    vehicle_id = fields.Many2one('fleet.vehicle', required=True, ondelete='cascade')
    service_type_id = fields.Many2one('fleet.service.type', string='Service Type')
    interval_km = fields.Float(string='Every (km)')
    interval_days = fields.Integer(string='Every (days)')
    last_done_date = fields.Date(default=fields.Date.context_today)
    last_done_odometer = fields.Float()
    next_due_date = fields.Date(compute='_compute_next', store=True)
    next_due_odometer = fields.Float(compute='_compute_next', store=True)
    current_odometer = fields.Float(related='vehicle_id.odometer', string='Current Odometer')
    due_state = fields.Selection([('ok', 'OK'), ('soon', 'Due Soon'), ('overdue', 'Overdue')],
                                 compute='_compute_due_state')
    active = fields.Boolean(default=True)

    @api.depends('last_done_date', 'last_done_odometer', 'interval_km', 'interval_days')
    def _compute_next(self):
        for plan in self:
            plan.next_due_date = (plan.last_done_date + relativedelta(days=plan.interval_days)) \
                if plan.interval_days and plan.last_done_date else False
            plan.next_due_odometer = (plan.last_done_odometer + plan.interval_km) if plan.interval_km else 0.0

    def _compute_due_state(self):
        today = fields.Date.context_today(self)
        for plan in self:
            overdue = (plan.next_due_date and plan.next_due_date < today) or \
                (plan.next_due_odometer and plan.current_odometer >= plan.next_due_odometer)
            soon = (plan.next_due_date and plan.next_due_date <= today + timedelta(days=7)) or \
                (plan.next_due_odometer and plan.current_odometer >= plan.next_due_odometer - 500)
            plan.due_state = 'overdue' if overdue else ('soon' if soon else 'ok')

    def action_mark_done(self):
        Service = self.env['fleet.vehicle.log.services']
        default_type = self.env.ref('cargo_pilot_tms.fleet_service_type_preventive', raise_if_not_found=False)
        for plan in self:
            vals = {
                'vehicle_id': plan.vehicle_id.id,
                'service_type_id': (plan.service_type_id or default_type).id,
                'description': plan.name,
                'date': fields.Date.context_today(self),
                'state': 'done',
            }
            if plan.current_odometer:
                vals['odometer'] = plan.current_odometer
            Service.create(vals)
            plan.write({'last_done_date': fields.Date.context_today(self),
                        'last_done_odometer': plan.current_odometer})
        return True

    @api.model
    def _cron_maintenance_due(self):
        for plan in self.search([]):
            if plan.due_state in ('soon', 'overdue'):
                summary = _('Maintenance %(s)s: %(n)s', s=dict(self._fields['due_state'].selection)[plan.due_state],
                            n=plan.name)
                vehicle = plan.vehicle_id
                if not vehicle.activity_ids.filtered(lambda a: a.summary == summary):
                    vehicle.activity_schedule('mail.mail_activity_data_todo', summary=summary,
                                              user_id=(vehicle.manager_id or self.env.user).id)
        return True


class CpTyre(models.Model):
    _name = 'cp.tyre'
    _description = 'Tyre'
    _order = 'vehicle_id, position'

    name = fields.Char(string='Serial No.', required=True)
    brand = fields.Char()
    size = fields.Char()
    vehicle_id = fields.Many2one('fleet.vehicle', ondelete='set null')
    position = fields.Selection([
        ('fl', 'Front Left'), ('fr', 'Front Right'),
        ('r1lo', 'Rear 1 Left Outer'), ('r1li', 'Rear 1 Left Inner'),
        ('r1ri', 'Rear 1 Right Inner'), ('r1ro', 'Rear 1 Right Outer'),
        ('r2lo', 'Rear 2 Left Outer'), ('r2li', 'Rear 2 Left Inner'),
        ('r2ri', 'Rear 2 Right Inner'), ('r2ro', 'Rear 2 Right Outer'),
        ('spare', 'Spare'),
    ])
    state = fields.Selection([('stock', 'In Stock'), ('fitted', 'Fitted'), ('retread', 'At Retreading'),
                              ('scrapped', 'Scrapped')], default='stock')
    purchase_date = fields.Date()
    cost = fields.Monetary()
    fitted_date = fields.Date()
    fitted_odometer = fields.Float()
    previous_km = fields.Float(string='Km Before This Fitment')
    expected_life_km = fields.Float(default=80000.0)
    km_run = fields.Float(compute='_compute_wear', string='Total Km Run')
    wear_pct = fields.Float(compute='_compute_wear', string='Life Used %')
    cost_per_km = fields.Float(compute='_compute_wear', digits=(8, 3))
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)

    _serial_uniq = models.Constraint(
        'unique(name)',
        'Tyre serial numbers must be unique.',
    )

    @api.depends('vehicle_id.odometer', 'fitted_odometer', 'previous_km', 'expected_life_km', 'state')
    def _compute_wear(self):
        for tyre in self:
            current = (tyre.vehicle_id.odometer - tyre.fitted_odometer) \
                if tyre.state == 'fitted' and tyre.vehicle_id else 0.0
            tyre.km_run = tyre.previous_km + max(current, 0.0)
            tyre.wear_pct = (tyre.km_run / tyre.expected_life_km * 100.0) if tyre.expected_life_km else 0.0
            tyre.cost_per_km = (tyre.cost / tyre.km_run) if tyre.km_run else 0.0

    def action_fit(self):
        for tyre in self:
            if not tyre.vehicle_id or not tyre.position:
                raise ValidationError(_('Set the vehicle and wheel position for tyre %s.', tyre.name))
            clash = self.search([('vehicle_id', '=', tyre.vehicle_id.id), ('position', '=', tyre.position),
                                 ('state', '=', 'fitted'), ('id', '!=', tyre.id)], limit=1)
            if clash:
                raise ValidationError(_('Tyre %s is already fitted at that position.', clash.name))
            tyre.write({'state': 'fitted', 'fitted_date': fields.Date.context_today(self),
                        'fitted_odometer': tyre.vehicle_id.odometer})

    def action_remove(self):
        for tyre in self:
            tyre.write({'previous_km': tyre.km_run, 'state': 'stock', 'vehicle_id': False, 'position': False})


class CpDocument(models.Model):
    _name = 'cp.document'
    _description = 'Vehicle / Driver Compliance Document'
    _order = 'expiry_date'

    name = fields.Char(string='Document No.')
    doc_type = fields.Selection([
        ('rc', 'Registration Certificate'), ('insurance', 'Insurance'), ('fitness', 'Fitness Certificate'),
        ('permit', 'National / State Permit'), ('puc', 'Pollution (PUC)'), ('road_tax', 'Road Tax'),
        ('license', 'Driving License'), ('hazmat', 'Hazardous Goods License'), ('other', 'Other'),
    ], required=True)
    vehicle_id = fields.Many2one('fleet.vehicle', ondelete='cascade')
    driver_id = fields.Many2one('res.partner', ondelete='cascade', domain=[('cp_is_driver', '=', True)])
    issue_date = fields.Date()
    expiry_date = fields.Date(required=True)
    attachment = fields.Binary(attachment=True)
    attachment_name = fields.Char()
    days_left = fields.Integer(compute='_compute_status')
    status = fields.Selection([('valid', 'Valid'), ('expiring', 'Expiring'), ('expired', 'Expired')],
                              compute='_compute_status')

    @api.constrains('vehicle_id', 'driver_id')
    def _check_owner(self):
        for doc in self:
            if not doc.vehicle_id and not doc.driver_id:
                raise ValidationError(_('Link the document to a vehicle or a driver.'))

    def _compute_display_name(self):
        types = dict(self._fields['doc_type'].selection)
        for doc in self:
            owner = doc.vehicle_id.license_plate or doc.driver_id.name or ''
            doc.display_name = '%s - %s' % (types.get(doc.doc_type, ''), owner)

    @api.depends('expiry_date')
    def _compute_status(self):
        today = fields.Date.context_today(self)
        alert = self.env.company.cp_document_alert_days or 15
        for doc in self:
            doc.days_left = (doc.expiry_date - today).days if doc.expiry_date else 0
            doc.status = 'expired' if doc.days_left < 0 else ('expiring' if doc.days_left <= alert else 'valid')

    @api.model
    def _cron_document_expiry(self):
        alert = self.env.company.cp_document_alert_days or 15
        limit = fields.Date.context_today(self) + timedelta(days=alert)
        types = dict(self._fields['doc_type'].selection)
        for doc in self.search([('expiry_date', '<=', limit)]):
            target = doc.vehicle_id or doc.driver_id
            self.env['cp.exception'].cp_raise(
                'document', doc, _('%(t)s of %(o)s expires %(d)s', t=types[doc.doc_type],
                                   o=doc.vehicle_id.license_plate or target.display_name,
                                   d=doc.expiry_date),
                severity='high' if doc.expiry_date < fields.Date.context_today(self) else 'low',
                vehicle=doc.vehicle_id, suggestion=_('Renew it and upload the new copy.'))
            summary = _('%(t)s expires %(d)s', t=types[doc.doc_type], d=doc.expiry_date)
            if doc.vehicle_id and not doc.vehicle_id.activity_ids.filtered(lambda a: a.summary == summary):
                doc.vehicle_id.activity_schedule('mail.mail_activity_data_warning', summary=summary,
                                                 date_deadline=doc.expiry_date,
                                                 user_id=(doc.vehicle_id.manager_id or self.env.user).id)
            elif doc.driver_id and not doc.vehicle_id:
                doc.driver_id.message_post(body=summary)
        return True


class CpDrivingEvent(models.Model):
    _name = 'cp.driving.event'
    _description = 'Driving Safety Event'
    _order = 'event_time desc'

    PENALTY = {'overspeed': 2.0, 'harsh_brake': 1.5, 'harsh_accel': 1.0, 'sharp_turn': 1.0,
               'distraction': 3.0, 'drowsiness': 4.0, 'seatbelt': 2.0, 'collision': 10.0, 'night_drive': 0.5}
    SEVERITY = {'low': 0.5, 'medium': 1.0, 'high': 2.0}

    vehicle_id = fields.Many2one('fleet.vehicle', required=True, index=True)
    driver_id = fields.Many2one('res.partner', index=True, domain=[('cp_is_driver', '=', True)])
    trip_id = fields.Many2one('cp.trip')
    event_time = fields.Datetime(default=fields.Datetime.now, required=True)
    event_type = fields.Selection([
        ('overspeed', 'Overspeeding'), ('harsh_brake', 'Harsh Braking'), ('harsh_accel', 'Harsh Acceleration'),
        ('sharp_turn', 'Sharp Cornering'), ('distraction', 'Distraction / Phone Use'),
        ('drowsiness', 'Drowsiness'), ('seatbelt', 'No Seatbelt'), ('collision', 'Collision'),
        ('night_drive', 'Restricted-Hour Driving'),
    ], required=True)
    severity = fields.Selection([('low', 'Low'), ('medium', 'Medium'), ('high', 'High')], default='medium')
    speed = fields.Float(string='Speed (km/h)')
    latitude = fields.Float(digits=(10, 7))
    longitude = fields.Float(digits=(10, 7))
    video_url = fields.Char(string='Dashcam Clip URL')
    penalty = fields.Float(compute='_compute_penalty', store=True)
    coached = fields.Boolean(string='Driver Coached')

    @api.depends('event_type', 'severity')
    def _compute_penalty(self):
        for event in self:
            event.penalty = self.PENALTY.get(event.event_type, 1.0) * self.SEVERITY.get(event.severity, 1.0)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('vehicle_id') and not vals.get('driver_id'):
                vehicle = self.env['fleet.vehicle'].browse(vals['vehicle_id'])
                vals['driver_id'] = vehicle.driver_id.id
        events = super().create(vals_list)
        for event in events.filtered(lambda e: e.severity == 'high'):
            self.env['cp.exception'].cp_raise(
                'safety', event, _('%(e)s: %(d)s', e=dict(self._fields['event_type'].selection)[event.event_type],
                                   d=event.driver_id.name or event.vehicle_id.license_plate or ''),
                severity='critical' if event.event_type == 'collision' else 'medium', vehicle=event.vehicle_id,
                trip=event.trip_id, suggestion=_('Review the dashcam clip and coach the driver.'))
        return events
