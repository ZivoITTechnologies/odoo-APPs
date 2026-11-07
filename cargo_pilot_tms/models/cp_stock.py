# -*- coding: utf-8 -*-
"""Odoo Inventory and Sales with Cargo Pilot: plan trucks from deliveries, carry serials to the POD,
freight cost and margin after freight on sales orders, warehouse allocation rules."""
from collections import defaultdict

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from odoo.addons.cargo_pilot_tms.models.cp_common import CHANNELS


class StockWarehouse(models.Model):
    _inherit = 'stock.warehouse'

    cp_location_id = fields.Many2one('cp.location', string='Cargo Pilot Location',
                                     help='Where trucks load for this warehouse.')

    def _cp_location(self):
        self.ensure_one()
        if not self.cp_location_id:
            partner = self.partner_id or self.company_id.partner_id
            self.cp_location_id = self.env['cp.location'].create({
                'name': self.name, 'location_type': 'warehouse', 'city': partner.city,
                'state_id': partner.state_id.id, 'pincode': partner.zip,
                'latitude': getattr(partner, 'partner_latitude', 0.0) or 0.0,
                'longitude': getattr(partner, 'partner_longitude', 0.0) or 0.0})
        return self.cp_location_id


class ResPartner(models.Model):
    _inherit = 'res.partner'

    cp_location_id = fields.Many2one('cp.location', string='Delivery Location (Cargo Pilot)')

    def _cp_location(self):
        self.ensure_one()
        partner = self
        if partner.cp_location_id:
            return partner.cp_location_id
        Loc = self.env['cp.location']
        loc = Loc.search([('partner_id', '=', partner.id)], limit=1) if 'partner_id' in Loc._fields else Loc
        if not loc:
            vals = {'name': partner.display_name[:60], 'location_type': 'customer', 'city': partner.city,
                    'state_id': partner.state_id.id, 'pincode': partner.zip,
                    'latitude': getattr(partner, 'partner_latitude', 0.0) or 0.0,
                    'longitude': getattr(partner, 'partner_longitude', 0.0) or 0.0}
            if 'partner_id' in Loc._fields:
                vals['partner_id'] = partner.id
            loc = Loc.create(vals)
        partner.cp_location_id = loc
        return loc


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    cp_trip_order_ids = fields.One2many('cp.trip.order', 'picking_id', string='Shipment Lines')
    cp_trip_id = fields.Many2one('cp.trip', string='Shipment', compute='_compute_cp_trip', store=True)
    cp_indent_id = fields.Many2one('cp.indent', string='Load Request', copy=False, readonly=True)
    cp_trip_state = fields.Selection(related='cp_trip_id.state', string='Shipment Status')
    cp_freight_cost = fields.Monetary(compute='_compute_cp_trip', store=True, string='Freight Cost',
                                      currency_field='cp_currency_id')
    cp_currency_id = fields.Many2one(related='company_id.currency_id')
    cp_channel = fields.Selection(CHANNELS, string='Channel', compute='_compute_cp_channel', store=True,
                                  readonly=False)
    cp_weight_kg = fields.Float(compute='_compute_cp_weight', string='Weight (kg)')
    cp_serials = fields.Text(compute='_compute_cp_serials', string='Serial Numbers')

    @api.depends('cp_trip_order_ids.trip_id', 'cp_trip_order_ids.freight_cost')
    def _compute_cp_trip(self):
        for rec in self:
            lines = rec.cp_trip_order_ids.filtered(lambda l: l.trip_id.state != 'cancel')
            rec.cp_trip_id = lines[-1:].trip_id
            rec.cp_freight_cost = sum(lines.mapped('freight_cost'))

    @api.depends('sale_id.cp_channel')
    def _compute_cp_channel(self):
        for rec in self:
            rec.cp_channel = rec.sale_id.cp_channel or rec.cp_channel or (
                'stock_transfer' if rec.picking_type_code == 'internal' else False)

    def _compute_cp_weight(self):
        for rec in self:
            rec.cp_weight_kg = sum((m.product_id.weight or 0.0) * (m.quantity or m.product_uom_qty)
                                   for m in rec.move_ids)

    def _compute_cp_serials(self):
        for rec in self:
            names = rec.move_ids.move_line_ids.lot_id.mapped('name')
            rec.cp_serials = '\n'.join(names) or False

    def _cp_destination_location(self):
        self.ensure_one()
        if self.picking_type_code == 'internal' and self.location_dest_id.warehouse_id:
            return self.location_dest_id.warehouse_id._cp_location()
        partner = self.partner_id
        if not partner:
            raise UserError(_('%s has no delivery address.', self.name))
        return partner._cp_location()

    def _cp_indent_line_vals(self):
        self.ensure_one()
        sale = self.sale_id
        invoice = sale.invoice_ids.filtered(lambda m: m.state == 'posted' and m.move_type == 'out_invoice')[:1] \
            if sale else self.env['account.move']
        value = 0.0
        for move in self.move_ids:
            qty = move.quantity or move.product_uom_qty
            line = move.sale_line_id
            value += (line.price_reduce_taxexcl * qty) if line else move.product_id.standard_price * qty
        boxes = sum(m.quantity or m.product_uom_qty for m in self.move_ids)
        return {
            'description': ', '.join(self.move_ids.product_id.mapped('display_name'))[:200] or self.name,
            'product_id': self.move_ids.product_id[:1].id if len(self.move_ids.product_id) == 1 else False,
            'order_ref': sale.name or self.origin or self.name, 'invoice_ref': invoice.name or False,
            'customer_id': (self.partner_id.commercial_partner_id or self.partner_id).id,
            'quantity': boxes or 1, 'weight_kg': self.cp_weight_kg, 'order_value': value,
            'serial_numbers': self.cp_serials, 'drop_point': self.partner_id.city or False,
            'picking_id': self.id, 'sale_order_id': sale.id or False,
        }

    def action_cp_plan_transport(self, mode='ftl', vehicle_type=None):
        """One load request per (warehouse, destination, channel), one line per delivery."""
        todo = self.filtered(lambda p: p.state not in ('done', 'cancel') and not p.cp_indent_id)
        if not todo:
            raise UserError(_('Select deliveries that are not planned yet.'))
        groups = defaultdict(lambda: self.browse())
        for picking in todo:
            warehouse = picking.picking_type_id.warehouse_id
            if not warehouse:
                raise UserError(_('%s does not belong to a warehouse.', picking.name))
            key = (warehouse._cp_location().id, picking._cp_destination_location().id, picking.cp_channel or '')
            groups[key] |= picking
        Indent = self.env['cp.indent']
        indents = Indent
        for (origin_id, dest_id, channel), pickings in groups.items():
            lane = self.env['cp.lane'].search([('origin_id', '=', origin_id), ('destination_id', '=', dest_id)],
                                              limit=1)
            if not lane:
                lane = self.env['cp.lane'].create({'origin_id': origin_id, 'destination_id': dest_id})
                if hasattr(lane, '_onchange_points'):
                    lane._onchange_points()
            first = pickings[:1]
            indent = Indent.create({
                'lane_id': lane.id, 'shipment_mode': mode, 'channel': channel or False,
                'vehicle_type_id': vehicle_type.id if vehicle_type else False,
                'consignee_id': first.partner_id.commercial_partner_id.id if len(pickings.partner_id) == 1 else False,
                'required_date': min(pickings.mapped('scheduled_date')) or fields.Datetime.now(),
                'company_id': first.company_id.id,
                'line_ids': [(0, 0, p._cp_indent_line_vals()) for p in pickings]})
            pickings.write({'cp_indent_id': indent.id})
            indents |= indent
        if len(indents) == 1:
            return {'type': 'ir.actions.act_window', 'res_model': 'cp.indent', 'res_id': indents.id,
                    'view_mode': 'form'}
        return {'type': 'ir.actions.act_window', 'res_model': 'cp.indent', 'name': _('Load Requests'),
                'domain': [('id', 'in', indents.ids)], 'view_mode': 'list,form'}

    def action_cp_open_plan_wizard(self):
        return {'type': 'ir.actions.act_window', 'res_model': 'cp.picking.plan.wizard', 'view_mode': 'form',
                'target': 'new', 'context': {'active_ids': self.ids}}

    def action_cp_view_trip(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'cp.trip', 'res_id': self.cp_trip_id.id,
                'view_mode': 'form'}


UNDELIVERED_SO_DOMAIN = "[('state', '=', 'sale'), ('delivery_status', 'in', ('pending', 'started', 'partial')), " \
                        "('company_id', 'in', [company_id, False])]"


def _sale_order_values(order, weight_in_tons=True):
    """What is still to deliver on a sales order: quantity, weight, value, serials, open delivery."""
    open_pickings = order.picking_ids.filtered(
        lambda p: p.picking_type_code == 'outgoing' and p.state not in ('done', 'cancel'))
    qty = weight = value = 0.0
    products = order.env['product.product']
    for line in order.order_line.filtered(lambda l: not l.display_type and l.product_id.type != 'service'):
        pending = max(line.product_uom_qty - line.qty_delivered, 0.0)
        if not pending:
            continue
        base_qty = line.product_uom_id._compute_quantity(pending, line.product_id.uom_id)
        qty += pending
        weight += base_qty * (line.product_id.weight or 0.0)
        value += pending * line.price_reduce_taxexcl
        products |= line.product_id
    invoice = order.invoice_ids.filtered(lambda m: m.state == 'posted' and m.move_type == 'out_invoice')[:1]
    serials = open_pickings.move_ids.move_line_ids.lot_id.mapped('name')
    vals = {
        'order_ref': order.name,
        'invoice_ref': invoice.name or False,
        'customer_id': (order.partner_shipping_id or order.partner_id).id,
        'description': ', '.join(products.mapped('display_name'))[:200] or order.name,
        'product_id': products.id if len(products) == 1 else False,
        'quantity': qty or 1.0,
        'order_value': value,
        'serial_numbers': '\n'.join(serials) or False,
        'picking_id': open_pickings[:1].id or False,
        'drop_point': (order.partner_shipping_id or order.partner_id).city or False,
    }
    if weight_in_tons:
        vals['weight_ton'] = weight / 1000.0
    else:
        vals['weight_kg'] = weight
    return vals


class CpIndentLine(models.Model):
    _inherit = 'cp.indent.line'

    picking_id = fields.Many2one('stock.picking', string='Delivery', ondelete='set null')
    company_id = fields.Many2one(related='indent_id.company_id')
    sale_order_id = fields.Many2one('sale.order', string='Sales Order', ondelete='set null', index=True,
                                    domain=UNDELIVERED_SO_DOMAIN,
                                    help='Confirmed sales orders that are not fully delivered yet.')

    @api.onchange('sale_order_id')
    def _onchange_sale_order_id(self):
        for line in self.filtered('sale_order_id'):
            line.update(_sale_order_values(line.sale_order_id))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('sale_order_id') and not vals.get('order_ref'):
                order = self.env['sale.order'].browse(vals['sale_order_id'])
                vals.update({k: v for k, v in _sale_order_values(order).items() if k not in vals})
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('sale_order_id') and 'order_ref' not in vals:
            order = self.env['sale.order'].browse(vals['sale_order_id'])
            vals = dict(_sale_order_values(order), **vals)
        return super().write(vals)

    def _cp_order_vals(self):
        vals = super()._cp_order_vals()
        vals['picking_id'] = self.picking_id.id
        sale = self.sale_order_id or self.picking_id.sale_id
        if sale:
            vals['sale_order_id'] = sale.id
        if self.product_id and 'product_id' not in vals:
            vals['product_id'] = self.product_id.id
        return vals


class CpTripOrder(models.Model):
    _inherit = 'cp.trip.order'

    picking_id = fields.Many2one('stock.picking', string='Delivery', ondelete='set null', index=True)
    sale_order_id = fields.Many2one('sale.order', string='Sales Order', ondelete='set null', index=True,
                                    domain=UNDELIVERED_SO_DOMAIN)

    @api.onchange('sale_order_id')
    def _onchange_sale_order_id(self):
        for line in self.filtered('sale_order_id'):
            line.update(_sale_order_values(line.sale_order_id, weight_in_tons=False))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('sale_order_id') and not vals.get('order_ref'):
                order = self.env['sale.order'].browse(vals['sale_order_id'])
                vals.update({k: v for k, v in _sale_order_values(order, weight_in_tons=False).items()
                             if k not in vals})
        return super().create(vals_list)


class CpTrip(models.Model):
    _inherit = 'cp.trip'

    picking_ids = fields.Many2many('stock.picking', compute='_compute_pickings', string='Deliveries')
    picking_count = fields.Integer(compute='_compute_pickings', string='Delivery Count')

    def _compute_pickings(self):
        for rec in self:
            rec.picking_ids = rec.order_ids.picking_id
            rec.picking_count = len(rec.picking_ids)

    def action_cp_view_pickings(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('stock.action_picking_tree_all')
        action['domain'] = [('id', 'in', self.picking_ids.ids)]
        action['context'] = {}
        return action

    def action_cp_validate_pickings(self):
        """After delivery: validate the linked deliveries with the quantities reserved on them."""
        for rec in self:
            for picking in rec.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')):
                if picking.state in ('draft', 'confirmed', 'waiting'):
                    picking.action_assign()
                for move in picking.move_ids:
                    if not move.quantity:
                        move.quantity = move.product_uom_qty
                picking.with_context(skip_backorder=True, skip_sms=True).button_validate()
        return True


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    cp_channel = fields.Selection(CHANNELS, string='Sales Channel')
    cp_trip_order_ids = fields.One2many('cp.trip.order', 'sale_order_id', string='Shipment Lines')
    cp_freight_cost = fields.Monetary(compute='_compute_cp_freight', store=True, string='Freight Cost')
    cp_margin_after_freight = fields.Monetary(compute='_compute_cp_freight', store=True,
                                              string='Margin after Freight')
    cp_margin_after_freight_pct = fields.Float(compute='_compute_cp_freight', store=True,
                                               string='Margin after Freight (%)', aggregator='avg')
    cp_freight_pct = fields.Float(compute='_compute_cp_freight', store=True, string='Freight % of Sales',
                                  aggregator='avg')
    cp_shipment_count = fields.Integer(compute='_compute_cp_shipments', string='Shipment Count')

    @api.depends('cp_trip_order_ids.freight_cost', 'cp_trip_order_ids.trip_id.state', 'margin', 'amount_untaxed')
    def _compute_cp_freight(self):
        for order in self:
            freight = sum(order.cp_trip_order_ids.filtered(lambda l: l.trip_id.state != 'cancel').mapped('freight_cost'))
            order.cp_freight_cost = freight
            order.cp_margin_after_freight = (order.margin or 0.0) - freight
            base = order.amount_untaxed
            order.cp_margin_after_freight_pct = order.cp_margin_after_freight / base * 100.0 if base else 0.0
            order.cp_freight_pct = freight / base * 100.0 if base else 0.0

    def _compute_cp_shipments(self):
        for order in self:
            order.cp_shipment_count = len(order.cp_trip_order_ids.trip_id)

    def action_cp_view_shipments(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('cargo_pilot_tms.action_cp_trip')
        action['domain'] = [('id', 'in', self.cp_trip_order_ids.trip_id.ids)]
        action['context'] = {}
        return action

    # ------------------------------------------------------------------
    # Warehouse allocation
    # ------------------------------------------------------------------
    def action_cp_allocate_warehouse(self):
        for order in self.filtered(lambda o: o.state in ('draft', 'sent')):
            warehouse, rule = self.env['cp.allocation.rule'].cp_choose(order)
            if warehouse and warehouse != order.warehouse_id:
                order.warehouse_id = warehouse
                order.message_post(body=_('Ships from %(w)s (rule: %(r)s).', w=warehouse.name,
                                          r=rule.name if rule else _('stock on hand')))
        return True

    def action_confirm(self):
        if self.env.company.cp_auto_allocate_warehouse:
            self.action_cp_allocate_warehouse()
        return super().action_confirm()


class ResCompany(models.Model):
    _inherit = 'res.company'

    cp_auto_allocate_warehouse = fields.Boolean(string='Choose Warehouse on Confirmation', default=True)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    cp_auto_allocate_warehouse = fields.Boolean(related='company_id.cp_auto_allocate_warehouse', readonly=False)


class CpAllocationRule(models.Model):
    _name = 'cp.allocation.rule'
    _description = 'Warehouse Allocation Rule'
    _order = 'sequence, id'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    warehouse_id = fields.Many2one('stock.warehouse', required=True,
                                   domain="[('company_id', '=', company_id)]")
    state_ids = fields.Many2many('res.country.state', string='Ship-to States',
                                 help='Empty means every state.')
    channel = fields.Selection(CHANNELS, help='Empty means every channel.')
    partner_ids = fields.Many2many('res.partner', string='Customers', help='Empty means every customer.')
    check_stock = fields.Boolean(string='Only When In Stock', default=True,
                                 help='Skip this warehouse when it cannot ship every line from stock.')

    def _cp_matches(self, order):
        self.ensure_one()
        ship_to = order.partner_shipping_id or order.partner_id
        if self.state_ids and ship_to.state_id not in self.state_ids:
            return False
        if self.channel and order.cp_channel != self.channel:
            return False
        if self.partner_ids and order.partner_id.commercial_partner_id not in self.partner_ids:
            return False
        return True

    @api.model
    def _cp_has_stock(self, warehouse, order):
        need = defaultdict(float)
        for line in order.order_line.filtered(lambda l: l.product_id.is_storable):
            need[line.product_id] += line.product_uom_id._compute_quantity(line.product_uom_qty,
                                                                           line.product_id.uom_id)
        for product, qty in need.items():
            available = product.with_context(warehouse_id=warehouse.id).free_qty
            if available < qty:
                return False
        return True

    @api.model
    def cp_choose(self, order):
        """(warehouse, rule) for a sales order: first matching rule with stock, else any warehouse with stock."""
        rules = self.search([('company_id', '=', order.company_id.id)])
        for rule in rules:
            if rule._cp_matches(order) and (not rule.check_stock or self._cp_has_stock(rule.warehouse_id, order)):
                return rule.warehouse_id, rule
        if rules.filtered(lambda r: r._cp_matches(order)):
            for warehouse in self.env['stock.warehouse'].search([('company_id', '=', order.company_id.id)]):
                if self._cp_has_stock(warehouse, order):
                    return warehouse, self.browse()
        return order.warehouse_id, self.browse()


class CpPickingPlanWizard(models.TransientModel):
    _name = 'cp.picking.plan.wizard'
    _description = 'Plan Transport for Deliveries'

    picking_ids = fields.Many2many('stock.picking', default=lambda self: self.env.context.get('active_ids'))
    shipment_mode = fields.Selection([('ftl', 'Full truck (FTL)'), ('ltl', 'Part load (LTL)')], default='ftl',
                                     required=True)
    vehicle_type_id = fields.Many2one('cp.vehicle.type')
    picking_count = fields.Integer(compute='_compute_totals')
    total_weight_kg = fields.Float(compute='_compute_totals', string='Total Weight (kg)')
    serial_count = fields.Integer(compute='_compute_totals', string='Serials')

    @api.depends('picking_ids')
    def _compute_totals(self):
        for wiz in self:
            todo = wiz.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel') and not p.cp_indent_id)
            wiz.picking_count = len(todo)
            wiz.total_weight_kg = sum(todo.mapped('cp_weight_kg'))
            wiz.serial_count = sum(len((p.cp_serials or '').split()) for p in todo)

    def action_plan(self):
        self.ensure_one()
        if self.shipment_mode == 'ftl' and not self.vehicle_type_id:
            raise UserError(_('Choose the truck type for a full truck load.'))
        return self.picking_ids.action_cp_plan_transport(mode=self.shipment_mode, vehicle_type=self.vehicle_type_id)
