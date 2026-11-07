# -*- coding: utf-8 -*-
from collections import defaultdict

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from odoo.addons.cargo_pilot_tms.models.cp_common import CHANNELS

from .cp_easyecom import pick, to_float

PUSH_STATES = ('assigned', 'in_transit', 'arrived', 'delivered', 'closed', 'cancel')


class CpChannelOrder(models.Model):
    _name = 'cp.channel.order'
    _description = 'Sales Channel Order'
    _inherit = ['mail.thread']
    _order = 'order_date desc, id desc'
    _rec_name = 'reference'

    account_id = fields.Many2one('cp.easyecom.account', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='account_id.company_id', store=True)
    external_id = fields.Char(string='EasyEcom Order ID', required=True, index=True)
    reference = fields.Char(string='Order No.', required=True, index=True)
    invoice_ref = fields.Char(string='Invoice No.', index=True)
    invoice_id = fields.Char(string='EasyEcom Invoice ID')
    marketplace = fields.Char()
    channel = fields.Selection(CHANNELS)
    order_date = fields.Datetime()
    customer_name = fields.Char()
    phone = fields.Char()
    address = fields.Char()
    city = fields.Char()
    state_name = fields.Char(string='State (as sent)')
    state_id = fields.Many2one('res.country.state', string='Delivery State')
    pincode = fields.Char()
    location_key = fields.Char(string='EasyEcom Warehouse')
    origin_id = fields.Many2one('cp.location', string='Ship From')
    destination_id = fields.Many2one('cp.location', string='Ship To')
    quantity = fields.Float(string='Units')
    boxes = fields.Integer()
    weight_kg = fields.Float(string='Weight (kg)')
    order_value = fields.Monetary()
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    serial_numbers = fields.Text()
    eway_bill_no = fields.Char(string='E-way Bill No.')
    awb_number = fields.Char(string='AWB / Docket')
    line_ids = fields.One2many('cp.channel.order.line', 'order_id', string='Items')
    state = fields.Selection([('new', 'To Plan'), ('planned', 'Planned'), ('shipped', 'On the Way'),
                              ('delivered', 'Delivered'), ('cancel', 'Cancelled')], default='new', index=True,
                             tracking=True)
    indent_id = fields.Many2one('cp.indent', string='Load Request', readonly=True, copy=False)
    trip_id = fields.Many2one('cp.trip', string='Shipment', readonly=True, copy=False, index=True)
    trip_order_id = fields.Many2one('cp.trip.order', readonly=True, copy=False)
    freight_cost = fields.Monetary(related='trip_order_id.freight_cost')
    tracking_dirty = fields.Boolean(copy=False, index=True)
    last_push = fields.Datetime(readonly=True, copy=False)
    last_push_status = fields.Char(readonly=True, copy=False)
    push_error = fields.Char(readonly=True, copy=False)
    raw = fields.Text(string='EasyEcom Data', readonly=True)

    _external_unique = models.Constraint('unique(account_id, external_id)', 'This EasyEcom order already exists.')

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------
    @api.model
    def cp_upsert(self, account, data):
        external = str(pick(data, 'order_id', 'orderId', 'id', default='') or '')
        if not external:
            return self.browse()
        items = pick(data, 'suborders', 'order_items', 'items', default=[]) or []
        state_name = pick(data, 'state', 'shipping_state', 'state_name', default='') or ''
        state = self.env['res.country.state'].search(
            [('country_id.code', '=', 'IN'), '|', ('name', '=ilike', state_name), ('code', '=ilike', state_name)],
            limit=1) if state_name else self.env['res.country.state']
        serials = []
        for item in items:
            serial = pick(item, 'serial_no', 'serialNumber', 'serial_numbers', default='')
            if isinstance(serial, list):
                serials.extend(str(s) for s in serial if s)
            elif serial:
                serials.extend(s.strip() for s in str(serial).split(',') if s.strip())
        marketplace = pick(data, 'marketplace', 'marketplace_name', 'channel', default='') or ''
        weight = to_float(pick(data, 'total_weight', 'weight', 'package_weight'))
        if not weight:
            weight = sum(to_float(pick(i, 'weight', 'item_weight')) * to_float(pick(i, 'item_quantity', 'quantity',
                                                                                     default=1)) for i in items)
        location_key = str(pick(data, 'location_key', 'warehouseId', 'warehouse_id', default='') or '')
        warehouse = account.warehouse_ids.filtered(lambda w: w.location_key == location_key)[:1]
        vals = {
            'account_id': account.id, 'external_id': external,
            'reference': str(pick(data, 'reference_code', 'order_number', 'marketplace_order_id', default=external)),
            'invoice_ref': pick(data, 'invoice_number', 'invoiceNumber', default=False),
            'invoice_id': str(pick(data, 'invoice_id', 'invoiceId', default='') or '') or False,
            'marketplace': marketplace, 'channel': account._channel_for(marketplace),
            'order_date': self._cp_date(pick(data, 'order_date', 'orderDate')),
            'customer_name': pick(data, 'customer_name', 'shipping_name', 'buyer_name', default=False),
            'phone': pick(data, 'contact_num', 'shipping_phone', 'phone', default=False),
            'address': pick(data, 'address_line_1', 'shipping_address', 'address', default=False),
            'city': pick(data, 'city', 'shipping_city', default=False),
            'state_name': state_name or False, 'state_id': state.id or False,
            'pincode': str(pick(data, 'pin_code', 'pincode', 'shipping_pincode', default='') or '') or False,
            'location_key': location_key or False,
            'origin_id': warehouse.location_id.id or False,
            'quantity': sum(to_float(pick(i, 'item_quantity', 'quantity', default=1)) for i in items),
            'boxes': int(to_float(pick(data, 'package_count', 'boxes', default=0))) or len(items) or 1,
            'weight_kg': weight,
            'order_value': to_float(pick(data, 'total_amount', 'order_amount', 'invoice_amount')),
            'serial_numbers': '\n'.join(dict.fromkeys(serials)) or False,
            'eway_bill_no': pick(data, 'eway_bill_number', 'ewaybill_no', 'eway_bill_no', default=False),
            'raw': str(data)[:20000],
        }
        status = str(pick(data, 'order_status', 'status', default='') or '').lower()
        existing = self.search([('account_id', '=', account.id), ('external_id', '=', external)], limit=1)
        if existing:
            if 'cancel' in status and existing.state in ('new', 'planned'):
                vals['state'] = 'cancel'
            existing.write({k: v for k, v in vals.items() if k not in ('account_id', 'external_id')})
            order = existing
        else:
            if 'cancel' in status:
                vals['state'] = 'cancel'
            order = self.create(vals)
        order.line_ids.unlink()
        order.line_ids = [(0, 0, {'sku': pick(i, 'sku', 'SKU', default='') or '',
                                  'name': pick(i, 'productName', 'product_name', 'name', default=False),
                                  'quantity': to_float(pick(i, 'item_quantity', 'quantity', default=1)),
                                  'price': to_float(pick(i, 'selling_price', 'price'))}) for i in items]
        return order

    @staticmethod
    def _cp_date(value):
        if not value:
            return False
        try:
            return fields.Datetime.to_datetime(str(value).replace('T', ' ')[:19])
        except ValueError:
            return False

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------
    def _cp_destination(self):
        self.ensure_one()
        if self.destination_id:
            return self.destination_id
        Loc = self.env['cp.location']
        loc = Loc.search([('pincode', '=', self.pincode), ('location_type', '=', 'customer')], limit=1) \
            if self.pincode else Loc
        if not loc:
            loc = Loc.create({'name': '%s %s' % (self.city or self.customer_name or _('Customer'), self.pincode or ''),
                              'location_type': 'customer', 'city': self.city, 'state_id': self.state_id.id,
                              'pincode': self.pincode})
        self.destination_id = loc
        return loc

    def _cp_plan_key(self):
        """Orders with the same key share one load request (see the connection's Combine Orders By)."""
        self.ensure_one()
        grouping = self.account_id.plan_grouping or 'city'
        if grouping == 'order':
            where = ('order', self.id)
        elif grouping == 'city' and (self.city or '').strip():
            where = ('city', ' '.join(self.city.lower().split()), self.state_id.id or 0)
        else:
            where = ('dest', self.destination_id.id)
        channel = (self.channel or '') if self.account_id.plan_split_channel else ''
        return (self.origin_id.id, where, channel)

    def _cp_group_destination(self):
        """One delivery point for the load: the orders' own drop when they share it, else a city-level drop
        (each order keeps its own address and PIN code on its line as a drop point)."""
        dests = self.mapped('destination_id')
        if len(dests) <= 1:
            return dests or self[:1]._cp_destination()
        first = self[:1]
        city = ' '.join((first.city or '').split()).title()
        code = 'CITY-%s' % city.upper().replace(' ', '-')
        Loc = self.env['cp.location']
        loc = Loc.search([('code', '=', code), ('location_type', '=', 'customer'),
                          ('state_id', '=', first.state_id.id or False)], limit=1)
        if not loc:
            loc = Loc.create({'name': _('%s (multi-drop)', city), 'code': code, 'location_type': 'customer',
                              'city': city, 'state_id': first.state_id.id or False})
        return loc

    def _cp_line_vals(self):
        self.ensure_one()
        drop = ' '.join(p for p in (self.city, self.pincode) if p) or False
        return {'description': self.marketplace or _('Order'), 'order_ref': self.reference,
                'invoice_ref': self.invoice_ref, 'quantity': self.boxes or self.quantity or 1,
                'weight_kg': self.weight_kg, 'order_value': self.order_value, 'serial_numbers': self.serial_numbers,
                'eway_bill_no': self.eway_bill_no, 'drop_point': drop}

    def action_plan_transport(self, mode=None, vehicle_type=None):
        """Group the selected orders by ship-from and ship-to and open one load request per group."""
        todo = self.filtered(lambda o: o.state == 'new')
        if not todo:
            raise UserError(_('Select orders that still need transport.'))
        missing = todo.filtered(lambda o: not o.origin_id)
        if missing:
            raise UserError(_('Map the EasyEcom warehouse of %s to a Cargo Pilot location first '
                              '(Setup > EasyEcom > Connection > Warehouses).', ', '.join(missing.mapped('reference'))))
        groups = defaultdict(lambda: self.browse())
        for order in todo:
            order._cp_destination()
            groups[order._cp_plan_key()] |= order
        Indent = self.env['cp.indent']
        indents = Indent
        for key, orders in groups.items():
            origin_id = key[0]
            dest_id = orders._cp_group_destination().id
            channels = set(orders.mapped('channel'))
            channel = channels.pop() if len(channels) == 1 else False
            lane = self.env['cp.lane'].search([('origin_id', '=', origin_id), ('destination_id', '=', dest_id)], limit=1)
            if not lane:
                lane = self.env['cp.lane'].create({'origin_id': origin_id, 'destination_id': dest_id})
                if hasattr(lane, '_onchange_points'):
                    lane._onchange_points()
            use_mode = mode or orders[:1].account_id.carrier_mode or 'ltl'
            indent = Indent.create({
                'lane_id': lane.id, 'shipment_mode': use_mode, 'channel': channel or False,
                'vehicle_type_id': vehicle_type.id if vehicle_type else False,
                'origin': ', '.join(orders.mapped('reference'))[:200], 'company_id': orders[:1].company_id.id,
                'line_ids': [(0, 0, o._cp_line_vals()) for o in orders]})
            orders.write({'indent_id': indent.id, 'state': 'planned'})
            indents |= indent
        if len(indents) == 1:
            return {'type': 'ir.actions.act_window', 'res_model': 'cp.indent', 'res_id': indents.id,
                    'view_mode': 'form'}
        return {'type': 'ir.actions.act_window', 'res_model': 'cp.indent', 'name': _('Load Requests'),
                'domain': [('id', 'in', indents.ids)], 'view_mode': 'list,form'}

    def action_open_wizard(self):
        return {'type': 'ir.actions.act_window', 'res_model': 'cp.channel.plan.wizard', 'view_mode': 'form',
                'target': 'new', 'context': {'active_ids': self.ids}}

    # ------------------------------------------------------------------
    # Tracking
    # ------------------------------------------------------------------
    def cp_link_trip_orders(self, trip_orders):
        """Called when shipments with orders are created: link EasyEcom orders by order number."""
        for line in trip_orders:
            order = self.search([('reference', '=', line.order_ref), ('trip_id', '=', False),
                                 ('state', 'in', ('new', 'planned'))], limit=1)
            if order:
                order.write({'trip_id': line.trip_id.id, 'trip_order_id': line.id})
        return True

    def cp_push_tracking(self):
        for order in self:
            account = order.account_id
            body = account._tracking_body(order) if order.trip_id else None
            if not body:
                order.tracking_dirty = False
                continue
            try:
                if account.provider != 'simulator':
                    account._call('tracking', 'POST', account.path_tracking, json_body=body, record=order)
                order.write({'tracking_dirty': False, 'last_push': fields.Datetime.now(),
                             'last_push_status': order.trip_id.state, 'push_error': False})
            except UserError as err:
                order.push_error = str(err.args[0] if err.args else err)[:250]
        return True

    def action_push_tracking(self):
        self.cp_push_tracking()
        return True

    def action_view_trip(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'cp.trip', 'res_id': self.trip_id.id, 'view_mode': 'form'}


class CpChannelOrderLine(models.Model):
    _name = 'cp.channel.order.line'
    _description = 'Sales Channel Order Item'

    order_id = fields.Many2one('cp.channel.order', required=True, ondelete='cascade')
    sku = fields.Char(string='SKU')
    name = fields.Char(string='Product Name')
    product_id = fields.Many2one('product.product', compute='_compute_product', store=True)
    quantity = fields.Float()
    price = fields.Float()

    @api.depends('sku')
    def _compute_product(self):
        Product = self.env['product.product']
        for line in self:
            line.product_id = Product.search([('default_code', '=', line.sku)], limit=1) if line.sku else Product


class CpTripOrder(models.Model):
    _inherit = 'cp.trip.order'

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self.env['cp.channel.order'].sudo().cp_link_trip_orders(records)
        return records


class CpTrip(models.Model):
    _inherit = 'cp.trip'

    channel_order_ids = fields.One2many('cp.channel.order', 'trip_id', string='EasyEcom Orders')

    def write(self, vals):
        before = {rec.id: (rec.state, rec.docket_number) for rec in self} if (
            'state' in vals or 'docket_number' in vals) else {}
        res = super().write(vals)
        if before:
            changed = self.filtered(lambda t: before.get(t.id) != (t.state, t.docket_number) and t.state in PUSH_STATES)
            for order in changed.sudo().channel_order_ids:
                state = order.trip_id.state
                new = {'tracking_dirty': True}
                if state in ('delivered', 'closed'):
                    new['state'] = 'delivered'
                elif state in ('assigned', 'in_transit', 'arrived'):
                    new['state'] = 'shipped'
                elif state == 'cancel':
                    # push the cancellation, then free the order for a new shipment
                    if order.account_id.push_tracking:
                        order.cp_push_tracking()
                    new = {'state': 'new', 'indent_id': False, 'trip_id': False, 'trip_order_id': False,
                           'tracking_dirty': False}
                order.write(new)
        return res


class CpChannelPlanWizard(models.TransientModel):
    _name = 'cp.channel.plan.wizard'
    _description = 'Plan Transport for Orders'

    order_ids = fields.Many2many('cp.channel.order', string='Orders',
                                 default=lambda self: self.env.context.get('active_ids'))
    shipment_mode = fields.Selection([('ltl', 'Part load (LTL)'), ('ftl', 'Full truck (FTL)')], default='ltl',
                                     required=True)
    vehicle_type_id = fields.Many2one('cp.vehicle.type')
    order_count = fields.Integer(compute='_compute_totals')
    total_weight_kg = fields.Float(compute='_compute_totals', string='Total Weight (kg)')
    total_value = fields.Float(compute='_compute_totals', string='Total Value')

    @api.depends('order_ids')
    def _compute_totals(self):
        for wiz in self:
            orders = wiz.order_ids.filtered(lambda o: o.state == 'new')
            wiz.order_count = len(orders)
            wiz.total_weight_kg = sum(orders.mapped('weight_kg'))
            wiz.total_value = sum(orders.mapped('order_value'))

    def action_plan(self):
        self.ensure_one()
        if self.shipment_mode == 'ftl' and not self.vehicle_type_id:
            raise UserError(_('Choose the truck type for a full truck load.'))
        return self.order_ids.action_plan_transport(mode=self.shipment_mode, vehicle_type=self.vehicle_type_id)
