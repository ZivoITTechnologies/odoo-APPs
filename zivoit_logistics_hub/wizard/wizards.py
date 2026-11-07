# -*- coding: utf-8 -*-
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..providers.base import NotSupported, ProviderError


class RateShop(models.TransientModel):
    _name = 'zivo.logistics.rate.shop'
    _description = 'Compare Courier Rates (AI)'

    picking_id = fields.Many2one('stock.picking', required=True)
    carrier_id = fields.Many2one('delivery.carrier', string='Delivery Method',
                                 domain=[('delivery_type', '=', 'zivo_hub')])
    strategy = fields.Selection([('ai_best', 'AI Best'), ('cheapest', 'Cheapest'), ('fastest', 'Fastest'),
                                 ('reliable', 'Most reliable')], default='ai_best', required=True)
    line_ids = fields.One2many('zivo.logistics.rate.shop.line', 'wizard_id')
    errors = fields.Text(readonly=True)
    summary = fields.Html(readonly=True)
    rto_risk = fields.Integer(readonly=True)
    rto_risk_level = fields.Selection([('low', 'Low'), ('medium', 'Medium'), ('high', 'High')], readonly=True)
    rto_risk_reasons = fields.Text(readonly=True)
    address_score = fields.Integer(readonly=True)
    address_issues = fields.Text(readonly=True)
    weight_text = fields.Char(readonly=True)
    # AI address repair
    suggested_street = fields.Char()
    suggested_street2 = fields.Char()
    suggested_city = fields.Char()
    suggested_zip = fields.Char()
    suggestion_notes = fields.Text(readonly=True)

    def _carrier(self):
        carrier = self.carrier_id or (self.picking_id.carrier_id if self.picking_id.carrier_id.delivery_type == 'zivo_hub' else False)
        if not carrier:
            carrier = self.env['delivery.carrier'].search([('delivery_type', '=', 'zivo_hub'),
                                                            ('company_id', 'in', (False, self.picking_id.company_id.id))], limit=1)
        if not carrier:
            raise UserError(_('Create a delivery method of type "ZivoIT Logistics Hub" first.'))
        return carrier

    def action_compute(self):
        self.ensure_one()
        picking = self.picking_id
        carrier = self._carrier()
        self.carrier_id = carrier
        request = carrier._zivo_request_from_picking(picking)
        ctx = carrier._zivo_ctx(picking.partner_id, picking.company_id, 'stock.picking', picking.id, request.declared_value)
        ctx['strategy'] = self.strategy
        request.service_code = ''  # show every service of every courier
        ranked, errors = carrier.with_context(zivo_all_services=True)._zivo_ranked_options(request, ctx)
        risk = self.env['zivo.logistics.ai']._risk_values(picking.partner_id, request.cod_amount or request.declared_value,
                                                         request.is_cod, picking.sale_id.date_order if picking.sale_id else None,
                                                         request.delivery.zip)
        self.line_ids.unlink()
        self.write({
            'errors': '\n'.join(errors) or False,
            'rto_risk': risk.get('rto_risk', 0), 'rto_risk_level': risk.get('rto_risk_level'),
            'rto_risk_reasons': risk.get('rto_risk_reasons'), 'address_score': risk.get('address_score', 0),
            'address_issues': risk.get('address_issues'),
            'weight_text': _('%(actual).3f kg actual · %(vol).3f kg volumetric · %(pcs)s package(s) · %(mode)s%(cod)s',
                             actual=request.total_weight, vol=request.volumetric_weight, pcs=len(request.parcels),
                             mode='COD' if request.is_cod else 'Prepaid',
                             cod=(' %.2f' % request.cod_amount) if request.is_cod else ''),
            'line_ids': [(0, 0, {
                'account_id': o['account'].id, 'service_code': o['quote'].service_code,
                'service_name': o['quote'].service_name or o['quote'].service_code,
                'courier_name': o['quote'].courier_name, 'price': o['price'], 'tat_days': o['quote'].tat_days,
                'ai_score': o['ai_score'], 'reliability': o.get('reliability', 0.0), 'rank': o['rank'],
                'recommended': o['rank'] == 1, 'reasons': ', '.join(o.get('reasons') or []),
                'rate_source': o['quote'].source,
            }) for o in ranked],
        })
        return True

    def action_ai_fix_address(self):
        self.ensure_one()
        data = self.env['zivo.logistics.ai']._ai_fix_address(self.picking_id.partner_id)
        if not data:
            raise UserError(_('Configure an AI provider in Settings > Logistics Hub to use AI address repair. '
                              'The built-in engine already lists the issues above.'))
        self.write({'suggested_street': data.get('street'), 'suggested_street2': data.get('street2'),
                    'suggested_city': data.get('city'), 'suggested_zip': data.get('zip'),
                    'suggestion_notes': '\n'.join(data.get('issues') or [])})
        return self._reopen()

    def action_apply_address(self):
        self.ensure_one()
        vals = {k: v for k, v in {'street': self.suggested_street, 'street2': self.suggested_street2,
                                  'city': self.suggested_city, 'zip': self.suggested_zip}.items() if v}
        if vals:
            self.picking_id.partner_id.write(vals)
            self.picking_id.message_post(body=_('Delivery address updated from AI suggestion.'))
        self.action_compute()
        return self._reopen()

    def action_recompute(self):
        self.action_compute()
        return self._reopen()

    def _reopen(self):
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id, 'view_mode': 'form',
                'target': 'new', 'name': _('Compare Courier Rates (AI)')}


class RateShopLine(models.TransientModel):
    _name = 'zivo.logistics.rate.shop.line'
    _description = 'Courier Rate Option'
    _order = 'rank'

    wizard_id = fields.Many2one('zivo.logistics.rate.shop', required=True, ondelete='cascade')
    account_id = fields.Many2one('zivo.logistics.account', string='Carrier Account')
    service_code = fields.Char()
    service_name = fields.Char(string='Service')
    courier_name = fields.Char(string='Courier')
    price = fields.Float(digits='Product Price')
    tat_days = fields.Float(string='Transit (days)')
    ai_score = fields.Float(string='AI Score')
    reliability = fields.Float(string='Reliability %')
    rank = fields.Integer()
    recommended = fields.Boolean()
    reasons = fields.Char(string='Why')
    rate_source = fields.Char(string='Source')

    def _select(self, ship_now, force=False):
        self.ensure_one()
        picking = self.wizard_id.picking_id
        picking.write({'zivo_selected_account_id': self.account_id.id, 'zivo_selected_service_code': self.service_code,
                       'zivo_selected_price': self.price})
        if picking.carrier_id != self.wizard_id.carrier_id:
            picking.carrier_id = self.wizard_id.carrier_id
        picking.message_post(body=_('Courier selected: %(acc)s / %(svc)s (%(price).2f, AI score %(score)s).',
                                    acc=self.account_id.name, svc=self.service_name, price=self.price, score=self.ai_score))
        if ship_now:
            if picking.state != 'done':
                raise UserError(_('Validate the transfer first; the courier will be booked automatically.'))
            if picking.carrier_tracking_ref:
                raise UserError(_('This transfer already has a tracking number.'))
            picking.with_context(zivo_force_ship=force).send_to_shipper()
        return {'type': 'ir.actions.act_window_close'}

    def action_select(self):
        return self._select(ship_now=False)

    def action_ship(self):
        return self._select(ship_now=True)

    def action_ship_force(self):
        return self._select(ship_now=True, force=True)


class NdrAction(models.TransientModel):
    _name = 'zivo.logistics.ndr.action'
    _description = 'Resolve Failed Delivery (NDR)'

    shipment_id = fields.Many2one('zivo.logistics.shipment', required=True)
    action = fields.Selection([('reattempt', 'Re-attempt delivery'), ('change_address', 'Update address & re-attempt'),
                               ('contact_customer', 'Contact customer first'), ('rto', 'Return to origin (RTO)')],
                              required=True, default='reattempt')
    reattempt_date = fields.Date(default=lambda self: fields.Date.add(fields.Date.today(), days=1))
    new_address = fields.Char()
    new_phone = fields.Char()
    comments = fields.Char()
    message = fields.Text(string='Message to customer')
    send_message = fields.Boolean(string='Email the message to the customer', default=True)
    ndr_reason = fields.Char(related='shipment_id.ndr_reason')
    ndr_advice = fields.Text(related='shipment_id.ndr_advice')
    rto_risk = fields.Integer(related='shipment_id.rto_risk')

    def action_confirm(self):
        self.ensure_one()
        shp = self.shipment_id
        notes = []
        if self.action in ('reattempt', 'change_address', 'rto'):
            data = {'comments': self.comments or '', 'phone': self.new_phone or '', 'address': self.new_address or '',
                    'reattempt_date': str(self.reattempt_date or '')}
            try:
                shp.account_id._get_provider(shp._name, shp.id).ndr_action(shp.tracking_number, self.action, data)
                notes.append(_('NDR action "%s" sent to the carrier.', dict(self._fields['action'].selection)[self.action]))
            except NotSupported:
                notes.append(_('%s has no NDR API: action recorded, please inform the carrier panel.', shp.account_id.name))
            except ProviderError as exc:
                raise UserError(_('Carrier rejected the NDR action: %s', exc.message))
        if self.action == 'change_address' and shp.partner_id and (self.new_address or self.new_phone):
            vals = {}
            if self.new_address:
                vals['street2'] = self.new_address
            if self.new_phone:
                vals['phone'] = self.new_phone
            shp.partner_id.write(vals)
        if self.message and self.send_message and shp.partner_id.email:
            shp.message_post(body=self.message, partner_ids=shp.partner_id.ids, subtype_xmlid='mail.mt_comment',
                             message_type='comment')
            notes.append(_('Message emailed to %s.', shp.partner_id.email))
        shp.write({'ndr_state': 'actioned'})
        shp.activity_ids.filtered(lambda a: (a.summary or '').startswith('NDR')).action_done()
        shp.message_post(body=Markup('<br/>').join(notes) if notes else _('NDR actioned.'))
        return {'type': 'ir.actions.act_window_close'}


class AiQuery(models.TransientModel):
    _name = 'zivo.logistics.ai.query'
    _description = 'Ask Logistics AI'

    question = fields.Char(required=True, default='Delayed COD shipments this week')
    interpretation = fields.Text(readonly=True)

    def action_search(self):
        self.ensure_one()
        domain, filters = self.env['zivo.logistics.ai']._nl_domain(self.question)
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_logistics_hub.action_zivo_shipment')
        action.update({'domain': domain, 'name': _('AI: %s', self.question), 'context': {'search_default_filter_open': 0}})
        self.interpretation = str({k: v for k, v in filters.items() if v not in (None, [], False)})
        return action


class BulkShip(models.TransientModel):
    _name = 'zivo.logistics.bulk.ship'
    _description = 'Bulk Ship & Print Labels'

    picking_ids = fields.Many2many('stock.picking', string='Transfers')
    result = fields.Text(readonly=True)
    state = fields.Selection([('draft', 'Draft'), ('done', 'Done')], default='draft')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if self.env.context.get('active_model') == 'stock.picking' and 'picking_ids' in fields_list:
            res['picking_ids'] = [(6, 0, self.env.context.get('active_ids', []))]
        return res

    def action_ship(self):
        self.ensure_one()
        lines, shipped = [], self.env['stock.picking']
        for picking in self.picking_ids:
            if picking.carrier_id.delivery_type != 'zivo_hub':
                lines.append('%s: %s' % (picking.name, _('delivery method is not ZivoIT Logistics Hub')))
                continue
            if picking.carrier_tracking_ref:
                lines.append('%s: %s %s' % (picking.name, _('already shipped'), picking.carrier_tracking_ref))
                shipped |= picking
                continue
            if picking.state != 'done':
                lines.append('%s: %s' % (picking.name, _('validate the transfer first')))
                continue
            try:
                with self.env.cr.savepoint():
                    picking.send_to_shipper()
                lines.append('%s: ✔ %s' % (picking.name, picking.carrier_tracking_ref))
                shipped |= picking
            except UserError as exc:
                lines.append('%s: ✘ %s' % (picking.name, str(exc).splitlines()[0]))
        self.write({'result': '\n'.join(lines), 'state': 'done', 'picking_ids': [(6, 0, shipped.ids)]})
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id, 'view_mode': 'form', 'target': 'new'}

    def action_print_labels(self):
        return self.picking_ids.action_zivo_print_labels()
