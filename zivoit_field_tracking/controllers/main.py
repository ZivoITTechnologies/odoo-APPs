# -*- coding: utf-8 -*-
import json
import logging

from odoo import fields, http
from odoo.http import request
from odoo.tools import file_open

from ..models import geo
from ..models.zivo_track_device import expense_currency, get_param

_logger = logging.getLogger(__name__)

MAX_BODY = 6 * 1024 * 1024  # 6 MB per sync request (bill photos included)
APP_VERSION = '19.0.2.0.0'


def _as_list(value):
    return value if isinstance(value, list) else []


def _json(data, status=200):
    return request.make_json_response(data, status=status, headers=[('Cache-Control', 'no-store')])


def _client_config(env, user=None):
    config = {
        'interval_sec': get_param(env, 'interval_sec'),
        'min_distance_m': get_param(env, 'min_distance_m'),
        'max_accuracy_m': get_param(env, 'max_accuracy_m'),
    }
    if user:
        config['expense'] = _expense_config(env, user)
    return config


def _expense_config(env, user):
    """Policy, mileage rates and bill categories cached on the phone (works offline)."""
    user = user.sudo()
    company = user.company_id
    policy = env['zivo.expense.policy'].sudo()._for_user(user, company)
    currency = expense_currency(env, company)
    vehicles = []
    labels = dict(env['zivo.track.session']._fields['vehicle_type']._description_selection(env))
    for key, label in labels.items():
        rate = policy._rate_for(key) if policy else env['zivo.expense.policy.rate']
        vehicles.append({'key': key, 'label': label, 'rate': rate.rate_per_km if rate else 0.0,
                         'cap_km': rate.max_km_per_day if rate else 0.0})
    categories = env['zivo.expense.category'].sudo().search([
        ('line_type', '=', 'expense'), ('company_id', 'in', [False, company.id])])
    return {
        'enabled': bool(policy),
        'currency': {'symbol': currency.symbol or currency.name, 'position': currency.position,
                     'digits': currency.decimal_places, 'code': currency.name},
        'vehicle': user.zivo_vehicle_type or 'two_wheeler',
        'vehicles': vehicles,
        'da': {
            'full': policy.da_full_amount if policy else 0.0,
            'full_hours': policy.da_full_min_hours if policy else 0.0,
            'half': policy.da_half_amount if policy else 0.0,
            'half_hours': policy.da_half_min_hours if policy else 0.0,
            'min_visits': policy.da_min_visits if policy else 0,
            'outstation_km': policy.outstation_km if policy else 0.0,
            'outstation': policy.da_outstation_amount if policy else 0.0,
        },
        'categories': [{
            'id': c.id, 'name': c.name, 'icon': c.icon or '',
            'receipt': c.receipt_policy, 'receipt_above': c.receipt_threshold, 'daily_limit': c.daily_limit,
        } for c in categories],
    }


class ZivoFieldTracking(http.Controller):

    # ------------------------------------------------------------------
    # Offline web app (PWA) shell
    # ------------------------------------------------------------------
    def _render_page(self, template, values):
        html = request.env['ir.qweb']._render(template, values)
        return request.make_response('<!DOCTYPE html>\n' + str(html), headers=[
            ('Content-Type', 'text/html; charset=utf-8'),
            ('Cache-Control', 'no-cache'),
        ])

    @http.route('/zivoit_tracking/app', type='http', auth='user', methods=['GET'], sitemap=False)
    def pwa_app(self, **kw):
        user = request.env.user
        if not user.zivo_tracking_enabled:
            return self._render_page('zivoit_field_tracking.pwa_not_allowed', {'user': user})
        device = request.env['zivo.track.device']._get_or_create_pwa_device(
            user, request.httprequest.headers.get('User-Agent'))
        boot = {
            'token': device.token,
            'device_id': device.id,
            'user_id': user.id,
            'user_name': user.name,
            'company': user.company_id.name,
            'db': request.db,
            'version': APP_VERSION,
            'config': _client_config(request.env, user),
        }
        return self._render_page('zivoit_field_tracking.pwa_app', {
            'boot_json': json.dumps(boot),
            'user': user,
        })

    @http.route('/zivoit_tracking/sw.js', type='http', auth='public', methods=['GET'], sitemap=False)
    def service_worker(self, **kw):
        with file_open('zivoit_field_tracking/static/app/sw.js', 'rb') as f:
            body = f.read()
        return request.make_response(body, headers=[
            ('Content-Type', 'text/javascript; charset=utf-8'),
            ('Service-Worker-Allowed', '/zivoit_tracking/'),
            ('Cache-Control', 'no-cache'),
        ])

    @http.route('/zivoit_tracking/manifest.webmanifest', type='http', auth='public', methods=['GET'],
                sitemap=False)
    def web_manifest(self, **kw):
        manifest = {
            'name': 'ZivoIT Field Tracker',
            'short_name': 'Field Tracker',
            'description': 'Offline GPS tracking for field sales (Odoo)',
            'start_url': '/zivoit_tracking/app',
            'scope': '/zivoit_tracking/',
            'display': 'standalone',
            'orientation': 'portrait',
            'background_color': '#0b1f3a',
            'theme_color': '#0b1f3a',
            'icons': [
                {'src': '/zivoit_field_tracking/static/app/icons/icon-192.png', 'sizes': '192x192',
                 'type': 'image/png', 'purpose': 'any maskable'},
                {'src': '/zivoit_field_tracking/static/app/icons/icon-512.png', 'sizes': '512x512',
                 'type': 'image/png', 'purpose': 'any maskable'},
            ],
        }
        return request.make_response(json.dumps(manifest), headers=[
            ('Content-Type', 'application/manifest+json'), ('Cache-Control', 'no-cache')])

    # ------------------------------------------------------------------
    # Token-authenticated sync API (works without an Odoo session cookie,
    # so phones that were offline for days still upload their backlog)
    # ------------------------------------------------------------------
    def _device_from_request(self):
        auth = request.httprequest.headers.get('Authorization', '')
        token = auth[7:].strip() if auth.lower().startswith('bearer ') else \
            request.httprequest.headers.get('X-Zivo-Device', '')
        return request.env['zivo.track.device']._authenticate(token)

    @http.route('/zivoit_tracking/api/ping', type='http', auth='public', methods=['GET'], csrf=False,
                sitemap=False)
    def api_ping(self, **kw):
        device = self._device_from_request()
        return _json({'ok': True, 'server_time': fields.Datetime.to_string(fields.Datetime.now()),
                      'device': bool(device)})

    @http.route('/zivoit_tracking/api/sync', type='http', auth='public', methods=['POST'], csrf=False,
                sitemap=False)
    def api_sync(self, **kw):
        device = self._device_from_request()
        if not device:
            return _json({'ok': False, 'error': 'invalid_device'}, status=401)
        raw = request.httprequest.get_data(cache=False)
        if len(raw) > MAX_BODY:
            return _json({'ok': False, 'error': 'payload_too_large'}, status=413)
        try:
            payload = json.loads(raw or b'{}')
        except ValueError:
            return _json({'ok': False, 'error': 'invalid_json'}, status=400)
        if not isinstance(payload, dict):
            return _json({'ok': False, 'error': 'invalid_json'}, status=400)

        Point = request.env['zivo.track.point'].sudo()
        points = [Point._normalize_pwa(p) for p in _as_list(payload.get('points')) if isinstance(p, dict)]
        point_result = Point._ingest(device, points)
        visit_items = [v for v in _as_list(payload.get('visits')) if isinstance(v, dict)]
        visit_result = request.env['zivo.track.visit'].sudo()._ingest(device, visit_items)
        event_result = self._apply_events(device, _as_list(payload.get('events')))
        expense_items = [e for e in _as_list(payload.get('expenses')) if isinstance(e, dict)]
        expense_result = request.env['zivo.expense.line'].sudo()._ingest(device, expense_items)
        if payload.get('user_agent'):
            device.user_agent = str(payload['user_agent'])[:250]
        return _json({
            'ok': True,
            'points': point_result,
            'visits': visit_result,
            'events': event_result,
            'expenses': expense_result,
            'today': self._today_summary(device),
            'config': _client_config(request.env, device.user_id),
            'server_time': fields.Datetime.to_string(fields.Datetime.now()),
        })

    def _apply_events(self, device, events):
        Session = request.env['zivo.track.session'].sudo()
        accepted = []
        vehicles = dict(Session._fields['vehicle_type']._description_selection(request.env))
        for event in (events if isinstance(events, list) else [])[:100]:
            if not isinstance(event, dict):
                continue
            when = geo.parse_timestamp(event.get('ts')) or fields.Datetime.now()
            session = Session._get_for(device.user_id, when)
            if event.get('type') == 'day_start':
                vals = {'state': 'active', 'end_time': False}
                if not session.start_time or when < session.start_time:
                    vals['start_time'] = when
                vehicle = event.get('vehicle')
                if vehicle in vehicles and (not session.claim_id or session.claim_id.state in ('draft', 'cancel')):
                    vals['vehicle_type'] = vehicle
                session.write(vals)
            elif event.get('type') == 'day_end':
                session.write({'state': 'closed', 'end_time': when})
            else:
                continue
            accepted.append(event.get('uid'))
        return {'accepted': accepted}

    def _today_summary(self, device):
        """Allowance of today as calculated by Odoo (shown on the phone)."""
        user = device.user_id
        Session = request.env['zivo.track.session'].sudo()
        day = Session._local_date(user, fields.Datetime.now())
        session = Session.search([('user_id', '=', user.id), ('date', '=', day)], limit=1)
        if not session:
            return {'date': fields.Date.to_string(day), 'km': 0, 'ta': 0, 'da': 0, 'bills': 0, 'total': 0}
        return {
            'date': fields.Date.to_string(day), 'km': session.distance_km, 'ta': session.ta_amount,
            'da': session.da_amount, 'da_kind': session.da_kind, 'bills': session.bill_amount,
            'total': session.allowance_total, 'vehicle': session.vehicle_type,
        }

    @http.route('/zivoit_tracking/api/customers', type='http', auth='public', methods=['GET'], csrf=False,
                sitemap=False)
    def api_customers(self, **kw):
        """Customers cached on the phone so visits can be checked-in offline."""
        device = self._device_from_request()
        if not device:
            return _json({'ok': False, 'error': 'invalid_device'}, status=401)
        user = device.user_id
        Partner = request.env['res.partner'].with_user(user)
        domain = [('active', '=', True), ('type', '=', 'contact')]
        assigned = Partner.search(domain + [('user_id', '=', user.id)], limit=2000, order='name')
        others = Partner.search(domain + [('is_company', '=', True), ('id', 'not in', assigned.ids)],
                                limit=max(0, 1000 - len(assigned)), order='write_date desc')
        result = []
        for partner in assigned | others:
            result.append({
                'id': partner.id,
                'name': partner.display_name,
                'city': partner.city or '',
                'street': partner.street or '',
                'phone': partner.phone or '',
                'lat': partner.partner_latitude or 0,
                'lon': partner.partner_longitude or 0,
                'mine': partner in assigned,
            })
        return _json({'ok': True, 'customers': result})

    # ------------------------------------------------------------------
    # OsmAnd protocol (Traccar Client, OsmAnd, GPSLogger, ...)
    # Background tracking with the screen locked and on-device buffering.
    # ------------------------------------------------------------------
    @http.route(['/zivoit_tracking/osmand', '/zivoit_tracking/osmand/<path:extra>'], type='http',
                auth='public', methods=['GET', 'POST'], csrf=False, sitemap=False)
    def osmand(self, extra=None, **params):
        httpreq = request.httprequest
        Point = request.env['zivo.track.point'].sudo()
        content_type = (httpreq.headers.get('Content-Type') or '').lower()
        if content_type.startswith('application/json'):
            raw = httpreq.get_data(cache=False)
            if len(raw) > MAX_BODY:
                return request.make_response('Payload too large', status=413)
            try:
                payload = json.loads(raw or b'{}')
            except ValueError:
                return request.make_response('Bad Request', status=400)
            if not isinstance(payload, dict):
                return request.make_response('Bad Request', status=400)
            device = request.env['zivo.track.device']._authenticate(
                str(payload.get('device_id') or payload.get('id') or ''))
            if not device:
                return request.make_response('Not Found', status=404)
            locations = payload.get('location')
            locations = locations if isinstance(locations, list) else [locations]
            items = [Point._normalize_traccar_json({'location': loc}) for loc in locations if isinstance(loc, dict)]
        else:
            device = request.env['zivo.track.device']._authenticate(
                str(params.get('id') or params.get('deviceid') or ''))
            if not device:
                return request.make_response('Bad Request', status=400)
            items = [Point._normalize_osmand_params(params)]
        Point._ingest(device, items)
        return request.make_response('', status=200)
