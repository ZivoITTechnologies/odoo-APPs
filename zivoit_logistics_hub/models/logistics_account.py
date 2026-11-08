# -*- coding: utf-8 -*-
import json
import logging
import secrets
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.modules import module as odoo_module

from ..ai.india import zone_for
from ..providers import PROVIDERS, get_provider_class
from ..providers.base import HttpClient, NotSupported, ProviderConfig, ProviderError, RateOption, ceil_slab

_logger = logging.getLogger(__name__)

ZONES = [('local', 'Local (same city)'), ('regional', 'Regional (same state)'), ('metro', 'Metro to Metro'),
         ('national', 'Rest of India'), ('special', 'Special (J&K, North-East, Islands)'), ('any', 'Any zone')]
SECRET_GROUP = 'zivoit_logistics_hub.group_logistics_manager'


class LogisticsAccount(models.Model):
    _name = 'zivo.logistics.account'
    _description = 'Courier / Aggregator Account'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'sequence, id'
    _check_company_auto = True

    name = fields.Char(required=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    color = fields.Integer()
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    provider = fields.Selection(selection='_selection_provider', required=True, default='demo', tracking=True)
    provider_label = fields.Char(compute='_compute_provider_meta')
    is_aggregator = fields.Boolean(compute='_compute_provider_meta')
    credential_fields = fields.Char(compute='_compute_provider_meta')
    capability_text = fields.Char(string='Supported Operations', compute='_compute_provider_meta')
    environment = fields.Selection([('sandbox', 'Sandbox / Test'), ('production', 'Production')],
                                   default='sandbox', required=True, tracking=True)
    base_url = fields.Char(string='Base URL Override', help='Leave empty to use the official endpoint of the carrier.')
    state = fields.Selection([('draft', 'Not Tested'), ('connected', 'Connected'), ('error', 'Error')],
                             default='draft', readonly=True, tracking=True)
    last_test_message = fields.Text(readonly=True)

    # credentials (only visible to Logistics Managers)
    username = fields.Char(string='Username / Email', groups=SECRET_GROUP)
    password = fields.Char(groups=SECRET_GROUP)
    api_key = fields.Char(string='API Key / Token', groups=SECRET_GROUP)
    client_id = fields.Char(string='Client ID / Consumer Key', groups=SECRET_GROUP)
    client_secret = fields.Char(string='Client Secret', groups=SECRET_GROUP)
    license_key = fields.Char(string='Licence Key', groups=SECRET_GROUP)
    account_number = fields.Char(string='Account / Area Code', groups=SECRET_GROUP)
    customer_code = fields.Char(string='Customer / Client Code', groups=SECRET_GROUP)
    extra_config = fields.Text(string='Extra Settings (JSON)', default='{}', groups=SECRET_GROUP,
                               help='Carrier specific options, e.g. {"origin_area": "HYD"} for Blue Dart, '
                                    '{"step_minutes": 60} for the sandbox courier.')
    token = fields.Char(readonly=True, copy=False, groups=SECRET_GROUP)
    token_expiry = fields.Datetime(readonly=True, copy=False, groups=SECRET_GROUP)
    webhook_token = fields.Char(copy=False, readonly=True, groups=SECRET_GROUP,
                                default=lambda self: secrets.token_urlsafe(24))
    webhook_url = fields.Char(compute='_compute_webhook_url', groups=SECRET_GROUP)

    # generic / template driven connectors
    auth_style = fields.Selection([
        ('none', 'No authentication'), ('api_key_header', 'API key in header'), ('bearer_static', 'Static bearer token'),
        ('basic', 'HTTP Basic (username/password)'), ('token_login', 'Login endpoint returns a token'),
    ], default='none')
    auth_header_name = fields.Char(default='Authorization')
    endpoint_ids = fields.One2many('zivo.logistics.endpoint', 'account_id', string='API Endpoints', copy=True)

    # shipping defaults
    pickup_partner_id = fields.Many2one('res.partner', string='Pickup Address', check_company=True,
                                        help='Warehouse address used as shipper. Defaults to the warehouse/company address.')
    pickup_location_code = fields.Char(string='Pickup Location Code',
                                       help='Pickup location nickname / warehouse name registered with the carrier '
                                            '(Shiprocket pickup_location, Delhivery warehouse name, Xpressbees warehouse).')
    default_service_code = fields.Char(string='Default Service Code')
    cod_enabled = fields.Boolean(string='COD Allowed', default=True)
    max_weight_kg = fields.Float(string='Max Weight (kg)', default=0.0, help='0 = no limit')
    max_cod_amount = fields.Float(string='Max COD Amount', default=0.0, help='0 = no limit')
    volumetric_divisor = fields.Float(default=5000.0, help='cm3 per kg used for volumetric weight (5000 is standard).')
    auto_pickup = fields.Boolean(string='Auto-schedule Pickup', help='Schedule a pickup right after booking.')
    auto_track = fields.Boolean(string='Auto Tracking', default=True)
    rate_source = fields.Selection([('api', 'Carrier API'), ('rate_card', 'Contract Rate Card'), ('both', 'API, else Rate Card')],
                                   default='both', required=True)
    rate_card_ids = fields.One2many('zivo.logistics.rate.card', 'account_id', copy=True)
    fuel_surcharge_pct = fields.Float(string='Fuel Surcharge %')
    gst_pct = fields.Float(string='GST %', default=18.0)
    rate_currency_id = fields.Many2one('res.currency', string='Rate Currency',
                                       default=lambda self: self.env.ref('base.INR', raise_if_not_found=False))
    tracking_url_template = fields.Char(help='Public tracking link of the carrier. Use {awb} as placeholder.')

    # analytics
    shipment_ids = fields.One2many('zivo.logistics.shipment', 'account_id')
    shipment_count = fields.Integer(compute='_compute_stats')
    delivered_rate = fields.Float(compute='_compute_stats', string='Delivered %')
    rto_rate = fields.Float(compute='_compute_stats', string='RTO %')
    ndr_rate = fields.Float(compute='_compute_stats', string='NDR %')
    avg_tat = fields.Float(compute='_compute_stats', string='Avg Transit (days)')
    open_shipment_count = fields.Integer(compute='_compute_stats')

    _name_company_uniq = models.Constraint('UNIQUE(name, company_id)', 'Account name must be unique per company.')

    # ------------------------------------------------------------------ meta
    @api.model
    def _selection_provider(self):
        order = ['shiprocket', 'delhivery', 'dtdc', 'bluedart', 'fedex', 'xpressbees', 'shree_maruti', 'generic', 'demo']
        codes = sorted(PROVIDERS, key=lambda c: order.index(c) if c in order else 99)
        return [(code, PROVIDERS[code].name) for code in codes]

    @api.depends('provider')
    def _compute_provider_meta(self):
        for acc in self:
            cls = PROVIDERS.get(acc.provider)
            acc.provider_label = cls.name if cls else ''
            acc.is_aggregator = bool(cls and cls.aggregator)
            acc.credential_fields = ','.join(cls.credential_fields) if cls else ''
            caps = sorted(cls.capabilities) if cls else []
            if acc.provider in ('generic', 'shree_maruti'):
                caps = sorted(acc.endpoint_ids.mapped('operation')) or ['configure endpoints']
            acc.capability_text = ', '.join(caps)

    def _compute_webhook_url(self):
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        for acc in self:
            acc.webhook_url = '%s/zivo_logistics/webhook/%s/%s' % (base, acc.id, acc.webhook_token) if acc.id else ''

    def _compute_stats(self):
        Shipment = self.env['zivo.logistics.shipment']
        data = {acc.id: {} for acc in self}
        for account, state, count in Shipment._read_group(
                [('account_id', 'in', self.ids)], ['account_id', 'state'], ['__count']):
            data[account.id][state] = count
        tat = {account.id: avg for account, avg in Shipment._read_group(
            [('account_id', 'in', self.ids), ('state', '=', 'delivered'), ('transit_days', '>', 0)],
            ['account_id'], ['transit_days:avg'])}
        for acc in self:
            counts = data.get(acc.id, {})
            total = sum(v for k, v in counts.items() if k not in ('draft', 'cancelled'))
            closed = sum(counts.get(k, 0) for k in ('delivered', 'rto_delivered', 'rto_in_transit', 'rto_initiated', 'lost'))
            acc.shipment_count = sum(counts.values())
            acc.open_shipment_count = sum(v for k, v in counts.items()
                                          if k not in ('delivered', 'rto_delivered', 'cancelled', 'lost', 'draft'))
            acc.delivered_rate = 100.0 * counts.get('delivered', 0) / closed if closed else 0.0
            acc.rto_rate = 100.0 * sum(counts.get(k, 0) for k in ('rto_initiated', 'rto_in_transit', 'rto_delivered')) / closed if closed else 0.0
            acc.ndr_rate = 100.0 * len(acc.shipment_ids.filtered(lambda s: s.ndr_count)) / total if total else 0.0
            acc.avg_tat = tat.get(acc.id) or 0.0

    @api.constrains('extra_config')
    def _check_extra_config(self):
        for acc in self.sudo():
            try:
                value = json.loads(acc.extra_config or '{}')
            except ValueError as exc:
                raise ValidationError(_('Extra Settings must be valid JSON: %s', exc))
            if not isinstance(value, dict):
                raise ValidationError(_('Extra Settings must be a JSON object.'))

    @api.onchange('provider')
    def _onchange_provider(self):
        cls = PROVIDERS.get(self.provider)
        if cls and not self.tracking_url_template:
            self.tracking_url_template = cls.tracking_url
        if self.provider in ('dtdc', 'bluedart') and self.rate_source == 'api':
            self.rate_source = 'rate_card'
        if self.provider == 'demo':
            self.rate_source = 'api'

    @api.model_create_multi
    def create(self, vals_list):
        accounts = super().create(vals_list)
        for acc in accounts:
            cls = PROVIDERS.get(acc.provider)
            if not acc.tracking_url_template and cls and cls.tracking_url:
                acc.tracking_url_template = cls.tracking_url
            preset = getattr(cls, 'PRESET', None)
            if preset and not acc.endpoint_ids:
                acc._apply_preset(preset)
        return accounts

    def _apply_preset(self, preset):
        self.ensure_one()
        self.write({
            'auth_style': preset.get('auth_style', 'none'),
            'auth_header_name': preset.get('auth_header_name', 'Authorization'),
            'endpoint_ids': [(0, 0, dict(ep)) for ep in preset.get('endpoints', [])],
        })

    # ------------------------------------------------------------------ provider factory
    def _log_api_call(self, values, res_model=False, res_id=False):
        self.ensure_one()
        values = dict(values, account_id=self.id, company_id=self.company_id.id, res_model=res_model, res_id=res_id)
        self.env['zivo.logistics.api.log']._write_log(values)

    def _get_provider(self, res_model=False, res_id=False):
        """Instantiate the python adapter for this account with logging + token persistence."""
        self.ensure_one()
        acc = self.sudo()
        try:
            extra = json.loads(acc.extra_config or '{}')
        except ValueError:
            extra = {}
        config = ProviderConfig(
            environment=acc.environment, base_url=acc.base_url or '', username=acc.username or '',
            password=acc.password or '', api_key=acc.api_key or '', client_id=acc.client_id or '',
            client_secret=acc.client_secret or '', account_number=acc.account_number or '',
            customer_code=acc.customer_code or '', license_key=acc.license_key or '',
            pickup_location_code=acc.pickup_location_code or '', default_service_code=acc.default_service_code or '',
            token=acc.token or '', token_expiry=acc.token_expiry or None, extra=extra,
            endpoints={ep.operation: ep._as_dict() for ep in acc.endpoint_ids if ep.active},
            auth_style=acc.auth_style or 'none', auth_header_name=acc.auth_header_name or '',
        )
        in_tests = odoo_module.current_test
        http = HttpClient(acc.provider, log_callback=lambda vals: self._log_api_call(vals, res_model, res_id),
                          timeout=int(extra.get('timeout', 30)), max_retries=0 if in_tests else 2,
                          backoff=0 if in_tests else 1.0)

        def save_token(token, expiry):
            acc.write({'token': token, 'token_expiry': expiry})
        return get_provider_class(acc.provider)(config, http=http, token_callback=save_token)

    # ------------------------------------------------------------------ actions
    def action_test_connection(self):
        for acc in self:
            try:
                msg = acc._get_provider().test_connection()
                acc.write({'state': 'connected', 'last_test_message': msg})
            except ProviderError as exc:
                acc.write({'state': 'error', 'last_test_message': exc.message})
        if len(self) == 1:
            return {
                'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': _('Connection test'), 'message': self.last_test_message,
                           'type': 'success' if self.state == 'connected' else 'danger', 'sticky': False,
                           'next': {'type': 'ir.actions.act_window_close'}},
            }
        return True

    def action_refresh_token(self):
        for acc in self:
            provider = acc._get_provider()
            if provider.supports('auth'):
                provider.get_token(force=True)
        return True

    def action_regenerate_webhook_token(self):
        for acc in self:
            acc.sudo().webhook_token = secrets.token_urlsafe(24)

    def action_apply_preset(self):
        for acc in self:
            preset = getattr(PROVIDERS.get(acc.provider), 'PRESET', None)
            if not preset:
                raise UserError(_('No template is available for %s.', acc.provider_label))
            acc.endpoint_ids.unlink()
            acc._apply_preset(preset)

    def action_view_shipments(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_logistics_hub.action_zivo_shipment')
        action['domain'] = [('account_id', '=', self.id)]
        action['context'] = {'default_account_id': self.id}
        return action

    def action_view_api_logs(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_logistics_hub.action_zivo_api_log')
        action['domain'] = [('account_id', '=', self.id)]
        return action

    # ------------------------------------------------------------------ business helpers
    def _tracking_link(self, awb):
        self.ensure_one()
        template = self.tracking_url_template or (PROVIDERS.get(self.provider) and PROVIDERS[self.provider].tracking_url)
        return template.format(awb=awb) if template and awb else ''

    def _check_limits(self, request):
        """Return an error string if the shipment request exceeds account limits, else ''."""
        self.ensure_one()
        if self.max_weight_kg and request.chargeable_weight > self.max_weight_kg:
            return _('%(acc)s: chargeable weight %(w).2f kg exceeds the %(max).2f kg limit.',
                     acc=self.name, w=request.chargeable_weight, max=self.max_weight_kg)
        if request.is_cod and not self.cod_enabled:
            return _('%s does not accept COD shipments.', self.name)
        if request.is_cod and self.max_cod_amount and request.cod_amount > self.max_cod_amount:
            return _('%(acc)s: COD amount exceeds %(max).2f.', acc=self.name, max=self.max_cod_amount)
        return ''

    def _get_rates(self, request, res_model=False, res_id=False):
        """Return list of RateOption for this account (API and/or rate card)."""
        self.ensure_one()
        limit_error = self._check_limits(request)
        if limit_error:
            raise ProviderError(limit_error)
        request.volumetric_divisor = self.volumetric_divisor or 5000.0
        provider = self._get_provider(res_model, res_id)
        rates, api_error = [], None
        if self.rate_source in ('api', 'both') and provider.supports('rate'):
            try:
                rates = provider.get_rates(request)
            except NotSupported:
                rates = []
            except ProviderError as exc:
                api_error = exc
        if not rates and self.rate_source in ('rate_card', 'both'):
            rates = self._rate_card_quote(request)
        if not rates and api_error:
            raise api_error
        return rates

    def _rate_card_quote(self, request):
        self.ensure_one()
        zone = zone_for(request.pickup.zip, request.delivery.zip)
        cards = self.rate_card_ids.filtered(lambda c: c.active)
        options = []
        for service in set(cards.mapped('service_code')) or set():
            lines = cards.filtered(lambda c: c.service_code == service)
            card = lines.filtered(lambda c: c.zone == zone)[:1] or lines.filtered(lambda c: c.zone == 'any')[:1]
            if not card:
                continue
            price = card._compute_price(request)
            options.append(RateOption(service_code=service, service_name=card.service_name or service,
                                      courier_name=self.provider_label, price=price,
                                      currency=(self.rate_currency_id.name or 'INR'), tat_days=card.tat_days,
                                      cod_available=self.cod_enabled, source='rate_card', extra={'zone': zone}))
        return options

    def _check_serviceability(self, pickup_zip, delivery_zip, weight=0.5, cod=False):
        """Cached serviceability (7 days)."""
        self.ensure_one()
        Pincode = self.env['zivo.logistics.pincode'].sudo()
        cache = Pincode.search([('account_id', '=', self.id), ('zip', '=', delivery_zip)], limit=1)
        if cache and cache.checked_at and cache.checked_at > fields.Datetime.now() - timedelta(days=7):
            return cache
        provider = self._get_provider()
        if not provider.supports('serviceability'):
            return False
        res = provider.check_serviceability(pickup_zip, delivery_zip, weight, cod)
        vals = {'account_id': self.id, 'zip': delivery_zip, 'serviceable': res.serviceable, 'cod': res.cod,
                'prepaid': res.prepaid, 'pickup': res.pickup, 'tat_days': res.tat_days, 'message': res.message,
                'checked_at': fields.Datetime.now()}
        if cache:
            cache.write(vals)
            return cache
        return Pincode.create(vals)


class LogisticsEndpoint(models.Model):
    _name = 'zivo.logistics.endpoint'
    _description = 'Courier API Endpoint (template connector)'
    _order = 'sequence, id'

    account_id = fields.Many2one('zivo.logistics.account', required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    operation = fields.Selection([
        ('auth', 'Login / Token'), ('serviceability', 'Pincode Serviceability'), ('rate', 'Rate Calculation'),
        ('create', 'Create Shipment / AWB'), ('label', 'Label'), ('track', 'Tracking'), ('cancel', 'Cancel'),
        ('pickup', 'Pickup Request'),
    ], required=True)
    method = fields.Selection([('GET', 'GET'), ('POST', 'POST'), ('PUT', 'PUT'), ('PATCH', 'PATCH'), ('DELETE', 'DELETE')],
                              default='POST', required=True)
    path = fields.Char(help='Relative path (appended to the account base URL) or full URL. Placeholders allowed, e.g. /track/{{awb}}')
    content_type = fields.Char(default='application/json')
    body_template = fields.Text(help='JSON body (or query params for GET). Use {{ placeholders }}: reference, '
                                     'payment_mode, cod_amount, weight_kg, weight_grams, delivery.name, delivery.zip, '
                                     'pickup.city, awb, username, password, api_key, customer_code ...')
    headers_template = fields.Text(help='Optional extra headers as JSON.')
    success_path = fields.Char(help='JSON path of a success flag (e.g. "status"). Empty = HTTP status only.')
    error_path = fields.Char(default='message')
    token_path = fields.Char(help='Auth: JSON path of the token, e.g. data.token')
    token_lifetime_hours = fields.Float(default=12)
    awb_path = fields.Char(string='AWB Path', help='e.g. data.awb_number')
    reference_path = fields.Char()
    label_path = fields.Char()
    label_type = fields.Selection([('url', 'URL'), ('base64', 'Base64 PDF'), ('pdf', 'Raw PDF response')], default='url')
    price_path = fields.Char()
    list_path = fields.Char(help='Rates: JSON path of the list of services')
    service_path = fields.Char()
    service_name_path = fields.Char()
    tat_path = fields.Char()
    serviceable_path = fields.Char()
    cod_path = fields.Char()
    status_path = fields.Char()
    status_desc_path = fields.Char()
    edd_path = fields.Char()
    events_path = fields.Char()
    event_time_path = fields.Char()
    event_status_path = fields.Char()
    event_desc_path = fields.Char()
    event_location_path = fields.Char()

    _account_operation_uniq = models.Constraint('UNIQUE(account_id, operation)', 'Only one endpoint per operation.')

    def _as_dict(self):
        self.ensure_one()
        keys = ['method', 'path', 'content_type', 'body_template', 'headers_template', 'success_path', 'error_path',
                'token_path', 'token_lifetime_hours', 'awb_path', 'reference_path', 'label_path', 'label_type',
                'price_path', 'list_path', 'service_path', 'service_name_path', 'tat_path', 'serviceable_path',
                'cod_path', 'status_path', 'status_desc_path', 'edd_path', 'events_path', 'event_time_path',
                'event_status_path', 'event_desc_path', 'event_location_path']
        return {k: (self[k] or '') for k in keys}


class LogisticsRateCard(models.Model):
    _name = 'zivo.logistics.rate.card'
    _description = 'Courier Contract Rate Card'
    _order = 'service_code, zone'

    account_id = fields.Many2one('zivo.logistics.account', required=True, ondelete='cascade')
    active = fields.Boolean(default=True)
    service_code = fields.Char(required=True, default='STD')
    service_name = fields.Char()
    zone = fields.Selection(ZONES, required=True, default='any')
    base_weight = fields.Float(string='First Slab (kg)', default=0.5)
    base_price = fields.Float(string='First Slab Price', required=True)
    additional_weight = fields.Float(string='Additional Slab (kg)', default=0.5)
    additional_price = fields.Float(string='Additional Slab Price')
    cod_fixed = fields.Float(string='COD Min Charge')
    cod_percent = fields.Float(string='COD %')
    tat_days = fields.Float(string='TAT (days)', default=4)

    def _compute_price(self, request):
        self.ensure_one()
        weight = request.chargeable_weight
        price = self.base_price
        if weight > self.base_weight and self.additional_weight:
            extra = ceil_slab(weight - self.base_weight, self.additional_weight) / self.additional_weight
            price += extra * self.additional_price
        account = self.account_id
        price *= 1 + (account.fuel_surcharge_pct or 0.0) / 100.0
        if request.is_cod:
            price += max(self.cod_fixed, request.cod_amount * self.cod_percent / 100.0)
        price *= 1 + (account.gst_pct or 0.0) / 100.0
        return round(price, 2)


class LogisticsPincode(models.Model):
    _name = 'zivo.logistics.pincode'
    _description = 'Pincode Serviceability Cache'
    _order = 'checked_at desc'
    _rec_name = 'zip'

    account_id = fields.Many2one('zivo.logistics.account', required=True, ondelete='cascade', index=True)
    zip = fields.Char(required=True, index=True)
    serviceable = fields.Boolean()
    cod = fields.Boolean(string='COD')
    prepaid = fields.Boolean()
    pickup = fields.Boolean()
    tat_days = fields.Float()
    message = fields.Char()
    checked_at = fields.Datetime()

    _account_zip_uniq = models.Constraint('UNIQUE(account_id, zip)', 'Pincode already cached for this account.')
