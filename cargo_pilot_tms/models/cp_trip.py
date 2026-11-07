# -*- coding: utf-8 -*-
import uuid
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .cp_master import haversine_km

ROAD_FACTOR = 1.25

TRIP_STATES = [
    ('draft', 'Planned'),
    ('assigned', 'Vehicle Assigned'),
    ('reported', 'Reported at Origin'),
    ('loading', 'Loading'),
    ('in_transit', 'In Transit'),
    ('arrived', 'Arrived'),
    ('delivered', 'Delivered'),
    ('closed', 'Closed'),
    ('cancel', 'Cancelled'),
]


class CpTrip(models.Model):
    _name = 'cp.trip'
    _description = 'Trip / Shipment'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'portal.mixin']
    _order = 'planned_dispatch desc, id desc'

    name = fields.Char(default=lambda self: _('New'), copy=False, readonly=True)
    indent_id = fields.Many2one('cp.indent', ondelete='set null', index=True)
    auction_id = fields.Many2one('cp.auction', ondelete='set null', index=True)
    contract_line_id = fields.Many2one('cp.rate.contract.line', string='Contract Rate')
    lane_id = fields.Many2one('cp.lane', required=True, tracking=True)
    origin_id = fields.Many2one(related='lane_id.origin_id', store=True, string='Origin')
    destination_id = fields.Many2one(related='lane_id.destination_id', store=True, string='Destination')
    vehicle_type_id = fields.Many2one('cp.vehicle.type')
    transporter_id = fields.Many2one('res.partner', string='Transporter', tracking=True, index=True,
                                     domain=[('cp_is_transporter', '=', True)])
    consignee_id = fields.Many2one('res.partner', string='Consignee')

    # Vehicle & driver
    vehicle_id = fields.Many2one('fleet.vehicle', string='Own Vehicle', tracking=True)
    vehicle_number = fields.Char(string='Vehicle No.', tracking=True)
    is_market_vehicle = fields.Boolean(string='Market (Hired) Vehicle')
    driver_id = fields.Many2one('res.partner', string='Driver', domain=[('cp_is_driver', '=', True)])
    driver_name = fields.Char()
    driver_phone = fields.Char()

    # Schedule
    planned_dispatch = fields.Datetime(string='Planned Dispatch', default=fields.Datetime.now)
    actual_dispatch = fields.Datetime(readonly=True)
    promised_delivery = fields.Datetime(string='Promised Delivery')
    eta = fields.Datetime(string='ETA', readonly=True)
    actual_arrival = fields.Datetime(readonly=True)
    actual_delivery = fields.Datetime(readonly=True)
    state = fields.Selection(TRIP_STATES, default='draft', tracking=True)

    # Load & money
    loaded_weight_ton = fields.Float(string='Loaded Weight (t)')
    delivered_weight_ton = fields.Float(string='Delivered Weight (t)', readonly=True)
    agreed_freight = fields.Monetary(string='Agreed Freight', tracking=True)
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    eway_bill_no = fields.Char(string='E-way Bill No.')
    eway_bill_expiry = fields.Datetime(string='E-way Bill Valid Until')

    # Tracking
    ping_ids = fields.One2many('cp.trip.ping', 'trip_id', string='GPS Pings')
    event_ids = fields.One2many('cp.trip.event', 'trip_id', string='Timeline')
    last_latitude = fields.Float(digits=(10, 7), readonly=True)
    last_longitude = fields.Float(digits=(10, 7), readonly=True)
    last_ping_time = fields.Datetime(readonly=True)
    last_speed = fields.Float(string='Last Speed (km/h)', readonly=True)
    last_moved_time = fields.Datetime(readonly=True)
    gps_distance_km = fields.Float(string='GPS Distance (km)', readonly=True, digits=(12, 1))
    remaining_km = fields.Float(string='Remaining (km)', readonly=True, digits=(12, 1))
    progress_pct = fields.Float(compute='_compute_progress', string='Progress %')
    is_late = fields.Boolean(compute='_compute_is_late', store=True, string='Late')
    is_stopped = fields.Boolean(string='Stoppage Alert', readonly=True)
    has_left_origin = fields.Boolean(readonly=True)
    tracking_token = fields.Char(copy=False, default=lambda self: uuid.uuid4().hex, readonly=True)
    driver_token = fields.Char(copy=False, default=lambda self: uuid.uuid4().hex, readonly=True)
    tracking_url = fields.Char(compute='_compute_urls')
    driver_url = fields.Char(compute='_compute_urls')

    # Links
    epod_ids = fields.One2many('cp.epod', 'trip_id', string='ePODs')
    epod_state = fields.Selection([
        ('none', 'Not Received'), ('pending', 'Pending Review'),
        ('verified', 'Verified'), ('exception', 'Exception'),
    ], compute='_compute_epod_state', store=True, string='ePOD')
    consignment_ids = fields.One2many('cp.consignment', 'trip_id', string='Consignments')
    expense_ids = fields.One2many('cp.trip.expense', 'trip_id', string='Expenses')
    hire_slip_ids = fields.One2many('cp.hire.slip', 'trip_id', string='Hire Slips')
    gate_entry_ids = fields.One2many('cp.gate.entry', 'trip_id', string='Gate Entries')
    freight_bill_line_ids = fields.One2many('cp.freight.bill.line', 'trip_id', string='Freight Bill Lines')
    is_billed = fields.Boolean(compute='_compute_is_billed', store=True)

    # Transporter P&L
    revenue_amount = fields.Monetary(compute='_compute_pnl', string='Revenue')
    hire_amount = fields.Monetary(compute='_compute_pnl', string='Hire Cost')
    expense_amount = fields.Monetary(compute='_compute_pnl', string='Expense Total')
    profit_amount = fields.Monetary(compute='_compute_pnl', string='Profit')
    margin_pct = fields.Float(compute='_compute_pnl', string='Margin %')

    # Reference & sustainability
    customer_ref = fields.Char(string='Customer Reference', help='PO, sales order or delivery number.',
                               compute='_compute_customer_ref', store=True, readonly=False)
    co2_kg = fields.Float(string='CO2e (kg)', compute='_compute_co2', store=True, digits=(12, 1),
                          help='Well-to-wheel estimate: tonnes x km x vehicle emission factor (GLEC method).')
    co2_intensity = fields.Float(string='g CO2e / t-km', compute='_compute_co2', store=True, digits=(8, 1))
    issue_ids = fields.One2many('cp.exception', 'trip_id', string='Issues')
    open_issue_count = fields.Integer(compute='_compute_open_issues')

    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)

    @api.depends('indent_id.origin', 'order_ids.order_ref')
    def _compute_customer_ref(self):
        for rec in self:
            refs = list(dict.fromkeys(r.strip() for r in rec.order_ids.mapped('order_ref') if r and r.strip()))
            if refs:
                rec.customer_ref = ', '.join(refs)
            elif rec.indent_id.origin and not rec.customer_ref:
                rec.customer_ref = rec.indent_id.origin

    @api.depends('loaded_weight_ton', 'gps_distance_km', 'lane_id.distance_km', 'vehicle_type_id.emission_factor',
                 'state')
    def _compute_co2(self):
        for rec in self:
            km = rec.gps_distance_km if rec.gps_distance_km and rec.state in ('delivered', 'closed') \
                else (rec.lane_id.distance_km or rec.gps_distance_km)
            factor = rec.vehicle_type_id.emission_factor or 80.0
            tonnes = rec.loaded_weight_ton or 0.0
            rec.co2_kg = tonnes * km * factor / 1000.0
            rec.co2_intensity = factor if tonnes and km else 0.0

    def _compute_open_issues(self):
        for rec in self:
            rec.open_issue_count = len(rec.issue_ids.filtered(lambda i: i.state != 'resolved'))

    def action_view_issues(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('cargo_pilot_tms.action_cp_exception')
        action['domain'] = [('trip_id', '=', self.id)]
        action['context'] = {}
        return action

    @api.model
    def _group_expand_states(self, states, domain):
        return [key for key, _label in TRIP_STATES if key not in ('cancel', 'closed')]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('cp.trip') or _('New')
        trips = super().create(vals_list)
        for trip in trips:
            trip._cp_log('created', _('Trip planned on %s.', trip.lane_id.name))
            if trip.vehicle_id and not trip.vehicle_number:
                trip.vehicle_number = trip.vehicle_id.license_plate
        return trips

    def write(self, vals):
        if vals.get('vehicle_id') and not vals.get('vehicle_number'):
            vehicle = self.env['fleet.vehicle'].browse(vals['vehicle_id'])
            vals['vehicle_number'] = vehicle.license_plate
            if vehicle.driver_id and not vals.get('driver_id'):
                vals.setdefault('driver_id', vehicle.driver_id.id)
        return super().write(vals)

    def _compute_access_url(self):
        super()._compute_access_url()
        for rec in self:
            rec.access_url = '/my/cp/trips/%s' % rec.id

    def _compute_urls(self):
        for rec in self:
            base = rec.get_base_url()  # per record: get_base_url() only accepts one record
            rec.tracking_url = '%s/cp/track/%s' % (base, rec.tracking_token)
            rec.driver_url = '%s/cp/driver/%s' % (base, rec.driver_token)

    @api.onchange('driver_id')
    def _onchange_driver(self):
        for rec in self:
            if rec.driver_id:
                rec.driver_name = rec.driver_id.name
                rec.driver_phone = rec.driver_id.phone

    @api.depends('gps_distance_km', 'remaining_km', 'state', 'lane_id.distance_km')
    def _compute_progress(self):
        for rec in self:
            if rec.state in ('arrived', 'delivered', 'closed'):
                rec.progress_pct = 100.0
            elif rec.state == 'in_transit':
                total = rec.lane_id.distance_km or (rec.gps_distance_km + rec.remaining_km)
                done = max(total - rec.remaining_km, 0.0) if rec.remaining_km else rec.gps_distance_km
                rec.progress_pct = min(done / total * 100.0, 99.0) if total else 0.0
            else:
                rec.progress_pct = 0.0

    @api.depends('promised_delivery', 'eta', 'actual_arrival', 'actual_delivery')
    def _compute_is_late(self):
        for rec in self:
            reference = rec.actual_arrival or rec.actual_delivery or rec.eta
            rec.is_late = bool(rec.promised_delivery and reference and reference > rec.promised_delivery)

    @api.depends('epod_ids.state')
    def _compute_epod_state(self):
        for rec in self:
            states = rec.epod_ids.mapped('state')
            if 'verified' in states:
                rec.epod_state = 'verified'
            elif 'exception' in states:
                rec.epod_state = 'exception'
            elif 'submitted' in states:
                rec.epod_state = 'pending'
            else:
                rec.epod_state = 'none'

    @api.depends('freight_bill_line_ids.bill_id.state')
    def _compute_is_billed(self):
        for rec in self:
            rec.is_billed = any(l.bill_id.state != 'cancel' for l in rec.freight_bill_line_ids)

    @api.depends('consignment_ids.total_amount', 'agreed_freight', 'hire_slip_ids.total_hire',
                 'hire_slip_ids.state', 'expense_ids.amount')
    def _compute_pnl(self):
        for rec in self:
            consignments = rec.consignment_ids.filtered(lambda c: c.state != 'cancel')
            revenue = sum(consignments.mapped('total_amount')) if consignments else rec.agreed_freight
            hire = sum(rec.hire_slip_ids.filtered(lambda h: h.state != 'cancel').mapped('total_hire'))
            expenses = sum(rec.expense_ids.filtered(lambda e: e.expense_type != 'driver_advance').mapped('amount'))
            rec.revenue_amount = revenue
            rec.hire_amount = hire
            rec.expense_amount = expenses
            rec.profit_amount = revenue - hire - expenses
            rec.margin_pct = ((revenue - hire - expenses) / revenue * 100.0) if revenue else 0.0

    # ------------------------------------------------------------------
    # Timeline helper
    # ------------------------------------------------------------------
    def _cp_log(self, event_type, description, lat=None, lon=None, when=None):
        Event = self.env['cp.trip.event'].sudo()
        for rec in self:
            Event.create({
                'trip_id': rec.id,
                'event_type': event_type,
                'description': description,
                'latitude': lat or 0.0,
                'longitude': lon or 0.0,
                'event_time': when or fields.Datetime.now(),
            })

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def _cp_require(self, states):
        for rec in self:
            if rec.state not in states:
                raise UserError(_('%(trip)s is "%(state)s"; this step needs it to be %(need)s.',
                                  trip=rec.name, state=dict(TRIP_STATES)[rec.state],
                                  need=' / '.join(dict(TRIP_STATES)[s] for s in states)))

    def action_assign(self):
        self._cp_require(('draft',))
        for rec in self:
            if rec.shipment_mode == 'ltl':
                if not (rec.docket_number or rec.vehicle_number):
                    raise UserError(_('Enter the carrier docket / LR number for the LTL shipment %s.', rec.name))
                if not rec.transporter_id:
                    raise UserError(_('Set the LTL carrier for %s.', rec.name))
            else:
                if not rec.vehicle_number:
                    raise UserError(_('Enter the vehicle number for %s.', rec.name))
                if not rec.transporter_id and not rec.vehicle_id:
                    raise UserError(_('Set the transporter or an own-fleet vehicle for %s.', rec.name))
            rec._cp_accept_pending_offer()
            rec.state = 'assigned'
            if rec.shipment_mode == 'ltl' and not rec.vehicle_number:
                rec._cp_log('assigned', _('Booked with %(c)s, docket %(d)s.', c=rec.transporter_id.name,
                                          d=rec.docket_number))
            else:
                rec._cp_log('assigned', _('Vehicle %(v)s assigned, driver %(d)s.',
                                          v=rec.vehicle_number, d=rec.driver_name or rec.driver_id.name or '-'))
            rec._cp_after_assign()

    def action_report(self):
        self._cp_require(('assigned',))
        self.write({'state': 'reported', 'reported_at': fields.Datetime.now()})
        self._cp_log('reported', _('Vehicle reported at origin.'))

    def action_start_loading(self):
        self._cp_require(('assigned', 'reported'))
        self.write({'state': 'loading'})
        self._cp_log('loading', _('Loading started.'))

    def action_dispatch(self):
        self._cp_require(('assigned', 'reported', 'loading'))
        now = fields.Datetime.now()
        for rec in self:
            speed = rec.company_id.cp_default_speed_kmph or 40.0
            rec.write({
                'state': 'in_transit',
                'actual_dispatch': now,
                'remaining_km': rec.lane_id.distance_km,
                'eta': now + timedelta(hours=(rec.lane_id.distance_km or 0.0) / speed),
                'last_moved_time': now,
            })
            rec._cp_log('dispatched', _('Dispatched. ETA %s.', fields.Datetime.to_string(rec.eta)))
            if not rec.eway_bill_no:
                rec.message_post(body=_('Dispatched without an e-way bill number.'))

    def action_mark_arrived(self):
        self._cp_require(('in_transit',))
        now = fields.Datetime.now()
        self.write({'state': 'arrived', 'actual_arrival': now, 'remaining_km': 0.0, 'is_stopped': False})
        self._cp_log('arrived', _('Arrived at destination.'))

    def action_mark_delivered(self):
        self._cp_require(('in_transit', 'arrived'))
        now = fields.Datetime.now()
        for rec in self:
            rec.write({'state': 'delivered', 'actual_delivery': now,
                       'actual_arrival': rec.actual_arrival or now, 'remaining_km': 0.0, 'is_stopped': False})
            rec._cp_log('delivered', _('Delivered.'))
            self.env['cp.exception'].cp_auto_resolve(rec, ('late', 'stopped', 'eway', 'vehicle'),
                                                     _('Shipment delivered.'))
            rec.consignment_ids.filtered(lambda c: c.state in ('booked', 'in_transit')).write({'state': 'delivered'})
        self.mapped('indent_id')._cp_check_done()

    def action_close(self):
        self._cp_require(('delivered',))
        for rec in self:
            if rec.epod_state != 'verified' and not self.env.context.get('cp_force_close'):
                raise UserError(_('%s has no verified ePOD yet. Verify the ePOD before closing.', rec.name))
            rec.state = 'closed'
            rec._cp_log('closed', _('Trip closed.'))

    def action_cancel(self):
        for rec in self:
            if rec.state in ('delivered', 'closed'):
                raise UserError(_('Delivered trips cannot be cancelled.'))
            rec.state = 'cancel'
            rec._cp_log('cancelled', _('Trip cancelled.'))

    def action_reset_draft(self):
        self.filtered(lambda t: t.state == 'cancel').write({'state': 'draft'})

    def action_share_tracking(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': self.tracking_url,
            'target': 'new',
        }

    def action_open_driver_page(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_url', 'url': self.driver_url, 'target': 'new'}

    def action_new_epod(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'cp.epod',
            'view_mode': 'form',
            'context': {'default_trip_id': self.id,
                        'default_received_weight_ton': self.loaded_weight_ton},
        }

    # ------------------------------------------------------------------
    # Tracking
    # ------------------------------------------------------------------
    def cp_add_ping(self, latitude, longitude, speed=None, ping_time=None, source='api', place=None):
        """Record a position and run geofence, distance, ETA and stoppage logic."""
        self.ensure_one()
        ping_time = fields.Datetime.to_datetime(ping_time) or fields.Datetime.now()
        prev_lat, prev_lon = self.last_latitude, self.last_longitude
        had_previous = bool(self.last_ping_time)
        self.env['cp.trip.ping'].sudo().create({
            'trip_id': self.id, 'latitude': latitude, 'longitude': longitude,
            'speed': speed or 0.0, 'ping_time': ping_time, 'source': source, 'place': place or False,
        })
        vals = {'last_latitude': latitude, 'last_longitude': longitude,
                'last_ping_time': ping_time, 'last_speed': speed or 0.0}
        step_km = haversine_km(prev_lat, prev_lon, latitude, longitude) if had_previous else 0.0
        if step_km > 0.2:
            vals['last_moved_time'] = ping_time
            vals['is_stopped'] = False
            if self.is_stopped:
                self.env['cp.exception'].cp_auto_resolve(self, ('stopped',), _('Vehicle is moving again.'))
            if self.state in ('in_transit', 'arrived'):
                vals['gps_distance_km'] = self.gps_distance_km + step_km

        dest, origin = self.destination_id, self.origin_id
        if dest and (dest.latitude or dest.longitude):
            remaining = haversine_km(latitude, longitude, dest.latitude, dest.longitude) * ROAD_FACTOR
            vals['remaining_km'] = remaining
            if self.state == 'in_transit':
                kmph = speed if speed and speed > 5 else (self.company_id.cp_default_speed_kmph or 40.0)
                vals['eta'] = ping_time + timedelta(hours=remaining / kmph)

        self.sudo().write(vals)
        radius = self.company_id.cp_epod_geofence_m or None
        # Auto status from geofences
        if origin and self.state in ('assigned', 'reported', 'loading'):
            inside_origin = origin.cp_contains(latitude, longitude)
            if inside_origin and self.state == 'assigned':
                self.sudo().write({'state': 'reported', 'reported_at': ping_time})
                self._cp_log('geofence_in', _('Entered origin geofence.'), latitude, longitude, ping_time)
            elif not inside_origin and self.state in ('reported', 'loading'):
                self.sudo().action_dispatch()
                self._cp_log('geofence_out', _('Left origin geofence; auto-dispatched.'),
                             latitude, longitude, ping_time)
        if dest and self.state == 'in_transit' and dest.cp_contains(latitude, longitude, radius):
            self.sudo().write({'state': 'arrived', 'actual_arrival': ping_time, 'remaining_km': 0.0})
            self._cp_log('geofence_in', _('Entered destination geofence.'), latitude, longitude, ping_time)
        self._cp_check_late()
        return True

    def _cp_check_late(self):
        Issue = self.env['cp.exception']
        for rec in self.filtered(lambda t: t.state == 'in_transit' and t.is_late):
            hours = (rec.eta - rec.promised_delivery).total_seconds() / 3600.0 if rec.eta else 0
            Issue.cp_raise('late', rec, _('%(v)s will be %(h)s h late', v=rec.vehicle_number or rec.name,
                                           h=round(hours, 1)),
                           severity='high' if hours > 6 else 'medium',
                           detail=_('ETA %(eta)s vs promised %(p)s on %(lane)s.', eta=rec.eta,
                                    p=rec.promised_delivery, lane=rec.lane_id.name),
                           suggestion=_('Call the driver, then tell the consignee the new ETA.'))

    @api.model
    def _cron_trip_monitor(self):
        now = fields.Datetime.now()
        for company in self.env['res.company'].search([]):
            hours = company.cp_stoppage_alert_hours or 3.0
            limit = now - timedelta(hours=hours)
            stopped = self.search([
                ('company_id', '=', company.id), ('state', '=', 'in_transit'), ('is_stopped', '=', False),
                '|', ('last_moved_time', '<', limit), ('last_moved_time', '=', False),
            ])
            for trip in stopped:
                trip.is_stopped = True
                trip._cp_log('stoppage', _('No movement for more than %s hours.', hours))
                self.env['cp.exception'].cp_raise(
                    'stopped', trip, _('%(v)s has not moved for %(h)s h', v=trip.vehicle_number or trip.name, h=hours),
                    severity='high', detail=_('Last position update: %s', trip.last_ping_time or '-'),
                    suggestion=_('Check for breakdown, rest stop or GPS fault.'))
                trip.activity_schedule('mail.mail_activity_data_warning',
                                       summary=_('Stoppage: %s', trip.vehicle_number or trip.name),
                                       user_id=(trip.indent_id.user_id or self.env.user).id)
        soon = now + timedelta(hours=6)
        expiring = self.search([('state', 'in', ('loading', 'in_transit')),
                                ('eway_bill_expiry', '!=', False), ('eway_bill_expiry', '<=', soon)])
        for trip in expiring:
            self.env['cp.exception'].cp_raise(
                'eway', trip, _('E-way bill %s expires soon', trip.eway_bill_no), severity='high',
                suggestion=_('Extend the e-way bill on the GST portal before it lapses.'))
            if not trip.activity_ids.filtered(lambda a: 'E-way' in (a.summary or '')):
                trip.activity_schedule('mail.mail_activity_data_warning',
                                       summary=_('E-way bill expiring: %s', trip.eway_bill_no),
                                       user_id=(trip.indent_id.user_id or self.env.user).id)
        # Trips about to be placed without a vehicle
        soon_dispatch = now + timedelta(hours=2)
        for trip in self.search([('state', '=', 'draft'), ('planned_dispatch', '<=', soon_dispatch),
                                 ('planned_dispatch', '>=', now - timedelta(hours=12))]):
            self.env['cp.exception'].cp_raise(
                'vehicle', trip, _('No vehicle yet for %s', trip.name), severity='medium',
                suggestion=_('Remind the transporter or re-run the load on bidding.'))
        self.search([('state', '=', 'in_transit')])._cp_check_late()
        return True

    def cp_public_timeline(self):
        """Safe subset of the timeline for the public tracking page."""
        self.ensure_one()
        public = ('created', 'assigned', 'reported', 'loading', 'dispatched', 'arrived', 'delivered',
                  'geofence_in', 'geofence_out', 'epod')
        return self.event_ids.filtered(lambda e: e.event_type in public).sorted('event_time')


class CpTripPing(models.Model):
    _name = 'cp.trip.ping'
    _description = 'GPS Ping'
    _order = 'ping_time desc'

    trip_id = fields.Many2one('cp.trip', required=True, ondelete='cascade', index=True)
    latitude = fields.Float(digits=(10, 7), required=True)
    longitude = fields.Float(digits=(10, 7), required=True)
    speed = fields.Float(string='Speed (km/h)')
    ping_time = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    source = fields.Selection([
        ('api', 'GPS Device / API'), ('driver', 'Driver Phone'), ('manual', 'Manual'),
        ('sim', 'SIM Tracking'), ('fastag', 'FASTag Toll Plaza'),
    ], default='api')
    place = fields.Char(help='Toll plaza or place name, when known.')


class CpTripEvent(models.Model):
    _name = 'cp.trip.event'
    _description = 'Trip Timeline Event'
    _order = 'event_time desc, id desc'

    trip_id = fields.Many2one('cp.trip', required=True, ondelete='cascade', index=True)
    event_time = fields.Datetime(default=fields.Datetime.now, required=True)
    event_type = fields.Selection([
        ('created', 'Planned'), ('assigned', 'Assigned'), ('reported', 'Reported'), ('loading', 'Loading'),
        ('dispatched', 'Dispatched'), ('arrived', 'Arrived'), ('delivered', 'Delivered'),
        ('closed', 'Closed'), ('cancelled', 'Cancelled'), ('geofence_in', 'Geofence In'),
        ('geofence_out', 'Geofence Out'), ('stoppage', 'Stoppage'), ('epod', 'ePOD'),
        ('exception', 'Exception'), ('note', 'Note'), ('offer', 'Offer'), ('tracking', 'Tracking'),
        ('compliance', 'Compliance'),
    ], required=True)
    description = fields.Char()
    latitude = fields.Float(digits=(10, 7))
    longitude = fields.Float(digits=(10, 7))


class CpTripExpense(models.Model):
    _name = 'cp.trip.expense'
    _description = 'Trip Expense'
    _order = 'date desc, id desc'

    trip_id = fields.Many2one('cp.trip', required=True, ondelete='cascade', index=True)
    date = fields.Date(default=fields.Date.context_today, required=True)
    expense_type = fields.Selection([
        ('fuel', 'Fuel'), ('toll', 'Toll / FASTag'), ('driver_advance', 'Driver Advance'),
        ('driver_bata', 'Driver Allowance'), ('loading', 'Loading / Unloading'),
        ('repair', 'Breakdown Repair'), ('permit', 'Permit / Entry Tax'), ('other', 'Other'),
    ], required=True, default='fuel')
    amount = fields.Monetary(required=True)
    payment_mode = fields.Selection([
        ('cash', 'Cash'), ('bank', 'Bank / UPI'), ('fuel_card', 'Fuel Card'), ('fastag', 'FASTag'),
    ], default='cash')
    paid_to = fields.Char()
    settled = fields.Boolean(string='Settled with Driver')
    note = fields.Char()
    currency_id = fields.Many2one(related='trip_id.currency_id')
