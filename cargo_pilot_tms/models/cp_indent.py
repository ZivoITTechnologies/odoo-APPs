# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

from .cp_common import CHANNELS, SHIPMENT_MODES


class CpIndent(models.Model):
    _name = 'cp.indent'
    _description = 'Vehicle Indent (Transport Requirement)'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'required_date asc, id desc'

    name = fields.Char(default=lambda self: _('New'), copy=False, readonly=True)
    origin = fields.Char(string='Source Document', compute='_compute_origin', store=True, readonly=False,
                         help='Filled with the order numbers of the lines; can also be typed.')
    source_channel = fields.Selection([
        ('manual', 'Manual'), ('booking', 'Quick Book'), ('portal', 'Customer Portal'),
        ('api', 'API / ERP'), ('email', 'Email'),
    ], default='manual', readonly=True)
    customer_id = fields.Many2one('res.partner', string='Requested By',
                                  help='Customer who asked for this load (customer portal bookings).')
    shipment_mode = fields.Selection(SHIPMENT_MODES, string='Mode', default='ftl', required=True, tracking=True)
    channel = fields.Selection(CHANNELS, tracking=True, help='Sales or supply channel this load serves.')
    channel_rule_id = fields.Many2one('cp.channel.rule', string='Channel Rule', readonly=True)
    lane_id = fields.Many2one('cp.lane', required=True, tracking=True)
    movement = fields.Selection(related='lane_id.movement')
    origin_location_id = fields.Many2one(related='lane_id.origin_id', store=True)
    destination_location_id = fields.Many2one(related='lane_id.destination_id', store=True)
    consignee_id = fields.Many2one('res.partner', string='Consignee')
    vehicle_type_id = fields.Many2one('cp.vehicle.type', tracking=True, help='Required for FTL loads.')
    vehicle_count = fields.Integer(string='Vehicles Needed', default=1, required=True)
    required_date = fields.Datetime(string='Placement Date', required=True, default=fields.Datetime.now,
                                    tracking=True)
    delivery_deadline = fields.Datetime(string='Delivery By')
    priority = fields.Selection([('0', 'Normal'), ('1', 'Urgent')], default='0')
    line_ids = fields.One2many('cp.indent.line', 'indent_id', string='Materials', copy=True)
    total_weight_ton = fields.Float(compute='_compute_totals', store=True, string='Total Weight (t)')
    total_volume_cbm = fields.Float(compute='_compute_totals', store=True, string='Total Volume (m³)')
    total_boxes = fields.Integer(compute='_compute_totals', store=True, string='Boxes')
    goods_value = fields.Monetary(compute='_compute_totals', store=True, string='Goods Value')
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('confirmed', 'Confirmed'),
        ('sourcing', 'Sourcing'),
        ('allocated', 'Allocated'),
        ('done', 'Done'),
        ('cancel', 'Cancelled'),
    ], default='draft', tracking=True)
    trip_ids = fields.One2many('cp.trip', 'indent_id', string='Trips')
    auction_ids = fields.One2many('cp.auction', 'indent_id', string='Auctions')
    trip_count = fields.Integer(compute='_compute_counts')
    auction_count = fields.Integer(compute='_compute_counts')
    allocated_count = fields.Integer(compute='_compute_counts', string='Allocated Vehicles')
    pending_count = fields.Integer(compute='_compute_counts', string='Pending Vehicles')
    user_id = fields.Many2one('res.users', string='Planner', default=lambda self: self.env.user)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)
    notes = fields.Text()

    @api.model
    def _group_expand_states(self, states, domain):
        return [key for key, _label in self._fields['state'].selection if key != 'cancel']

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('cp.indent') or _('New')
        return super().create(vals_list)

    @api.depends('line_ids.weight_ton', 'line_ids.volume_cbm', 'line_ids.quantity', 'line_ids.order_value')
    def _compute_totals(self):
        for rec in self:
            rec.total_weight_ton = sum(rec.line_ids.mapped('weight_ton'))
            rec.total_volume_cbm = sum(rec.line_ids.mapped('volume_cbm'))
            rec.total_boxes = int(sum(rec.line_ids.mapped('quantity')))
            rec.goods_value = sum(rec.line_ids.mapped('order_value'))

    @api.constrains('shipment_mode', 'vehicle_type_id', 'vehicle_count')
    def _check_mode(self):
        for rec in self:
            if rec.shipment_mode == 'ftl' and not rec.vehicle_type_id:
                raise ValidationError(_('Choose the truck type for the FTL load %s.', rec.name))
            if rec.shipment_mode == 'ltl' and rec.vehicle_count != 1:
                raise ValidationError(_('An LTL load is one docket; set the vehicle count to 1.'))

    @api.onchange('shipment_mode')
    def _onchange_shipment_mode(self):
        if self.shipment_mode == 'ltl':
            self.vehicle_count = 1

    def _cp_apply_channel_rule(self):
        Rule = self.env['cp.channel.rule']
        for rec in self:
            rule = Rule.cp_match(rec.channel, rec.customer_id or rec.consignee_id, rec.company_id)
            rec.channel_rule_id = rule
            if rule and rule.promise_hours and not rec.delivery_deadline and rec.required_date:
                rec.delivery_deadline = rec.required_date + timedelta(hours=rule.promise_hours)

    @api.depends('trip_ids.state', 'auction_ids', 'vehicle_count')
    def _compute_counts(self):
        for rec in self:
            live = rec.trip_ids.filtered(lambda t: t.state != 'cancel')
            rec.trip_count = len(live)
            rec.auction_count = len(rec.auction_ids)
            rec.allocated_count = len(live)
            rec.pending_count = max(rec.vehicle_count - len(live), 0)

    @api.depends('line_ids.order_ref')
    def _compute_origin(self):
        for rec in self:
            refs = list(dict.fromkeys(r.strip() for r in rec.line_ids.mapped('order_ref') if r and r.strip()))
            if refs:
                rec.origin = ', '.join(refs)
            elif not rec.origin:
                rec.origin = False

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def action_confirm(self):
        for rec in self:
            if rec.vehicle_count < 1:
                raise UserError(_('An indent needs at least one vehicle.'))
        self._cp_apply_channel_rule()
        self.write({'state': 'confirmed'})

    def action_cancel(self):
        for rec in self:
            if rec.trip_ids.filtered(lambda t: t.state not in ('draft', 'cancel')):
                raise UserError(_('Cancel the dispatched trips of %s first.', rec.name))
            rec.trip_ids.filtered(lambda t: t.state == 'draft').write({'state': 'cancel'})
            rec.auction_ids.filtered(lambda a: a.state in ('draft', 'live')).write({'state': 'cancel'})
        self.write({'state': 'cancel'})

    def action_reset(self):
        self.write({'state': 'draft'})

    def _cp_trip_vals(self, transporter, rate_amount, contract_line=False, auction=False):
        self.ensure_one()
        vals = {
            'indent_id': self.id,
            'lane_id': self.lane_id.id,
            'shipment_mode': self.shipment_mode,
            'channel': self.channel,
            'vehicle_type_id': self.vehicle_type_id.id,
            'transporter_id': transporter.id,
            'consignee_id': self.consignee_id.id,
            'planned_dispatch': self.required_date,
            'promised_delivery': self.delivery_deadline,
            'agreed_freight': rate_amount,
            'contract_line_id': contract_line.id if contract_line else False,
            'auction_id': auction.id if auction else False,
            'loaded_weight_ton': (self.total_weight_ton / self.vehicle_count) if self.vehicle_count else 0.0,
            'company_id': self.company_id.id,
        }
        if self.shipment_mode == 'ltl':
            vals['tracking_method'] = 'carrier'
        if self.channel_rule_id:
            rule = self.channel_rule_id
            vals.update(appointment_required=rule.appointment_required, grn_required=rule.grn_required,
                        serial_scan_required=rule.serial_scan_required)
        if self.vehicle_count == 1 and self.line_ids:
            vals['order_ids'] = [(0, 0, line._cp_order_vals()) for line in self.line_ids]
        return vals

    def _cp_rate_kwargs(self):
        self.ensure_one()
        per = self.vehicle_count or 1
        return {
            'weight_ton': self.total_weight_ton / per,
            'goods_value': self.goods_value / per,
            'boxes': int(self.total_boxes / per),
            'drops': max(len(set(self.line_ids.mapped(lambda l: (l.customer_id.id or 0, l.drop_point or '')))), 1),
            'on_date': fields.Date.to_date(self.required_date),
            'oda': bool(self.lane_id.destination_id.is_oda),
        }

    def _cp_allowed_lines(self, lines):
        """Respect the transporters allowed by the channel rule."""
        self.ensure_one()
        allowed = self.channel_rule_id.allowed_transporter_ids
        return lines.filtered(lambda l: l.contract_id.transporter_id in allowed) if allowed else lines

    def action_auto_allocate(self):
        """Allocate pending vehicles to contracted transporters by priority."""
        Contract = self.env['cp.rate.contract']
        for rec in self:
            if rec.state not in ('confirmed', 'sourcing'):
                raise UserError(_('Confirm the indent before allocating vehicles.'))
            lines = rec._cp_allowed_lines(Contract.cp_find_rates(
                rec.lane_id, rec.vehicle_type_id, fields.Date.to_date(rec.required_date), mode=rec.shipment_mode))
            line = lines[:1]
            if not line:
                raise UserError(_('No active rate contract covers %(lane)s for %(what)s. Run a spot auction instead.',
                                  lane=rec.lane_id.name,
                                  what=rec.vehicle_type_id.name or dict(SHIPMENT_MODES)[rec.shipment_mode]))
            amount = line.cp_compute_amount(**rec._cp_rate_kwargs())
            vals = [rec._cp_trip_vals(line.contract_id.transporter_id, amount, contract_line=line)
                    for _i in range(rec.pending_count)]
            self.env['cp.trip'].create(vals)
            rec.state = 'allocated'
            rec.message_post(body=_('%(n)s vehicle(s) allocated to %(t)s under contract %(c)s.',
                                    n=len(vals), t=line.contract_id.transporter_id.name, c=line.contract_id.name))
        return True

    def cp_allocate_to(self, contract_line):
        """Allocate every pending vehicle to one chosen contract line."""
        self.ensure_one()
        amount = contract_line.cp_compute_amount(**self._cp_rate_kwargs())
        self.env['cp.trip'].create([self._cp_trip_vals(contract_line.contract_id.transporter_id, amount,
                                                       contract_line=contract_line)
                                    for _i in range(self.pending_count)])
        self.state = 'allocated'
        return True

    def action_create_auction(self):
        self.ensure_one()
        if self.state not in ('confirmed', 'sourcing'):
            raise UserError(_('Confirm the indent before starting an auction.'))
        if not self.pending_count:
            raise UserError(_('All vehicles of this indent are already allocated.'))
        company = self.company_id
        start = fields.Datetime.now()
        auction = self.env['cp.auction'].create({
            'indent_id': self.id,
            'vehicle_count': self.pending_count,
            'date_start': start,
            'date_end': fields.Datetime.add(start, minutes=company.cp_auction_default_duration or 60),
            'transporter_ids': [(6, 0, self._cp_suggest_transporters().ids)],
        })
        self.state = 'sourcing'
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'cp.auction',
            'res_id': auction.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _cp_suggest_transporters(self):
        """Transporters that serve this lane or vehicle type; all transporters as fallback."""
        Partner = self.env['res.partner']
        base = [('cp_is_transporter', '=', True), ('is_company', '=', True)]
        matched = Partner.search(base + ['|', ('cp_lane_ids', 'in', self.lane_id.ids),
                                         ('cp_vehicle_type_ids', 'in', self.vehicle_type_id.ids)])
        return matched or Partner.search(base)

    def action_view_trips(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('cargo_pilot_tms.action_cp_trip')
        action['domain'] = [('indent_id', '=', self.id)]
        action['context'] = {'default_indent_id': self.id}
        return action

    def action_view_auctions(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('cargo_pilot_tms.action_cp_auction')
        action['domain'] = [('indent_id', '=', self.id)]
        return action

    def _cp_check_done(self):
        for rec in self:
            trips = rec.trip_ids.filtered(lambda t: t.state != 'cancel')
            if trips and not rec.pending_count and all(t.state in ('delivered', 'closed') for t in trips):
                rec.state = 'done'


class CpIndentLine(models.Model):
    _name = 'cp.indent.line'
    _description = 'Indent Material Line'

    indent_id = fields.Many2one('cp.indent', required=True, ondelete='cascade')
    product_id = fields.Many2one('product.product', string='Material')
    description = fields.Char()
    quantity = fields.Float(default=1.0, string='Qty / Boxes')
    uom_label = fields.Char(string='Unit', default='Boxes')
    weight_ton = fields.Float(string='Weight (t)')
    weight_kg = fields.Float(string='Weight (kg)', compute='_compute_weight_kg', inverse='_inverse_weight_kg')
    volume_cbm = fields.Float(string='Volume (m³)')
    order_ref = fields.Char(string='Order No.')
    invoice_ref = fields.Char(string='Invoice No.')
    customer_id = fields.Many2one('res.partner', string='Customer')
    drop_point = fields.Char(help='Delivery point when one truck makes several drops.')
    order_value = fields.Monetary(string='Order Value')
    serial_numbers = fields.Text(string='Serial Numbers')
    eway_bill_no = fields.Char(string='E-way Bill No.')
    currency_id = fields.Many2one(related='indent_id.currency_id')

    @api.depends('weight_ton')
    def _compute_weight_kg(self):
        for line in self:
            line.weight_kg = line.weight_ton * 1000.0

    def _inverse_weight_kg(self):
        for line in self:
            line.weight_ton = line.weight_kg / 1000.0

    def _cp_order_vals(self):
        self.ensure_one()
        return {
            'order_ref': self.order_ref or self.indent_id.origin or self.indent_id.name,
            'invoice_ref': self.invoice_ref, 'customer_id': (self.customer_id or self.indent_id.consignee_id).id,
            'product_id': self.product_id.id, 'description': self.description or self.product_id.display_name,
            'quantity': self.quantity, 'weight_kg': self.weight_ton * 1000.0, 'volume_cbm': self.volume_cbm,
            'order_value': self.order_value, 'serial_numbers': self.serial_numbers, 'drop_point': self.drop_point,
            'eway_bill_no': self.eway_bill_no, 'channel': self.indent_id.channel,
        }

    @api.onchange('product_id', 'quantity')
    def _onchange_product(self):
        for line in self:
            if line.product_id:
                line.description = line.description or line.product_id.display_name
                if line.product_id.weight:
                    line.weight_ton = line.product_id.weight * line.quantity / 1000.0
                if line.product_id.volume:
                    line.volume_cbm = line.product_id.volume * line.quantity
