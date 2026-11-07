# -*- coding: utf-8 -*-
"""Dispatch Plan (FR-01): the Sales dispatch request that drives the whole flow.

Regular partner first (FR-02) -> 3PL time-bound bid when every regular partner is unavailable
(FR-03/04) -> vehicle reporting and gate (FR-05) -> detention (FR-06) -> tracking (FR-07)
-> POD (FR-08) -> POD-gated vendor bill (FR-09).
"""
import math
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.tools import format_amount, format_datetime
from odoo.exceptions import UserError, ValidationError

from .dispatch_res_partner import CHECK_STATUSES

PLAN_STATES = [
    ('draft', 'Draft'),
    ('ready', 'Ready for Carrier'),
    ('bidding', 'Bidding Live'),
    ('assigned', 'Vehicle Assigned'),
    ('loading', 'Loading'),
    ('in_transit', 'In Transit'),
    ('delivered', 'Delivered - POD Pending'),
    ('pod_verified', 'Delivered - POD Verified'),
    ('closed', 'Closed'),
    ('cancel', 'Cancelled'),
]
MILESTONES = [('picked_up', 'Picked Up'), ('in_transit', 'In Transit'), ('out_for_delivery', 'Out for Delivery'),
              ('delivered', 'Delivered')]


class CpDispatchPlan(models.Model):
    _name = 'cp.dispatch.plan'
    _description = 'Dispatch Plan'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'portal.mixin']
    _order = 'id desc'

    name = fields.Char(default=lambda self: _('New'), copy=False, readonly=True, index=True)
    state = fields.Selection(PLAN_STATES, default='draft', tracking=True, index=True, copy=False,
                             group_expand='_cp_expand_states')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one(related='company_id.currency_id')
    user_id = fields.Many2one('res.users', string='Logistics Officer', default=lambda self: self.env.user,
                              tracking=True)
    sales_user_id = fields.Many2one('res.users', string='Requested By (Sales)', default=lambda self: self.env.user)

    # What to move
    picking_ids = fields.Many2many(
        'stock.picking', 'cp_dispatch_plan_picking_rel', 'plan_id', 'picking_id', string='Delivery Orders',
        domain="[('picking_type_code', '=', 'outgoing'), ('state', 'not in', ('done', 'cancel')), "
               "('company_id', '=', company_id)]")
    sale_order_ids = fields.Many2many('sale.order', compute='_compute_sale_orders', string='Sales Orders')
    sale_order_count = fields.Integer(compute='_compute_sale_orders')
    warehouse_id = fields.Many2one('stock.warehouse', string='Origin Warehouse', required=True,
                                   domain="[('company_id', '=', company_id)]", tracking=True)
    partner_id = fields.Many2one('res.partner', string='Deliver To', required=True, tracking=True)
    dest_pincode = fields.Char(string='Destination PIN Code', compute='_compute_destination', store=True,
                               readonly=False)
    dest_city = fields.Char(string='Destination City', compute='_compute_destination', store=True, readonly=False)
    dest_state_id = fields.Many2one('res.country.state', string='Destination State', compute='_compute_destination',
                                    store=True, readonly=False)
    movement = fields.Selection([('intrastate', 'Intrastate'), ('interstate', 'Interstate')],
                                compute='_compute_movement', store=True)
    target_date = fields.Datetime(string='Target Delivery Date', tracking=True)
    loading_start = fields.Datetime(string='Loading Window From')
    loading_end = fields.Datetime(string='Loading Window To')
    goods_category = fields.Char(string='Goods Category')
    special_handling = fields.Text()
    package_count = fields.Integer(string='Packages / Boxes')
    gross_weight_kg = fields.Float(string='Gross Weight (kg)')
    volume_cbm = fields.Float(string='Volume (m³)', digits=(12, 3))
    goods_value = fields.Monetary(string='Goods Value')
    eway_required = fields.Boolean(compute='_compute_eway_required', string='E-way Bill Required')
    vehicle_type_id = fields.Many2one('cp.vehicle.type', string='Required Truck Type', tracking=True)
    suggested_vehicle_type_id = fields.Many2one('cp.vehicle.type', compute='_compute_capacity',
                                                string='Suggested Truck Type')
    capacity_ok = fields.Boolean(compute='_compute_capacity')
    capacity_note = fields.Char(compute='_compute_capacity')

    # Carrier selection
    check_log_ids = fields.One2many('cp.carrier.check.log', 'plan_id', string='Regular Partner Checks')
    regulars_exhausted = fields.Boolean(compute='_compute_regulars', string='All Regular Partners Unavailable')
    regulars_pending = fields.Char(compute='_compute_regulars', string='Partners Not Yet Checked')
    carrier_type = fields.Selection([('regular', 'Regular partner'), ('3pl', '3PL vendor')], tracking=True,
                                    copy=False)
    regular_partner_id = fields.Many2one('res.partner', string='Regular Partner', copy=False,
                                         domain=[('cp_regular_carrier', '=', True)])
    vendor_id = fields.Many2one('res.partner', string='Transport Provider', copy=False, tracking=True)
    booking_ref = fields.Char(string='AWB / Booking Ref.', copy=False, tracking=True)
    freight_amount = fields.Monetary(string='Agreed Freight', copy=False, tracking=True)
    target_rate = fields.Monetary(string='Target Rate', copy=False)
    auction_id = fields.Many2one('cp.auction', string='3PL Bid', copy=False, readonly=True)
    auction_ids = fields.One2many('cp.auction', 'dispatch_plan_id', string='Bids')
    auction_count = fields.Integer(compute='_compute_counts')
    proposed_bid_id = fields.Many2one(related='auction_id.proposed_bid_id', string='Proposed Winner (L1)')
    indent_id = fields.Many2one('cp.indent', string='Load Request', copy=False, readonly=True)
    trip_id = fields.Many2one('cp.trip', string='Shipment', copy=False, readonly=True, index=True)
    purchase_order_id = fields.Many2one('purchase.order', string='Freight PO', copy=False, readonly=True)
    reporting_cutoff = fields.Datetime(string='Report Vehicle By', copy=False,
                                       help='The vendor must report the truck at the gate before this time.')

    # Execution
    gate_entry_ids = fields.One2many('cp.gate.entry', 'dispatch_plan_id', string='Gate Entries')
    gate_in_time = fields.Datetime(compute='_compute_gate', store=True, string='Gate-In (T0)')
    loading_start_time = fields.Datetime(compute='_compute_gate', store=True, string='Loading Start (T1)')
    loading_end_time = fields.Datetime(compute='_compute_gate', store=True, string='Loading End (T2)')
    gate_out_time = fields.Datetime(compute='_compute_gate', store=True, string='Gate-Out (T3)')
    reported_on_time = fields.Boolean(compute='_compute_gate', store=True)
    vehicle_number = fields.Char(related='trip_id.vehicle_number', string='Vehicle No.')
    driver_name = fields.Char(related='trip_id.driver_name')
    driver_phone = fields.Char(related='trip_id.driver_phone')
    eway_bill_no = fields.Char(string='E-way Bill No.', copy=False)
    tracking_url = fields.Char(compute='_compute_tracking_url', string='Tracking Link')
    milestone = fields.Selection(MILESTONES, copy=False, tracking=True)
    picked_up_at = fields.Datetime(copy=False, readonly=True)
    delivered_at = fields.Datetime(copy=False, readonly=True)
    is_delayed = fields.Boolean(compute='_compute_delay', search='_search_delayed', string='Delayed')
    on_time_delivery = fields.Boolean(compute='_compute_delay_stored', store=True)
    board_status = fields.Selection([('in_transit', 'In Transit'), ('delayed', 'Delayed'),
                                     ('out_for_delivery', 'Out for Delivery'), ('delivered', 'Delivered')],
                                    compute='_compute_board_status', store=True, string='Transit Board',
                                    group_expand='_cp_expand_board')
    detention_ids = fields.One2many('cp.detention.record', 'plan_id', string='Detention')
    detention_approved = fields.Monetary(compute='_compute_money', string='Approved Detention')
    detention_unbilled = fields.Monetary(compute='_compute_money', string='Approved Detention Not Billed',
                                         help='Approved detention that is not on a vendor bill yet.')
    pod_ids = fields.One2many('cp.dispatch.pod', 'plan_id', string='Proofs of Delivery')
    pod_status = fields.Selection([('missing', 'Not Received'), ('pending', 'Pending Verification'),
                                   ('rejected', 'Rejected - Re-upload'), ('verified', 'Verified')],
                                  compute='_compute_pod_status', store=True, string='POD Status')
    pod_verified_at = fields.Datetime(compute='_compute_pod_status', store=True)
    pod_tat_hours = fields.Float(compute='_compute_pod_status', store=True, string='POD Turnaround (h)')
    move_ids = fields.One2many('account.move', 'cp_dispatch_id', string='Vendor Bills')
    bill_count = fields.Integer(compute='_compute_counts')
    total_freight_cost = fields.Monetary(compute='_compute_money', string='Total Freight Cost')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('cp.dispatch.plan') or _('New')
        records = super().create(vals_list)
        for rec in records.filtered('picking_ids'):
            rec._cp_fill_from_pickings(overwrite=False)
        return records

    @api.model
    def _cp_expand_states(self, states, domain):
        """Kanban columns in workflow order (Draft ... Closed); Cancelled only when it has plans."""
        return [key for key, _label in PLAN_STATES if key != 'cancel' or key in states]

    @api.model
    def _cp_expand_board(self, statuses, domain):
        return [key for key, _label in self._fields['board_status'].selection]

    def _compute_access_url(self):
        super()._compute_access_url()
        for rec in self:
            rec.access_url = '/my/cp/dispatches/%s' % rec.id

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('picking_ids.sale_id')
    def _compute_sale_orders(self):
        for rec in self:
            rec.sale_order_ids = rec.picking_ids.sale_id
            rec.sale_order_count = len(rec.sale_order_ids)

    @api.depends('partner_id')
    def _compute_destination(self):
        for rec in self:
            partner = rec.partner_id
            rec.dest_pincode = partner.zip or rec.dest_pincode
            rec.dest_city = partner.city or rec.dest_city
            rec.dest_state_id = partner.state_id or rec.dest_state_id

    @api.depends('warehouse_id.partner_id.state_id', 'dest_state_id')
    def _compute_movement(self):
        for rec in self:
            origin = (rec.warehouse_id.partner_id or rec.company_id.partner_id).state_id
            if origin and rec.dest_state_id:
                rec.movement = 'intrastate' if origin == rec.dest_state_id else 'interstate'
            else:
                rec.movement = False

    @api.depends('movement', 'goods_value', 'company_id.cp_eway_threshold')
    def _compute_eway_required(self):
        for rec in self:
            threshold = rec.company_id.cp_eway_threshold
            rec.eway_required = bool(rec.company_id.cp_dispatch_require_eway) and (
                rec.movement == 'interstate' or (rec.goods_value or 0.0) > (threshold or 0.0))

    @api.depends('gross_weight_kg', 'volume_cbm', 'vehicle_type_id')
    def _compute_capacity(self):
        types = self.env['cp.vehicle.type'].search([], order='capacity_ton, volume_cbm')
        for rec in self:
            fits = types.filtered(lambda t: t.capacity_ton * 1000.0 >= rec.gross_weight_kg and
                                  (not rec.volume_cbm or not t.volume_cbm or t.volume_cbm >= rec.volume_cbm))
            rec.suggested_vehicle_type_id = fits[:1]
            vt = rec.vehicle_type_id
            if not vt:
                rec.capacity_ok = True
                rec.capacity_note = _('Suggested: %s', fits[:1].name) if fits else (
                    _('No truck type is large enough; split the dispatch.') if rec.gross_weight_kg else False)
                continue
            problems = []
            if rec.gross_weight_kg > vt.capacity_ton * 1000.0:
                problems.append(_('%(w).0f kg is over the %(c).0f kg capacity of %(t)s',
                                  w=rec.gross_weight_kg, c=vt.capacity_ton * 1000.0, t=vt.name))
            if rec.volume_cbm and vt.volume_cbm and rec.volume_cbm > vt.volume_cbm:
                problems.append(_('%(v).1f m³ is over the %(c).1f m³ of %(t)s', v=rec.volume_cbm, c=vt.volume_cbm,
                                  t=vt.name))
            rec.capacity_ok = not problems
            rec.capacity_note = '; '.join(problems) or _('Fits %s', vt.name)

    @api.depends('check_log_ids.status', 'check_log_ids.carrier_id', 'check_log_ids.superseded', 'company_id')
    def _compute_regulars(self):
        for rec in self:
            carriers = rec._cp_regular_carriers()
            latest = rec._cp_latest_checks()
            pending = carriers.filtered(lambda c: c.id not in latest)
            rec.regulars_pending = ', '.join(pending.mapped('name')) or False
            rec.regulars_exhausted = bool(carriers) and not pending and all(
                log.status != 'confirmed' for log in latest.values())

    def _compute_counts(self):
        for rec in self:
            rec.auction_count = len(rec.auction_ids)
            rec.bill_count = len(rec.move_ids)

    @api.depends('gate_entry_ids.gate_in_time', 'gate_entry_ids.dock_in_time', 'gate_entry_ids.dock_out_time',
                 'gate_entry_ids.gate_out_time', 'gate_entry_ids.state', 'reporting_cutoff')
    def _compute_gate(self):
        for rec in self:
            entry = rec.gate_entry_ids.filtered(lambda g: g.state != 'cancel')[:1]
            rec.gate_in_time = entry.gate_in_time
            rec.loading_start_time = entry.dock_in_time
            rec.loading_end_time = entry.dock_out_time
            rec.gate_out_time = entry.gate_out_time
            rec.reported_on_time = bool(entry.gate_in_time) and (
                not rec.reporting_cutoff or entry.gate_in_time <= rec.reporting_cutoff)

    @api.depends('carrier_type', 'booking_ref', 'regular_partner_id', 'trip_id.tracking_url')
    def _compute_tracking_url(self):
        for rec in self:
            link = False
            if rec.carrier_type == 'regular' and rec.regular_partner_id:
                link = rec.regular_partner_id.cp_tracking_link(rec.booking_ref)
            rec.tracking_url = link or rec.trip_id.tracking_url or False

    def _compute_delay(self):
        now = fields.Datetime.now()
        for rec in self:
            rec.is_delayed = bool(rec.target_date) and rec.state in ('assigned', 'loading', 'in_transit') \
                and rec.target_date < now

    def _search_delayed(self, operator, value):
        if operator not in ('=', '!=') or not isinstance(value, bool):
            raise UserError(_('Unsupported search.'))
        live = ('assigned', 'loading', 'in_transit')
        now = fields.Datetime.now()
        if (operator == '=') == value:
            return [('target_date', '<', now), ('state', 'in', live)]
        return ['|', '|', ('target_date', '=', False), ('target_date', '>=', now), ('state', 'not in', live)]

    @api.depends('delivered_at', 'target_date')
    def _compute_delay_stored(self):
        for rec in self:
            rec.on_time_delivery = bool(rec.delivered_at) and (not rec.target_date or rec.delivered_at <= rec.target_date)

    @api.depends('state', 'milestone', 'target_date')
    def _compute_board_status(self):
        now = fields.Datetime.now()
        for rec in self:
            live = rec.state in ('assigned', 'loading', 'in_transit')
            if rec.state in ('delivered', 'pod_verified', 'closed'):
                rec.board_status = 'delivered'
            elif not live:
                rec.board_status = False
            elif rec.target_date and rec.target_date < now:
                rec.board_status = 'delayed'
            elif rec.state != 'in_transit':
                rec.board_status = False
            elif rec.milestone == 'out_for_delivery':
                rec.board_status = 'out_for_delivery'
            else:
                rec.board_status = 'in_transit'

    @api.depends('detention_ids.state', 'detention_ids.approved_amount', 'detention_ids.move_id.state',
                 'freight_amount')
    def _compute_money(self):
        for rec in self:
            approved = rec.detention_ids.filtered(lambda d: d.state == 'approved')
            rec.detention_approved = sum(approved.mapped('approved_amount'))
            rec.detention_unbilled = sum(approved._cp_unbilled().mapped('approved_amount'))
            rec.total_freight_cost = rec.freight_amount + rec.detention_approved

    @api.depends('pod_ids.status', 'pod_ids.verified_at', 'delivered_at')
    def _compute_pod_status(self):
        for rec in self:
            pods = rec.pod_ids.sorted('id')
            verified = pods.filtered(lambda p: p.status == 'verified')
            if verified:
                rec.pod_status = 'verified'
                rec.pod_verified_at = verified[-1].verified_at
                reference = rec.delivered_at or verified[-1].create_date
                rec.pod_tat_hours = max((rec.pod_verified_at - reference).total_seconds() / 3600.0, 0.0) \
                    if rec.pod_verified_at and reference else 0.0
                continue
            rec.pod_verified_at = False
            rec.pod_tat_hours = 0.0
            if pods.filtered(lambda p: p.status == 'pending'):
                rec.pod_status = 'pending'
            elif pods:
                rec.pod_status = 'rejected'
            else:
                rec.pod_status = 'missing'

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _cp_money(self, amount):
        return format_amount(self.env, amount or 0.0, self.currency_id or self.env.company.currency_id)

    def _cp_tz(self):
        self.ensure_one()
        return self.user_id.tz or self.env.user.tz or self.company_id.partner_id.tz or 'UTC'

    def _cp_local(self, value):
        """A datetime as the planner reads it (their time zone), for messages sent to vendors and customers."""
        self.ensure_one()
        if not value:
            return '-'
        return format_datetime(self.env, value, tz=self._cp_tz(), dt_format='dd MMM yyyy, HH:mm')

    def _cp_transit_delta(self):
        """Expected road time: lane distance at the planning speed plus a handling buffer, or the company default."""
        self.ensure_one()
        origin = 'cp_location_id' in self.warehouse_id._fields and self.warehouse_id.cp_location_id
        dest = 'cp_location_id' in self.partner_id._fields and self.partner_id.cp_location_id
        lane = self.env['cp.lane']
        if origin and dest:
            lane = lane.search([('origin_id', '=', origin.id), ('destination_id', '=', dest.id)], limit=1)
        if lane.distance_km:
            speed = self.company_id.cp_default_speed_kmph or 40.0
            return timedelta(hours=lane.distance_km / speed + 4.0)
        return timedelta(days=self.company_id.cp_dispatch_transit_days or 2.0)

    def _cp_default_target_date(self, pickings=None):
        """Customer deadline when the delivery has one, else departure + transit time (never 'now')."""
        self.ensure_one()
        pickings = self.picking_ids if pickings is None else pickings
        now = fields.Datetime.now()
        start = max([p.scheduled_date for p in pickings if p.scheduled_date] + [self.loading_start or now, now])
        promised = [d for d in pickings.sale_id.mapped('commitment_date') if d]
        if promised:
            return max(promised)
        # Odoo stamps every delivery with a deadline of 'now' when the order has no commitment date: only a
        # deadline after departure is a real customer promise.
        deadlines = [p.date_deadline for p in pickings if p.date_deadline and p.date_deadline > start]
        if deadlines:
            return max(deadlines)
        return start + self._cp_transit_delta()

    def _cp_regular_carriers(self):
        self.ensure_one()
        return self.env['res.partner'].search([('cp_regular_carrier', '=', True), '|', ('company_id', '=', False),
                                               ('company_id', '=', self.company_id.id)],
                                              order='cp_carrier_sequence, name')

    def _cp_latest_checks(self):
        """{carrier id: latest check log} for this plan."""
        self.ensure_one()
        latest = {}
        for log in self.check_log_ids.filtered(lambda l: not l.superseded).sorted(
                lambda l: (l.checked_at or fields.Datetime.now(), l.id)):
            latest[log.carrier_id.id] = log
        return latest

    def _cp_fill_from_pickings(self, overwrite=True):
        for rec in self:
            pickings = rec.picking_ids
            if not pickings:
                continue
            weight = volume = qty = value = 0.0
            for move in pickings.move_ids.filtered(lambda m: m.state != 'cancel'):
                q = move.product_uom_qty
                weight += (move.product_id.weight or 0.0) * q
                volume += (move.product_id.volume or 0.0) * q
                qty += q
                line = move.sale_line_id
                value += (line.price_reduce_taxexcl * q) if line else (move.product_id.standard_price * q)
            vals = {}
            for field, new in (('gross_weight_kg', weight), ('volume_cbm', volume), ('package_count', int(qty)),
                               ('goods_value', value)):
                if overwrite or not rec[field]:
                    vals[field] = new
            first = pickings[:1]
            if overwrite or not rec.warehouse_id:
                vals['warehouse_id'] = first.picking_type_id.warehouse_id.id or rec.warehouse_id.id
            if overwrite or not rec.partner_id:
                vals['partner_id'] = first.partner_id.id or rec.partner_id.id
            rec.write(vals)
            if not rec.target_date:
                rec.target_date = rec._cp_default_target_date(pickings)

    @api.onchange('picking_ids')
    def _onchange_picking_ids(self):
        if self.picking_ids:
            pickings = self.picking_ids
            self.warehouse_id = pickings[:1].picking_type_id.warehouse_id or self.warehouse_id
            self.partner_id = pickings[:1].partner_id or self.partner_id
            weight = volume = qty = value = 0.0
            for move in pickings.move_ids.filtered(lambda m: m.state != 'cancel'):
                q = move.product_uom_qty
                weight += (move.product_id.weight or 0.0) * q
                volume += (move.product_id.volume or 0.0) * q
                qty += q
                line = move.sale_line_id
                value += (line.price_reduce_taxexcl * q) if line else (move.product_id.standard_price * q)
            self.gross_weight_kg, self.volume_cbm, self.package_count, self.goods_value = weight, volume, int(qty), value
            if not self.target_date:
                self.target_date = self._cp_default_target_date(pickings)

    @api.constrains('picking_ids')
    def _check_pickings(self):
        for rec in self:
            if len(rec.picking_ids.picking_type_id.warehouse_id) > 1:
                raise ValidationError(_('All delivery orders of a dispatch plan must leave from one warehouse.'))
            others = self.search([('id', '!=', rec.id), ('state', 'not in', ('cancel',)),
                                  ('picking_ids', 'in', rec.picking_ids.ids)], limit=1)
            if others:
                raise ValidationError(_('%(p)s is already on dispatch plan %(o)s.',
                                        p=', '.join((rec.picking_ids & others.picking_ids).mapped('name')),
                                        o=others.name))

    def _cp_require(self, states):
        for rec in self:
            if rec.state not in states:
                raise UserError(_('%(plan)s is "%(state)s"; this step needs %(need)s.', plan=rec.name,
                                  state=dict(PLAN_STATES)[rec.state],
                                  need=' / '.join(dict(PLAN_STATES)[s] for s in states)))

    def _cp_lane(self):
        self.ensure_one()
        origin = self.warehouse_id._cp_location()
        dest = self.partner_id._cp_location()
        Lane = self.env['cp.lane']
        lane = Lane.search([('origin_id', '=', origin.id), ('destination_id', '=', dest.id)], limit=1)
        if not lane:
            lane = Lane.create({'origin_id': origin.id, 'destination_id': dest.id})
            if hasattr(lane, '_onchange_points'):
                lane._onchange_points()
        return lane

    def _cp_indent(self, mode):
        """The load request behind the plan (bidding and shipments reuse Cargo Pilot's engine)."""
        self.ensure_one()
        if self.indent_id and self.indent_id.state not in ('cancel', 'done'):
            self.indent_id.write({'shipment_mode': mode, 'vehicle_type_id': self.vehicle_type_id.id})
            return self.indent_id
        lines = [(0, 0, p._cp_indent_line_vals()) for p in self.picking_ids]
        if not lines:
            lines = [(0, 0, {'description': self.goods_category or _('Goods'), 'order_ref': self.name,
                             'quantity': self.package_count or 1, 'weight_ton': self.gross_weight_kg / 1000.0,
                             'volume_cbm': self.volume_cbm, 'order_value': self.goods_value,
                             'customer_id': self.partner_id.id})]
        indent = self.env['cp.indent'].create({
            'lane_id': self._cp_lane().id, 'shipment_mode': mode, 'vehicle_type_id': self.vehicle_type_id.id,
            'vehicle_count': 1, 'consignee_id': self.partner_id.id, 'company_id': self.company_id.id,
            'required_date': self.loading_start or fields.Datetime.now(), 'delivery_deadline': self.target_date,
            'user_id': self.user_id.id, 'line_ids': lines, 'notes': self.special_handling})
        indent.action_confirm()
        self.indent_id = indent
        self.picking_ids.write({'cp_indent_id': indent.id})
        return indent

    def _cp_freight_product(self):
        product = self.env.ref('cargo_pilot_tms.product_cp_freight', raise_if_not_found=False)
        if not product:
            raise UserError(_('The Freight Charges product is missing.'))
        return product

    def _cp_create_po(self):
        self.ensure_one()
        if self.purchase_order_id and self.purchase_order_id.state != 'cancel':
            return self.purchase_order_id
        product = self._cp_freight_product()
        order = self.env['purchase.order'].sudo().create({
            'partner_id': self.vendor_id.id, 'origin': self.name, 'partner_ref': self.booking_ref or False,
            'company_id': self.company_id.id, 'cp_dispatch_id': self.id,
            'order_line': [(0, 0, {
                'product_id': product.id, 'product_qty': 1.0, 'price_unit': self.freight_amount,
                'name': _('Freight %(plan)s: %(o)s to %(d)s', plan=self.name, o=self.warehouse_id.name,
                          d=self.dest_city or self.partner_id.display_name),
                'date_planned': self.loading_start or fields.Datetime.now(), 'tax_ids': [(5, 0, 0)]})]})
        order.button_confirm()
        self.purchase_order_id = order
        return order

    def _cp_message_partner(self, partner, subject, body):
        """E-mail (chatter), SMS and WhatsApp when connected."""
        self.ensure_one()
        if not partner:
            return
        self.message_post(body=body, subject=subject, partner_ids=partner.ids, message_type='comment',
                          subtype_xmlid='mail.mt_comment')
        phone = partner.phone
        if phone and self.company_id.cp_dispatch_sms and hasattr(self, '_message_sms'):
            try:
                self._message_sms(body, partner_ids=partner.ids, sms_numbers=None)
            except Exception:  # noqa: BLE001 - an SMS gateway problem must not stop dispatch
                self.message_post(body=_('SMS to %s could not be queued.', partner.display_name))
        if phone and 'cp.whatsapp.account' in self.env:
            account = self.env['cp.whatsapp.account'].sudo().cp_for(self.company_id)
            template_event = 'dispatched' if 'Track' in body or 'dispatched' in body else 'offer'
            if account:
                account.cp_send(template_event, phone, self.trip_id, text=body, url=self.tracking_url or '',
                                partner=partner)

    # ------------------------------------------------------------------
    # FR-01: plan
    # ------------------------------------------------------------------
    def action_ready(self):
        self._cp_require(('draft',))
        for rec in self:
            if not (rec.gross_weight_kg or rec.package_count):
                raise UserError(_('Enter the weight or package count of %s.', rec.name))
            if rec.vehicle_type_id and not rec.capacity_ok:
                raise UserError(_('%(plan)s: %(note)s. Choose a larger truck type or split the dispatch.',
                                  plan=rec.name, note=rec.capacity_note))
            if not rec.vehicle_type_id and rec.suggested_vehicle_type_id:
                rec.vehicle_type_id = rec.suggested_vehicle_type_id
            if not rec.target_date:
                rec.target_date = rec._cp_default_target_date()
            rec.state = 'ready'
            rec.message_post(body=_('Ready for carrier. Check the regular partners first.'))
        return True

    def action_refresh_from_deliveries(self):
        self._cp_fill_from_pickings(overwrite=True)
        return True

    # ------------------------------------------------------------------
    # FR-02: regular partners first
    # ------------------------------------------------------------------
    def action_open_carrier_check(self):
        self.ensure_one()
        self._cp_require(('ready',))
        carriers = self._cp_regular_carriers()
        if not carriers:
            raise UserError(_('Mark DHL, Delhivery, FedEx or your other regular partners as Regular Logistics '
                              'Partner on their contact first.'))
        latest = self._cp_latest_checks()
        hints = {'over_capacity': _('Over its weight / volume limit'), 'no_service': _('PIN code not served')}
        wizard = self.env['cp.carrier.check.wizard'].create({
            'plan_id': self.id,
            'line_ids': [(0, 0, {
                'carrier_id': c.id,
                'status': latest[c.id].status if c.id in latest else (c.cp_precheck(self) or False),
                'auto_reason': hints.get(c.cp_precheck(self)) or False,
            }) for c in carriers]})
        return {'type': 'ir.actions.act_window', 'name': _('Regular Partner Availability'),
                'res_model': 'cp.carrier.check.wizard', 'res_id': wizard.id, 'view_mode': 'form', 'target': 'new'}

    def cp_log_check(self, carrier, status, rate=0.0, note=False, pickup=False, channel='manual', booking_ref=False):
        self.ensure_one()
        if not carrier.cp_regular_carrier:
            raise UserError(_('%s is not a regular logistics partner.', carrier.display_name))
        return self.env['cp.carrier.check.log'].create({
            'plan_id': self.id, 'carrier_id': carrier.id, 'status': status, 'quoted_rate': rate, 'note': note,
            'pickup_time': pickup, 'channel': channel, 'booking_ref': booking_ref})

    def cp_book_regular(self, carrier, rate=0.0, booking_ref=False):
        """Book a regular partner that confirmed capacity: shipment, AWB / booking reference and PO."""
        self.ensure_one()
        self._cp_require(('ready',))
        latest = self._cp_latest_checks().get(carrier.id)
        if not latest or latest.status != 'confirmed':
            raise UserError(_('Log %s as Confirmed before booking it.', carrier.display_name))
        rate = rate or latest.quoted_rate
        if not rate:
            contract_line = self.env['cp.rate.contract'].cp_find_rate(carrier, self._cp_lane(), self.vehicle_type_id,
                                                                      mode='ltl')
            rate = contract_line.cp_compute_amount(weight_ton=self.gross_weight_kg / 1000.0) if contract_line else 0.0
        if not rate:
            raise UserError(_('Enter the rate %s quoted, or add a rate contract for it.', carrier.display_name))
        ref = (booking_ref or latest.booking_ref or '').strip() or \
            self.env['ir.sequence'].next_by_code('cp.dispatch.awb') or self.name
        indent = self._cp_indent('ltl')
        vals = indent._cp_trip_vals(carrier, rate)
        vals.update({'docket_number': ref, 'tracking_method': 'carrier', 'shipment_mode': 'ltl',
                     'dispatch_plan_id': self.id})
        trip = self.env['cp.trip'].with_context(cp_no_offer=True, cp_from_plan=True).create(vals)
        trip.with_context(cp_from_plan=True).action_assign()
        indent.state = 'allocated'
        self.write({'carrier_type': 'regular', 'regular_partner_id': carrier.id, 'vendor_id': carrier.id,
                    'booking_ref': ref, 'freight_amount': rate, 'trip_id': trip.id, 'state': 'assigned'})
        latest.booked = True
        self._cp_create_po()
        self.message_post(body=_('Booked with regular partner %(c)s, AWB / booking %(r)s, freight %(a)s.',
                                 c=carrier.name, r=ref, a=self._cp_money(rate)))
        return trip

    # ------------------------------------------------------------------
    # FR-03 / FR-04: 3PL bidding
    # ------------------------------------------------------------------
    def _cp_eligible_vendors(self, vehicle_type=None):
        self.ensure_one()
        vehicle_type = vehicle_type or self.vehicle_type_id
        vendors = self.env['res.partner'].search([('cp_is_3pl_vendor', '=', True), ('is_company', '=', True)])
        lane = self.indent_id.lane_id

        def eligible(v):
            if vehicle_type and v.cp_vehicle_type_ids and vehicle_type not in v.cp_vehicle_type_ids:
                return False
            if v.cp_service_state_ids and self.dest_state_id and self.dest_state_id not in v.cp_service_state_ids:
                return False
            if v.cp_lane_ids and lane and lane not in v.cp_lane_ids and not v.cp_service_state_ids:
                return False
            return True
        return vendors.filtered(eligible)

    def action_escalate_bid(self):
        self.ensure_one()
        self._cp_require(('ready',))
        if not self.regulars_exhausted:
            if self.regulars_pending:
                raise UserError(_('Regular partners first: log the availability of %s.', self.regulars_pending))
            raise UserError(_('A regular partner confirmed capacity. Book it instead of bidding.'))
        company = self.company_id
        minutes = company.cp_dispatch_bid_minutes or 15
        wizard = self.env['cp.bid.launch.wizard'].create({
            'plan_id': self.id, 'vehicle_type_id': (self.vehicle_type_id or self.suggested_vehicle_type_id).id,
            'duration_minutes': minutes, 'target_price': self.target_rate,
            'pickup_slot': self.loading_start or fields.Datetime.now() + timedelta(hours=2),
            'vendor_ids': [(6, 0, self._cp_eligible_vendors(self.vehicle_type_id or
                                                            self.suggested_vehicle_type_id).ids)]})
        return {'type': 'ir.actions.act_window', 'name': _('Launch 3PL Bid'), 'res_model': 'cp.bid.launch.wizard',
                'res_id': wizard.id, 'view_mode': 'form', 'target': 'new'}

    def cp_launch_bid(self, vendors, duration, mode='reverse_auction', target_price=0.0, vehicle_type=None,
                      pickup_slot=None, ceiling=0.0, reporting_hours=None):
        self.ensure_one()
        self._cp_require(('ready',))
        if not self.regulars_exhausted:
            raise UserError(_('Every regular partner must be logged as unavailable before 3PL bidding.'))
        if not 10 <= duration <= 20:
            raise UserError(_('The bid must last between 10 and 20 minutes.'))
        if mode == 'target_price' and target_price <= 0:
            raise UserError(_('Enter the target price vendors accept.'))
        if not vendors:
            raise UserError(_('No approved 3PL vendor serves this route and truck type. Approve vendors first.'))
        if vehicle_type:
            self.vehicle_type_id = vehicle_type
        if not self.vehicle_type_id:
            raise UserError(_('Choose the truck type for the bid.'))
        indent = self._cp_indent('ftl')
        now = fields.Datetime.now()
        hours = reporting_hours if reporting_hours is not None else (self.company_id.cp_dispatch_reporting_hours or 2.0)
        slot = pickup_slot or now + timedelta(hours=hours)
        auction = self.env['cp.auction'].create({
            'indent_id': indent.id, 'vehicle_count': 1, 'date_start': now,
            'date_end': now + timedelta(minutes=duration), 'extension_minutes': 0, 'allow_pre_bid': False,
            'allow_part_bid': False, 'min_decrement_pct': 0.0, 'bid_mode': 'open',
            'ceiling_price': ceiling or (target_price if mode == 'target_price' else 0.0),
            'transporter_ids': [(6, 0, vendors.ids)], 'dispatch_plan_id': self.id, 'bidding_mode': mode,
            'target_price': target_price, 'pickup_slot': slot, 'reporting_cutoff': slot,
            'duration_minutes': duration})
        auction.action_publish()
        self.write({'auction_id': auction.id, 'state': 'bidding', 'carrier_type': '3pl', 'target_rate': target_price,
                    'reporting_cutoff': slot})
        mode_label = _('accept the target price of %s', self._cp_money(target_price)) if mode == 'target_price' else \
            _('quote your lowest rate')
        for vendor in vendors:
            self._cp_message_partner(
                vendor, _('Transport bid %s', self.name),
                _('New transport requirement %(plan)s: %(o)s to %(d)s, %(t)s, pickup %(p)s. Bid closes in %(m)s '
                  'minutes: %(how)s at %(url)s',
                  plan=self.name, o=self.warehouse_id.name, d=self.dest_city or self.partner_id.display_name,
                  t=self.vehicle_type_id.name, p=self._cp_local(slot), m=duration, how=mode_label,
                  url='%s/my/cp/auctions/%s' % (self.get_base_url(), auction.id)))
        return auction

    def cp_after_bid_close(self):
        """Called when the bid locks: propose the winner, or go back to Ready when nobody bid."""
        for rec in self:
            auction = rec.auction_id
            if not auction or rec.state != 'bidding':
                continue
            bid = auction._cp_dispatch_winner()
            auction.proposed_bid_id = bid
            if not bid:
                rec.state = 'ready'
                self.env['cp.exception'].cp_raise('no_bids', rec, _('No 3PL vendor bid on %s', rec.name),
                                                  severity='high',
                                                  suggestion=_('Raise the target price, approve more vendors or '
                                                               'bid again.'))
                rec.message_post(body=_('The bid closed with no offers. Launch it again or change the terms.'))
                continue
            rec.message_post(body=_('Bid closed. Proposed winner: %(v)s at %(r)s. Approve to book.',
                                    v=bid.transporter_id.name, r=rec._cp_money(bid.rate)))
            manager_group = self.env.ref('cargo_pilot_tms.group_cp_manager')
            manager = (rec.user_id if rec.user_id.has_group('cargo_pilot_tms.group_cp_manager') else
                       manager_group.user_ids.filtered(lambda u: rec.company_id in u.company_ids)[:1]) or self.env.user
            rec.activity_schedule('mail.mail_activity_data_todo', user_id=manager.id,
                                  summary=_('Approve 3PL winner'),
                                  note=_('%(v)s quoted %(r)s.', v=bid.transporter_id.name, r=rec._cp_money(bid.rate)))

    def action_record_bid(self):
        """A vendor quoted by phone, WhatsApp or e-mail: logistics staff enter it on the vendor's behalf."""
        self.ensure_one()
        self._cp_require(('bidding',))
        self.auction_id.cp_close_if_expired()
        if self.auction_id.state != 'live':
            raise UserError(_('The bid for %s has closed; offers can no longer be recorded.', self.name))
        wizard = self.env['cp.bid.record.wizard'].create({'plan_id': self.id})
        return {'type': 'ir.actions.act_window', 'name': _('Record Bid for Vendor'), 'res_model': 'cp.bid.record.wizard',
                'res_id': wizard.id, 'view_mode': 'form', 'target': 'new'}

    def action_close_bid_now(self):
        for rec in self:
            if rec.auction_id.state == 'live':
                rec.auction_id.sudo().write({'state': 'closed'})
            rec.cp_after_bid_close()
        return True

    def action_open_award(self):
        self.ensure_one()
        if not self.env.user.has_group('cargo_pilot_tms.group_cp_manager'):
            raise UserError(_('Only the Logistics Manager approves the bid winner.'))
        auction = self.auction_id
        if auction.state == 'live':
            raise UserError(_('The bid is still live. Wait for it to close or close it now.'))
        if auction.state == 'closed' and not auction.proposed_bid_id:
            self.cp_after_bid_close()
        wizard = self.env['cp.bid.award.wizard'].create({'plan_id': self.id,
                                                          'bid_id': auction.proposed_bid_id.id})
        return {'type': 'ir.actions.act_window', 'name': _('Approve Winner'), 'res_model': 'cp.bid.award.wizard',
                'res_id': wizard.id, 'view_mode': 'form', 'target': 'new'}

    def cp_award(self, bid, justification=False):
        """Book the chosen bid: L1 by default; another vendor needs a written justification (FR-04)."""
        self.ensure_one()
        if not self.env.user.has_group('cargo_pilot_tms.group_cp_manager') and not self.env.su:
            raise UserError(_('Only the Logistics Manager approves the bid winner.'))
        auction = self.auction_id
        if self.state != 'bidding' or auction.state != 'closed':
            raise UserError(_('Only a closed bid can be awarded.'))
        if bid.auction_id != auction:
            raise UserError(_('That offer belongs to another bid.'))
        proposed = auction.proposed_bid_id or auction._cp_dispatch_winner()
        if bid != proposed and not (justification or '').strip():
            raise UserError(_('Explain why %(v)s is chosen over the proposed winner %(p)s.',
                              v=bid.transporter_id.name, p=proposed.transporter_id.name))
        auction.write({'override_reason': justification if bid != proposed else False})
        auction.cp_award([(bid, 1)])
        trip = auction.trip_ids[:1]
        trip.with_context(cp_from_plan=True).write({'dispatch_plan_id': self.id})
        self.write({'vendor_id': bid.transporter_id.id, 'freight_amount': bid.rate, 'trip_id': trip.id,
                    'state': 'assigned', 'reporting_cutoff': auction.reporting_cutoff or self.reporting_cutoff})
        self._cp_create_po()
        summary = _('Approve 3PL winner')
        self.activity_ids.filtered(lambda a: a.summary == summary).action_done()
        cutoff = self._cp_local(self.reporting_cutoff)
        self._cp_message_partner(
            bid.transporter_id, _('Booking confirmed: %s', self.name),
            _('You won %(plan)s at %(r)s. Report a %(t)s at %(wh)s before %(c)s. Purchase order %(po)s.',
              plan=self.name, r=self._cp_money(bid.rate), t=self.vehicle_type_id.name, wh=self.warehouse_id.name, c=cutoff,
              po=self.purchase_order_id.name))
        losers = auction.bid_ids.filtered(lambda b: b.is_latest and b.transporter_id != bid.transporter_id) \
            .transporter_id | (auction.transporter_ids - bid.transporter_id)
        for vendor in losers:
            self.message_post(body=_('Thank you for taking part in bid %s. This load has been awarded; we look '
                                     'forward to your next offer.', self.name),
                              partner_ids=vendor.ids, message_type='comment', subtype_xmlid='mail.mt_comment')
        note = _(' Justification: %s', justification) if bid != proposed else ''
        self.message_post(body=_('3PL winner approved: %(v)s at %(r)s.%(n)s', v=bid.transporter_id.name,
                                 r=self._cp_money(bid.rate), n=note))
        return trip

    # ------------------------------------------------------------------
    # FR-05: gate
    # ------------------------------------------------------------------
    def action_new_gate_entry(self):
        self.ensure_one()
        self._cp_require(('assigned', 'loading'))
        entry = self.gate_entry_ids.filtered(lambda g: g.state not in ('cancel', 'gate_out'))[:1]
        if not entry:
            entry = self.env['cp.gate.entry'].create({
                'dispatch_plan_id': self.id, 'trip_id': self.trip_id.id,
                'location_id': self.warehouse_id._cp_location().id, 'direction': 'outbound',
                'vehicle_number': self.trip_id.vehicle_number or _('TBA'), 'driver_name': self.trip_id.driver_name,
                'driver_phone': self.trip_id.driver_phone, 'slot_start': self.loading_start,
                'slot_end': self.loading_end, 'eway_bill_no': self.eway_bill_no, 'company_id': self.company_id.id})
        return {'type': 'ir.actions.act_window', 'res_model': 'cp.gate.entry', 'res_id': entry.id,
                'view_mode': 'form', 'target': 'current'}

    # ------------------------------------------------------------------
    # FR-07: tracking
    # ------------------------------------------------------------------
    def _cp_notify_customer_dispatched(self):
        for rec in self:
            via = rec.vendor_id.name or _('our transporter')
            link = rec.tracking_url or ''
            body = _('Your shipment #%(plan)s has been dispatched via %(via)s. Track here: %(url)s',
                     plan=rec.name, via=via, url=link or _('(link to follow)'))
            if rec.booking_ref:
                body += ' ' + _('AWB / reference: %s', rec.booking_ref)
            rec._cp_message_partner(rec.partner_id, _('Shipment %s dispatched', rec.name), body)

    def cp_mark_in_transit(self):
        """Gate-out (3PL) or pickup (regular partner)."""
        now = fields.Datetime.now()
        for rec in self.filtered(lambda p: p.state in ('assigned', 'loading')):
            rec.write({'state': 'in_transit', 'milestone': 'in_transit', 'picked_up_at': rec.picked_up_at or now})
            rec._cp_notify_customer_dispatched()

    def action_mark_picked_up(self):
        """Regular partner collected the goods (no truck gate entry needed)."""
        self._cp_require(('assigned', 'loading'))
        for rec in self:
            if rec.carrier_type == '3pl':
                raise UserError(_('3PL trucks leave through the gate: record the gate-out instead.'))
            if rec.eway_required and not (rec.eway_bill_no or rec.trip_id.eway_bill_no):
                raise UserError(_('Enter the e-way bill number of %s before pickup.', rec.name))
            trip = rec.trip_id
            if rec.eway_bill_no:
                trip.eway_bill_no = rec.eway_bill_no
            if trip.state in ('assigned', 'reported', 'loading'):
                trip.with_context(cp_from_plan=True).action_dispatch()
            rec.with_context(cp_from_plan=True).cp_mark_in_transit()
        return True

    def action_out_for_delivery(self):
        self._cp_require(('in_transit',))
        for rec in self:
            rec.milestone = 'out_for_delivery'
            rec._cp_message_partner(rec.partner_id, _('Out for delivery'),
                                    _('Your shipment #%s is out for delivery today.', rec.name))
        return True

    def action_mark_delivered(self):
        self._cp_require(('in_transit',))
        now = fields.Datetime.now()
        for rec in self:
            trip = rec.trip_id
            if trip and trip.state in ('in_transit', 'arrived'):
                trip.with_context(cp_from_plan=True).action_mark_delivered()
            rec.write({'state': 'delivered', 'milestone': 'delivered', 'delivered_at': now})
            rec._cp_check_pod_state()
            rec.message_post(body=_('Delivered. Waiting for the proof of delivery.'))
        return True

    # ------------------------------------------------------------------
    # FR-08: POD
    # ------------------------------------------------------------------
    def _cp_check_pod_state(self):
        for rec in self:
            if rec.state == 'delivered' and rec.pod_status == 'verified':
                rec.state = 'pod_verified'
            elif rec.state == 'pod_verified' and rec.pod_status != 'verified':
                rec.state = 'delivered'

    def action_add_pod(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'cp.dispatch.pod', 'view_mode': 'form',
                'target': 'new', 'context': {'default_plan_id': self.id, 'default_source': 'internal'}}

    # ------------------------------------------------------------------
    # FR-09: vendor bill
    # ------------------------------------------------------------------
    def cp_create_vendor_bill(self, bill_ref=False, freight_billed=None, detention_billed=None, attachment=None):
        """Draft vendor bill with two lines: base freight (from the PO) and approved detention."""
        self.ensure_one()
        if not self.vendor_id or not self.purchase_order_id:
            raise UserError(_('%s has no booked transport provider yet.', self.name))
        if self.state in ('draft', 'ready', 'bidding', 'cancel'):
            raise UserError(_('Bill only after the transport is booked.'))
        po_line = self.purchase_order_id.order_line[:1]
        detentions = self.detention_ids.filtered(lambda d: d.state == 'approved' and d.approved_amount > 0) \
            ._cp_unbilled()
        detention_total = sum(detentions.mapped('approved_amount'))
        amount = detention_total if detention_billed is None else detention_billed
        open_bills = self.move_ids.filtered(lambda m: m.move_type == 'in_invoice' and m.state != 'cancel')
        supplementary = bool(open_bills)
        if supplementary:
            # Freight is already billed: a later bill carries only detention approved after the first bill.
            if not detentions:
                raise UserError(_('%s already has a vendor bill and no approved detention left to bill.', self.name))
            lines = []
        else:
            lines = [(0, 0, {
                'product_id': po_line.product_id.id, 'name': po_line.name, 'quantity': 1.0,
                'price_unit': po_line.price_unit if freight_billed is None else freight_billed,
                'purchase_line_id': po_line.id, 'tax_ids': [(6, 0, po_line.tax_ids.ids)]})]
        if amount:
            product = self.env.ref('cargo_pilot_tms.product_cp_detention')
            lines.append((0, 0, {'product_id': product.id, 'quantity': 1.0, 'price_unit': amount,
                                 'name': _('Detention / halting %(plan)s (%(refs)s)', plan=self.name,
                                           refs=', '.join(detentions.mapped('name')) or _('as billed'))}))
        move = self.env['account.move'].create({
            'move_type': 'in_invoice', 'partner_id': self.vendor_id.id, 'ref': bill_ref or self.booking_ref,
            'invoice_origin': '%s, %s' % (self.name, self.purchase_order_id.name), 'cp_dispatch_id': self.id,
            'invoice_date': fields.Date.context_today(self), 'company_id': self.company_id.id,
            'invoice_line_ids': lines})
        detentions.write({'move_id': move.id})
        if attachment:
            attachment.write({'res_model': 'account.move', 'res_id': move.id})
        if supplementary:
            self.message_post(body=_('Detention bill drafted for %(v)s: %(d)s (%(refs)s).', v=self.vendor_id.name,
                                     d=self._cp_money(amount), refs=', '.join(detentions.mapped('name'))))
        else:
            self.message_post(body=_('Vendor bill drafted for %(v)s: freight %(f)s, detention %(d)s.',
                                     v=self.vendor_id.name, f=self._cp_money(lines[0][2]['price_unit']),
                                     d=self._cp_money(amount)))
        pending = self.detention_ids.filtered(lambda d: d.state in ('draft', 'submitted', 'logistics_approved'))
        if pending:
            self.message_post(body=_('Detention %s is still awaiting approval. Once approved, use "Bill Detention" '
                                     'to add it on a separate vendor bill.', ', '.join(pending.mapped('name'))))
        return move

    def action_create_vendor_bill(self):
        self.ensure_one()
        move = self.cp_create_vendor_bill()
        return {'type': 'ir.actions.act_window', 'res_model': 'account.move', 'res_id': move.id, 'view_mode': 'form'}

    def action_view_bills(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Vendor Bills'), 'res_model': 'account.move',
                'view_mode': 'list,form', 'domain': [('cp_dispatch_id', '=', self.id)],
                'context': {'default_move_type': 'in_invoice'}}

    def action_view_po(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'purchase.order', 'res_id': self.purchase_order_id.id,
                'view_mode': 'form'}

    def action_view_auctions(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Bids'), 'res_model': 'cp.auction',
                'view_mode': 'list,form', 'domain': [('dispatch_plan_id', '=', self.id)]}

    def action_close(self):
        for rec in self:
            if rec.pod_status != 'verified':
                raise UserError(_('%s needs a verified POD before closing.', rec.name))
            pending = rec.detention_ids.filtered(lambda d: d.state in ('draft', 'submitted', 'logistics_approved'))
            if pending:
                raise UserError(_('Approve or reject the detention claims of %s first.', rec.name))
            bills = rec.move_ids.filtered(lambda m: m.move_type == 'in_invoice' and m.state != 'cancel')
            if not bills.filtered(lambda m: m.state == 'posted'):
                raise UserError(_('Post the vendor bill of %s before closing.', rec.name))
            if bills.filtered(lambda m: m.state == 'draft'):
                raise UserError(_('Post or cancel the draft vendor bills of %s before closing.', rec.name))
            if rec.detention_unbilled > 0:
                raise UserError(_('%(plan)s has approved detention of %(amt)s that is not billed yet. Use "Bill '
                                  'Detention" first.', plan=rec.name, amt=rec._cp_money(rec.detention_unbilled)))
            rec.state = 'closed'
            if rec.trip_id.state == 'delivered':
                rec.trip_id.with_context(cp_force_close=True, cp_from_plan=True).action_close()
        return True

    def action_cancel(self):
        for rec in self:
            if rec.state in ('in_transit', 'delivered', 'pod_verified', 'closed'):
                raise UserError(_('%s has left the warehouse and cannot be cancelled.', rec.name))
            if rec.auction_id.state in ('draft', 'scheduled', 'live'):
                rec.auction_id.action_cancel()
            if rec.trip_id and rec.trip_id.state not in ('delivered', 'closed', 'cancel'):
                rec.trip_id.with_context(cp_from_plan=True).action_cancel()
            if rec.purchase_order_id and rec.purchase_order_id.state != 'cancel':
                rec.purchase_order_id.button_cancel()
            rec.picking_ids.write({'cp_indent_id': False})
            rec.state = 'cancel'
        return True

    def action_reset_draft(self):
        """Back to draft after a cancellation: earlier partner checks no longer count, so the regular partners
        are asked again before any new 3PL bid (they stay in the audit trail, marked superseded)."""
        for rec in self.filtered(lambda p: p.state == 'cancel'):
            old = rec.check_log_ids.filtered(lambda l: not l.superseded)
            old.write({'superseded': True})
            rec.write({'state': 'draft', 'trip_id': False, 'auction_id': False, 'purchase_order_id': False,
                       'indent_id': False, 'carrier_type': False, 'vendor_id': False, 'regular_partner_id': False,
                       'booking_ref': False, 'freight_amount': 0.0, 'reporting_cutoff': False, 'milestone': False})
            if old:
                rec.message_post(body=_('Reset to draft. %s earlier partner check(s) no longer count; check the '
                                        'regular partners again.', len(old)))
        return True

    @api.model
    def _cron_followups(self):
        """Hourly: late reporting and delayed deliveries open issues; POD chasers after 24 h."""
        now = fields.Datetime.now()
        Issue = self.env['cp.exception']
        live = self.search([('state', 'in', ('assigned', 'loading', 'in_transit'))])
        self.env.add_to_compute(self._fields['board_status'], live)
        live.flush_recordset(['board_status'])
        for plan in self.search([('state', '=', 'assigned'), ('reporting_cutoff', '<', now),
                                 ('gate_in_time', '=', False), ('carrier_type', '=', '3pl')]):
            Issue.cp_raise('vehicle', plan, _('%(v)s has not reported for %(p)s', v=plan.vendor_id.name, p=plan.name),
                           severity='high', suggestion=_('Call the vendor or re-bid the load.'))
        for plan in self.search([('state', '=', 'delivered'), ('delivered_at', '<', now - timedelta(hours=24))]):
            Issue.cp_raise('epod_missing', plan, _('POD pending for %s', plan.name), severity='medium',
                           suggestion=_('Ask %s to upload the signed POD.', plan.vendor_id.name or '-'))
        return True


class CpCarrierCheckLog(models.Model):
    """FR-02 audit trail: every availability check with a regular partner."""
    _name = 'cp.carrier.check.log'
    _description = 'Regular Partner Availability Check'
    _order = 'checked_at desc, id desc'

    plan_id = fields.Many2one('cp.dispatch.plan', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='plan_id.company_id', store=True)
    carrier_id = fields.Many2one('res.partner', string='Carrier', required=True, domain=[('cp_regular_carrier', '=', True)])
    status = fields.Selection(CHECK_STATUSES, string='Availability', required=True)
    quoted_rate = fields.Monetary(string='Quoted Rate')
    currency_id = fields.Many2one(related='plan_id.currency_id')
    pickup_time = fields.Datetime(string='Pickup Offered')
    expected_delivery = fields.Datetime()
    booking_ref = fields.Char(string='AWB / Booking Ref.')
    note = fields.Char()
    channel = fields.Selection([('manual', 'Phone / e-mail'), ('portal', 'Carrier portal'), ('api', 'API')],
                               default='manual')
    checked_at = fields.Datetime(default=fields.Datetime.now, required=True, readonly=True)
    checked_by = fields.Many2one('res.users', default=lambda self: self.env.user, readonly=True)
    booked = fields.Boolean(readonly=True)
    superseded = fields.Boolean(readonly=True, help='Logged before the plan was cancelled and reset; no longer counts.')

    def write(self, vals):
        if set(vals) - {'booked', 'superseded'}:
            raise UserError(_('Availability checks are an audit trail and cannot be changed. Log a new check.'))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_never(self):
        if not self.env.context.get('cp_plan_unlink'):
            raise UserError(_('Availability checks are an audit trail and cannot be deleted.'))
