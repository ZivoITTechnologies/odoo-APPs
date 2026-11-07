# -*- coding: utf-8 -*-
"""Template driven "connect any courier" engine + Shree Maruti preset.

Each operation (auth, serviceability, rate, create, label, track, cancel, pickup) is described by an
endpoint record: HTTP method, path, JSON body template with ``{{ placeholders }}`` and JSON paths
telling where to read the AWB, label, status, events... in the response. This lets a functional
consultant onboard a regional courier (Shree Maruti, Professional Couriers, Trackon, Gati...)
from the courier's API document without writing Python.
"""
import base64
import copy
import json
import re
from datetime import timedelta

from .base import (BaseProvider, LabelResult, ProviderError, RateOption, ServiceabilityResult, ShipmentResult,
                   TrackingEvent, TrackingResult, json_path, parse_datetime, register, to_bool)

PLACEHOLDER = re.compile(r'\{\{\s*([\w.]+)\s*\}\}')


def render_template(template, context):
    """Render a JSON (or plain string) template. ``"{{x}}"`` alone keeps the native type."""
    if template in (None, ''):
        return None
    if isinstance(template, str):
        stripped = template.strip()
        if stripped.startswith(('{', '[')):
            try:
                template = json.loads(stripped)
            except ValueError as exc:
                raise ProviderError('Invalid JSON template: %s' % exc)
        else:
            return _render_string(template, context)
    return _render_node(copy.deepcopy(template), context)


def _lookup(context, path):
    value = json_path(context, path)
    return '' if value is None else value


def _render_string(text, context):
    full = PLACEHOLDER.fullmatch(text.strip())
    if full:
        return _lookup(context, full.group(1))
    return PLACEHOLDER.sub(lambda m: str(_lookup(context, m.group(1))), text)


def _render_node(node, context):
    if isinstance(node, dict):
        return {k: _render_node(v, context) for k, v in node.items()}
    if isinstance(node, list):
        return [_render_node(v, context) for v in node]
    if isinstance(node, str):
        return _render_string(node, context)
    return node


class TemplateProvider(BaseProvider):
    """Base for template driven carriers. Endpoints live in ``config.endpoints``."""
    capabilities = {'auth', 'serviceability', 'rate', 'create', 'label', 'track', 'cancel', 'pickup'}
    credential_fields = ('username', 'password', 'api_key', 'customer_code')

    def supports(self, operation):
        if operation == 'auth':
            return self.config.auth_style == 'token_login' and 'auth' in self.config.endpoints
        return operation in self.config.endpoints

    def _endpoint(self, operation):
        ep = self.config.endpoints.get(operation)
        if not ep or not ep.get('path'):
            raise ProviderError('%s: endpoint for "%s" is not configured. Add it under Carrier Account > '
                                'API Endpoints using the courier\'s API document.' % (self.name, operation))
        return ep

    def _context(self, extra=None):
        cfg = self.config
        ctx = {'username': cfg.username, 'password': cfg.password, 'api_key': cfg.api_key,
               'customer_code': cfg.customer_code, 'account_number': cfg.account_number,
               'client_id': cfg.client_id, 'client_secret': cfg.client_secret, 'license_key': cfg.license_key,
               'pickup_location_code': cfg.pickup_location_code, 'extra': cfg.extra or {}}
        ctx.update(extra or {})
        return ctx

    def _auth_headers(self):
        cfg, style = self.config, self.config.auth_style
        header = cfg.auth_header_name or 'Authorization'
        if style == 'api_key_header':
            return {cfg.auth_header_name or 'api-key': cfg.api_key}
        if style == 'bearer_static':
            return {header: 'Bearer %s' % cfg.api_key}
        if style == 'basic':
            raw = base64.b64encode(('%s:%s' % (cfg.username, cfg.password)).encode()).decode()
            return {header: 'Basic %s' % raw}
        if style == 'token_login':
            prefix = (cfg.extra or {}).get('token_prefix', 'Bearer ')
            return {header: '%s%s' % (prefix, self.get_token())}
        return {}

    def authenticate(self):
        ep = self._endpoint('auth')
        data = self._call('auth', self._context(), ep=ep, auth=False)
        token = json_path(data, ep.get('token_path') or 'token')
        if not token:
            raise ProviderError('%s login failed: %s' % (self.name, self.http.extract_error(data)))
        hours = float(ep.get('token_lifetime_hours') or 12)
        return str(token), timedelta(hours=hours)

    def _call(self, operation, context, ep=None, auth=True, expect='json'):
        ep = ep or self._endpoint(operation)
        method = (ep.get('method') or 'POST').upper()
        path = _render_string(ep['path'], context)
        body = render_template(ep.get('body_template'), context)
        headers = {'Content-Type': ep.get('content_type') or 'application/json'}
        headers.update(render_template(ep.get('headers_template'), context) or {})
        if auth:
            headers.update(self._auth_headers())
        kwargs = {'headers': headers, 'operation': operation, 'expect': expect}
        if method == 'GET':
            kwargs['params'] = body if isinstance(body, dict) else None
        elif headers['Content-Type'].startswith('application/x-www-form-urlencoded'):
            kwargs['data'] = body
        else:
            kwargs['json_body'] = body
        data = self.http.request(method, self.url(path), **kwargs)
        success_path = ep.get('success_path')
        if success_path and isinstance(data, (dict, list)):
            flag = json_path(data, success_path)
            if flag in (None, '') or not to_bool(flag) and flag not in (200, '200'):
                err = json_path(data, ep.get('error_path') or 'message') or self.http.extract_error(data)
                raise ProviderError('%s %s failed: %s' % (self.name, operation, err), payload=data)
        return data

    # ------------------------------------------------------------ operations
    def check_serviceability(self, pickup_zip, delivery_zip, weight_kg=0.5, cod=False):
        ep = self._endpoint('serviceability')
        data = self._call('serviceability', self._context({'pickup': {'zip': pickup_zip}, 'delivery': {'zip': delivery_zip},
                                                            'weight_kg': weight_kg, 'is_cod': cod}), ep=ep)
        serviceable = to_bool(json_path(data, ep.get('serviceable_path') or 'serviceable', False))
        return ServiceabilityResult(zip=delivery_zip, serviceable=serviceable,
                                    cod=to_bool(json_path(data, ep.get('cod_path') or 'cod', serviceable)),
                                    prepaid=serviceable, pickup=serviceable,
                                    tat_days=float(json_path(data, ep.get('tat_path') or 'tat', 0) or 0))

    def get_rates(self, request):
        ep = self._endpoint('rate')
        data = self._call('rate', self._context(request.as_context()), ep=ep)
        rows = json_path(data, ep.get('list_path')) if ep.get('list_path') else [data]
        rates = []
        for row in rows or []:
            price = json_path(row, ep.get('price_path') or 'price')
            if price in (None, ''):
                continue
            rates.append(RateOption(service_code=str(json_path(row, ep.get('service_path') or 'service', '') or request.service_code or 'standard'),
                                    service_name=str(json_path(row, ep.get('service_name_path') or 'service_name', '') or self.name),
                                    courier_name=self.name, price=float(price),
                                    tat_days=float(json_path(row, ep.get('tat_path') or 'tat', 0) or 0)))
        return rates

    def create_shipment(self, request):
        ep = self._endpoint('create')
        data = self._call('create', self._context(request.as_context()), ep=ep)
        awb = json_path(data, ep.get('awb_path') or 'awb')
        if not awb:
            raise ProviderError('%s booking failed: %s' % (self.name, self.http.extract_error(data)), payload=data)
        label = self._label_from(data, ep)
        return ShipmentResult(tracking_number=str(awb),
                              carrier_reference=str(json_path(data, ep.get('reference_path') or '', '') or request.reference),
                              price=float(json_path(data, ep.get('price_path') or '', 0) or 0),
                              service_code=request.service_code, courier_name=self.name,
                              label_pdf=label.pdf, label_url=label.url, raw=data if isinstance(data, dict) else {})

    def _label_from(self, data, ep):
        value = json_path(data, ep.get('label_path') or '')
        if not value:
            return LabelResult()
        if ep.get('label_type') == 'base64':
            return LabelResult(pdf=base64.b64decode(value))
        return LabelResult(url=str(value))

    def get_label(self, tracking_number, carrier_reference=''):
        ep = self._endpoint('label')
        ctx = self._context({'awb': tracking_number, 'carrier_reference': carrier_reference})
        if ep.get('label_type') == 'pdf':
            content = self._call('label', ctx, ep=ep, expect='bytes')
            if not isinstance(content, bytes):
                raise ProviderError('%s label not available.' % self.name)
            return LabelResult(pdf=content)
        label = self._label_from(self._call('label', ctx, ep=ep), ep)
        if not (label.pdf or label.url):
            raise ProviderError('%s label not available.' % self.name)
        return label

    def track(self, tracking_numbers):
        ep = self._endpoint('track')
        results = {}
        for awb in tracking_numbers:
            data = self._call('track', self._context({'awb': awb}), ep=ep)
            raw = json_path(data, ep.get('status_path') or 'status', '')
            events = []
            for ev in json_path(data, ep.get('events_path') or 'events', []) or []:
                events.append(TrackingEvent(time=parse_datetime(json_path(ev, ep.get('event_time_path') or 'time')),
                                            raw_status=str(json_path(ev, ep.get('event_status_path') or 'status', '')),
                                            description=str(json_path(ev, ep.get('event_desc_path') or 'description', '')),
                                            location=str(json_path(ev, ep.get('event_location_path') or 'location', ''))))
            if not raw and not events:
                results[awb] = TrackingResult(tracking_number=awb, found=False)
                continue
            results[awb] = self._result_from_events(
                awb, events, raw_status=str(raw), description=str(json_path(data, ep.get('status_desc_path') or '', '') or raw),
                edd=parse_datetime(json_path(data, ep.get('edd_path') or '')))
        return results

    def cancel(self, tracking_number, carrier_reference=''):
        self._call('cancel', self._context({'awb': tracking_number, 'carrier_reference': carrier_reference}))
        return True

    def schedule_pickup(self, tracking_numbers, pickup_date, carrier_references=None, package_count=1):
        data = self._call('pickup', self._context({'awbs': list(tracking_numbers), 'pickup_date': pickup_date.strftime('%Y-%m-%d'),
                                                   'pickup_time': pickup_date.strftime('%H:%M'),
                                                   'package_count': max(package_count, len(tracking_numbers))}))
        ep = self._endpoint('pickup')
        return str(json_path(data, ep.get('reference_path') or 'pickup_id', 'scheduled'))


@register
class GenericRestProvider(TemplateProvider):
    code = 'generic'
    name = 'Any Courier (Configurable REST)'
    STATUS_MAP = {}


@register
class ShreeMarutiProvider(TemplateProvider):
    """Shree Maruti Integrated Logistics.

    Shree Maruti shares its API document and a username/password with each onboarded business
    (no public developer portal). The account is pre-filled with a login + booking + tracking +
    cancellation template; the consultant pastes the endpoint paths and confirms field names
    from the document Shree Maruti provides.
    """
    code = 'shree_maruti'
    name = 'Shree Maruti'
    tracking_url = 'https://www.shreemaruti.com/track-your-shipment/?awb={awb}'
    credential_fields = ('username', 'password', 'customer_code')
    STATUS_MAP = {
        'booked': 'booked', 'manifested': 'booked', 'pickup pending': 'pickup_scheduled',
        'picked up': 'picked_up', 'in transit': 'in_transit', 'reached at hub': 'in_transit',
        'reached destination': 'in_transit', 'out for delivery': 'out_for_delivery', 'delivered': 'delivered',
        'undelivered': 'ndr', 'not delivered': 'ndr', 'rto': 'rto_initiated', 'rto in transit': 'rto_in_transit',
        'rto delivered': 'rto_delivered', 'cancelled': 'cancelled', 'lost': 'lost',
    }
    # Starting template only: field names must be confirmed against Shree Maruti's onboarding document.
    PRESET = {
        'auth_style': 'token_login',
        'auth_header_name': 'Authorization',
        'endpoints': [
            {'operation': 'auth', 'method': 'POST', 'path': '',
             'body_template': '{"username": "{{username}}", "password": "{{password}}"}',
             'token_path': 'data.token', 'token_lifetime_hours': 12},
            {'operation': 'create', 'method': 'POST', 'path': '',
             'body_template': json.dumps({
                 'clientCode': '{{customer_code}}', 'orderNo': '{{reference}}', 'paymentMode': '{{payment_mode}}',
                 'codAmount': '{{cod_amount}}', 'invoiceValue': '{{declared_value}}', 'weight': '{{weight_kg}}',
                 'length': '{{length_cm}}', 'breadth': '{{width_cm}}', 'height': '{{height_cm}}', 'pieces': '{{pieces}}',
                 'productDesc': '{{items_description}}', 'ewaybill': '{{ewaybill}}',
                 'consignee': {'name': '{{delivery.name}}', 'address': '{{delivery.full_street}}', 'city': '{{delivery.city}}',
                               'state': '{{delivery.state}}', 'pincode': '{{delivery.zip}}', 'mobile': '{{delivery.clean_phone}}'},
                 'pickup': {'name': '{{pickup.name}}', 'address': '{{pickup.full_street}}', 'city': '{{pickup.city}}',
                            'state': '{{pickup.state}}', 'pincode': '{{pickup.zip}}', 'mobile': '{{pickup.clean_phone}}'},
             }, indent=2),
             'awb_path': 'data.awbNo', 'label_path': 'data.labelUrl', 'label_type': 'url', 'error_path': 'message'},
            {'operation': 'track', 'method': 'GET', 'path': '', 'body_template': '{"awbNo": "{{awb}}"}',
             'status_path': 'data.currentStatus', 'events_path': 'data.scans', 'event_time_path': 'dateTime',
             'event_status_path': 'status', 'event_desc_path': 'remarks', 'event_location_path': 'location'},
            {'operation': 'cancel', 'method': 'POST', 'path': '', 'body_template': '{"awbNo": "{{awb}}"}'},
        ],
    }
