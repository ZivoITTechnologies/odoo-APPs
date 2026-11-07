# -*- coding: utf-8 -*-
# Part of ZivoIT Logistics Hub. See LICENSE file for full copyright and licensing details.
"""Carrier-agnostic connector framework.

Everything in this package is plain Python (no ORM access) so every carrier
adapter can be unit tested in isolation. The Odoo layer converts records into
the normalized dataclasses defined here, calls a provider, and converts the
normalized results back into records.
"""
import base64
import json
import logging
import math
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import requests

_logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Normalized vocabulary
# --------------------------------------------------------------------------

NORMALIZED_STATUSES = [
    ('draft', 'Draft'),
    ('booked', 'Booked'),
    ('pickup_scheduled', 'Pickup Scheduled'),
    ('picked_up', 'Picked Up'),
    ('in_transit', 'In Transit'),
    ('out_for_delivery', 'Out for Delivery'),
    ('delivered', 'Delivered'),
    ('ndr', 'Delivery Failed (NDR)'),
    ('rto_initiated', 'RTO Initiated'),
    ('rto_in_transit', 'RTO In Transit'),
    ('rto_delivered', 'RTO Delivered'),
    ('cancelled', 'Cancelled'),
    ('lost', 'Lost / Damaged'),
    ('exception', 'Exception'),
]
STATUS_CODES = {code for code, _label in NORMALIZED_STATUSES}
FINAL_STATUSES = {'delivered', 'rto_delivered', 'cancelled', 'lost'}
# Order of progress, used so an out-of-order webhook never moves a shipment backwards.
STATUS_RANK = {
    'draft': 0, 'booked': 10, 'pickup_scheduled': 15, 'picked_up': 20, 'in_transit': 30,
    'out_for_delivery': 40, 'ndr': 45, 'exception': 45, 'delivered': 100,
    'rto_initiated': 60, 'rto_in_transit': 70, 'rto_delivered': 100,
    'cancelled': 100, 'lost': 100,
}

OPERATIONS = ['auth', 'serviceability', 'rate', 'create', 'label', 'track', 'cancel', 'pickup', 'ndr']

SENSITIVE_KEYS = re.compile(
    r'(authorization|api[-_]?key|token|password|secret|licen[cs]e[-_]?key|client[-_]?secret|jwt)', re.I)


class ProviderError(Exception):
    """Raised for any carrier side failure. ``retryable`` drives the job queue."""

    def __init__(self, message, *, status_code=None, retryable=False, payload=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.payload = payload


class NotSupported(ProviderError):
    def __init__(self, provider, operation):
        super().__init__('%s does not support "%s" through its API.' % (provider, operation))
        self.operation = operation


# --------------------------------------------------------------------------
# Normalized data objects
# --------------------------------------------------------------------------

@dataclass
class Address:
    name: str = ''
    company: str = ''
    street: str = ''
    street2: str = ''
    city: str = ''
    state: str = ''
    state_code: str = ''
    zip: str = ''
    country_code: str = 'IN'
    phone: str = ''
    email: str = ''

    @property
    def full_street(self):
        return ', '.join(p for p in (self.street, self.street2) if p)

    @property
    def clean_phone(self):
        digits = re.sub(r'\D', '', self.phone or '')
        if self.country_code == 'IN' and len(digits) > 10 and digits.startswith('91'):
            digits = digits[-10:]
        if len(digits) == 11 and digits.startswith('0'):
            digits = digits[1:]
        return digits


@dataclass
class Parcel:
    weight_kg: float = 0.5
    length_cm: float = 10.0
    width_cm: float = 10.0
    height_cm: float = 10.0
    declared_value: float = 0.0
    description: str = ''
    reference: str = ''


@dataclass
class Item:
    sku: str = ''
    name: str = ''
    qty: float = 1.0
    unit_price: float = 0.0
    hsn: str = ''
    weight_kg: float = 0.0


@dataclass
class ShipmentRequest:
    reference: str
    pickup: Address
    delivery: Address
    parcels: list = field(default_factory=list)
    items: list = field(default_factory=list)
    payment_mode: str = 'prepaid'  # prepaid | cod
    cod_amount: float = 0.0
    declared_value: float = 0.0
    currency: str = 'INR'
    service_code: str = ''
    pickup_location_code: str = ''
    return_address: Address = None
    invoice_number: str = ''
    invoice_date: str = ''
    ewaybill: str = ''
    order_date: datetime = None
    is_return: bool = False
    volumetric_divisor: float = 5000.0
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.parcels:
            self.parcels = [Parcel()]
        if not self.order_date:
            self.order_date = datetime.now()

    @property
    def is_cod(self):
        return self.payment_mode == 'cod'

    @property
    def total_weight(self):
        return round(sum(p.weight_kg or 0.0 for p in self.parcels), 3)

    @property
    def volumetric_weight(self):
        divisor = self.volumetric_divisor or 5000.0
        return round(sum((p.length_cm * p.width_cm * p.height_cm) / divisor for p in self.parcels), 3)

    @property
    def chargeable_weight(self):
        return max(self.total_weight, self.volumetric_weight, 0.001)

    @property
    def max_dimensions(self):
        p = max(self.parcels, key=lambda x: x.length_cm * x.width_cm * x.height_cm)
        return p.length_cm, p.width_cm, p.height_cm

    def as_context(self):
        """Flat dict used by template driven (generic) connectors."""
        length, width, height = self.max_dimensions
        ctx = {
            'reference': self.reference,
            'payment_mode': self.payment_mode,
            'is_cod': self.is_cod,
            'cod_amount': round(self.cod_amount, 2),
            'declared_value': round(self.declared_value, 2),
            'currency': self.currency,
            'service_code': self.service_code,
            'pickup_location_code': self.pickup_location_code,
            'weight_kg': self.chargeable_weight,
            'weight_grams': int(round(self.chargeable_weight * 1000)),
            'actual_weight_kg': self.total_weight,
            'length_cm': length, 'width_cm': width, 'height_cm': height,
            'pieces': len(self.parcels),
            'invoice_number': self.invoice_number,
            'invoice_date': self.invoice_date,
            'ewaybill': self.ewaybill,
            'order_date': self.order_date.strftime('%Y-%m-%d %H:%M'),
            'order_date_iso': self.order_date.isoformat(),
            'items_description': ', '.join(i.name for i in self.items)[:200] or 'Goods',
            'items_qty': int(sum(i.qty for i in self.items) or 1),
            'items': [asdict(i) for i in self.items],
        }
        for prefix, addr in (('pickup', self.pickup), ('delivery', self.delivery),
                             ('return', self.return_address or self.pickup)):
            data = asdict(addr)
            data['full_street'] = addr.full_street
            data['clean_phone'] = addr.clean_phone
            ctx[prefix] = data
        return ctx


@dataclass
class RateOption:
    service_code: str
    service_name: str = ''
    courier_name: str = ''
    price: float = 0.0
    currency: str = 'INR'
    tat_days: float = 0.0
    cod_available: bool = True
    source: str = 'api'  # api | rate_card
    extra: dict = field(default_factory=dict)


@dataclass
class ServiceabilityResult:
    zip: str
    serviceable: bool
    cod: bool = False
    prepaid: bool = False
    pickup: bool = False
    tat_days: float = 0.0
    message: str = ''


@dataclass
class ShipmentResult:
    tracking_number: str
    carrier_reference: str = ''
    price: float = 0.0
    currency: str = 'INR'
    service_code: str = ''
    courier_name: str = ''
    label_pdf: bytes = None
    label_url: str = ''
    edd: datetime = None
    raw: dict = field(default_factory=dict)


@dataclass
class TrackingEvent:
    time: datetime
    raw_status: str
    description: str = ''
    location: str = ''
    normalized: str = None


@dataclass
class TrackingResult:
    tracking_number: str
    raw_status: str = ''
    normalized: str = None
    description: str = ''
    events: list = field(default_factory=list)
    edd: datetime = None
    delivered_at: datetime = None
    charged_weight: float = 0.0
    ndr_reason: str = ''
    found: bool = True


@dataclass
class LabelResult:
    pdf: bytes = None
    url: str = ''
    filename: str = ''


@dataclass
class ProviderConfig:
    """Everything a provider needs, built by ``zivo.logistics.account``."""
    environment: str = 'sandbox'
    base_url: str = ''
    username: str = ''
    password: str = ''
    api_key: str = ''
    client_id: str = ''
    client_secret: str = ''
    account_number: str = ''
    customer_code: str = ''
    license_key: str = ''
    pickup_location_code: str = ''
    default_service_code: str = ''
    token: str = ''
    token_expiry: datetime = None
    extra: dict = field(default_factory=dict)
    endpoints: dict = field(default_factory=dict)  # generic connector: operation -> endpoint dict
    auth_style: str = 'none'
    auth_header_name: str = ''


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def mask_secrets(obj):
    """Recursively mask secret values in headers / payloads before logging."""
    if isinstance(obj, dict):
        return {k: ('***' if SENSITIVE_KEYS.search(str(k)) and v else mask_secrets(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask_secrets(v) for v in obj]
    return obj


def json_path(data, path, default=None):
    """Tiny JSON-path: ``data.0.awb`` or ``response.data.awb_code``."""
    if not path:
        return default
    cur = data
    for part in str(path).split('.'):
        if cur is None:
            return default
        if isinstance(cur, list):
            if part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            elif part == '*':
                return cur
            else:
                return default
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return default
    return default if cur is None else cur


DATE_FORMATS = (
    '%Y-%m-%dT%H:%M:%S.%f%z', '%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S',
    '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d', '%d-%m-%Y %H:%M:%S', '%d-%m-%Y %H:%M',
    '%d-%m-%Y', '%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%d/%m/%Y', '%d %b %Y %H:%M', '%d %b %Y',
    '%d%m%Y%H%M', '%d%m%Y',
)


def parse_datetime(value):
    """Best effort parsing of the many date formats Indian carriers return. Returns naive UTC-ish."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, (int, float)):
        ts = value / 1000.0 if value > 1e11 else value
        return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)
    text = str(value).strip().replace('Z', '+0000')
    # Python < 3.11 cannot parse "+05:30" with %z, normalise it.
    text = re.sub(r'([+-]\d\d):(\d\d)$', r'\1\2', text)
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if dt.tzinfo:
            dt = (dt - dt.utcoffset()).replace(tzinfo=None)
        return dt
    return None


def ceil_slab(weight, slab):
    return math.ceil(round(weight / slab, 6)) * slab if slab else weight


def to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('y', 'yes', 'true', '1', 't')


# --------------------------------------------------------------------------
# HTTP client with logging, masking and retries
# --------------------------------------------------------------------------

class HttpClient:
    RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, provider_code, log_callback=None, timeout=30, max_retries=2, backoff=1.0):
        self.provider_code = provider_code
        self.log_callback = log_callback
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.session = requests.Session()

    def request(self, method, url, *, operation='', params=None, json_body=None, data=None,
                headers=None, expect='json', ok_statuses=None):
        headers = dict(headers or {})
        headers.setdefault('Accept', 'application/json' if expect == 'json' else '*/*')
        attempt = 0
        while True:
            attempt += 1
            started = time.time()
            response, error = None, None
            try:
                response = self.session.request(
                    method, url, params=params, json=json_body, data=data, headers=headers, timeout=self.timeout)
            except requests.exceptions.RequestException as exc:
                error = exc
            duration = int((time.time() - started) * 1000)
            body = self._parse(response, expect) if response is not None else None
            success = error is None and response is not None and (
                response.status_code in (ok_statuses or ()) or 200 <= response.status_code < 300)
            self._log(operation, method, url, params, json_body if json_body is not None else data, headers,
                      response, body, duration, success, error)
            retryable = error is not None or (response is not None and response.status_code in self.RETRY_STATUS)
            if success:
                return body
            if retryable and attempt <= self.max_retries:
                time.sleep(self.backoff * attempt)
                continue
            if error is not None:
                raise ProviderError('%s: connection error (%s)' % (self.provider_code, error), retryable=True)
            raise ProviderError(
                '%s %s failed (HTTP %s): %s' % (self.provider_code, operation or url, response.status_code,
                                                self.extract_error(body)),
                status_code=response.status_code, retryable=retryable, payload=body)

    @staticmethod
    def _parse(response, expect):
        if expect == 'bytes':
            ctype = response.headers.get('Content-Type', '')
            if 'json' in ctype:
                try:
                    return response.json()
                except ValueError:
                    pass
            return response.content
        try:
            return response.json()
        except ValueError:
            return response.text

    @staticmethod
    def extract_error(body):
        if isinstance(body, dict):
            for key in ('message', 'error', 'errors', 'rmk', 'remarks', 'error_message', 'detail', 'Message'):
                if body.get(key):
                    value = body[key]
                    if isinstance(value, (list, dict)):
                        value = json.dumps(value)[:500]
                    return str(value)[:500]
            return json.dumps(body)[:500]
        if isinstance(body, bytes):
            return '<binary %d bytes>' % len(body)
        return str(body)[:500]

    def _log(self, operation, method, url, params, payload, headers, response, body, duration, success, error):
        if not self.log_callback:
            return
        try:
            if isinstance(payload, (dict, list)):
                req_text = json.dumps(mask_secrets(payload), default=str)
            elif isinstance(payload, str) and payload.startswith(('format=', '{')):
                req_text = payload
            else:
                req_text = str(mask_secrets(payload) if payload else '')
            if isinstance(body, bytes):
                resp_text = '<binary %d bytes>' % len(body)
            elif isinstance(body, (dict, list)):
                resp_text = json.dumps(mask_secrets(body), default=str)
            else:
                resp_text = str(body or '')
            self.log_callback({
                'provider': self.provider_code,
                'operation': operation,
                'method': method,
                'url': url + ('?' + '&'.join('%s=%s' % (k, '***' if SENSITIVE_KEYS.search(k) else v)
                                             for k, v in (params or {}).items()) if params else ''),
                'request_headers': json.dumps(mask_secrets(headers)),
                'request_body': req_text[:20000],
                'response_status': response.status_code if response is not None else 0,
                'response_body': resp_text[:20000],
                'duration_ms': duration,
                'success': success,
                'error': str(error) if error else ('' if success else self.extract_error(body)),
            })
        except Exception:  # logging must never break a shipment
            _logger.exception('ZivoIT Logistics Hub: unable to log API call')


# --------------------------------------------------------------------------
# Provider base + registry
# --------------------------------------------------------------------------

PROVIDERS = {}


def register(cls):
    PROVIDERS[cls.code] = cls
    return cls


def get_provider_class(code):
    if code not in PROVIDERS:
        raise ProviderError('Unknown logistics provider "%s"' % code)
    return PROVIDERS[code]


class BaseProvider:
    code = None
    name = None
    country = 'IN'
    aggregator = False
    sandbox_url = ''
    production_url = ''
    tracking_url = ''  # use {awb}
    capabilities = set()
    credential_fields = ()  # used by the UI to show only relevant credentials
    token_lifetime = timedelta(hours=23)
    STATUS_MAP = {}

    def __init__(self, config, http=None, token_callback=None):
        self.config = config if isinstance(config, ProviderConfig) else ProviderConfig(**(config or {}))
        self.http = http or HttpClient(self.code)
        self.token_callback = token_callback

    # ---------------- generic helpers ----------------
    @property
    def base_url(self):
        url = self.config.base_url or (
            self.production_url if self.config.environment == 'production' else (self.sandbox_url or self.production_url))
        return (url or '').rstrip('/')

    def url(self, path):
        return path if path.startswith('http') else '%s/%s' % (self.base_url, path.lstrip('/'))

    def supports(self, operation):
        return operation in self.capabilities

    def require(self, operation):
        if not self.supports(operation):
            raise NotSupported(self.name, operation)

    def require_fields(self, *names):
        missing = [n for n in names if not getattr(self.config, n, None)]
        if missing:
            raise ProviderError('%s: missing credentials: %s' % (self.name, ', '.join(missing)))

    def normalize_status(self, raw_status, description=''):
        """Provider map first; ``None`` means unknown (Odoo layer then asks the status map / AI)."""
        if raw_status is None:
            return None
        key = str(raw_status).strip().lower()
        return self.STATUS_MAP.get(key)

    def get_token(self, force=False):
        cfg = self.config
        if not force and cfg.token and cfg.token_expiry and cfg.token_expiry > utcnow() + timedelta(minutes=5):
            return cfg.token
        token, lifetime = self.authenticate()
        cfg.token = token
        cfg.token_expiry = utcnow() + (lifetime or self.token_lifetime)
        if self.token_callback:
            self.token_callback(cfg.token, cfg.token_expiry)
        return token

    def tracking_link(self, awb):
        return self.tracking_url.format(awb=awb) if self.tracking_url and awb else ''

    # ---------------- operations (override) ----------------
    def authenticate(self):
        """Return (token, lifetime timedelta)."""
        return '', None

    def test_connection(self):
        if self.supports('auth'):
            self.get_token(force=True)
            return '%s: authentication successful.' % self.name
        return '%s: credentials stored (this carrier has no login endpoint to test).' % self.name

    def check_serviceability(self, pickup_zip, delivery_zip, weight_kg=0.5, cod=False):
        raise NotSupported(self.name, 'serviceability')

    def get_rates(self, request):
        raise NotSupported(self.name, 'rate')

    def create_shipment(self, request):
        raise NotSupported(self.name, 'create')

    def get_label(self, tracking_number, carrier_reference=''):
        raise NotSupported(self.name, 'label')

    def track(self, tracking_numbers):
        raise NotSupported(self.name, 'track')

    def cancel(self, tracking_number, carrier_reference=''):
        raise NotSupported(self.name, 'cancel')

    def schedule_pickup(self, tracking_numbers, pickup_date, carrier_references=None, package_count=1):
        raise NotSupported(self.name, 'pickup')

    def ndr_action(self, tracking_number, action, data=None):
        raise NotSupported(self.name, 'ndr')

    def parse_webhook(self, payload, headers=None):
        """Return a list of TrackingResult built from a push notification."""
        raise NotSupported(self.name, 'webhook')

    # ---------------- shared builders ----------------
    def _result_from_events(self, awb, events, raw_status='', description='', **kwargs):
        events = sorted([e for e in events if e.time], key=lambda e: e.time) + [e for e in events if not e.time]
        for ev in events:
            if ev.normalized is None:
                ev.normalized = self.normalize_status(ev.raw_status, ev.description)
        if not raw_status and events:
            raw_status = events[-1].raw_status
            description = description or events[-1].description
        normalized = self.normalize_status(raw_status, description)
        delivered_at = kwargs.pop('delivered_at', None)
        if normalized == 'delivered' and not delivered_at:
            delivered = [e.time for e in events if e.normalized == 'delivered' and e.time]
            delivered_at = delivered[-1] if delivered else None
        return TrackingResult(tracking_number=awb, raw_status=raw_status or '', normalized=normalized,
                              description=description or '', events=events, delivered_at=delivered_at, **kwargs)


def b64_to_bytes(value):
    if not value:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, list):  # Blue Dart returns a byte array
        return bytes(value)
    return base64.b64decode(value)
