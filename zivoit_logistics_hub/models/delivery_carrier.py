# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..providers.base import Address, Item, Parcel, ProviderError, ShipmentRequest

_logger = logging.getLogger(__name__)

STRATEGIES = [
    ('fixed', 'Fixed carrier account'),
    ('cheapest', 'Cheapest'),
    ('fastest', 'Fastest'),
    ('reliable', 'Most reliable on the lane'),
    ('ai_best', 'AI Best (price + speed + reliability)'),
    ('rules', 'Allocation rules'),
]


class DeliveryCarrier(models.Model):
    _inherit = 'delivery.carrier'

    delivery_type = fields.Selection(selection_add=[('zivo_hub', 'ZivoIT Logistics Hub')],
                                     ondelete={'zivo_hub': lambda recs: recs.write({'delivery_type': 'fixed', 'fixed_price': 0})})
    zivo_strategy = fields.Selection(STRATEGIES, string='Carrier Selection', default='ai_best')
    zivo_account_id = fields.Many2one('zivo.logistics.account', string='Carrier Account')
    zivo_account_ids = fields.Many2many('zivo.logistics.account', string='Candidate Accounts',
                                        help='Accounts competing for the shipment. Empty = all active accounts of the company.')
    zivo_service_code = fields.Char(string='Service Code', help='Force a carrier service (e.g. Delhivery "E" for express).')
    zivo_default_package_type_id = fields.Many2one('stock.package.type', string='Default Package')
    zivo_failover = fields.Boolean(string='Smart Failover', default=True,
                                   help='If the chosen carrier rejects the booking, automatically try the next best one.')
    zivo_hold_high_risk = fields.Boolean(string='Hold High-Risk COD', default=False,
                                         help='Block automatic booking of COD shipments whose AI RTO risk is above the threshold.')
    zivo_risk_threshold = fields.Integer(string='RTO Risk Threshold %', default=70)
    zivo_min_address_score = fields.Integer(string='Minimum Address Score', default=0,
                                            help='Block booking when the AI address score is below this value (0 = never block).')

    # ------------------------------------------------------------------ helpers
    def _zivo_candidate_accounts(self, request, ctx):
        """Return (accounts, strategy) after applying allocation rules."""
        self.ensure_one()
        Account = self.env['zivo.logistics.account']
        company = ctx.get('company') or self.env.company
        if ctx.get('strategy'):  # explicit override (rate shopping screen)
            accounts = self.zivo_account_ids.filtered('active') or Account.search([('company_id', '=', company.id)])
            return accounts, ctx['strategy']
        if self.zivo_strategy == 'fixed':
            if not self.zivo_account_id:
                raise UserError(_('Select a carrier account on delivery method %s.', self.name))
            return self.zivo_account_id, 'fixed'
        if self.zivo_strategy == 'rules':
            rule = self.env['zivo.logistics.allocation.rule']._match(request, ctx, company)
            if rule:
                accounts = rule.account_ids.filtered('active') or self.zivo_account_ids or \
                    Account.search([('company_id', '=', company.id)])
                return accounts, rule.strategy
            accounts = self.zivo_account_ids or Account.search([('company_id', '=', company.id)])
            return accounts, 'ai_best'
        accounts = self.zivo_account_ids.filtered('active') or Account.search([('company_id', '=', company.id)])
        return accounts, self.zivo_strategy

    def _zivo_ranked_options(self, request, ctx):
        """Collect quotes from every candidate and rank them. Returns (ranked, errors)."""
        self.ensure_one()
        accounts, strategy = self._zivo_candidate_accounts(request, ctx)
        quotes, errors = [], []
        for account in accounts:
            try:
                if strategy != 'fixed':
                    cache = account._check_serviceability(request.pickup.zip, request.delivery.zip,
                                                          request.chargeable_weight, request.is_cod)
                    if cache and (not cache.serviceable or (request.is_cod and not cache.cod)):
                        errors.append('%s: %s' % (account.name, cache.message or _('not serviceable')))
                        continue
                options = account._get_rates(request, ctx.get('res_model'), ctx.get('res_id'))
            except ProviderError as exc:
                errors.append('%s: %s' % (account.name, exc.message))
                continue
            service = '' if self.env.context.get('zivo_all_services') else (
                self.zivo_service_code or (account.default_service_code if strategy == 'fixed' else ''))
            for option in options:
                if service and option.service_code != service:
                    continue
                if request.is_cod and not option.cod_available:
                    continue
                quotes.append((account, option))
            if not options and strategy == 'fixed':
                # carriers without rate API nor rate card can still ship; price is billed later
                from ..providers.base import RateOption
                quotes.append((account, RateOption(service_code=service or '', service_name=account.name,
                                                   courier_name=account.provider_label, price=0.0, source='none')))
        ranked = self.env['zivo.logistics.ai']._recommend(
            quotes, request, strategy=strategy if strategy != 'fixed' else 'ai_best', dest_state=ctx.get('dest_state'))
        return ranked, errors

    def _zivo_option_text(self, opt):
        _ = self.env._
        quote = opt['quote']
        txt = '%s - %s' % (opt['account'].name, quote.service_name or quote.service_code)
        if quote.courier_name and quote.courier_name not in txt:
            txt += ' (%s)' % quote.courier_name
        if quote.tat_days:
            txt += _(', %g day', quote.tat_days) if quote.tat_days == 1 else _(', %g days', quote.tat_days)
        return txt + _(', AI score %s', opt['ai_score'])

    # ------------------------------------------------------------------ request builders
    @api.model
    def _zivo_partner_address(self, partner):
        partner = partner or self.env.company.partner_id
        return Address(
            name=partner.name or partner.commercial_partner_id.name or '', company=partner.commercial_partner_id.name or '',
            street=partner.street or '', street2=partner.street2 or '', city=partner.city or '',
            state=partner.state_id.name or '', state_code=partner.state_id.code or '', zip=(partner.zip or '').strip(),
            country_code=(partner.country_id.code or 'IN').upper(), phone=partner.phone or getattr(partner, 'mobile', '') or '',
            email=partner.email or '')

    def _zivo_to_kg(self, weight):
        uom = self.env['product.template']._get_weight_uom_id_from_ir_config_parameter()
        kg = self.env.ref('uom.product_uom_kgm')
        return uom._compute_quantity(weight, kg, round=False) if uom != kg else weight

    def _zivo_to_cm(self, length):
        if not length:
            return 0.0
        uom = self.env['product.template']._get_length_uom_id_from_ir_config_parameter()
        cm = self.env.ref('uom.product_uom_cm')
        return round(uom._compute_quantity(length, cm, round=False), 2) if uom != cm else length

    def _zivo_parcels(self, packages):
        parcels = []
        default = self.zivo_default_package_type_id
        for pkg in packages:
            dims = pkg.dimension or {}
            length = self._zivo_to_cm(dims.get('length') or default.packaging_length) or 10.0
            width = self._zivo_to_cm(dims.get('width') or default.width) or 10.0
            height = self._zivo_to_cm(dims.get('height') or default.height) or 10.0
            parcels.append(Parcel(weight_kg=round(max(self._zivo_to_kg(pkg.weight or 0.0), 0.05), 3), length_cm=length,
                                  width_cm=width, height_cm=height, declared_value=pkg.total_cost or 0.0,
                                  description=pkg.name or '', reference=pkg.name or ''))
        return parcels or [Parcel()]

    def _zivo_pickup_partner(self, warehouse=None, account=None):
        if account and account.pickup_partner_id:
            return account.pickup_partner_id
        if warehouse and warehouse.partner_id:
            return warehouse.partner_id
        return self.env.company.partner_id

    def _zivo_request_from_order(self, order):
        self.ensure_one()
        packages = self._get_packages_from_order(order, self.zivo_default_package_type_id)
        is_cod = order.zivo_payment_mode == 'cod'
        amount = order._compute_amount_total_without_delivery()
        items = [Item(sku=l.product_id.default_code or '', name=l.product_id.name or l.name, qty=l.product_uom_qty,
                      unit_price=l.price_reduce_taxinc, hsn=getattr(l.product_id, 'l10n_in_hsn_code', '') or '',
                      weight_kg=self._zivo_to_kg(l.product_id.weight or 0.0))
                 for l in order.order_line.filtered(lambda l: not l.is_delivery and not l.display_type and l.product_id)]
        return ShipmentRequest(
            reference=order.name, pickup=self._zivo_partner_address(self._zivo_pickup_partner(order.warehouse_id)),
            delivery=self._zivo_partner_address(order.partner_shipping_id), parcels=self._zivo_parcels(packages),
            items=items, payment_mode='cod' if is_cod else 'prepaid', cod_amount=amount if is_cod else 0.0,
            declared_value=amount, currency=order.currency_id.name, service_code=self.zivo_service_code or '',
            order_date=order.date_order)

    def _zivo_request_from_picking(self, picking, account=None):
        self.ensure_one()
        packages = self._get_packages_from_picking(picking, self.zivo_default_package_type_id)
        sale = picking.sale_id
        is_return = picking._zivo_is_return()
        is_cod = bool(sale and sale.zivo_payment_mode == 'cod') and not is_return
        value = picking._zivo_picking_value()
        items = []
        for move in picking.move_ids.filtered(lambda m: m.product_id.type == 'consu' and m.state != 'cancel'):
            qty = move.quantity or move.product_uom_qty
            price = move.sale_line_id.price_reduce_taxinc if move.sale_line_id else move.product_id.lst_price
            items.append(Item(sku=move.product_id.default_code or '', name=move.product_id.name, qty=qty, unit_price=price,
                              hsn=getattr(move.product_id, 'l10n_in_hsn_code', '') or '',
                              weight_kg=self._zivo_to_kg(move.product_id.weight or 0.0)))
        warehouse = picking.picking_type_id.warehouse_id
        pickup = self._zivo_pickup_partner(warehouse, account)
        delivery_partner = picking.partner_id
        if is_return:  # reverse pickup: customer -> warehouse
            pickup, delivery_partner = picking.partner_id, self._zivo_pickup_partner(warehouse, account)
        return ShipmentRequest(
            reference=picking.name if not sale else '%s-%s' % (sale.name, picking.name.split('/')[-1]),
            pickup=self._zivo_partner_address(pickup), delivery=self._zivo_partner_address(delivery_partner),
            parcels=self._zivo_parcels(packages), items=items, payment_mode='cod' if is_cod else 'prepaid',
            cod_amount=value if is_cod else 0.0, declared_value=value,
            currency=(sale.currency_id if sale else picking.company_id.currency_id).name,
            service_code=picking.zivo_selected_service_code or self.zivo_service_code or '',
            pickup_location_code=account.pickup_location_code if account else '',
            invoice_number=(sale.invoice_ids.filtered(lambda m: m.state == 'posted')[:1].name if sale else '') or '',
            ewaybill=picking.zivo_ewaybill or '', is_return=is_return,
            order_date=sale.date_order if sale else picking.scheduled_date)

    def _zivo_ctx(self, partner, company, res_model=False, res_id=False, value=0.0):
        return {'company': company, 'partner': partner, 'dest_state': partner.state_id if partner else False,
                'res_model': res_model, 'res_id': res_id, 'value': value}

    # ------------------------------------------------------------------ Odoo carrier API
    def zivo_hub_rate_shipment(self, order):
        self.ensure_one()
        request = self._zivo_request_from_order(order)
        ctx = self._zivo_ctx(order.partner_shipping_id, order.company_id, 'sale.order', order.id, request.declared_value)
        try:
            ranked, errors = self._zivo_ranked_options(request, ctx)
        except UserError as exc:
            return {'success': False, 'price': 0.0, 'error_message': str(exc), 'warning_message': False}
        if not ranked:
            return {'success': False, 'price': 0.0,
                    'error_message': _('No courier can serve this shipment.\n%s', '\n'.join(errors) or ''),
                    'warning_message': False}
        best = ranked[0]
        return {'success': True, 'price': best['price'], 'error_message': False,
                'warning_message': _('Recommended: %s', self._zivo_option_text(best))}

    def zivo_hub_send_shipping(self, pickings):
        self.ensure_one()
        res = []
        for picking in pickings:
            shipment = self._zivo_ship_picking(picking)
            res.append({'exact_price': shipment.actual_price or shipment.quoted_price or 0.0,
                        'tracking_number': shipment.tracking_number})
        return res

    def _zivo_ship_picking(self, picking):
        self.ensure_one()
        ai = self.env['zivo.logistics.ai']
        partner = picking.partner_id
        account = picking.zivo_selected_account_id
        request = self._zivo_request_from_picking(picking, account)
        ai_values = ai._risk_values(partner, request.cod_amount or request.declared_value, request.is_cod,
                                    picking.sale_id.date_order if picking.sale_id else None, request.delivery.zip)
        force = self.env.context.get('zivo_force_ship')
        if not force and self.zivo_min_address_score and ai_values.get('address_score', 100) < self.zivo_min_address_score:
            raise UserError(_('Address quality is too low (%(score)s/100) to ship %(name)s:\n%(issues)s\n\n'
                              'Fix the address or use "Compare Rates" > Ship anyway.',
                              score=ai_values['address_score'], name=picking.name, issues=ai_values.get('address_issues')))
        if (not force and self.zivo_hold_high_risk and request.is_cod
                and ai_values.get('rto_risk', 0) >= self.zivo_risk_threshold):
            raise UserError(_('%(name)s is on hold: AI RTO risk %(risk)s%% is at or above %(thr)s%%.\n%(reasons)s\n\n'
                              'Confirm the order with the customer or convert it to prepaid, then use '
                              '"Compare Rates" > Ship anyway.', name=picking.name, risk=ai_values['rto_risk'],
                              thr=self.zivo_risk_threshold, reasons=ai_values.get('rto_risk_reasons')))
        ctx = self._zivo_ctx(partner, picking.company_id, 'stock.picking', picking.id, request.declared_value)
        manual_note = ''
        if account:
            candidates = [{'account': account, 'quote': None, 'price': picking.zivo_selected_price or 0.0,
                           'ai_score': 0, 'reasons': [_('selected manually')]}]
            try:  # keep the AI ranking for reference, so overrides can be audited
                reference, _errors = self._zivo_ranked_options(request, ctx)
            except (UserError, ProviderError):
                reference = []
            manual_note = self._zivo_reasoning(reference, manual_account=account)
        else:
            candidates, errors = self._zivo_ranked_options(request, ctx)
            if not candidates:
                raise UserError(_('No courier can serve %(name)s:\n%(errors)s', name=picking.name, errors='\n'.join(errors)))
        attempts = candidates if self.zivo_failover and not account else candidates[:1]
        failures = []
        Shipment = self.env['zivo.logistics.shipment']
        for option in attempts[:4]:
            acc = option['account']
            req = self._zivo_request_from_picking(picking, acc)
            if option.get('quote') is not None:
                req.service_code = option['quote'].service_code
            reasoning = manual_note or self._zivo_reasoning(candidates)
            values = dict(ai_values, quoted_price=option.get('price') or 0.0, ai_recommendation=reasoning or False,
                          rate_source=option['quote'].source if option.get('quote') else 'manual',
                          user_id=self.env.user.id)
            try:
                with self.env.cr.savepoint():
                    shipment = Shipment._book(acc, req, picking=picking, carrier=self, sale=picking.sale_id,
                                              partner=partner, ai_values=values)
            except UserError as exc:
                failures.append(str(exc))
                continue
            if failures:
                shipment.message_post(body=_('Smart failover used. Previous attempts:\n%s', '\n'.join(failures)))
            return shipment
        raise UserError('\n'.join(failures))

    def _zivo_reasoning(self, ranked, manual_account=None):
        """Human readable AI ranking stored on the shipment."""
        _ = self.env._
        lines = [_('#%(rank)s %(text)s: %(reasons)s', rank=o.get('rank', ''), text=self._zivo_option_text(o),
                   reasons=', '.join(o.get('reasons') or [])) for o in ranked[:5] if o.get('quote')]
        if manual_account:
            head = _('Courier chosen manually: %s.', manual_account.name)
            return head + ('\n' + _('AI ranking at booking time:') + '\n' + '\n'.join(lines) if lines else '')
        return '\n'.join(lines)

    def zivo_hub_get_tracking_link(self, picking):
        shipment = picking.zivo_shipment_id
        return shipment.public_tracking_url if shipment else False

    def zivo_hub_cancel_shipment(self, pickings):
        for picking in pickings:
            shipments = picking.zivo_shipment_ids.filtered(lambda s: s.state not in ('cancelled', 'delivered', 'rto_delivered'))
            shipments.action_cancel_shipment()
            picking.write({'carrier_tracking_ref': False, 'carrier_price': 0.0})
        return True

    def zivo_hub_get_return_label(self, pickings, tracking_number=None, origin_date=None):
        for picking in pickings:
            self._zivo_ship_picking(picking)

    @api.depends('delivery_type')
    def _compute_can_generate_return(self):
        super()._compute_can_generate_return()
        for carrier in self.filtered(lambda c: c.delivery_type == 'zivo_hub'):
            carrier.can_generate_return = True


class AllocationRule(models.Model):
    _name = 'zivo.logistics.allocation.rule'
    _description = 'Courier Allocation Rule'
    _order = 'sequence, id'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)
    payment_mode = fields.Selection([('any', 'Any'), ('cod', 'COD only'), ('prepaid', 'Prepaid only')], default='any', required=True)
    min_weight = fields.Float(string='Min Weight (kg)')
    max_weight = fields.Float(string='Max Weight (kg)', help='0 = no limit')
    min_value = fields.Float(string='Min Order Value')
    max_value = fields.Float(string='Max Order Value', help='0 = no limit')
    zone = fields.Selection([('any', 'Any'), ('local', 'Local'), ('regional', 'Regional'), ('metro', 'Metro'),
                             ('national', 'National'), ('special', 'Special')], default='any', required=True)
    zip_prefixes = fields.Char(string='PIN Prefixes', help='Comma separated, e.g. 500,501,502')
    state_ids = fields.Many2many('res.country.state', string='Destination States')
    min_rto_risk = fields.Integer(string='Min RTO Risk %', help='Apply only when the AI RTO risk is at least this value.')
    strategy = fields.Selection([s for s in STRATEGIES if s[0] not in ('rules',)], default='ai_best', required=True)
    account_ids = fields.Many2many('zivo.logistics.account', string='Allowed Carrier Accounts')
    note = fields.Text()

    @api.model
    def _match(self, request, ctx, company):
        partner = ctx.get('partner')
        zone = self.env['zivo.logistics.ai']._zone(request.pickup.zip, request.delivery.zip)
        risk = None
        for rule in self.search([('company_id', '=', company.id)]):
            if rule.payment_mode != 'any' and rule.payment_mode != request.payment_mode:
                continue
            weight = request.chargeable_weight
            if weight < rule.min_weight or (rule.max_weight and weight > rule.max_weight):
                continue
            value = ctx.get('value') or request.declared_value
            if value < rule.min_value or (rule.max_value and value > rule.max_value):
                continue
            if rule.zone != 'any' and rule.zone != zone:
                continue
            prefixes = [p.strip() for p in (rule.zip_prefixes or '').split(',') if p.strip()]
            if prefixes and not any(request.delivery.zip.startswith(p) for p in prefixes):
                continue
            if rule.state_ids and (not partner or partner.state_id not in rule.state_ids):
                continue
            if rule.min_rto_risk:
                if risk is None:
                    risk = self.env['zivo.logistics.ai']._risk_values(partner, value, request.is_cod).get('rto_risk', 0) if partner else 0
                if risk < rule.min_rto_risk:
                    continue
            return rule
        return self.browse()
