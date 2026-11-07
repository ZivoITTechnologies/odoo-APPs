# -*- coding: utf-8 -*-
"""Shipment extensions for a shipper running FTL, LTL and multi-order loads:
orders on a shipment, freight cost per order, placement and POD timing, retailer appointments,
import containers and notification / webhook hooks."""
from datetime import timedelta

from odoo import api, fields, models, _

from .cp_common import CHANNELS, SHIPMENT_MODES, TRACKING_METHODS, split_serials

POD_BUCKETS = [
    ('na', 'Not due'),
    ('d0', 'Under 24 h'),
    ('d1', '1 to 3 days'),
    ('d3', '3 to 7 days'),
    ('d7', 'Over 7 days'),
    ('done', 'Received'),
]
ALLOCATION_BASIS = [
    ('weight', 'Weight'),
    ('volume', 'Volume'),
    ('value', 'Order value'),
    ('units', 'Units'),
    ('equal', 'Equal split'),
]


class CpTrip(models.Model):
    _inherit = 'cp.trip'

    shipment_mode = fields.Selection(SHIPMENT_MODES, string='Mode', default='ftl', required=True, tracking=True)
    channel = fields.Selection(CHANNELS, tracking=True)
    movement = fields.Selection(related='lane_id.movement', store=True)
    docket_number = fields.Char(string='Docket / LR No.', tracking=True, copy=False,
                                help='Carrier docket or lorry receipt number.')
    tracking_method = fields.Selection(TRACKING_METHODS, default='driver', string='Tracking')
    drop_count = fields.Integer(string='Drops', compute='_compute_order_totals', store=True)

    # Placement
    reported_at = fields.Datetime(string='Reported at Origin', readonly=True, copy=False)
    placement_delay_hours = fields.Float(compute='_compute_placement', store=True, string='Placement Delay (h)',
                                         aggregator='avg', digits=(8, 1))
    placement_on_time = fields.Boolean(compute='_compute_placement', store=True, string='Placed on Time')

    # Orders and freight cost per order
    order_ids = fields.One2many('cp.trip.order', 'trip_id', string='Orders', copy=True)
    order_count = fields.Integer(compute='_compute_order_totals', store=True, string='Order Count')
    order_value = fields.Monetary(compute='_compute_order_totals', store=True, string='Goods Value')
    order_weight_kg = fields.Float(compute='_compute_order_totals', store=True, string='Order Weight (kg)')
    box_count = fields.Integer(compute='_compute_order_totals', store=True, string='Boxes')
    serial_count = fields.Integer(compute='_compute_order_totals', store=True, string='Serials')
    allocation_basis = fields.Selection(ALLOCATION_BASIS, default='weight', required=True,
                                        string='Split Freight By')
    billed_amount = fields.Monetary(compute='_compute_freight_cost', store=True, string='Billed (Approved)')
    freight_cost_total = fields.Monetary(compute='_compute_freight_cost', store=True, string='Freight Cost',
                                         help='Approved transporter bill when there is one, otherwise the agreed freight.')
    freight_variance = fields.Monetary(compute='_compute_freight_cost', store=True, string='Billed vs Agreed')
    cost_per_kg = fields.Float(compute='_compute_freight_cost', store=True, string='Cost / kg', aggregator='avg',
                               digits=(10, 2))
    freight_pct = fields.Float(compute='_compute_freight_cost', store=True, string='Freight % of Value',
                               aggregator='avg', digits=(6, 2))

    # Channel requirements
    appointment_required = fields.Boolean(string='Appointment Needed')
    grn_required = fields.Boolean(string='GRN Needed with POD')
    serial_scan_required = fields.Boolean(string='Scan Serials at Delivery')
    appointment_ref = fields.Char(string='Appointment No.', tracking=True)
    appointment_start = fields.Datetime(string='Appointment From')
    appointment_end = fields.Datetime(string='Appointment To')
    appointment_status = fields.Selection([('none', 'Not needed'), ('missing', 'Not booked'), ('booked', 'Booked'),
                                           ('at_risk', 'At risk'), ('met', 'Met'), ('missed', 'Missed')],
                                          compute='_compute_appointment_status', store=True)

    # Import containers
    container_number = fields.Char(string='Container No.')
    container_size = fields.Selection([('20', "20 ft"), ('40', "40 ft"), ('40hc', "40 ft HC")])
    shipping_line = fields.Char()
    bill_of_entry = fields.Char(string='Bill of Entry')
    free_days = fields.Integer(string='Free Days', help='Free days before the shipping line charges detention.')
    container_picked_at = fields.Datetime(string='Picked from Port / CFS')
    empty_return_due = fields.Datetime(compute='_compute_container', store=True, string='Empty Return Due')
    empty_returned_at = fields.Datetime(string='Empty Returned')
    container_detention_days = fields.Integer(compute='_compute_container_detention', string='Detention Days')

    # POD timing
    pod_age_bucket = fields.Selection(POD_BUCKETS, default='na', string='POD Age', readonly=True, copy=False)
    epod_tat_hours = fields.Float(string='POD Turnaround (h)', readonly=True, copy=False, aggregator='avg',
                                  digits=(8, 1))

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('planned_dispatch', 'reported_at', 'actual_dispatch', 'company_id.cp_placement_grace_hours')
    def _compute_placement(self):
        for rec in self:
            placed = rec.reported_at or rec.actual_dispatch
            if not (placed and rec.planned_dispatch):
                rec.placement_delay_hours = 0.0
                rec.placement_on_time = False
                continue
            delay = (placed - rec.planned_dispatch).total_seconds() / 3600.0
            rec.placement_delay_hours = round(max(delay, 0.0), 1)
            rec.placement_on_time = delay <= (rec.company_id.cp_placement_grace_hours or 2.0)

    @api.depends('order_ids.weight_kg', 'order_ids.order_value', 'order_ids.quantity', 'order_ids.serial_numbers',
                 'order_ids.customer_id', 'order_ids.drop_point')
    def _compute_order_totals(self):
        for rec in self:
            orders = rec.order_ids
            rec.order_count = len(orders)
            rec.order_value = sum(orders.mapped('order_value'))
            rec.order_weight_kg = sum(orders.mapped('weight_kg'))
            rec.box_count = int(sum(orders.mapped('quantity')))
            rec.serial_count = sum(len(split_serials(o.serial_numbers)) for o in orders)
            rec.drop_count = max(len({(o.customer_id.id or 0, o.drop_point or '') for o in orders}), 1)

    @api.depends('agreed_freight', 'freight_bill_line_ids.approved_amount', 'freight_bill_line_ids.audited',
                 'freight_bill_line_ids.bill_id.state', 'loaded_weight_ton', 'order_weight_kg', 'order_value')
    def _compute_freight_cost(self):
        for rec in self:
            lines = rec.freight_bill_line_ids.filtered(lambda l: l.audited and l.bill_id.state == 'approved')
            billed = sum(lines.mapped('approved_amount'))
            rec.billed_amount = billed
            rec.freight_cost_total = billed if lines else rec.agreed_freight
            rec.freight_variance = (billed - rec.agreed_freight) if lines else 0.0
            kg = rec.order_weight_kg or (rec.loaded_weight_ton or 0.0) * 1000.0
            rec.cost_per_kg = rec.freight_cost_total / kg if kg else 0.0
            rec.freight_pct = rec.freight_cost_total / rec.order_value * 100.0 if rec.order_value else 0.0

    @api.depends('appointment_required', 'appointment_ref', 'appointment_start', 'appointment_end',
                 'actual_arrival', 'eta', 'state')
    def _compute_appointment_status(self):
        for rec in self:
            if not rec.appointment_required and not rec.appointment_ref:
                rec.appointment_status = 'none'
            elif not rec.appointment_end:
                rec.appointment_status = 'missing'
            elif rec.actual_arrival:
                start = rec.appointment_start or rec.appointment_end - timedelta(hours=4)
                rec.appointment_status = 'met' if start - timedelta(hours=1) <= rec.actual_arrival <= rec.appointment_end \
                    else 'missed'
            elif rec.state == 'in_transit' and rec.eta and rec.eta > rec.appointment_end:
                rec.appointment_status = 'at_risk'
            else:
                rec.appointment_status = 'booked'

    @api.depends('container_picked_at', 'free_days')
    def _compute_container(self):
        for rec in self:
            rec.empty_return_due = rec.container_picked_at + timedelta(days=rec.free_days) \
                if rec.container_picked_at and rec.free_days else False

    def _compute_container_detention(self):
        now = fields.Datetime.now()
        for rec in self:
            if not rec.empty_return_due:
                rec.container_detention_days = 0
                continue
            end = rec.empty_returned_at or now
            rec.container_detention_days = max((end - rec.empty_return_due).days + (1 if end > rec.empty_return_due else 0), 0)

    @api.depends('indent_id.origin', 'order_ids.order_ref')
    def _compute_customer_ref(self):
        super()._compute_customer_ref()
        for rec in self:
            if rec.order_ids and not rec.customer_ref:
                refs = [r for r in rec.order_ids.mapped('order_ref') if r]
                rec.customer_ref = ', '.join(refs[:5]) + (' +%s' % (len(refs) - 5) if len(refs) > 5 else '')

    # ------------------------------------------------------------------
    # Hooks used by other parts and add-ons
    # ------------------------------------------------------------------
    def _cp_after_assign(self):
        """Called once a vehicle or docket is assigned (add-ons update e-way bills here)."""
        return True

    def _cp_notify(self, event, partner=None, body=None, url=None):
        """Tell a transporter, driver or customer about a shipment event.

        The core posts it on the shipment (and e-mails followers / the partner). Add-ons add channels
        such as WhatsApp. ``event`` is one of: offer, assigned, dispatched, delivered, pod_missing.
        """
        for rec in self:
            text = body or _('%(trip)s: %(event)s', trip=rec.name, event=event)
            if url:
                text = '%s %s' % (text, url)
            partner_ids = partner.ids if partner else []
            rec.message_post(body=text, partner_ids=partner_ids, message_type='comment',
                             subtype_xmlid='mail.mt_comment' if partner_ids else 'mail.mt_note')
        return True

    def _cp_rate_kwargs(self):
        """Pricing inputs for this shipment (used by the freight audit)."""
        self.ensure_one()
        return {
            'goods_value': self.order_value,
            'boxes': self.box_count,
            'drops': self.drop_count or 1,
            'on_date': fields.Date.to_date(self.planned_dispatch),
            'oda': bool(self.destination_id.is_oda),
        }

    def _cp_pod_bucket(self, now=None):
        self.ensure_one()
        now = now or fields.Datetime.now()
        if self.epod_state == 'verified':
            return 'done'
        reference = self.actual_arrival or self.actual_delivery
        if self.state not in ('arrived', 'delivered', 'closed') or not reference:
            return 'na'
        hours = (now - reference).total_seconds() / 3600.0
        if hours < 24:
            return 'd0'
        if hours < 72:
            return 'd1'
        if hours < 168:
            return 'd3'
        return 'd7'

    def _cp_refresh_pod_bucket(self):
        now = fields.Datetime.now()
        for rec in self:
            bucket = rec._cp_pod_bucket(now)
            if rec.pod_age_bucket != bucket:
                rec.sudo().pod_age_bucket = bucket

    # ------------------------------------------------------------------
    # Workflow additions
    # ------------------------------------------------------------------
    def action_mark_arrived(self):
        res = super().action_mark_arrived()
        self._cp_refresh_pod_bucket()
        return res

    def action_mark_delivered(self):
        res = super().action_mark_delivered()
        self._cp_refresh_pod_bucket()
        self._cp_notify_customers('delivered')
        return res

    def action_dispatch(self):
        res = super().action_dispatch()
        self._cp_notify_customers('dispatched')
        return res

    def _cp_notify_customers(self, event):
        for rec in self:
            customers = rec.order_ids.customer_id | rec.consignee_id
            customers = customers.filtered(lambda p: p.email or p.phone)
            if not customers:
                continue
            if event == 'dispatched':
                body = _('Your shipment %(trip)s from %(o)s is on the way. Track it here:',
                         trip=rec.name, o=rec.origin_id.name)
            else:
                body = _('Your shipment %s has been delivered.', rec.name)
            rec._cp_notify(event, partner=customers, body=body, url=rec.tracking_url if event == 'dispatched' else None)

    def _cp_check_late(self):
        res = super()._cp_check_late()
        Issue = self.env['cp.exception']
        for rec in self.filtered(lambda t: t.state == 'in_transit' and t.appointment_status == 'at_risk'):
            Issue.cp_raise('appointment', rec, _('%(t)s may miss appointment %(a)s', t=rec.name,
                                                 a=rec.appointment_ref or ''),
                           severity='high', detail=_('ETA %(e)s, slot ends %(s)s', e=rec.eta, s=rec.appointment_end),
                           suggestion=_('Ask the retailer for a later slot before the truck arrives.'))
        return res

    @api.model
    def _cron_trip_followups(self):
        """Hourly: POD ageing buckets, container free time and missing appointments."""
        open_pod = self.search([('state', 'in', ('arrived', 'delivered', 'closed')), ('pod_age_bucket', '!=', 'done')])
        open_pod._cp_refresh_pod_bucket()
        Issue = self.env['cp.exception']
        soon = fields.Datetime.now() + timedelta(hours=24)
        for trip in self.search([('empty_return_due', '!=', False), ('empty_returned_at', '=', False),
                                 ('empty_return_due', '<=', soon), ('state', '!=', 'cancel')]):
            Issue.cp_raise('container', trip, _('Return empty container %(c)s by %(d)s',
                                                 c=trip.container_number or trip.name, d=trip.empty_return_due),
                           severity='high' if trip.container_detention_days else 'medium',
                           detail=_('%s detention day(s) so far.', trip.container_detention_days),
                           suggestion=_('Book the empty return with the transporter today.'))
        for trip in self.search([('appointment_required', '=', True), ('appointment_end', '=', False),
                                 ('state', 'in', ('draft', 'assigned', 'reported', 'loading', 'in_transit'))]):
            Issue.cp_raise('appointment', trip, _('Book the delivery appointment for %s', trip.name),
                           severity='medium', suggestion=_("Get a slot on the retailer's vendor portal and enter it here."))
        return True

    def action_mark_empty_returned(self):
        self.write({'empty_returned_at': fields.Datetime.now()})
        for rec in self:
            self.env['cp.exception'].cp_auto_resolve(rec, ('container',), _('Empty container returned.'))

    def action_view_orders(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('cargo_pilot_tms.action_cp_trip_order')
        action['domain'] = [('trip_id', '=', self.id)]
        action['context'] = {'default_trip_id': self.id}
        return action

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        trips = super().create(vals_list)
        Webhook = self.env['cp.webhook'].sudo()
        for trip in trips:
            Webhook._cp_emit('shipment.created', trip)
        return trips

    def write(self, vals):
        before = {rec.id: rec.state for rec in self} if 'state' in vals else {}
        res = super().write(vals)
        if before:
            Webhook = self.env['cp.webhook'].sudo()
            for rec in self:
                if before.get(rec.id) != rec.state:
                    Webhook._cp_emit('shipment.status_changed', rec, {'previous_state': before.get(rec.id)})
                    if rec.state in ('delivered', 'closed'):
                        Webhook._cp_emit('order.freight_allocated', rec)
        return res

    def cp_api_dict(self, full=False):
        """Shape used by the REST API and webhooks."""
        self.ensure_one()
        data = {
            'name': self.name, 'state': self.state, 'mode': self.shipment_mode, 'channel': self.channel or None,
            'lane': self.lane_id.name, 'origin': self.origin_id.code or self.origin_id.name,
            'destination': self.destination_id.code or self.destination_id.name,
            'movement': self.movement or None,
            'transporter': self.transporter_id.name or None, 'vehicle_number': self.vehicle_number or None,
            'docket_number': self.docket_number or None, 'driver_phone': self.driver_phone or None,
            'eway_bill_no': self.eway_bill_no or None,
            'planned_dispatch': self.planned_dispatch, 'actual_dispatch': self.actual_dispatch,
            'eta': self.eta, 'promised_delivery': self.promised_delivery, 'actual_delivery': self.actual_delivery,
            'is_late': self.is_late, 'epod_state': self.epod_state, 'tracking_url': self.tracking_url,
            'agreed_freight': self.agreed_freight, 'freight_cost': self.freight_cost_total,
            'currency': self.currency_id.name, 'reference': self.customer_ref or None,
            'acceptance': self.acceptance_state,
        }
        if full:
            data['orders'] = [o.cp_api_dict() for o in self.order_ids]
            data['last_position'] = {'latitude': self.last_latitude, 'longitude': self.last_longitude,
                                     'time': self.last_ping_time} if self.last_ping_time else None
            data['timeline'] = [{'time': e.event_time, 'type': e.event_type, 'text': e.description}
                                for e in self.event_ids.sorted('event_time')]
        return data


class CpTripOrder(models.Model):
    _name = 'cp.trip.order'
    _description = 'Order on a Shipment'
    _order = 'trip_id desc, id'

    trip_id = fields.Many2one('cp.trip', required=True, ondelete='cascade', index=True)
    trip_state = fields.Selection(related='trip_id.state', store=True, string='Shipment Status')
    shipment_mode = fields.Selection(related='trip_id.shipment_mode', store=True)
    transporter_id = fields.Many2one(related='trip_id.transporter_id', store=True)
    lane_id = fields.Many2one(related='trip_id.lane_id', store=True)
    planned_dispatch = fields.Datetime(related='trip_id.planned_dispatch', store=True, string='Dispatch Date')
    order_ref = fields.Char(string='Order No.', required=True, index=True)
    invoice_ref = fields.Char(string='Invoice No.', index=True)
    channel = fields.Selection(CHANNELS)
    customer_id = fields.Many2one('res.partner', string='Customer')
    drop_point = fields.Char()
    product_id = fields.Many2one('product.product', string='Product')
    description = fields.Char()
    quantity = fields.Float(string='Qty / Boxes', default=1.0)
    weight_kg = fields.Float(string='Weight (kg)')
    volume_cbm = fields.Float(string='Volume (m³)')
    order_value = fields.Monetary(string='Order Value')
    serial_numbers = fields.Text(string='Serial Numbers', help='One per line, or separated by commas.')
    serial_count = fields.Integer(compute='_compute_serial_count', store=True, string='Serials')
    eway_bill_no = fields.Char(string='E-way Bill No.')
    share_pct = fields.Float(compute='_compute_freight', store=True, string='Share %', digits=(6, 2))
    freight_cost = fields.Monetary(compute='_compute_freight', store=True, string='Freight Cost')
    freight_per_unit = fields.Monetary(compute='_compute_freight', store=True, string='Freight / Unit')
    freight_pct = fields.Float(compute='_compute_freight', store=True, string='Freight % of Value',
                               aggregator='avg', digits=(6, 2))
    currency_id = fields.Many2one(related='trip_id.currency_id')
    company_id = fields.Many2one(related='trip_id.company_id', store=True)

    @api.depends('serial_numbers')
    def _compute_serial_count(self):
        for rec in self:
            rec.serial_count = len(split_serials(rec.serial_numbers))

    @api.depends('trip_id.freight_cost_total', 'trip_id.allocation_basis', 'trip_id.order_ids.weight_kg',
                 'trip_id.order_ids.volume_cbm', 'trip_id.order_ids.order_value', 'trip_id.order_ids.quantity',
                 'weight_kg', 'volume_cbm', 'order_value', 'quantity')
    def _compute_freight(self):
        for rec in self:
            trip = rec.trip_id
            siblings = trip.order_ids
            basis = trip.allocation_basis or 'weight'
            field = {'weight': 'weight_kg', 'volume': 'volume_cbm', 'value': 'order_value',
                     'units': 'quantity'}.get(basis)
            total = sum(siblings.mapped(field)) if field else 0.0
            if field and total:
                share = (rec[field] or 0.0) / total
            else:
                share = 1.0 / len(siblings) if siblings else 0.0
            cost = (trip.freight_cost_total or 0.0) * share
            rec.share_pct = round(share * 100.0, 2)
            rec.freight_cost = round(cost, 2)
            rec.freight_per_unit = round(cost / rec.quantity, 2) if rec.quantity else 0.0
            rec.freight_pct = round(cost / rec.order_value * 100.0, 2) if rec.order_value else 0.0

    def cp_api_dict(self):
        self.ensure_one()
        return {
            'order_ref': self.order_ref, 'invoice_ref': self.invoice_ref or None, 'channel': self.channel or None,
            'customer': self.customer_id.name or None, 'quantity': self.quantity, 'weight_kg': self.weight_kg,
            'volume_cbm': self.volume_cbm, 'order_value': self.order_value,
            'serial_numbers': split_serials(self.serial_numbers), 'eway_bill_no': self.eway_bill_no or None,
            'freight_cost': self.freight_cost, 'freight_share_pct': self.share_pct,
            'freight_pct_of_value': self.freight_pct, 'shipment': self.trip_id.name,
            'shipment_state': self.trip_state, 'mode': self.shipment_mode,
        }
