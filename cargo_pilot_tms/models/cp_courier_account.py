# -*- coding: utf-8 -*-
import math
import secrets

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .providers import PROVIDER_SELECTION, PROVIDERS, get_provider

MANAGER = 'cargo_pilot_tms.group_cp_manager'

# Which credential fields each provider uses, so the form only shows what matters.
PROVIDER_FIELDS = {
    'delhivery': {'api_key', 'pickup_location_name', 'service_code'},
    'bluedart': {'client_id', 'client_secret', 'login_id', 'licence_key', 'customer_code', 'service_code',
                 'origin_area'},
    'dtdc': {'api_key', 'customer_code', 'service_code', 'username', 'password', 'commodity_code'},
    'shiprocket': {'username', 'password', 'pickup_location_name', 'channel_id'},
    'xpressbees': {'username', 'password', 'pickup_location_name'},
    'clickpost': {'username', 'api_key', 'courier_partner_id', 'account_code', 'courier_display_name'},
    'manual': {'courier_display_name', 'awb_sequence_id'},
    'demo': set(),
}


class CpCourierAccount(models.Model):
    _name = 'cp.courier.account'
    _description = 'Courier Account'
    _inherit = ['mail.thread']
    _order = 'sequence, name'

    name = fields.Char(required=True, tracking=True)
    provider = fields.Selection(PROVIDER_SELECTION, required=True, default='delhivery', tracking=True)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    color = fields.Integer()
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one(related='company_id.currency_id')
    environment = fields.Selection([('test', 'Test / sandbox'), ('production', 'Live')], default='test',
                                   required=True, tracking=True)
    state = fields.Selection([('new', 'Not checked'), ('ok', 'Connected'), ('error', 'Problem')], default='new',
                             readonly=True, copy=False)
    last_check_message = fields.Char(readonly=True, copy=False)
    last_check_at = fields.Datetime(readonly=True, copy=False)
    debug_logging = fields.Boolean(string='Log every request', help='Keep a copy of every request and answer. '
                                   'Failed requests are always logged.')

    # Credentials (visible to Cargo Pilot administrators only)
    api_key = fields.Char(string='API Key / Token', groups=MANAGER, copy=False)
    username = fields.Char(string='API User / E-mail', groups=MANAGER, copy=False)
    password = fields.Char(string='API Password', groups=MANAGER, copy=False)
    client_id = fields.Char(string='Client ID (Consumer Key)', groups=MANAGER, copy=False)
    client_secret = fields.Char(string='Client Secret', groups=MANAGER, copy=False)
    login_id = fields.Char(string='Login ID', groups=MANAGER, copy=False)
    licence_key = fields.Char(string='Licence Key', groups=MANAGER, copy=False)
    token = fields.Char(groups=MANAGER, copy=False, readonly=True)
    token_expiry = fields.Datetime(groups=MANAGER, copy=False, readonly=True)

    customer_code = fields.Char(help='Your customer / account code with the courier.')
    account_code = fields.Char(help='ClickPost account code of the courier account.')
    courier_partner_id = fields.Char(string='Courier Partner ID', help='ClickPost courier partner id, for example '
                                     '69 for Shree Maruti Courier.')
    courier_display_name = fields.Char(string='Courier Name', help='Name shown to users and customers, for '
                                       'example "Shree Maruti Courier".')
    pickup_location_name = fields.Char(string='Pickup Location Name',
                                       help='The pickup warehouse exactly as it is registered with the courier '
                                            '(Delhivery client warehouse, Shiprocket pickup location).')
    channel_id = fields.Char(string='Channel ID', help='Optional Shiprocket channel id.')
    service_code = fields.Char(string='Service', help='Delhivery: Surface or Express. Blue Dart: product code '
                               '(A = Apex air, D = Domestic Priority, E = Surface). DTDC: service type, for example '
                               'B2C PRIORITY or GROUND EXPRESS.')
    origin_area = fields.Char(string='Origin Area Code', help='Blue Dart area code of your pickup branch.')
    commodity_code = fields.Char(string='Commodity ID', help='DTDC commodity id, if your contract needs one.')
    awb_sequence_id = fields.Many2one('ir.sequence', string='AWB Number Series',
                                      help='For couriers that give you a book of docket numbers.')
    base_url_override = fields.Char(string='API Address', help='Leave empty to use the standard address. Fill it in '
                                    'only if the courier gave you a different one.')
    tracking_url_template = fields.Char(string='Courier Tracking Page',
                                        help='Use {awb} where the tracking number goes.')
    pickup_partner_id = fields.Many2one('res.partner', string='Pickup Address', required=True,
                                        default=lambda self: self.env.company.partner_id)
    auto_pickup = fields.Boolean(string='Ask for pickup when booking', default=True)
    auto_book_label = fields.Boolean(string='Download label when booking', default=True)
    notify_customer = fields.Boolean(string='E-mail the receiver a tracking link', default=True)

    # Rate card (used when the courier has no price API)
    supports_cod = fields.Boolean(string='Cash on Delivery', default=True)
    volumetric_divisor = fields.Integer(default=5000, help='Length x width x height in cm divided by this number '
                                        'gives the volumetric weight in kg. Most air services use 5000.')
    rate_slab_kg = fields.Float(string='Slab (kg)', default=0.5)
    rate_first_slab = fields.Monetary(string='First Slab Price', default=45.0)
    rate_additional_slab = fields.Monetary(string='Each Extra Slab', default=40.0)
    rate_cod_fixed = fields.Monetary(string='COD Fee (min)', default=35.0)
    rate_cod_percent = fields.Float(string='COD Fee (%)', default=1.75)
    rate_fuel_percent = fields.Float(string='Fuel Surcharge (%)', default=0.0)
    rate_card_days = fields.Integer(string='Usual Transit (days)', default=4)
    max_weight_kg = fields.Float(string='Max Weight (kg)', help='Leave 0 for no limit.')

    # Computed helpers
    can_rate = fields.Boolean(compute='_compute_capabilities')
    can_label = fields.Boolean(compute='_compute_capabilities')
    can_track = fields.Boolean(compute='_compute_capabilities')
    can_cancel = fields.Boolean(compute='_compute_capabilities')
    can_pickup = fields.Boolean(compute='_compute_capabilities')
    can_ndr = fields.Boolean(compute='_compute_capabilities')
    can_webhook = fields.Boolean(compute='_compute_capabilities')
    capability_summary = fields.Char(compute='_compute_capabilities')
    visible_fields = fields.Char(compute='_compute_capabilities')
    default_tracking_url = fields.Char(compute='_compute_capabilities')
    default_base_url = fields.Char(compute='_compute_capabilities')
    webhook_token = fields.Char(copy=False, readonly=True, groups=MANAGER,
                                default=lambda self: secrets.token_urlsafe(24))
    webhook_url = fields.Char(compute='_compute_webhook_url', groups=MANAGER)
    initials = fields.Char(compute='_compute_initials')

    parcel_ids = fields.One2many('cp.parcel', 'account_id')
    parcel_count = fields.Integer(compute='_compute_stats')
    active_parcel_count = fields.Integer(compute='_compute_stats')
    delivered_pct = fields.Integer(compute='_compute_stats', string='Delivered %')
    rto_pct = fields.Integer(compute='_compute_stats', string='Returned %')
    avg_cost = fields.Monetary(compute='_compute_stats', string='Average Cost')

    @api.depends('provider')
    def _compute_capabilities(self):
        labels = [('rate', _('Live prices')), ('label', _('Labels')), ('track', _('Tracking')),
                  ('cancel', _('Cancel')), ('pickup', _('Pickups')), ('ndr', _('Failed delivery actions')),
                  ('webhook', _('Push updates'))]
        for acc in self:
            cls = PROVIDERS.get(acc.provider)
            caps = cls.capabilities if cls else {}
            for key in ('rate', 'label', 'track', 'cancel', 'pickup', 'ndr', 'webhook'):
                acc['can_%s' % key] = bool(caps.get(key))
            acc.capability_summary = ' · '.join(label for key, label in labels if caps.get(key))
            acc.visible_fields = ','.join(sorted(PROVIDER_FIELDS.get(acc.provider, set())))
            acc.default_tracking_url = cls.tracking_url if cls else ''
            acc.default_base_url = (cls.base_urls.get('production' if acc.environment == 'production' else 'test')
                                    if cls else '')

    @api.depends('provider', 'name')
    def _compute_initials(self):
        for acc in self:
            words = (acc.courier_display_name or acc.name or '?').replace('(', ' ').split()
            acc.initials = ''.join(w[0] for w in words[:2]).upper()

    def _compute_webhook_url(self):
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        for acc in self:
            token = acc.sudo().webhook_token
            acc.webhook_url = '%s/cp/courier/webhook/%s/%s' % (base, acc.id, token) if acc.id and token else ''

    def _compute_stats(self):
        Parcel = self.env['cp.parcel']
        groups = Parcel._read_group([('account_id', 'in', self.ids), ('state', '!=', 'draft')],
                                    ['account_id', 'state'], ['__count', 'shipping_cost:sum'])
        stats = {}
        for account, state, count, cost in groups:
            entry = stats.setdefault(account.id, {'all': 0, 'delivered': 0, 'returned': 0, 'active': 0, 'cost': 0.0})
            entry['all'] += count
            entry['cost'] += cost or 0.0
            if state == 'delivered':
                entry['delivered'] += count
            elif state in ('returned', 'rto'):
                entry['returned'] += count
            if state not in ('delivered', 'returned', 'cancelled', 'lost'):
                entry['active'] += count
        for acc in self:
            entry = stats.get(acc.id, {'all': 0, 'delivered': 0, 'returned': 0, 'active': 0, 'cost': 0.0})
            closed = entry['delivered'] + entry['returned']
            acc.parcel_count = entry['all']
            acc.active_parcel_count = entry['active']
            acc.delivered_pct = round(entry['delivered'] * 100 / closed) if closed else 0
            acc.rto_pct = round(entry['returned'] * 100 / closed) if closed else 0
            acc.avg_cost = entry['cost'] / entry['all'] if entry['all'] else 0.0

    @api.onchange('provider')
    def _onchange_provider(self):
        defaults = {'delhivery': 'Surface', 'bluedart': 'A', 'dtdc': 'B2C PRIORITY'}
        self.service_code = defaults.get(self.provider, False)
        if self.provider == 'clickpost' and not self.courier_partner_id:
            self.courier_partner_id = '69'
            self.courier_display_name = self.courier_display_name or 'Shree Maruti Courier'
        if not self.name:
            self.name = dict(PROVIDER_SELECTION).get(self.provider)

    # ------------------------------------------------------------------
    # Helpers used by parcels and adapters
    # ------------------------------------------------------------------
    def _cp_provider(self):
        self.ensure_one()
        return get_provider(self)

    def _cp_tracking_url(self, awb):
        self.ensure_one()
        template = self.tracking_url_template or self.default_tracking_url
        if not template or not awb:
            return False
        return template.replace('{awb}', awb).replace('<shipmenttrackingnumber>', awb)

    def _cp_next_awb(self):
        self.ensure_one()
        return self.awb_sequence_id.next_by_id() if self.awb_sequence_id else False

    def _cp_rate_card_price(self, parcel):
        """Slab price from the account's rate card, used when the courier offers no price API."""
        self.ensure_one()
        weight = max(parcel.chargeable_weight_kg, 0.001)
        slab = self.rate_slab_kg or 0.5
        slabs = max(math.ceil(round(weight / slab, 6)), 1)
        price = self.rate_first_slab + (slabs - 1) * self.rate_additional_slab
        price *= 1 + (self.rate_fuel_percent or 0.0) / 100.0
        if parcel.payment_mode == 'cod':
            price += max(self.rate_cod_fixed, parcel.cod_amount * (self.rate_cod_percent or 0.0) / 100.0)
        return round(price, 2)

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def action_test_connection(self):
        self.ensure_one()
        try:
            message = self._cp_provider().test_connection()
        except UserError as err:
            self.write({'state': 'error', 'last_check_message': str(err.args[0] if err.args else err)[:250],
                        'last_check_at': fields.Datetime.now()})
            raise
        self.write({'state': 'ok', 'last_check_message': message[:250], 'last_check_at': fields.Datetime.now()})
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'type': 'success', 'title': self.name, 'message': message, 'sticky': False}}

    def action_toggle_environment(self):
        for acc in self:
            acc.sudo().write({'environment': 'production' if acc.environment == 'test' else 'test',
                              'token': False, 'token_expiry': False, 'state': 'new'})

    def action_regenerate_webhook(self):
        for acc in self:
            acc.sudo().webhook_token = secrets.token_urlsafe(24)

    def action_view_parcels(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('cargo_pilot_tms.action_cp_parcel')
        action['domain'] = [('account_id', '=', self.id)]
        action['context'] = {'default_account_id': self.id}
        return action

    def action_view_logs(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('cargo_pilot_tms.action_cp_courier_log')
        action['domain'] = [('account_id', '=', self.id)]
        return action


class CpCourierLog(models.Model):
    _name = 'cp.courier.log'
    _description = 'Courier API Log'
    _order = 'id desc'

    account_id = fields.Many2one('cp.courier.account', ondelete='cascade', index=True)
    provider = fields.Selection(related='account_id.provider')
    operation = fields.Char()
    method = fields.Char()
    url = fields.Char()
    request_body = fields.Text()
    response_body = fields.Text()
    status_code = fields.Integer()
    duration_ms = fields.Integer(string='Time (ms)')
    success = fields.Boolean()
    company_id = fields.Many2one('res.company')

    @api.autovacuum
    def _gc_old_logs(self):
        limit = fields.Datetime.subtract(fields.Datetime.now(), days=30)
        self.sudo().search([('create_date', '<', limit)]).unlink()
