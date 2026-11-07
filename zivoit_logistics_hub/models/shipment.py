# -*- coding: utf-8 -*-
import logging
import uuid
from datetime import timedelta

import requests
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.modules import module as odoo_module

from ..ai import engine
from ..providers.base import FINAL_STATUSES, NORMALIZED_STATUSES, STATUS_RANK, NotSupported, ProviderError

_logger = logging.getLogger(__name__)

RTO_STATES = ('rto_initiated', 'rto_in_transit', 'rto_delivered')
NDR_CATEGORIES = [(k, v) for k, v in engine.NDR_LABELS.items()]
NDR_ACTIONS = [('reattempt', 'Re-attempt delivery'), ('change_address', 'Update address & re-attempt'),
               ('contact_customer', 'Contact customer first'), ('rto', 'Return to origin (RTO)')]


class LogisticsShipment(models.Model):
    _name = 'zivo.logistics.shipment'
    _description = 'Shipment'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'
    _check_company_auto = True

    name = fields.Char(default='New', readonly=True, copy=False, index=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    account_id = fields.Many2one('zivo.logistics.account', string='Carrier Account', required=True, index=True,
                                 tracking=True, check_company=True)
    provider = fields.Selection(related='account_id.provider', store=True)
    carrier_id = fields.Many2one('delivery.carrier', string='Delivery Method')
    picking_id = fields.Many2one('stock.picking', string='Transfer', index=True, ondelete='set null', check_company=True)
    sale_id = fields.Many2one('sale.order', string='Sales Order', index=True)
    partner_id = fields.Many2one('res.partner', string='Consignee', index=True)
    user_id = fields.Many2one('res.users', string='Responsible', default=lambda self: self.env.user)
    is_return = fields.Boolean(string='Reverse Pickup')

    tracking_number = fields.Char(string='AWB / Tracking No.', index=True, tracking=True, copy=False)
    carrier_reference = fields.Char(copy=False)
    service_code = fields.Char()
    courier_name = fields.Char(help='Actual courier (useful for aggregators such as Shiprocket).')
    tracking_link = fields.Char(compute='_compute_links')
    public_tracking_url = fields.Char(compute='_compute_links')
    access_token = fields.Char(copy=False, default=lambda self: uuid.uuid4().hex, readonly=True)

    state = fields.Selection(NORMALIZED_STATUSES, default='draft', required=True, tracking=True, index=True,
                             group_expand='_group_expand_states')
    raw_status = fields.Char(string='Carrier Status')
    status_description = fields.Char()
    booked_at = fields.Datetime()
    picked_at = fields.Datetime()
    delivered_at = fields.Datetime()
    last_event_at = fields.Datetime()
    last_tracked_at = fields.Datetime()
    edd = fields.Datetime(string='Carrier EDD')
    ai_edd = fields.Datetime(string='AI Predicted Delivery')
    ai_edd_basis = fields.Char(string='EDD Basis')
    transit_days = fields.Float(compute='_compute_transit', store=True, aggregator='avg')
    is_delayed = fields.Boolean(compute='_compute_is_delayed', search='_search_is_delayed')
    is_final = fields.Boolean(compute='_compute_is_final', store=True)
    pickup_reference = fields.Char()

    payment_mode = fields.Selection([('prepaid', 'Prepaid'), ('cod', 'COD')], default='prepaid', required=True)
    cod_amount = fields.Monetary()
    declared_value = fields.Monetary()
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    quoted_price = fields.Monetary(string='Quoted Charge')
    actual_price = fields.Monetary(string='Billed Charge')
    rate_source = fields.Char()
    weight_kg = fields.Float(string='Actual Weight (kg)', digits=(10, 3))
    volumetric_weight = fields.Float(string='Volumetric Weight (kg)', digits=(10, 3))
    chargeable_weight = fields.Float(string='Chargeable Weight (kg)', digits=(10, 3))
    charged_weight = fields.Float(string='Carrier Charged Weight (kg)', digits=(10, 3))
    weight_dispute = fields.Boolean(compute='_compute_weight_dispute', store=True)
    package_count = fields.Integer(default=1)

    origin_zip = fields.Char()
    dest_zip = fields.Char(string='Destination PIN', index=True)
    dest_city = fields.Char()
    dest_state_id = fields.Many2one('res.country.state', string='Destination State')
    dest_phone = fields.Char()
    zone = fields.Selection([('local', 'Local'), ('regional', 'Regional'), ('metro', 'Metro'),
                             ('national', 'National'), ('special', 'Special')])

    label_attachment_id = fields.Many2one('ir.attachment', copy=False)
    label_url = fields.Char()
    event_ids = fields.One2many('zivo.logistics.tracking.event', 'shipment_id')
    event_count = fields.Integer(compute='_compute_event_count')

    # NDR / AI
    ndr_count = fields.Integer(string='Failed Attempts')
    ndr_reason = fields.Char(string='NDR Reason')
    ndr_category = fields.Selection(NDR_CATEGORIES, string='NDR Category')
    ndr_suggested_action = fields.Selection(NDR_ACTIONS, string='AI Suggested Action')
    ndr_advice = fields.Text(string='AI Advice')
    ndr_customer_message = fields.Text(string='Customer Message Draft')
    ndr_state = fields.Selection([('none', 'No NDR'), ('open', 'Action Needed'), ('actioned', 'Actioned'), ('closed', 'Closed')],
                                 default='none', tracking=True, index=True)
    rto_risk = fields.Integer(string='RTO Risk %')
    rto_risk_level = fields.Selection([('low', 'Low'), ('medium', 'Medium'), ('high', 'High')])
    rto_risk_reasons = fields.Text()
    address_score = fields.Integer()
    address_issues = fields.Text()
    ai_recommendation = fields.Text(string='AI Carrier Reasoning')

    _tracking_account_uniq = models.Constraint('UNIQUE(account_id, tracking_number)', 'Tracking number already exists for this carrier account.')

    # ------------------------------------------------------------------ compute
    @api.model
    def _group_expand_states(self, states, domain):
        return ['booked', 'picked_up', 'in_transit', 'out_for_delivery', 'ndr', 'delivered', 'rto_initiated', 'rto_delivered']

    @api.depends('tracking_number', 'access_token', 'account_id')
    def _compute_links(self):
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        for shp in self:
            shp.tracking_link = shp.account_id._tracking_link(shp.tracking_number) if shp.account_id else ''
            shp.public_tracking_url = '%s/logistics/track/%s' % (base, shp.access_token) if shp.access_token else ''

    @api.depends('booked_at', 'picked_at', 'delivered_at')
    def _compute_transit(self):
        for shp in self:
            start = shp.picked_at or shp.booked_at
            shp.transit_days = round((shp.delivered_at - start).total_seconds() / 86400.0, 2) \
                if shp.delivered_at and start and shp.delivered_at > start else 0.0

    @api.depends('state')
    def _compute_is_final(self):
        for shp in self:
            shp.is_final = shp.state in FINAL_STATUSES

    def _compute_is_delayed(self):
        now = fields.Datetime.now()
        for shp in self:
            promise = shp.edd or shp.ai_edd
            shp.is_delayed = bool(promise and not shp.is_final and shp.state not in RTO_STATES and now > promise)

    def _search_is_delayed(self, operator, value):
        if operator not in ('=', '!=') or not isinstance(value, bool):
            raise UserError(_('Unsupported search on delay.'))
        now = fields.Datetime.now()
        delayed = ['&', '&', ('is_final', '=', False), ('state', 'not in', RTO_STATES),
                   '|', ('edd', '<', now), '&', ('edd', '=', False), ('ai_edd', '<', now)]
        positive = (operator == '=') == value
        return delayed if positive else ['!'] + delayed

    @api.depends('chargeable_weight', 'weight_kg', 'charged_weight')
    def _compute_weight_dispute(self):
        for shp in self:
            shp.weight_dispute = engine.weight_dispute(shp.chargeable_weight or shp.weight_kg, shp.charged_weight)

    def _compute_event_count(self):
        for shp in self:
            shp.event_count = len(shp.event_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('zivo.logistics.shipment') or 'New'
        return super().create(vals_list)

    def _compute_display_name(self):
        for shp in self:
            shp.display_name = '%s%s' % (shp.name, ' [%s]' % shp.tracking_number if shp.tracking_number else '')

    # ------------------------------------------------------------------ booking
    @api.model
    def _book(self, account, request, picking=None, carrier=None, sale=None, partner=None, ai_values=None):
        """Book ``request`` with ``account`` and create the shipment record. Returns the shipment."""
        provider = account._get_provider(self._name)
        limit_error = account._check_limits(request)
        if limit_error:
            raise UserError(limit_error)
        request.volumetric_divisor = account.volumetric_divisor or 5000.0
        try:
            result = provider.create_shipment(request)
        except ProviderError as exc:
            raise UserError(_('%(carrier)s could not book the shipment: %(err)s', carrier=account.name, err=exc.message))
        ai = self.env['zivo.logistics.ai']
        currency = account.rate_currency_id or self.env.company.currency_id
        company_currency = (picking.company_id if picking else self.env.company).currency_id
        price = currency._convert(result.price or 0.0, company_currency, account.company_id, fields.Date.today()) \
            if result.price else 0.0
        state_id = partner.state_id.id if partner and partner.state_id else False
        vals = {
            'account_id': account.id, 'company_id': (picking.company_id if picking else account.company_id).id,
            'carrier_id': carrier.id if carrier else False, 'picking_id': picking.id if picking else False,
            'sale_id': sale.id if sale else False, 'partner_id': partner.id if partner else False,
            'tracking_number': result.tracking_number, 'carrier_reference': result.carrier_reference,
            'service_code': result.service_code or request.service_code, 'courier_name': result.courier_name,
            'state': 'booked', 'booked_at': fields.Datetime.now(), 'payment_mode': request.payment_mode,
            'cod_amount': request.cod_amount, 'declared_value': request.declared_value,
            'currency_id': company_currency.id, 'actual_price': price, 'weight_kg': request.total_weight,
            'volumetric_weight': request.volumetric_weight, 'chargeable_weight': request.chargeable_weight,
            'package_count': len(request.parcels), 'origin_zip': request.pickup.zip, 'dest_zip': request.delivery.zip,
            'dest_city': request.delivery.city, 'dest_state_id': state_id, 'dest_phone': request.delivery.clean_phone,
            'zone': ai._zone(request.pickup.zip, request.delivery.zip), 'edd': result.edd, 'label_url': result.label_url,
            'is_return': request.is_return,
        }
        vals.update(ai_values or {})
        shipment = self.create(vals)
        if result.label_pdf:
            shipment._store_label(result.label_pdf)
        shipment._predict_edd()
        shipment.event_ids.create({'shipment_id': shipment.id, 'event_time': fields.Datetime.now(),
                                   'raw_status': 'BOOKED', 'normalized_status': 'booked',
                                   'description': _('Shipment booked with %s', account.name), 'source': 'system'})
        if account.auto_pickup and provider.supports('pickup'):
            self.env['zivo.logistics.queue.job'].enqueue(shipment, 'action_schedule_pickup', 'pickup', account=account)
        if not shipment.label_attachment_id and (provider.supports('label') or shipment.label_url):
            self.env['zivo.logistics.queue.job'].enqueue(shipment, '_fetch_label', 'label', account=account, delay_minutes=1)
        return shipment

    def _store_label(self, pdf_bytes, name=None):
        self.ensure_one()
        attachment = self.env['ir.attachment'].create({
            'name': name or 'Label-%s.pdf' % self.tracking_number, 'raw': pdf_bytes, 'mimetype': 'application/pdf',
            'res_model': self._name, 'res_id': self.id,
        })
        self.label_attachment_id = attachment
        if self.picking_id:
            self.picking_id.message_post(body=_('Shipping label for AWB %s', self.tracking_number),
                                         attachment_ids=[attachment.copy({'res_model': 'stock.picking',
                                                                          'res_id': self.picking_id.id}).id])
        return attachment

    def _fetch_label(self):
        """Fetch & store the label (job)."""
        self.ensure_one()
        if self.label_attachment_id:
            return 'already stored'
        url = self.label_url
        if not url:
            provider = self.account_id._get_provider(self._name, self.id)
            label = provider.get_label(self.tracking_number, self.carrier_reference)
            if label.pdf:
                self._store_label(label.pdf)
                return 'stored'
            url = label.url
            self.label_url = url
        if url and not odoo_module.current_test:
            resp = requests.get(url, timeout=30)
            if resp.ok and resp.content[:4] == b'%PDF':
                self._store_label(resp.content)
                return 'downloaded'
        return 'link only'

    # ------------------------------------------------------------------ tracking
    def action_track(self):
        for account, shipments in self.filtered(lambda s: s.tracking_number and s.state != 'draft').grouped('account_id').items():
            provider = account._get_provider(self._name)
            try:
                for start in range(0, len(shipments), 50):
                    batch = shipments[start:start + 50]
                    results = provider.track(batch.mapped('tracking_number'))
                    for shp in batch:
                        res = results.get(shp.tracking_number)
                        if res and res.found:
                            shp._apply_tracking(res, source='api')
                        shp.last_tracked_at = fields.Datetime.now()
            except NotSupported:
                shipments.write({'last_tracked_at': fields.Datetime.now()})
            except ProviderError as exc:
                if len(self) == 1:
                    raise UserError(exc.message)
                _logger.warning('Tracking failed for %s: %s', account.name, exc.message)
        return True

    @api.model
    def _cron_track(self, limit=500):
        hours = float(self.env['ir.config_parameter'].sudo().get_param('zivoit_logistics_hub.track_interval_hours', 2))
        threshold = fields.Datetime.now() - timedelta(hours=hours)
        domain = [('is_final', '=', False), ('state', '!=', 'draft'), ('tracking_number', '!=', False),
                  ('account_id.auto_track', '=', True),
                  '|', ('last_tracked_at', '=', False), ('last_tracked_at', '<', threshold)]
        shipments = self.search(domain, limit=limit, order='last_tracked_at asc nulls first')
        for company, recs in shipments.grouped('company_id').items():
            recs.with_company(company).action_track()
        return len(shipments)

    def _normalize(self, provider_code, raw_status, description='', provided=None):
        if provided:
            return provided
        status, _mapping = self.env['zivo.logistics.status.map']._resolve(provider_code, raw_status, description)
        return status

    def _apply_tracking(self, result, source='api'):
        """Merge a normalized TrackingResult into the shipment (idempotent)."""
        self.ensure_one()
        Event = self.env['zivo.logistics.tracking.event']
        existing = {(e.event_time, (e.raw_status or '').lower(), (e.location or '').lower()) for e in self.event_ids}
        new_events = []
        for ev in result.events:
            key = (ev.time.replace(microsecond=0) if ev.time else False, (ev.raw_status or '').lower(), (ev.location or '').lower())
            if key in existing:
                continue
            existing.add(key)
            new_events.append({
                'shipment_id': self.id, 'event_time': key[0] or fields.Datetime.now(), 'raw_status': ev.raw_status,
                'normalized_status': self._normalize(self.provider, ev.raw_status, ev.description, ev.normalized),
                'description': ev.description, 'location': ev.location, 'source': source,
            })
        if new_events:
            Event.create(new_events)
        new_state = self._normalize(self.provider, result.raw_status, result.description, result.normalized)
        if not new_state and new_events:
            latest = max(new_events, key=lambda v: v['event_time'])
            new_state = latest['normalized_status']
        vals = {'raw_status': result.raw_status or self.raw_status, 'status_description': (result.description or '')[:250]}
        if result.edd:
            vals['edd'] = result.edd
        if result.charged_weight:
            vals['charged_weight'] = result.charged_weight
        times = [e.time for e in result.events if e.time]
        if times:
            vals['last_event_at'] = max(times)
        old_state = self.state
        if new_state and self._can_transition(old_state, new_state):
            vals['state'] = new_state
            if new_state == 'picked_up' and not self.picked_at:
                vals['picked_at'] = min([e.time for e in result.events if e.normalized == 'picked_up' and e.time] or [fields.Datetime.now()])
            if new_state == 'delivered':
                vals['delivered_at'] = result.delivered_at or vals.get('last_event_at') or fields.Datetime.now()
                if self.ndr_state == 'open' or self.ndr_state == 'actioned':
                    vals['ndr_state'] = 'closed'
            if new_state in RTO_STATES and self.ndr_state in ('open', 'actioned'):
                vals['ndr_state'] = 'closed'
        self.write(vals)
        changed = 'state' in vals and vals['state'] != old_state
        # failed attempts that happened "in between" two tracking refreshes (state already moved on)
        missed_ndr = [ev for ev in new_events if ev['normalized_status'] == 'ndr']
        if missed_ndr and not (changed and vals['state'] == 'ndr'):
            for ev in sorted(missed_ndr, key=lambda v: v['event_time']):
                self._handle_ndr(ev['description'] or ev['raw_status'], open_activity=False)
            if self.state != 'ndr':
                self.ndr_state = 'closed'
        if changed:
            self._on_state_change(old_state, vals['state'], result)
        return True

    @staticmethod
    def _can_transition(old, new):
        if old == new:
            return new == 'ndr'  # a repeated NDR is a new failed attempt
        if old in FINAL_STATUSES:
            return False
        if new in ('ndr', 'exception') or new in RTO_STATES or new in FINAL_STATUSES:
            return True
        if old in RTO_STATES:
            return False  # never go back to forward flow after RTO
        return STATUS_RANK.get(new, 0) >= STATUS_RANK.get(old, 0) or old in ('ndr', 'exception')

    def _on_state_change(self, old, new, result=None):
        self.ensure_one()
        label = dict(NORMALIZED_STATUSES)
        self.message_post(body=Markup('%s → <b>%s</b>%s') % (
            label.get(old, old), label.get(new, new), (' — %s' % result.description) if result and result.description else ''))
        if new == 'ndr':
            self._handle_ndr(result.ndr_reason or result.description if result else '')
        if new in ('out_for_delivery', 'delivered', 'ndr'):
            self._notify_customer(new)
        if new in RTO_STATES and self.picking_id:
            self.picking_id.message_post(body=_('Shipment %s is returning to origin (RTO).', self.tracking_number))

    def _handle_ndr(self, reason, open_activity=True):
        self.ensure_one()
        assist = self.env['zivo.logistics.ai']._ndr_assist(self, reason)
        self.write({
            'ndr_count': self.ndr_count + 1, 'ndr_reason': (reason or '')[:250], 'ndr_state': 'open',
            'ndr_category': assist['category'], 'ndr_suggested_action': assist['action'],
            'ndr_advice': assist['advice'], 'ndr_customer_message': assist['message'],
        })
        if not open_activity:
            return
        self.activity_schedule('mail.mail_activity_data_todo', user_id=self.user_id.id or self.env.uid,
                               summary=_('NDR: %s', engine.NDR_LABELS.get(assist['category'], '')),
                               note=assist['advice'], date_deadline=fields.Date.today())

    def _notify_customer(self, state):
        if not self.env['ir.config_parameter'].sudo().get_param('zivoit_logistics_hub.notify_customer'):
            return
        template = self.env.ref('zivoit_logistics_hub.mail_template_shipment_status', raise_if_not_found=False)
        if template and self.partner_id.email:
            self.env['zivo.logistics.queue.job'].enqueue(self, '_send_status_mail', 'notify', payload={'state': state})

    def _send_status_mail(self, state=None):
        template = self.env.ref('zivoit_logistics_hub.mail_template_shipment_status')
        template.send_mail(self.id, force_send=False)
        return 'queued'

    def _predict_edd(self):
        ai = self.env['zivo.logistics.ai']
        for shp in self:
            days, conf, basis = ai._predict_tat(shp.account_id, shp.dest_zip, shp.dest_state_id, shp.zone,
                                                carrier_tat=((shp.edd - shp.booked_at).total_seconds() / 86400.0)
                                                if shp.edd and shp.booked_at else 0.0)
            shp.write({'ai_edd': engine.add_business_days(shp.booked_at or fields.Datetime.now(), days),
                       'ai_edd_basis': '%s (confidence %d%%)' % (basis, conf * 100)})

    # ------------------------------------------------------------------ other operations
    def action_cancel_shipment(self):
        for shp in self:
            if shp.state in FINAL_STATUSES:
                raise UserError(_('Shipment %s is already %s.', shp.name, dict(NORMALIZED_STATUSES)[shp.state]))
            if shp.tracking_number:
                try:
                    shp.account_id._get_provider(self._name, shp.id).cancel(shp.tracking_number, shp.carrier_reference)
                except ProviderError as exc:
                    raise UserError(_('%(carrier)s refused the cancellation: %(err)s', carrier=shp.account_id.name, err=exc.message))
            shp.write({'state': 'cancelled'})
            shp.message_post(body=_('Shipment cancelled with the carrier.'))
            if shp.picking_id and shp.picking_id.carrier_tracking_ref:
                refs = [r for r in shp.picking_id.carrier_tracking_ref.split(',') if r.strip() != shp.tracking_number]
                shp.picking_id.carrier_tracking_ref = ','.join(refs) or False
        return True

    def action_schedule_pickup(self, pickup_date=None):
        for account, shipments in self.filtered(lambda s: s.state in ('booked',)).grouped('account_id').items():
            when = fields.Datetime.to_datetime(pickup_date) if pickup_date else fields.Datetime.now() + timedelta(hours=2)
            try:
                ref = account._get_provider(self._name).schedule_pickup(
                    shipments.mapped('tracking_number'), when, shipments.mapped('carrier_reference'),
                    sum(shipments.mapped('package_count')))
            except NotSupported as exc:
                raise UserError(exc.message)
            except ProviderError as exc:
                raise UserError(_('Pickup request failed: %s', exc.message)) if len(self) == 1 else ProviderError(exc.message, retryable=True)
            shipments.write({'state': 'pickup_scheduled', 'pickup_reference': ref})
        return True

    def action_print_label(self):
        self.ensure_one()
        if not self.label_attachment_id:
            try:
                self._fetch_label()
            except ProviderError as exc:
                raise UserError(exc.message)
        if self.label_attachment_id:
            return {'type': 'ir.actions.act_url', 'url': '/web/content/%s?download=true' % self.label_attachment_id.id,
                    'target': 'new'}
        if self.label_url:
            return {'type': 'ir.actions.act_url', 'url': self.label_url, 'target': 'new'}
        raise UserError(_('The carrier has not provided a label yet. Please retry in a few minutes.'))

    def action_open_carrier_tracking(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_url', 'url': self.tracking_link or self.public_tracking_url, 'target': 'new'}

    def action_open_public_tracking(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_url', 'url': self.public_tracking_url, 'target': 'new'}

    def action_ndr(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'res_model': 'zivo.logistics.ndr.action', 'view_mode': 'form', 'target': 'new',
            'context': {'default_shipment_id': self.id, 'default_action': self.ndr_suggested_action or 'reattempt',
                        'default_message': self.ndr_customer_message},
        }

    def action_refresh_ai(self):
        ai = self.env['zivo.logistics.ai']
        for shp in self:
            vals = ai._risk_values(shp.partner_id, shp.cod_amount or shp.declared_value, shp.payment_mode == 'cod',
                                   shp.create_date, shp.dest_zip)
            shp.write(vals)
            if shp.state == 'ndr':
                assist = ai._ndr_assist(shp, shp.ndr_reason)
                shp.write({'ndr_category': assist['category'], 'ndr_suggested_action': assist['action'],
                           'ndr_advice': assist['advice'], 'ndr_customer_message': assist['message']})
        self._predict_edd()
        return True


class LogisticsTrackingEvent(models.Model):
    _name = 'zivo.logistics.tracking.event'
    _description = 'Shipment Tracking Event'
    _order = 'event_time desc, id desc'

    shipment_id = fields.Many2one('zivo.logistics.shipment', required=True, ondelete='cascade', index=True)
    event_time = fields.Datetime(required=True)
    raw_status = fields.Char(string='Carrier Status')
    normalized_status = fields.Selection(NORMALIZED_STATUSES, string='Status')
    description = fields.Char()
    location = fields.Char()
    source = fields.Selection([('api', 'Tracking API'), ('webhook', 'Webhook'), ('manual', 'Manual'), ('system', 'System')],
                              default='api')
