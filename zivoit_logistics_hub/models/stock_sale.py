# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.pdf import merge_pdf

from ..providers.base import NORMALIZED_STATUSES

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    zivo_shipment_ids = fields.One2many('zivo.logistics.shipment', 'picking_id', string='Shipments')
    zivo_shipment_id = fields.Many2one('zivo.logistics.shipment', compute='_compute_zivo_shipment', string='Shipment')
    zivo_shipment_count = fields.Integer(compute='_compute_zivo_shipment')
    zivo_state = fields.Selection(NORMALIZED_STATUSES, compute='_compute_zivo_shipment', string='Shipment Status')
    zivo_rto_risk = fields.Integer(compute='_compute_zivo_shipment', string='RTO Risk %')
    zivo_selected_account_id = fields.Many2one('zivo.logistics.account', string='Chosen Carrier Account', copy=False,
                                               help='Set from "Compare Rates". Empty = automatic AI selection.')
    zivo_selected_service_code = fields.Char(copy=False)
    zivo_selected_price = fields.Float(copy=False)
    zivo_ewaybill = fields.Char(string='E-Way Bill No.', copy=False)
    zivo_is_hub = fields.Boolean(compute='_compute_zivo_is_hub')

    @api.depends('zivo_shipment_ids', 'zivo_shipment_ids.state', 'zivo_shipment_ids.rto_risk')
    def _compute_zivo_shipment(self):
        for picking in self:
            active = picking.zivo_shipment_ids.filtered(lambda s: s.state != 'cancelled').sorted('id', reverse=True)
            shipment = active[:1] or picking.zivo_shipment_ids.sorted('id', reverse=True)[:1]
            picking.zivo_shipment_id = shipment
            picking.zivo_shipment_count = len(picking.zivo_shipment_ids)
            picking.zivo_state = shipment.state if shipment else False
            picking.zivo_rto_risk = shipment.rto_risk if shipment else 0

    @api.depends('carrier_id.delivery_type')
    def _compute_zivo_is_hub(self):
        for picking in self:
            picking.zivo_is_hub = picking.carrier_id.delivery_type == 'zivo_hub'

    def _zivo_is_return(self):
        self.ensure_one()
        return bool(self.return_id) or self.picking_type_code == 'incoming' or any(
            m.origin_returned_move_id for m in self.move_ids)

    def _zivo_picking_value(self):
        """Value of goods in this transfer (tax included), used for COD & declared value."""
        self.ensure_one()
        value = 0.0
        for move in self.move_ids.filtered(lambda m: m.state != 'cancel'):
            qty = move.quantity or move.product_uom_qty
            if move.sale_line_id:
                line = move.sale_line_id
                qty_line = move.product_uom._compute_quantity(qty, line.product_uom_id, round=False) \
                    if move.product_uom and line.product_uom_id and move.product_uom != line.product_uom_id else qty
                value += line.price_reduce_taxinc * qty_line
            else:
                value += move.product_id.lst_price * qty
        if self.sale_id and self.sale_id.zivo_payment_mode == 'cod':
            # add delivery charge once (first outgoing picking of the order)
            first = self.sale_id.picking_ids.filtered(lambda p: p.picking_type_code == 'outgoing').sorted('id')[:1]
            if first == self:
                value += sum(self.sale_id.order_line.filtered('is_delivery').mapped('price_total'))
        return round(value, 2)

    def action_zivo_rate_shop(self):
        self.ensure_one()
        if not self.partner_id:
            raise UserError(_('Set a delivery address first.'))
        wizard = self.env['zivo.logistics.rate.shop'].create({'picking_id': self.id})
        wizard.action_compute()
        return {'type': 'ir.actions.act_window', 'res_model': 'zivo.logistics.rate.shop', 'res_id': wizard.id,
                'view_mode': 'form', 'target': 'new', 'name': _('Compare Courier Rates (AI)'),
                'context': {'dialog_size': 'extra-large'}}

    def action_zivo_track(self):
        self.zivo_shipment_ids.filtered(lambda s: s.state != 'cancelled').action_track()
        return True

    def action_zivo_view_shipments(self):
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_logistics_hub.action_zivo_shipment')
        action['domain'] = [('picking_id', 'in', self.ids)]
        if len(self.zivo_shipment_ids) == 1:
            action.update(view_mode='form', res_id=self.zivo_shipment_ids.id, views=[(False, 'form')])
        return action

    def action_zivo_print_labels(self):
        shipments = self.mapped('zivo_shipment_id').filtered(lambda s: s.state not in ('cancelled', 'draft'))
        for shp in shipments.filtered(lambda s: not s.label_attachment_id):
            try:
                shp._fetch_label()
            except Exception as exc:  # noqa: BLE001
                _logger.info('Label fetch failed for %s: %s', shp.name, exc)
        attachments = shipments.mapped('label_attachment_id')
        if not attachments:
            raise UserError(_('No labels available yet for the selected transfers.'))
        if len(attachments) == 1:
            att = attachments
        else:
            merged = merge_pdf([a.raw for a in attachments])
            att = self.env['ir.attachment'].create({'name': 'Labels-%s.pdf' % fields.Date.today(), 'raw': merged,
                                                    'mimetype': 'application/pdf'})
        return {'type': 'ir.actions.act_url', 'url': '/web/content/%s?download=true' % att.id, 'target': 'new'}


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    zivo_payment_mode = fields.Selection([('prepaid', 'Prepaid'), ('cod', 'Cash on Delivery')], string='Payment Mode',
                                         compute='_compute_zivo_payment_mode', store=True, readonly=False, precompute=True)
    zivo_rto_risk = fields.Integer(string='RTO Risk %', copy=False)
    zivo_rto_risk_level = fields.Selection([('low', 'Low'), ('medium', 'Medium'), ('high', 'High')], string='RTO Risk', copy=False)
    zivo_rto_risk_reasons = fields.Text(string='Risk Drivers', copy=False)
    zivo_address_score = fields.Integer(string='Address Score', copy=False)
    zivo_address_issues = fields.Text(string='Address Issues', copy=False)
    zivo_shipment_ids = fields.One2many('zivo.logistics.shipment', 'sale_id')
    zivo_shipment_count = fields.Integer(compute='_compute_zivo_shipment_count')

    @api.depends('transaction_ids.provider_id', 'transaction_ids.state')
    def _compute_zivo_payment_mode(self):
        for order in self:
            txs = order.transaction_ids.filtered(lambda t: t.state not in ('cancel', 'error'))
            if txs.filtered(lambda t: t.provider_id.code == 'custom' and t.provider_id.custom_mode == 'cash_on_delivery'):
                order.zivo_payment_mode = 'cod'
            elif not order.zivo_payment_mode:
                order.zivo_payment_mode = 'prepaid'

    def _compute_zivo_shipment_count(self):
        for order in self:
            order.zivo_shipment_count = len(order.zivo_shipment_ids)

    def action_zivo_check_risk(self):
        ai = self.env['zivo.logistics.ai']
        for order in self:
            vals = ai._risk_values(order.partner_shipping_id, order.amount_total, order.zivo_payment_mode == 'cod',
                                   order.date_order, order.partner_shipping_id.zip)
            order.write({'zivo_rto_risk': vals.get('rto_risk', 0), 'zivo_rto_risk_level': vals.get('rto_risk_level'),
                         'zivo_rto_risk_reasons': vals.get('rto_risk_reasons'),
                         'zivo_address_score': vals.get('address_score', 0),
                         'zivo_address_issues': vals.get('address_issues')})
        return True

    def action_confirm(self):
        res = super().action_confirm()
        for order in self.filtered(lambda o: o.carrier_id.delivery_type == 'zivo_hub'):
            try:
                with self.env.cr.savepoint():
                    order.action_zivo_check_risk()
            except Exception as exc:  # noqa: BLE001 - risk scoring must never block a confirmation
                _logger.warning('ZivoIT risk scoring failed for %s: %s', order.name, exc)
        return res

    def action_zivo_view_shipments(self):
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_logistics_hub.action_zivo_shipment')
        action['domain'] = [('sale_id', 'in', self.ids)]
        return action


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    zivo_ai_provider = fields.Selection([
        ('local', 'Built-in engine (no key)'), ('openai', 'OpenAI'), ('anthropic', 'Anthropic Claude'),
        ('gemini', 'Google Gemini'), ('openai_compatible', 'OpenAI-compatible (Azure, Ollama...)'),
    ], string='AI Provider', default='local', config_parameter='zivoit_logistics_hub.ai_provider')
    zivo_ai_api_key = fields.Char(string='AI API Key', config_parameter='zivoit_logistics_hub.ai_api_key')
    zivo_ai_model = fields.Char(string='AI Model', config_parameter='zivoit_logistics_hub.ai_model')
    zivo_ai_base_url = fields.Char(string='AI Base URL', config_parameter='zivoit_logistics_hub.ai_base_url')
    zivo_notify_customer = fields.Boolean(string='Email customers on status changes',
                                          config_parameter='zivoit_logistics_hub.notify_customer')
    zivo_track_interval_hours = fields.Float(string='Tracking refresh (hours)', default=2.0,
                                             config_parameter='zivoit_logistics_hub.track_interval_hours')
    zivo_log_retention_days = fields.Integer(string='Keep API logs (days)', default=30,
                                             config_parameter='zivoit_logistics_hub.log_retention_days')
