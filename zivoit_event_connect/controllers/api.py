# -*- coding: utf-8 -*-
"""ZivoIT Event Connect - Headless Events REST API (v1).

Authentication: ``X-API-Key: <key>`` or ``Authorization: Bearer <key>`` header
(``?api_key=`` query parameter is accepted for calendar subscriptions).
"""
import json
import logging
from datetime import datetime, time

from odoo import fields, http
from odoo.exceptions import ValidationError
from odoo.http import request
from odoo.tools import email_normalize

_logger = logging.getLogger(__name__)

API_PREFIX = '/api/zivo/v1'
SORTS = {
    'date_begin': 'date_begin asc, id asc',
    '-date_begin': 'date_begin desc, id desc',
    'name': 'name asc, id asc',
    '-updated': 'write_date desc, id desc',
}


class ZivoEventApi(http.Controller):

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------
    def _param(self, name, default=None):
        icp = request.env['ir.config_parameter'].sudo()
        return icp.get_param('zivoit_event_connect.%s' % name, default)

    def _cors_headers(self, client=None):
        origin = request.httprequest.headers.get('Origin')
        headers = [('Vary', 'Origin')]
        if origin and client and client._zivo_origin_allowed(origin):
            headers += [
                ('Access-Control-Allow-Origin', origin),
                ('Access-Control-Allow-Headers', 'Authorization, X-API-Key, Content-Type'),
                ('Access-Control-Allow-Methods', 'GET, POST, OPTIONS'),
                ('Access-Control-Max-Age', '600'),
            ]
        return headers

    def _json(self, data, status=200, client=None, cache=False):
        headers = self._cors_headers(client)
        if cache and status == 200:
            headers.append(('Cache-Control', 'public, max-age=%s' % int(self._param('api_cache_seconds', 60) or 0)))
        else:
            headers.append(('Cache-Control', 'no-store'))
        return request.make_json_response(data, headers=headers, status=status)

    def _error(self, status, code, message, client=None):
        return self._json({'error': {'code': code, 'message': message}}, status=status, client=client)

    def _get_key(self):
        headers = request.httprequest.headers
        key = headers.get('X-API-Key')
        auth = headers.get('Authorization') or ''
        if not key and auth.lower().startswith('bearer '):
            key = auth[7:].strip()
        if not key:
            key = request.httprequest.args.get('api_key')
        return key

    def _authenticate(self):
        """Return (client, error_response)."""
        if request.httprequest.method == 'OPTIONS':
            return None, self._preflight()
        if str(self._param('api_enabled', 'True')).lower() in ('false', '0', ''):
            return None, self._error(503, 'api_disabled', 'The Event Connect API is disabled.')
        client = request.env['zivo.api.client'].sudo()._zivo_authenticate(self._get_key())
        if not client:
            return None, self._error(401, 'unauthorized', 'Missing or invalid API key.')
        client._zivo_touch(request.httprequest.remote_addr)
        return client, None

    def _preflight(self):
        client = request.env['zivo.api.client'].sudo()._zivo_authenticate(self._get_key())
        if not client:
            # Preflight requests carry no credentials: allow any origin configured on any active client
            origin = request.httprequest.headers.get('Origin')
            for candidate in request.env['zivo.api.client'].sudo().search([('active', '=', True)]):
                if candidate._zivo_origin_allowed(origin):
                    client = candidate
                    break
        return request.make_response('', headers=self._cors_headers(client), status=204)

    def _parse_date(self, value, end=False):
        if not value:
            return None
        try:
            if len(value) == 10:
                day = fields.Date.from_string(value)
                return datetime.combine(day, time.max if end else time.min)
            return fields.Datetime.from_string(value.replace('T', ' ').replace('Z', '')[:19])
        except ValueError:
            raise ValidationError('Invalid date "%s" (use YYYY-MM-DD or ISO 8601).' % value)

    def _event_domain(self, client, kw):
        Event = request.env['event.event'].sudo()
        domain = client._zivo_event_domain()
        when = kw.get('when', 'upcoming')
        now = fields.Datetime.now()
        if when == 'upcoming':
            domain.append(('date_end', '>=', now))
        elif when == 'past':
            domain.append(('date_end', '<', now))
        date_from = self._parse_date(kw.get('from'))
        date_to = self._parse_date(kw.get('to'), end=True)
        if date_from:
            domain.append(('date_end', '>=', date_from))
        if date_to:
            domain.append(('date_begin', '<=', date_to))
        updated_since = self._parse_date(kw.get('updated_since'))
        if updated_since:
            domain.append(('write_date', '>=', updated_since))
        if kw.get('q'):
            domain += ['|', ('name', 'ilike', kw['q']), ('subtitle', 'ilike', kw['q'])]
        if kw.get('department'):
            domain += Event._zivo_department_domain(kw['department'])
        if kw.get('tag'):
            tag_ids = [int(t) for t in str(kw['tag']).split(',') if t.strip().isdigit()]
            domain.append(('tag_ids', 'in', tag_ids) if tag_ids else ('id', 'in', []))
        if kw.get('type') and str(kw['type']).isdigit():
            domain.append(('event_type_id', '=', int(kw['type'])))
        if kw.get('city'):
            domain.append(('address_id.city', 'ilike', kw['city']))
        if kw.get('online') == '1':
            domain.append(('address_id', '=', False))
        elif kw.get('online') == '0':
            domain.append(('address_id', '!=', False))
        if kw.get('access_mode') and client.include_restricted:
            domain.append(('zivo_access_mode', '=', kw['access_mode']))
        return domain

    def _find_event(self, client, ref):
        Event = request.env['event.event'].sudo()
        domain = client._zivo_event_domain()
        ref = str(ref)
        if ref.isdigit():
            return Event.search(domain + [('id', '=', int(ref))], limit=1)
        tail = ref.rsplit('-', 1)[-1]
        if tail.isdigit():
            event = Event.search(domain + [('id', '=', int(tail))], limit=1)
            if event and event.zivo_slug == ref:
                return event
        return Event

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------
    @http.route(f'{API_PREFIX}/ping', type='http', auth='public', methods=['GET', 'OPTIONS'], csrf=False, save_session=False)
    def api_ping(self, **kw):
        client, error = self._authenticate()
        if error:
            return error
        return self._json({'status': 'ok', 'client': client.name, 'version': '1.0',
                           'server_time': fields.Datetime.now().isoformat() + 'Z'}, client=client)

    @http.route(f'{API_PREFIX}/events', type='http', auth='public', methods=['GET', 'OPTIONS'], csrf=False, save_session=False)
    def api_events(self, **kw):
        client, error = self._authenticate()
        if error:
            return error
        try:
            domain = self._event_domain(client, kw)
        except ValidationError as exc:
            return self._error(400, 'bad_request', str(exc.args[0]), client)
        default_size = int(self._param('api_page_size', 20) or 20)
        max_size = int(self._param('api_max_page_size', 100) or 100)
        try:
            page = max(int(kw.get('page', 1)), 1)
            limit = min(max(int(kw.get('limit', default_size)), 1), max_size)
        except ValueError:
            return self._error(400, 'bad_request', 'page and limit must be integers.', client)
        order = SORTS.get(kw.get('sort', 'date_begin'), SORTS['date_begin'])
        Event = request.env['event.event'].sudo()
        total = Event.search_count(domain)
        events = Event.search(domain, order=order, limit=limit, offset=(page - 1) * limit)
        detail = str(kw.get('detail', '')).lower() in ('1', 'true', 'yes')
        return self._json({
            'data': [e._zivo_api_payload(detail=detail) for e in events],
            'meta': {'page': page, 'limit': limit, 'total': total,
                     'pages': (total + limit - 1) // limit if limit else 0},
        }, client=client, cache=True)

    @http.route(f'{API_PREFIX}/search', type='http', auth='public', methods=['GET', 'OPTIONS'], csrf=False,
                save_session=False)
    def api_ai_search(self, query='', **kw):
        """AI natural-language search: /search?query=hr workshops in hyderabad next month"""
        client, error = self._authenticate()
        if error:
            return error
        if not query.strip():
            return self._error(400, 'bad_request', 'query is required.', client)
        interpreted = request.env['event.event'].sudo()._zivo_ai_interpret_query(query)
        params = {k: v for k, v in interpreted.items() if k != 'source'}
        params.setdefault('when', kw.get('when', 'upcoming'))
        if 'from' in params or 'to' in params:
            params['when'] = 'all'
        try:
            domain = self._event_domain(client, params)
        except ValidationError as exc:
            return self._error(400, 'bad_request', str(exc.args[0]), client)
        try:
            limit = min(max(int(kw.get('limit', 10)), 1), int(self._param('api_max_page_size', 100) or 100))
        except ValueError:
            limit = 10
        events = request.env['event.event'].sudo().search(domain, order=SORTS['date_begin'], limit=limit)
        return self._json({
            'query': query,
            'interpreted': interpreted,
            'data': [e._zivo_api_payload() for e in events],
            'meta': {'total': request.env['event.event'].sudo().search_count(domain), 'limit': limit},
        }, client=client)

    @http.route(f'{API_PREFIX}/events/<string:ref>', type='http', auth='public', methods=['GET', 'OPTIONS'], csrf=False,
                save_session=False)
    def api_event(self, ref, **kw):
        client, error = self._authenticate()
        if error:
            return error
        if ref == 'calendar.ics':
            return self.api_calendar(**kw)
        event = self._find_event(client, ref)
        if not event:
            return self._error(404, 'not_found', 'Event not found.', client)
        return self._json({'data': event._zivo_api_payload(detail=True)}, client=client, cache=True)

    @http.route(f'{API_PREFIX}/events/<int:event_id>/registrations', type='http', auth='public',
                methods=['POST', 'OPTIONS'], csrf=False, save_session=False)
    def api_register(self, event_id, **kw):
        client, error = self._authenticate()
        if error:
            return error
        if not client.allow_registration:
            return self._error(403, 'forbidden', 'This API client may not create registrations.', client)
        event = self._find_event(client, event_id)
        if not event:
            return self._error(404, 'not_found', 'Event not found.', client)
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or '{}')
        except ValueError:
            return self._error(400, 'bad_request', 'Body must be valid JSON.', client)
        email = email_normalize(payload.get('email') or '')
        name = (payload.get('name') or '').strip()
        if not email or not name:
            return self._error(422, 'validation_error', 'name and a valid email are required.', client)
        if not event.event_registrations_open:
            return self._error(409, 'registration_closed', 'Registrations are closed for this event.', client)
        vals = {'event_id': event.id, 'name': name, 'email': email,
                'phone': payload.get('phone') or False, 'company_name': payload.get('company') or False}
        ticket_id = payload.get('ticket_id')
        if ticket_id:
            ticket = event.event_ticket_ids.filtered(lambda t: t.id == int(ticket_id))
            if not ticket:
                return self._error(422, 'validation_error', 'Unknown ticket for this event.', client)
            vals['event_ticket_id'] = ticket.id
        elif event.event_ticket_ids:
            available = event.event_ticket_ids.filtered('sale_available')
            if not available:
                return self._error(409, 'sold_out', 'No ticket is available.', client)
            vals['event_ticket_id'] = available[0].id
        try:
            ticket = request.env['event.event.ticket'].sudo().browse(vals.get('event_ticket_id'))
            event._verify_seats_availability([(False, ticket, 1)])
            registration = request.env['event.registration'].sudo().create(vals)
        except ValidationError as exc:
            return self._error(409, 'unavailable', str(exc.args[0]), client)
        request.env['zivo.audit.log'].sudo()._zivo_log(registration, 'api', note='Registered via API client "%s"' % client.name)
        return self._json({'data': registration._zivo_api_payload()}, status=201, client=client)

    @http.route(f'{API_PREFIX}/departments', type='http', auth='public', methods=['GET', 'OPTIONS'], csrf=False,
                save_session=False)
    def api_departments(self, **kw):
        client, error = self._authenticate()
        if error:
            return error
        domain = [('id', 'child_of', client.department_ids.ids)] if client.department_ids else []
        depts = request.env['zivo.event.department'].sudo().search(domain)
        return self._json({'data': [{
            'id': d.id, 'name': d.name, 'code': d.code, 'parent_id': d.parent_id.id or None,
            'color': d.color,
        } for d in depts]}, client=client, cache=True)

    @http.route(f'{API_PREFIX}/tags', type='http', auth='public', methods=['GET', 'OPTIONS'], csrf=False, save_session=False)
    def api_tags(self, **kw):
        client, error = self._authenticate()
        if error:
            return error
        tags = request.env['event.tag'].sudo().search([])
        return self._json({'data': [{
            'id': t.id, 'name': t.name, 'category': t.category_id.name, 'category_id': t.category_id.id,
        } for t in tags]}, client=client, cache=True)

    @http.route(f'{API_PREFIX}/calendar.ics', type='http', auth='public', methods=['GET', 'OPTIONS'], csrf=False,
                save_session=False)
    def api_calendar(self, **kw):
        client, error = self._authenticate()
        if error:
            return error
        kw.setdefault('when', 'upcoming')
        try:
            domain = self._event_domain(client, kw)
        except ValidationError as exc:
            return self._error(400, 'bad_request', str(exc.args[0]), client)
        events = request.env['event.event'].sudo().search(domain, order='date_begin', limit=500)
        stamp = fields.Datetime.now().strftime('%Y%m%dT%H%M%SZ')

        def esc(text):
            return (text or '').replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\n', '\\n')

        lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//ZivoIT//Event Connect//EN',
                 'CALSCALE:GREGORIAN', 'X-WR-CALNAME:%s' % esc(request.env.company.name + ' Events')]
        for ev in events:
            data = ev._zivo_api_payload()
            lines += [
                'BEGIN:VEVENT',
                'UID:zivo-event-%s@%s' % (ev.id, request.httprequest.host),
                'DTSTAMP:%s' % stamp,
                'DTSTART:%s' % ev.date_begin.strftime('%Y%m%dT%H%M%SZ'),
                'DTEND:%s' % ev.date_end.strftime('%Y%m%dT%H%M%SZ'),
                'SUMMARY:%s' % esc(ev.name),
                'DESCRIPTION:%s' % esc(data.get('summary') or ''),
                'LOCATION:%s' % esc(ev.address_id.contact_address.replace('\n', ', ') if ev.address_id else 'Online'),
                'URL:%s' % (data.get('url') or ''),
                'END:VEVENT',
            ]
        lines.append('END:VCALENDAR')
        headers = self._cors_headers(client) + [('Content-Type', 'text/calendar; charset=utf-8'),
                                                ('Content-Disposition', 'inline; filename="events.ics"')]
        return request.make_response('\r\n'.join(lines) + '\r\n', headers=headers)
