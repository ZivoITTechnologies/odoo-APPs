# -*- coding: utf-8 -*-
"""EasyEcom (OMS / WMS for marketplaces, modern retail and D2C) connector.

EasyEcom needs two credentials on every call: ``x-api-key`` and a JWT (``Authorization: Bearer``)
obtained from POST /access/token {email, password, location_key}. The JWT lasts about 90 days.

Directions:
* EasyEcom -> Cargo Pilot: orders (pull, or order webhook), inventory (pull, or inventory webhook),
  and custom-carrier calls (create / cancel shipment) when Cargo Pilot is set up as a carrier.
* Cargo Pilot -> EasyEcom: shipment status (AWB / docket, status, ETA, delivery) for each order.

Paths and status codes differ between EasyEcom set-ups and API versions, so every path and the
status map are fields on the account.
"""
import json
import logging
import secrets
import time
from datetime import timedelta

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

DEFAULT_STATUS_MAP = {'assigned': 2, 'reported': 2, 'loading': 2, 'in_transit': 3, 'arrived': 4, 'delivered': 5,
                      'closed': 5, 'cancel': 9}
DEFAULT_CHANNEL_MAP = {'amazon': 'marketplace', 'flipkart': 'marketplace', 'myntra': 'marketplace',
                       'nykaa': 'marketplace', 'meesho': 'marketplace', 'jiomart': 'marketplace',
                       'reliance': 'modern_retail', 'dmart': 'modern_retail', 'spencer': 'modern_retail',
                       'more retail': 'modern_retail', 'lulu': 'modern_retail', 'croma': 'modern_retail',
                       'shopify': 'd2c', 'woocommerce': 'd2c', 'website': 'd2c', 'offline': 'offline',
                       'b2b': 'offline', 'stock transfer': 'stock_transfer', 'sto': 'stock_transfer'}


def pick(data, *keys, default=None):
    """First non-empty value among several possible EasyEcom field names."""
    for key in keys:
        value = data.get(key) if isinstance(data, dict) else None
        if value not in (None, '', [], {}):
            return value
    return default


def to_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class CpEasyecomAccount(models.Model):
    _name = 'cp.easyecom.account'
    _description = 'EasyEcom Connection'

    name = fields.Char(required=True, default='EasyEcom')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    provider = fields.Selection([('easyecom', 'EasyEcom'), ('simulator', 'Simulator (no real calls)')],
                                default='easyecom', required=True)
    base_url = fields.Char(string='API Address', default='https://api.easyecom.io', required=True)
    api_key = fields.Char(string='x-api-key', groups='cargo_pilot_tms.group_cp_manager')
    email = fields.Char(groups='cargo_pilot_tms.group_cp_manager')
    password = fields.Char(groups='cargo_pilot_tms.group_cp_manager')
    location_key = fields.Char(help='EasyEcom location key of the main warehouse (used to get the token).')
    jwt_token = fields.Char(groups='cargo_pilot_tms.group_cp_manager', readonly=True)
    jwt_expiry = fields.Datetime(groups='cargo_pilot_tms.group_cp_manager', readonly=True)
    inbound_token = fields.Char(string='Token for EasyEcom Calls', readonly=True, copy=False,
                                default=lambda self: secrets.token_urlsafe(24),
                                groups='cargo_pilot_tms.group_cp_manager',
                                help='EasyEcom sends this in the X-CP-Token header (or ?token=) when it calls '
                                     'Cargo Pilot as a carrier or through webhooks.')
    carrier_url = fields.Char(compute='_compute_urls', string='Carrier Address')
    webhook_order_url = fields.Char(compute='_compute_urls', string='Order Webhook')
    webhook_inventory_url = fields.Char(compute='_compute_urls', string='Inventory Webhook')

    path_token = fields.Char(default='/access/token')
    path_orders = fields.Char(default='/orders/V2/getAllOrders')
    path_inventory = fields.Char(default='/getInventoryDetailsV3')
    path_tracking = fields.Char(default='/Carrier/updateTrackingStatus')
    status_map = fields.Text(default=lambda self: json.dumps(DEFAULT_STATUS_MAP, indent=1),
                             help='JSON: Cargo Pilot shipment status -> EasyEcom shipment status id.')
    channel_map = fields.Text(default=lambda self: json.dumps(DEFAULT_CHANNEL_MAP, indent=1),
                              help='JSON: word in the marketplace name -> channel (marketplace, modern_retail, '
                                   'offline, d2c, stock_transfer).')
    order_status_filter = fields.Char(string='Pull Order Status IDs', default='1,2',
                                      help='EasyEcom status ids to pull (for example 1 = open, 2 = confirmed).')
    pull_orders = fields.Boolean(string='Pull Orders', default=True)
    pull_inventory = fields.Boolean(string='Pull Inventory', default=True)
    push_tracking = fields.Boolean(string='Send Shipment Status', default=True)
    carrier_mode = fields.Selection([('ltl', 'Part load (LTL)'), ('ftl', 'Full truck (FTL)')], default='ltl',
                                    string='Shipments Booked by EasyEcom')
    plan_grouping = fields.Selection([('city', 'Same city (multi-drop load)'), ('pincode', 'Same PIN code only'),
                                      ('order', 'One load request per order')], default='city', required=True,
                                     string='Combine Orders By',
                                     help='How Plan Transport groups orders that leave the same warehouse.')
    plan_split_channel = fields.Boolean(string='Separate Loads per Channel',
                                        help='Keep marketplace, D2C and B2B orders on different loads even when they '
                                             'go to the same place.')
    last_order_sync = fields.Datetime(readonly=True)
    warehouse_ids = fields.One2many('cp.easyecom.warehouse', 'account_id', string='Warehouses')
    state = fields.Selection([('new', 'Not checked'), ('ok', 'Connected'), ('error', 'Problem')], default='new',
                             readonly=True)
    last_message = fields.Char(readonly=True)
    order_count = fields.Integer(compute='_compute_counts')
    stock_count = fields.Integer(compute='_compute_counts')

    def _compute_urls(self):
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        for acc in self:
            acc.carrier_url = '%s/cp/easyecom/carrier' % base
            acc.webhook_order_url = '%s/cp/easyecom/webhook/order' % base
            acc.webhook_inventory_url = '%s/cp/easyecom/webhook/inventory' % base

    def _compute_counts(self):
        for acc in self:
            acc.order_count = self.env['cp.channel.order'].search_count([('account_id', '=', acc.id)])
            acc.stock_count = self.env['cp.channel.stock'].search_count([('account_id', '=', acc.id)])

    @api.constrains('status_map', 'channel_map')
    def _check_maps(self):
        for acc in self:
            for value in (acc.status_map, acc.channel_map):
                try:
                    if not isinstance(json.loads(value or '{}'), dict):
                        raise ValueError
                except ValueError:
                    raise UserError(_('The status and channel maps must be JSON objects.'))

    @api.model
    def cp_for(self, company):
        return self.search([('company_id', '=', company.id)], limit=1)

    @api.model
    def cp_by_token(self, token):
        if not token or len(token) < 16:
            return self.browse()
        return self.sudo().search([('inbound_token', '=', token)], limit=1)

    def action_new_token(self):
        for acc in self:
            acc.sudo().inbound_token = secrets.token_urlsafe(24)
        return True

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def _log(self, operation, method, url, body, status, answer, started, ok, record=None):
        mask = [v for v in (self.sudo().api_key, self.sudo().password, self.sudo().jwt_token) if v and len(v) > 3]

        def clean(text):
            text = text if isinstance(text, str) else json.dumps(text, default=str)
            for secret in mask:
                text = text.replace(secret, '***')
            return text[:20000]

        self.env['cp.easyecom.log'].sudo().create({
            'account_id': self.id, 'operation': operation, 'method': method, 'url': clean(url),
            'request_body': clean(body or ''), 'response_body': clean(answer or ''), 'status_code': status or 0,
            'duration_ms': int((time.time() - started) * 1000), 'success': ok,
            'res_model': record._name if record else False, 'res_id': record.id if record else 0})

    def _jwt(self):
        acc = self.sudo()
        now = fields.Datetime.now()
        if acc.jwt_token and acc.jwt_expiry and acc.jwt_expiry > now:
            return acc.jwt_token
        if not (acc.email and acc.password and acc.api_key):
            raise UserError(_('Enter the EasyEcom x-api-key, e-mail and password.'))
        data = self._call('token', 'POST', acc.path_token, json_body={'email': acc.email, 'password': acc.password,
                                                                       'location_key': acc.location_key or ''},
                          auth=False)
        token = ((data.get('data') or {}).get('token') or {})
        token = token.get('jwt_token') if isinstance(token, dict) else token
        token = token or data.get('jwt_token') or data.get('token')
        if not token:
            raise UserError(_('EasyEcom did not return a token: %s', data.get('message') or data))
        acc.write({'jwt_token': token, 'jwt_expiry': now + timedelta(days=80)})
        return token

    def _call(self, operation, method, path, params=None, json_body=None, record=None, auth=True, retry=True):
        self.ensure_one()
        headers = {'Content-Type': 'application/json', 'x-api-key': self.sudo().api_key or ''}
        if auth:
            headers['Authorization'] = 'Bearer %s' % self._jwt()
        url = self.base_url.rstrip('/') + path
        started = time.time()
        try:
            res = requests.request(method, url, headers=headers, params=params, json=json_body, timeout=60)
        except requests.exceptions.RequestException as err:
            self._log(operation, method, url, json_body or params, 0, str(err), started, False, record)
            raise UserError(_('EasyEcom could not be reached (%s).', err.__class__.__name__)) from err
        ok = 200 <= res.status_code < 300
        self._log(operation, method, url, json_body or params, res.status_code, res.text, started, ok, record)
        if res.status_code == 401 and auth and retry:
            self.sudo().write({'jwt_token': False, 'jwt_expiry': False})
            return self._call(operation, method, path, params, json_body, record, auth, retry=False)
        try:
            data = res.json()
        except ValueError:
            raise UserError(_('EasyEcom sent an unexpected answer (HTTP %(c)s).', c=res.status_code))
        if not ok or str(data.get('code', 200)) not in ('200', '0', '201', '1'):
            raise UserError(_('EasyEcom refused %(op)s: %(m)s', op=operation,
                              m=data.get('message') or data.get('error') or res.status_code))
        return data

    def action_test(self):
        self.ensure_one()
        if self.provider == 'simulator':
            msg = _('Simulator ready.')
        else:
            self.sudo().write({'jwt_token': False, 'jwt_expiry': False})
            self._jwt()
            msg = _('Connected to EasyEcom.')
        self.write({'state': 'ok', 'last_message': msg})
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'type': 'success', 'title': self.name, 'message': msg}}

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def _channel_for(self, marketplace):
        text = (marketplace or '').lower()
        mapping = json.loads(self.channel_map or '{}')
        for word, channel in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
            if word.lower() in text:
                return channel
        return 'marketplace'

    def action_pull_orders(self):
        total = 0
        for acc in self:
            total += acc._pull_orders()
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'type': 'info', 'title': _('EasyEcom'), 'message': _('%s order(s) received.', total)}}

    def _pull_orders(self):
        self.ensure_one()
        if self.provider == 'simulator':
            return 0
        since = (self.last_order_sync or fields.Datetime.now() - timedelta(days=3)) - timedelta(minutes=10)
        started = fields.Datetime.now()
        params = {'updated_after': fields.Datetime.to_string(since), 'limit': 250}
        if self.order_status_filter:
            params['status_id'] = self.order_status_filter
        count, pages = 0, 0
        path = self.path_orders
        while path and pages < 40:
            data = self._call('orders', 'GET', path, params=params)
            body = data.get('data') or {}
            orders = body.get('orders') if isinstance(body, dict) else body
            for order in orders or []:
                self.env['cp.channel.order'].cp_upsert(self, order)
                count += 1
            next_url = body.get('nextUrl') if isinstance(body, dict) else None
            if not next_url:
                break
            path = next_url if next_url.startswith('/') else '/' + next_url.split('/', 3)[-1]
            params, pages = None, pages + 1
        self.last_order_sync = started
        return count

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------
    def action_pull_inventory(self):
        for acc in self:
            acc._pull_inventory()
        return True

    def _pull_inventory(self):
        self.ensure_one()
        if self.provider == 'simulator':
            return 0
        count, pages, params, path = 0, 0, {'includeLocations': 1, 'limit': 500}, self.path_inventory
        while path and pages < 40:
            data = self._call('inventory', 'GET', path, params=params)
            body = data.get('data') or {}
            rows = pick(body, 'inventoryData', 'inventory', default=[]) if isinstance(body, dict) else body
            count += self.env['cp.channel.stock'].cp_apply(self, rows or [])
            next_url = body.get('nextUrl') if isinstance(body, dict) else None
            if not next_url:
                break
            path = next_url if next_url.startswith('/') else '/' + next_url.split('/', 3)[-1]
            params, pages = None, pages + 1
        return count

    # ------------------------------------------------------------------
    # Tracking push
    # ------------------------------------------------------------------
    def _tracking_body(self, order):
        trip = order.trip_id
        mapping = json.loads(self.status_map or '{}')
        status = mapping.get(trip.state)
        if status is None:
            return None
        body = {'awb': order.awb_number or trip.docket_number or trip.name, 'current_shipment_status_id': status,
                'shipment_status': trip.state, 'invoice_id': order.invoice_id or '',
                'order_id': order.external_id or '', 'reference_code': order.reference or '',
                'carrier_name': trip.transporter_id.name or '', 'vehicle_number': trip.vehicle_number or '',
                'tracking_url': trip.tracking_url or ''}
        if trip.eta:
            body['estimated_delivery_date'] = fields.Datetime.to_string(trip.eta)
        if trip.actual_delivery:
            body['delivery_date'] = fields.Datetime.to_string(trip.actual_delivery)
        if trip.last_ping_time and trip.ping_ids[:1].place:
            body['current_location'] = trip.ping_ids[:1].place
        return body

    @api.model
    def _cron_sync(self):
        for acc in self.search([]):
            for flag, method in (('pull_orders', acc._pull_orders), ('pull_inventory', acc._pull_inventory)):
                if acc[flag]:
                    try:
                        method()
                    except UserError as err:
                        acc.write({'state': 'error', 'last_message': str(err.args[0] if err.args else err)[:250]})
            if acc.push_tracking:
                self.env['cp.channel.order'].search([('account_id', '=', acc.id), ('tracking_dirty', '=', True)],
                                                    limit=300).cp_push_tracking()
        return True


class CpEasyecomWarehouse(models.Model):
    _name = 'cp.easyecom.warehouse'
    _description = 'EasyEcom Warehouse'

    account_id = fields.Many2one('cp.easyecom.account', required=True, ondelete='cascade')
    name = fields.Char(required=True)
    location_key = fields.Char(required=True, help='EasyEcom location key or warehouse id.')
    location_id = fields.Many2one('cp.location', string='Cargo Pilot Location', required=True)

    _key_unique = models.Constraint('unique(account_id, location_key)', 'Each EasyEcom warehouse once.')


class CpEasyecomLog(models.Model):
    _name = 'cp.easyecom.log'
    _description = 'EasyEcom Request Log'
    _order = 'id desc'

    account_id = fields.Many2one('cp.easyecom.account', ondelete='cascade', index=True)
    company_id = fields.Many2one(related='account_id.company_id', store=True)
    operation = fields.Char()
    method = fields.Char()
    url = fields.Char()
    request_body = fields.Text()
    response_body = fields.Text()
    status_code = fields.Integer()
    duration_ms = fields.Integer(string='Time (ms)')
    success = fields.Boolean()
    res_model = fields.Char()
    res_id = fields.Integer()

    @api.autovacuum
    def _gc_old(self):
        self.sudo().search([('create_date', '<', fields.Datetime.now() - timedelta(days=30))]).unlink()


class CpEasyecomDemo(models.AbstractModel):
    _name = 'cp.easyecom.demo'
    _description = 'EasyEcom demo data'

    @api.model
    def _cp_load_demo(self):
        origin = self.env.ref('cargo_pilot_tms.demo_loc_raipur', raise_if_not_found=False)
        if not origin:
            return False
        account = self.env['cp.easyecom.account'].create({
            'name': 'EasyEcom (simulator)', 'provider': 'simulator', 'location_key': 'RPR-WH',
            'warehouse_ids': [(0, 0, {'name': 'Raipur Plant WH', 'location_key': 'RPR-WH', 'location_id': origin.id})]})
        Order = self.env['cp.channel.order']
        samples = [
            (1001, 'AMZ-402-1188231', 'Amazon.in', 'Hyderabad', 'Telangana', '500081', 85, 64000, 'SP3-0201,SP3-0202'),
            (1002, 'FK-OD43317790', 'Flipkart', 'Hyderabad', 'Telangana', '500032', 40, 21000, ''),
            (1003, 'RRL-PO-55120', 'Reliance Retail', 'Pune', 'Maharashtra', '411019', 620, 412000, ''),
            (1004, 'DMART-PO-8812', 'DMart', 'Mumbai', 'Maharashtra', '400072', 910, 588000, ''),
            (1005, 'SHOP-#10442', 'Shopify', 'Chennai', 'Tamil Nadu', '600040', 18, 33000, 'SP1-7781'),
            (1006, 'NYK-77210', 'Nykaa', 'Hyderabad', 'Telangana', '500034', 12, 9800, ''),
        ]
        for oid, ref, market, city, state, pin, kg, value, serials in samples:
            Order.cp_upsert(account, {
                'order_id': oid, 'reference_code': ref, 'invoice_number': 'EE/INV/%s' % oid, 'marketplace': market,
                'order_date': fields.Datetime.to_string(fields.Datetime.now() - timedelta(hours=oid - 990)),
                'customer_name': '%s customer' % market.split('.')[0], 'city': city, 'state': state, 'pin_code': pin,
                'location_key': 'RPR-WH', 'total_amount': value, 'total_weight': kg,
                'package_count': max(int(kg / 20), 1),
                'suborders': [{'sku': 'PUMP-3HP', 'productName': 'Solar pump 3 HP', 'item_quantity': 1,
                               'serial_no': serials}]})
        self.env['cp.channel.stock'].cp_apply(account, [
            {'sku': 'PUMP-3HP', 'productName': 'Solar pump 3 HP', 'locations': [
                {'location_key': 'RPR-WH', 'location_name': 'Raipur Plant WH', 'availableInventory': 340},
                {'location_key': 'FBA-HYD8', 'location_name': 'Amazon FBA HYD8', 'availableInventory': 46},
                {'location_key': 'FK-MUM', 'location_name': 'Flipkart Bhiwandi', 'availableInventory': 31}]},
            {'sku': 'CTRL-1', 'productName': 'Pump controller', 'locations': [
                {'location_key': 'RPR-WH', 'location_name': 'Raipur Plant WH', 'availableInventory': 1200},
                {'location_key': 'FBA-HYD8', 'location_name': 'Amazon FBA HYD8', 'availableInventory': 0}]}])
        return True
