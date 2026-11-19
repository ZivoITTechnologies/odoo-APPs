# -*- coding: utf-8 -*-
"""Warehouse operations: end-of-day manifests and customer returns (RMA)."""
import logging
import uuid
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..providers.base import NotSupported, ProviderError

_logger = logging.getLogger(__name__)

RETURN_REASONS = [
    ('damaged', 'Arrived damaged'), ('wrong_item', 'Wrong item received'), ('size_fit', 'Size or fit issue'),
    ('not_as_described', 'Not as described'), ('quality', 'Quality not as expected'), ('late', 'Arrived too late'),
    ('changed_mind', 'Changed my mind'), ('other', 'Other'),
]
RESOLUTIONS = [('refund', 'Refund'), ('exchange', 'Exchange'), ('store_credit', 'Store credit')]


class LogisticsManifest(models.Model):
    _name = 'zivo.logistics.manifest'
    _description = 'End-of-Day Manifest'
    _inherit = ['mail.thread']
    _order = 'date desc, id desc'
    _check_company_auto = True

    name = fields.Char(default='New', readonly=True, copy=False)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    account_id = fields.Many2one('zivo.logistics.account', string='Carrier Account', required=True, check_company=True)
    date = fields.Date(default=fields.Date.context_today, required=True)
    state = fields.Selection([('draft', 'Open'), ('closed', 'Handed Over')], default='draft', tracking=True)
    shipment_ids = fields.One2many('zivo.logistics.shipment', 'manifest_id', string='Shipments')
    shipment_count = fields.Integer(compute='_compute_totals', store=True)
    package_count = fields.Integer(compute='_compute_totals', store=True)
    total_weight = fields.Float(compute='_compute_totals', store=True, digits=(10, 3), string='Total Weight (kg)')
    cod_total = fields.Monetary(compute='_compute_totals', store=True, string='COD to Collect')
    currency_id = fields.Many2one(related='company_id.currency_id')
    pickup_reference = fields.Char(readonly=True)
    closed_at = fields.Datetime(readonly=True)
    note = fields.Text()

    @api.depends('shipment_ids', 'shipment_ids.package_count', 'shipment_ids.chargeable_weight', 'shipment_ids.cod_amount')
    def _compute_totals(self):
        for man in self:
            shp = man.shipment_ids
            man.shipment_count = len(shp)
            man.package_count = sum(shp.mapped('package_count'))
            man.total_weight = sum(shp.mapped('chargeable_weight'))
            man.cod_total = sum(shp.filtered(lambda s: s.payment_mode == 'cod').mapped('cod_amount'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('zivo.logistics.manifest') or 'New'
        return super().create(vals_list)

    @api.model
    def _create_end_of_day(self, shipments=None):
        """Group every booked, not yet manifested outbound shipment per carrier account. Returns the manifests."""
        if shipments is None:
            shipments = self.env['zivo.logistics.shipment'].search([
                ('company_id', '=', self.env.company.id), ('state', 'in', ('booked', 'pickup_scheduled')),
                ('manifest_id', '=', False), ('is_return', '=', False)])
        shipments = shipments.filtered(lambda s: not s.manifest_id and s.state in ('booked', 'pickup_scheduled'))
        manifests = self.browse()
        for account, recs in shipments.grouped('account_id').items():
            manifest = self.search([('account_id', '=', account.id), ('state', '=', 'draft'),
                                    ('date', '=', fields.Date.context_today(self))], limit=1)
            if not manifest:
                manifest = self.create({'account_id': account.id, 'company_id': account.company_id.id})
            recs.write({'manifest_id': manifest.id})
            manifests |= manifest
        return manifests

    def action_close(self):
        for man in self.filtered(lambda m: m.state == 'draft'):
            to_pickup = man.shipment_ids.filtered(lambda s: s.state == 'booked')
            if to_pickup:
                try:
                    with self.env.cr.savepoint():
                        to_pickup.action_schedule_pickup()
                    refs = {r for r in to_pickup.mapped('pickup_reference') if r}
                    man.pickup_reference = ', '.join(sorted(refs)) or False
                except (UserError, NotSupported, ProviderError) as exc:
                    man.message_post(body=_('Pickup was not requested automatically: %s', getattr(exc, 'message', str(exc))))
            man.write({'state': 'closed', 'closed_at': fields.Datetime.now()})
            man.message_post(body=_('%(count)s shipment(s) / %(pkgs)s package(s) handed over to %(carrier)s.',
                                    count=man.shipment_count, pkgs=man.package_count, carrier=man.account_id.name))
        return True

    def action_reopen(self):
        self.write({'state': 'draft', 'closed_at': False})
        return True

    def action_print(self):
        return self.env.ref('zivoit_logistics_hub.action_report_manifest').report_action(self)

    def action_view_shipments(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_logistics_hub.action_zivo_shipment')
        action['domain'] = [('manifest_id', '=', self.id)]
        return action


class ReturnRequest(models.Model):
    _name = 'zivo.logistics.return'
    _description = 'Customer Return Request (RMA)'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'
    _check_company_auto = True

    name = fields.Char(string='RMA', default='New', readonly=True, copy=False, index=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    sale_id = fields.Many2one('sale.order', string='Sales Order', required=True, check_company=True, index=True)
    partner_id = fields.Many2one('res.partner', string='Customer', required=True)
    picking_id = fields.Many2one('stock.picking', string='Delivery', check_company=True,
                                 domain="[('sale_id', '=', sale_id), ('picking_type_code', '=', 'outgoing'), ('state', '=', 'done')]")
    return_picking_id = fields.Many2one('stock.picking', string='Return Transfer', readonly=True, copy=False)
    exchange_picking_id = fields.Many2one('stock.picking', string='Exchange Delivery', readonly=True, copy=False)
    shipment_id = fields.Many2one('zivo.logistics.shipment', string='Reverse Pickup', readonly=True, copy=False)
    tracking_number = fields.Char(related='shipment_id.tracking_number', string='Return AWB')
    shipment_state = fields.Selection(related='shipment_id.state', string='Pickup Status')
    state = fields.Selection([('requested', 'Requested'), ('approved', 'Approved'), ('pickup', 'Pickup Booked'),
                              ('received', 'Received'), ('rejected', 'Rejected'), ('cancelled', 'Cancelled')],
                             default='requested', required=True, tracking=True, index=True)
    reason = fields.Selection(RETURN_REASONS, required=True, default='other', tracking=True)
    reason_detail = fields.Text(string='Customer Comment')
    resolution = fields.Selection(RESOLUTIONS, default='refund', required=True, tracking=True)
    source = fields.Selection([('portal', 'Customer portal'), ('backend', 'Back office')], default='backend', readonly=True)
    decision_note = fields.Text(string='Internal Note')
    line_ids = fields.One2many('zivo.logistics.return.line', 'return_id', string='Products', copy=True)
    amount = fields.Monetary(compute='_compute_amount', store=True, string='Return Value')
    currency_id = fields.Many2one(related='sale_id.currency_id')
    access_token = fields.Char(copy=False, default=lambda self: uuid.uuid4().hex, readonly=True)
    portal_url = fields.Char(compute='_compute_portal_url', string='Customer Status Page')

    @api.depends('line_ids.quantity', 'line_ids.price_unit')
    def _compute_amount(self):
        for ret in self:
            ret.amount = sum(line.quantity * line.price_unit for line in ret.line_ids)

    def _compute_portal_url(self):
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        for ret in self:
            ret.portal_url = '%s/logistics/returns/status/%s' % (base, ret.access_token) if ret.access_token else ''

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('zivo.logistics.return') or 'New'
        records = super().create(vals_list)
        for ret in records:
            if not ret.picking_id:
                ret.picking_id = ret.sale_id.picking_ids.filtered(
                    lambda p: p.picking_type_code == 'outgoing' and p.state == 'done').sorted('date_done', reverse=True)[:1]
        return records

    # ------------------------------------------------------------------ policy
    @api.model
    def _policy(self):
        get = self.env['ir.config_parameter'].sudo().get_param
        return {'window': int(get('zivoit_logistics_hub.return_window_days', 30) or 0),
                'auto_approve': get('zivoit_logistics_hub.return_auto_approve', 'False') not in ('False', '0', '', False),
                'auto_pickup': get('zivoit_logistics_hub.return_manual_pickup', 'False') in ('False', '0', '', False)}

    @api.model
    def _returnable_lines(self, order):
        """{sale.order.line: returnable qty} honouring the return window, non-returnable products and open RMAs."""
        policy = self._policy()
        done = order.picking_ids.filtered(lambda p: p.picking_type_code == 'outgoing' and p.state == 'done')
        if not done:
            return {}
        last = max(done.mapped('date_done'))
        if policy['window'] and fields.Datetime.now() > last + timedelta(days=policy['window']):
            return {}
        open_qty = {}
        for line in self.env['zivo.logistics.return.line'].sudo().search([
                ('return_id.sale_id', '=', order.id), ('return_id.state', 'not in', ('rejected', 'cancelled'))]):
            open_qty[line.sale_line_id.id] = open_qty.get(line.sale_line_id.id, 0.0) + line.quantity
        result = {}
        for line in order.order_line.filtered(lambda l: l.product_id.type == 'consu' and not l.is_delivery):
            if line.product_id.product_tmpl_id.zivo_non_returnable:
                continue
            qty = line.qty_delivered - open_qty.get(line.id, 0.0)
            if qty > 0:
                result[line] = qty
        return result

    # ------------------------------------------------------------------ workflow
    def action_approve(self):
        policy = self._policy()
        for ret in self:
            if ret.state != 'requested':
                raise UserError(_('Only requested returns can be approved (%s is %s).', ret.name, ret.state))
            if not ret.line_ids:
                raise UserError(_('Add at least one product to %s.', ret.name))
            ret._create_return_picking()
            ret.state = 'approved'
            ret._close_review()
            ret.message_post(body=_('Return approved.'))
            if policy['auto_pickup']:
                try:
                    with self.env.cr.savepoint():
                        ret.action_book_pickup()
                except UserError as exc:
                    ret.message_post(body=_('Reverse pickup could not be booked automatically: %s', exc))
        return True

    def action_reject(self):
        for ret in self:
            if ret.state not in ('requested', 'approved'):
                raise UserError(_('%s cannot be rejected any more.', ret.name))
            if ret.return_picking_id and ret.return_picking_id.state not in ('done', 'cancel'):
                ret.return_picking_id.action_cancel()
            ret.state = 'rejected'
            ret._close_review()
        return True

    def action_cancel(self):
        for ret in self:
            if ret.shipment_id and ret.shipment_id.state not in ('cancelled',) and not ret.shipment_id.is_final:
                ret.shipment_id.action_cancel_shipment()
            if ret.return_picking_id and ret.return_picking_id.state not in ('done', 'cancel'):
                ret.return_picking_id.action_cancel()
            ret.state = 'cancelled'
            ret._close_review()
        return True

    def _close_review(self):
        summary = self.env._('Review return request')
        self.activity_ids.filtered(lambda a: a.summary == summary).action_done()

    def _create_return_picking(self):
        self.ensure_one()
        if self.return_picking_id:
            return self.return_picking_id
        if not self.picking_id:
            raise UserError(_('%s: the order has no completed delivery to return.', self.name))
        wizard = self.env['stock.return.picking'].with_context(
            active_id=self.picking_id.id, active_ids=self.picking_id.ids, active_model='stock.picking').create(
            {'picking_id': self.picking_id.id})
        wanted = {}
        for line in self.line_ids:
            wanted[line.product_id.id] = wanted.get(line.product_id.id, 0.0) + line.quantity
        for wline in wizard.product_return_moves:
            qty = min(wanted.get(wline.product_id.id, 0.0), wline.move_id.quantity)
            wline.quantity = qty
            wanted[wline.product_id.id] = wanted.get(wline.product_id.id, 0.0) - qty
        picking = wizard._create_return()
        carrier = self._hub_carrier()
        if carrier:
            picking.carrier_id = carrier
        vals = {'return_picking_id': picking.id}
        if self.resolution == 'exchange':
            vals['exchange_picking_id'] = wizard._create_exchange(picking).id
        self.write(vals)
        picking.message_post(body=_('Created from return request %s (%s).', self.name, dict(RETURN_REASONS)[self.reason]))
        return picking

    def _hub_carrier(self):
        self.ensure_one()
        if self.sale_id.carrier_id.delivery_type == 'zivo_hub':
            return self.sale_id.carrier_id
        return self.env['delivery.carrier'].search([('delivery_type', '=', 'zivo_hub'),
                                                    ('company_id', 'in', (False, self.company_id.id))], limit=1)

    def action_book_pickup(self):
        for ret in self:
            if ret.state not in ('approved',):
                raise UserError(_('Approve %s before booking the reverse pickup.', ret.name))
            picking = ret._create_return_picking()
            carrier = picking.carrier_id if picking.carrier_id.delivery_type == 'zivo_hub' else ret._hub_carrier()
            if not carrier:
                raise UserError(_('Create a delivery method of type "ZivoIT Logistics Hub" to book reverse pickups.'))
            shipment = carrier._zivo_ship_picking(picking)
            shipment.return_request_id = ret
            picking.carrier_tracking_ref = shipment.tracking_number
            ret.write({'shipment_id': shipment.id, 'state': 'pickup'})
            ret.message_post(body=_('Reverse pickup booked with %(carrier)s, AWB %(awb)s.',
                                    carrier=shipment.account_id.name, awb=shipment.tracking_number))
        return True

    def action_mark_received(self):
        for ret in self:
            picking = ret.return_picking_id
            if picking and picking.state not in ('done', 'cancel'):
                for move in picking.move_ids:
                    move.quantity = move.product_uom_qty
                picking.move_ids.picked = True
                picking.with_context(skip_backorder=True, skip_sms=True).button_validate()
            ret.state = 'received'
            ret.message_post(body=_('Returned goods received in the warehouse.'))
        return True

    def action_open_portal(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_url', 'url': self.portal_url, 'target': 'new'}

    @api.model
    def _portal_submit(self, order, quantities, reason, comment='', resolution='refund'):
        """Create a request from the customer portal. ``quantities``: {sale.order.line id: qty}."""
        allowed = self._returnable_lines(order)
        lines = []
        for line, max_qty in allowed.items():
            qty = min(float(quantities.get(line.id) or 0.0), max_qty)
            if qty > 0:
                lines.append((0, 0, {'sale_line_id': line.id, 'product_id': line.product_id.id, 'quantity': qty,
                                     'price_unit': line.price_reduce_taxinc}))
        if not lines:
            raise UserError(_('Select at least one product to return.'))
        if reason not in dict(RETURN_REASONS):
            reason = 'other'
        if resolution not in dict(RESOLUTIONS):
            resolution = 'refund'
        ret = self.sudo().create({'sale_id': order.id, 'partner_id': order.partner_shipping_id.id,
                                  'company_id': order.company_id.id, 'reason': reason, 'reason_detail': comment or False,
                                  'resolution': resolution, 'source': 'portal', 'line_ids': lines})
        policy = self._policy()
        if policy['auto_approve'] and reason not in ('other',):
            try:
                with self.env.cr.savepoint():
                    ret.action_approve()
            except UserError as exc:
                ret.message_post(body=_('Automatic approval failed: %s', exc))
        else:
            activity = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
            if activity:
                ret.activity_schedule(activity_type_id=activity.id, summary=_('Review return request'),
                                      user_id=(order.user_id or self.env.ref('base.user_admin')).id)
        return ret


class ReturnLine(models.Model):
    _name = 'zivo.logistics.return.line'
    _description = 'Return Request Line'

    return_id = fields.Many2one('zivo.logistics.return', required=True, ondelete='cascade', index=True)
    sale_line_id = fields.Many2one('sale.order.line', string='Order Line')
    product_id = fields.Many2one('product.product', required=True)
    quantity = fields.Float(default=1.0, digits='Product Unit')
    price_unit = fields.Float(digits='Product Price')
    currency_id = fields.Many2one(related='return_id.currency_id')

    @api.constrains('quantity')
    def _check_quantity(self):
        if any(line.quantity <= 0 for line in self):
            raise ValidationError(_('Return quantities must be positive.'))


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    zivo_non_returnable = fields.Boolean(string='Not Returnable',
                                         help='Hide this product from the customer returns portal.')
