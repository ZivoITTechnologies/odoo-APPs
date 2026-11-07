# -*- coding: utf-8 -*-
"""Common plumbing for courier adapters.

Every adapter receives a ``cp.courier.account`` record and exposes the same small
interface, so the rest of the module never needs to know which courier it talks to:

    rates(parcel)                 -> list of option dicts
    create_shipment(parcel, opt)  -> booking dict (awb, label, ...)
    fetch_label(parcel)           -> PDF bytes or None
    track(parcels)                -> {awb: tracking dict}
    cancel(parcel)                -> True
    request_pickup(parcels, when) -> pickup reference
    ndr_action(parcel, action, note) -> True
    parse_webhook(payload, headers)  -> {awb: tracking dict}
    test_connection()             -> short success message

Tracking dict: {'status': <normalised>, 'status_text': str, 'events': [event dicts],
                'expected_delivery': date|None, 'courier_name': str|None}
Event dict:    {'time': datetime, 'code': str, 'description': str, 'location': str,
                'status': <normalised or ''>}
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from odoo.modules import module as odoo_module

from odoo import fields, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

TIMEOUT = 30

# Normalised parcel statuses, in the order a parcel normally moves through them.
FLOW = ['draft', 'booked', 'picked_up', 'in_transit', 'out_for_delivery', 'delivered']
EXCEPTION_STATES = ['ndr', 'rto', 'returned', 'cancelled', 'lost']
TERMINAL = {'delivered', 'returned', 'cancelled', 'lost'}

_KEYWORDS = [
    # order matters: the first match wins
    ('returned', (r'rto[\s_-]*deliver', r'return(ed)?\s+to\s+(origin|shipper|seller)\s*deliver',
                  r'returned to (shipper|origin|seller)', r'rto[\s_-]*dl\b')),
    ('rto', (r'\brto\b', r'return to origin', r'returning', r'\breturn\b', r'\brts\b')),
    ('cancelled', (r'cancel',)),
    ('lost', (r'\blost\b', r'\bdamaged\b', r'shortage')),
    ('ndr', (r'undeliver', r'not deliver', r'delivery attempt', r'attempt(ed)? fail', r'failed deliver',
             r'consignee (not available|refused|unavailable)', r'customer (not available|refused|unavailable)',
             r'refused', r'\bndr\b', r'door locked', r'incomplete address', r'not reachable',
             r'premises closed', r'reschedul')),
    ('out_for_delivery', (r'out for deliver', r'\bofd\b', r'dispatched for deliver', r'with delivery (boy|agent)',
                          r'^dispatched$')),
    ('delivered', (r'deliver(ed|y done|y successful)', r'\bdl\b', r'\bpod\b')),
    ('picked_up', (r'picked', r'pick[\s-]?up done', r'pickup complete', r'in[\s-]?scan', r'shipment received',
                   r'received at (origin|pickup)', r'collected')),
    ('in_transit', (r'transit', r'arrived', r'reached', r'bagg?ed', r'connected', r'forwarded', r'departed',
                    r'dispatched', r'hub', r'out[\s-]?scan', r'shipped', r'on the way', r'received at')),
    ('booked', (r'manifest', r'booked', r'awb', r'pickup (scheduled|pending|requested|generated)',
                r'pending pickup', r'not picked', r'out for pickup', r'order placed', r'soft ?data', r'created',
                r'pickup exception', r'pickup failed')),
]
_KEYWORDS = [(state, [re.compile(p, re.I) for p in pats]) for state, pats in _KEYWORDS]


def guess_status(*texts):
    """Map free-text courier statuses (any courier, any wording) to a normalised status."""
    text = ' '.join(t for t in texts if t)
    if not text:
        return ''
    for state, patterns in _KEYWORDS:
        if any(p.search(text) for p in patterns):
            return state
    return ''


def _from_epoch(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).replace(tzinfo=None)


def parse_dt(value, formats=()):
    """Parse the many date formats couriers use. Returns a naive UTC-ish datetime or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, (int, float)):
        return _from_epoch(value / 1000 if value > 1e11 else value)
    value = str(value).strip()
    match = re.match(r'/Date\((\d+)([+-]\d{4})?\)/', value)
    if match:
        return _from_epoch(int(match.group(1)) / 1000)
    candidates = list(formats) + [
        '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M',
        '%Y-%m-%d', '%d-%m-%Y %H:%M:%S', '%d-%m-%Y %H:%M', '%d-%m-%Y', '%d/%m/%Y %H:%M:%S',
        '%d/%m/%Y %H:%M', '%d/%m/%Y', '%d-%b-%Y %H:%M', '%d-%b-%Y', '%d %b %Y %H:%M', '%d %b %Y',
        '%d%m%Y %H%M', '%d%m%Y',
    ]
    cleaned = re.sub(r'(Z|[+-]\d{2}:?\d{2})$', '', value)
    for fmt in candidates:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


class CourierError(UserError):
    """A courier refused a request or could not be reached."""


class CourierProvider:
    code = 'base'
    label = 'Courier'
    #: what this adapter can do through the courier's API
    capabilities = {'rate': False, 'serviceability': False, 'book': True, 'label': False, 'track': False,
                    'cancel': False, 'pickup': False, 'ndr': False, 'webhook': False}
    base_urls = {'test': '', 'production': ''}
    tracking_url = ''
    token_hours = 0
    secret_fields = ('api_key', 'password', 'client_secret', 'licence_key', 'token')

    def __init__(self, account):
        self.account = account
        self.env = account.env

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @property
    def base_url(self):
        acc = self.account
        if acc.base_url_override:
            return acc.base_url_override.rstrip('/')
        return self.base_urls['production' if acc.environment == 'production' else 'test'].rstrip('/')

    def _secrets(self):
        acc = self.account.sudo()
        return [v for v in (getattr(acc, f, False) for f in self.secret_fields) if v and len(str(v)) > 3]

    def _mask(self, text):
        if text is None:
            return ''
        if not isinstance(text, str):
            try:
                text = json.dumps(text, default=str)
            except (TypeError, ValueError):
                text = str(text)
        for secret in self._secrets():
            text = text.replace(str(secret), '***')
        return text[:20000]

    def _log(self, operation, method, url, request_body, status, response_body, duration, ok):
        """Write a request log in its own transaction so it survives a failed booking."""
        acc = self.account
        if ok and not acc.debug_logging:
            return
        vals = {
            'account_id': acc.id, 'operation': operation, 'method': method, 'url': self._mask(url),
            'request_body': self._mask(request_body), 'status_code': status or 0,
            'response_body': self._mask(response_body), 'duration_ms': int(duration * 1000), 'success': ok,
            'company_id': acc.company_id.id,
        }
        if odoo_module.current_test or not acc.id:
            # tests share one transaction; an unsaved account cannot be referenced from another cursor
            self.env['cp.courier.log'].sudo().create(vals)
            return
        try:
            with self.env.registry.cursor() as cr:
                self.env(cr=cr, su=True)['cp.courier.log'].create(vals)
        except Exception:  # pragma: no cover - logging must never break shipping
            _logger.warning('Could not store courier log for %s', acc.display_name)

    def _request(self, operation, method, url, *, headers=None, params=None, json_body=None, data=None,
                 expect='json', auth=True, retry_auth=True, timeout=TIMEOUT):
        base_headers = dict(headers or {})
        headers = dict(base_headers)
        if auth:
            headers.update(self._auth_headers())
        started = time.time()
        body_for_log = json_body if json_body is not None else data
        try:
            response = requests.request(method, url, headers=headers, params=params, json=json_body,
                                        data=data, timeout=timeout)
        except requests.exceptions.RequestException as err:
            self._log(operation, method, url, body_for_log, 0, str(err), time.time() - started, False)
            raise CourierError(_('%(courier)s could not be reached (%(error)s). Check the internet '
                                 'connection of the server and try again.',
                                 courier=self.label, error=err.__class__.__name__)) from err
        duration = time.time() - started
        status = response.status_code
        if status in (401, 403) and auth and retry_auth and self.token_hours:
            self._log(operation, method, url, body_for_log, status, response.text, duration, False)
            self.account.sudo().write({'token': False, 'token_expiry': False})
            return self._request(operation, method, url, headers=base_headers, params=params, json_body=json_body, data=data, expect=expect, auth=auth,
                                 retry_auth=False, timeout=timeout)
        ok = 200 <= status < 300
        log_body = response.text if expect != 'bytes' else '<%s bytes>' % len(response.content)
        self._log(operation, method, url, body_for_log, status, log_body, duration, ok)
        if not ok:
            raise CourierError(_('%(courier)s refused the request (HTTP %(status)s): %(message)s',
                                 courier=self.label, status=status, message=self._error_text(response)))
        if expect == 'bytes':
            return response
        if expect == 'text':
            return response.text
        try:
            return response.json()
        except ValueError:
            raise CourierError(_('%(courier)s sent an unexpected answer: %(text)s',
                                 courier=self.label, text=response.text[:300]))

    @staticmethod
    def _error_text(response):
        try:
            payload = response.json()
        except ValueError:
            return (response.text or '').strip()[:300] or response.reason
        if isinstance(payload, dict):
            for key in ('message', 'error', 'rmk', 'msg', 'errors', 'detail', 'error_message', 'remark'):
                if payload.get(key):
                    value = payload[key]
                    return value if isinstance(value, str) else json.dumps(value)[:300]
        return json.dumps(payload)[:300]

    def _auth_headers(self):
        return {}

    def _cached_token(self, fetch):
        """Return a cached access token, fetching a fresh one when it is missing or expired."""
        acc = self.account.sudo()
        now = fields.Datetime.now()
        if acc.token and acc.token_expiry and acc.token_expiry > now:
            return acc.token
        token = fetch()
        if not token:
            raise CourierError(_('%s did not return an access token. Check the login details.', self.label))
        acc.write({'token': token, 'token_expiry': now + timedelta(hours=self.token_hours or 1)})
        return token

    def _require(self, *field_names):
        acc = self.account.sudo()
        missing = [acc._fields[f].string for f in field_names if not acc[f]]
        if missing:
            raise UserError(_('Fill in %(fields)s on the courier account "%(account)s" first.',
                              fields=', '.join(missing), account=acc.name))

    @staticmethod
    def _pdf_from(value):
        """Accept a PDF as bytes, a list of byte values, or base64 text."""
        import base64
        if not value:
            return None
        if isinstance(value, bytes):
            return value
        if isinstance(value, list):
            return bytes(value)
        if isinstance(value, str):
            try:
                return base64.b64decode(value)
            except (ValueError, TypeError):
                return None
        return None

    def _download(self, url, operation='label'):
        response = self._request(operation, 'GET', url, expect='bytes', auth=False)
        return response.content

    # ------------------------------------------------------------------
    # Interface (override in adapters)
    # ------------------------------------------------------------------
    def test_connection(self):
        return _('Settings saved. %s has no test call, the first booking will confirm the details.', self.label)

    def rates(self, parcel):
        return []

    def serviceable(self, parcel):
        return None

    def create_shipment(self, parcel, option=None):
        raise UserError(_('%s cannot book shipments through its API.', self.label))

    def fetch_label(self, parcel):
        return None

    def track(self, parcels):
        return {}

    def cancel(self, parcel):
        raise UserError(_('%s cannot cancel shipments through its API. Cancel it on the courier portal, '
                          'then use "Mark cancelled".', self.label))

    def request_pickup(self, parcels, when):
        raise UserError(_('%s does not take pickup requests through its API. Call the branch or book the '
                          'pickup on the courier portal.', self.label))

    def ndr_action(self, parcel, action, note=''):
        return False

    def parse_webhook(self, payload, headers=None):
        return {}

    # ------------------------------------------------------------------
    # Shared payload helpers
    # ------------------------------------------------------------------
    @staticmethod
    def address(partner):
        partner = partner.sudo()
        street = ', '.join(p for p in (partner.street, partner.street2) if p)
        return {
            'name': partner.name or '',
            'company': partner.commercial_partner_id.name if partner.parent_id else '',
            'street': partner.street or '',
            'street2': partner.street2 or '',
            'full_street': street,
            'city': partner.city or '',
            'state': partner.state_id.name or '',
            'state_code': partner.state_id.code or '',
            'zip': (partner.zip or '').replace(' ', ''),
            'country': partner.country_id.name or 'India',
            'country_code': partner.country_id.code or 'IN',
            'phone': re.sub(r'[^\d+]', '', partner.phone or '')[-10:] if partner.phone else '',
            'email': partner.email or '',
            'vat': partner.vat or '',
        }
