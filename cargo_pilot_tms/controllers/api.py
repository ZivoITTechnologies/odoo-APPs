# -*- coding: utf-8 -*-
"""Integration API for GPS devices, dashcams and ERPs.

Authentication: header ``X-CP-API-Key: <key>`` (Settings > Cargo Pilot TMS).
All endpoints accept and return JSON.

    POST /cp/api/v1/ping     {"device_id"|"vehicle_number", "latitude", "longitude", "speed", "timestamp"}
    POST /cp/api/v1/event    {"device_id"|"vehicle_number", "event_type", "severity", "speed",
                              "latitude", "longitude", "timestamp", "video_url"}
    POST /cp/api/v1/indent   {"origin_code", "destination_code", "vehicle_type", "vehicle_count",
                              "required_date", "reference", "lines": [{"description", "quantity", "weight_ton"}]}
    GET  /cp/api/v1/trip/<name>
"""
import json
import logging

from odoo import fields, http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request
from odoo.tools import consteq

_logger = logging.getLogger(__name__)

ACTIVE = ('assigned', 'reported', 'loading', 'in_transit', 'arrived')


class CargoPilotApi(http.Controller):

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------
    @staticmethod
    def _json(payload, status=200):
        return request.make_response(json.dumps(payload, default=str), status=status,
                                     headers=[('Content-Type', 'application/json')])

    def _authenticate(self):
        key = request.httprequest.headers.get('X-CP-API-Key') or ''
        if len(key) < 16:
            return None
        for company in request.env['res.company'].sudo().search([('cp_api_key', '!=', False)]):
            if consteq(company.cp_api_key, key):
                return request.env(su=True, context=dict(request.env.context, allowed_company_ids=[company.id])) \
                    ['res.company'].browse(company.id)
        return None

    @staticmethod
    def _payload():
        try:
            data = json.loads(request.httprequest.get_data(as_text=True) or '{}')
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _find_active_trip(self, env, data):
        Trip = env['cp.trip']
        company_id = env.context['allowed_company_ids'][0]
        device = (data.get('device_id') or '').strip()
        plate = (data.get('vehicle_number') or '').strip().upper()
        if device:
            vehicle = env['fleet.vehicle'].search([('cp_device_id', '=', device)], limit=1)
            if vehicle:
                trip = Trip.search([('vehicle_id', '=', vehicle.id), ('state', 'in', ACTIVE),
                                     ('company_id', '=', company_id)], limit=1)
                return trip, vehicle
        if plate:
            trip = Trip.search([('vehicle_number', '=ilike', plate), ('state', 'in', ACTIVE),
                                 ('company_id', '=', company_id)], limit=1)
            vehicle = trip.vehicle_id or env['fleet.vehicle'].search([('license_plate', '=ilike', plate)], limit=1)
            return trip, vehicle
        return Trip.browse(), env['fleet.vehicle'].browse()

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------
    @http.route('/cp/api/v1/ping', type='http', auth='public', methods=['POST'], csrf=False)
    def api_ping(self, **kw):
        company = self._authenticate()
        if not company:
            return self._json({'error': 'unauthorized'}, 401)
        data = self._payload()
        if data is None:
            return self._json({'error': 'invalid JSON body'}, 400)
        try:
            lat, lon = float(data['latitude']), float(data['longitude'])
        except (KeyError, TypeError, ValueError):
            return self._json({'error': 'latitude and longitude are required'}, 400)
        trip, vehicle = self._find_active_trip(company.env, data)
        if not trip:
            return self._json({'ok': True, 'matched': False, 'detail': 'no active trip for this vehicle'})
        trip.cp_add_ping(lat, lon, speed=data.get('speed'), ping_time=self._ts(data.get('timestamp')), source='api')
        return self._json({'ok': True, 'matched': True, 'trip': trip.name, 'state': trip.state,
                           'eta': trip.eta, 'remaining_km': round(trip.remaining_km, 1)})

    @http.route('/cp/api/v1/event', type='http', auth='public', methods=['POST'], csrf=False)
    def api_event(self, **kw):
        company = self._authenticate()
        if not company:
            return self._json({'error': 'unauthorized'}, 401)
        data = self._payload()
        if data is None:
            return self._json({'error': 'invalid JSON body'}, 400)
        trip, vehicle = self._find_active_trip(company.env, data)
        if not vehicle:
            return self._json({'error': 'unknown vehicle'}, 404)
        Event = company.env['cp.driving.event']
        if data.get('event_type') not in dict(Event._fields['event_type'].selection):
            return self._json({'error': 'unknown event_type'}, 400)
        event = Event.create({
            'vehicle_id': vehicle.id,
            'trip_id': trip.id or False,
            'driver_id': trip.driver_id.id or vehicle.driver_id.id or False,
            'event_type': data['event_type'],
            'severity': data.get('severity') if data.get('severity') in ('low', 'medium', 'high') else 'medium',
            'speed': float(data.get('speed') or 0),
            'latitude': float(data.get('latitude') or 0),
            'longitude': float(data.get('longitude') or 0),
            'video_url': data.get('video_url'),
            'event_time': self._ts(data.get('timestamp')) or fields.Datetime.now(),
        })
        return self._json({'ok': True, 'id': event.id})

    @http.route('/cp/api/v1/indent', type='http', auth='public', methods=['POST'], csrf=False)
    def api_indent(self, **kw):
        company = self._authenticate()
        if not company:
            return self._json({'error': 'unauthorized'}, 401)
        data = self._payload()
        if data is None:
            return self._json({'error': 'invalid JSON body'}, 400)
        env = company.env
        Location = env['cp.location']
        origin = Location.search([('code', '=', data.get('origin_code'))], limit=1)
        dest = Location.search([('code', '=', data.get('destination_code'))], limit=1)
        vtype = env['cp.vehicle.type'].search([('name', '=ilike', data.get('vehicle_type') or '')], limit=1)
        if not (origin and dest and vtype):
            return self._json({'error': 'origin_code, destination_code and vehicle_type must match master data'}, 400)
        lane = env['cp.lane'].search([('origin_id', '=', origin.id), ('destination_id', '=', dest.id)], limit=1) \
            or env['cp.lane'].create({'origin_id': origin.id, 'destination_id': dest.id})
        try:
            indent = env['cp.indent'].with_company(company).create({
                'lane_id': lane.id,
                'vehicle_type_id': vtype.id,
                'vehicle_count': int(data.get('vehicle_count') or 1),
                'required_date': self._ts(data.get('required_date')) or fields.Datetime.now(),
                'origin': data.get('reference'),
                'source_channel': 'api',
                'company_id': company.id,
                'line_ids': [(0, 0, {
                    'description': line.get('description'),
                    'quantity': float(line.get('quantity') or 1),
                    'weight_ton': float(line.get('weight_ton') or 0),
                }) for line in (data.get('lines') or []) if isinstance(line, dict)],
            })
            if data.get('confirm'):
                indent.action_confirm()
        except (UserError, ValidationError, ValueError) as exc:
            return self._json({'error': str(exc)}, 400)
        return self._json({'ok': True, 'indent': indent.name, 'id': indent.id, 'state': indent.state}, 201)

    @http.route('/cp/api/v1/trip/<path:name>', type='http', auth='public', methods=['GET'], csrf=False)
    def api_trip(self, name, **kw):
        company = self._authenticate()
        if not company:
            return self._json({'error': 'unauthorized'}, 401)
        trip = company.env['cp.trip'].search([('name', '=', name), ('company_id', '=', company.id)], limit=1)
        if not trip:
            return self._json({'error': 'not found'}, 404)
        return self._json({
            'trip': trip.name, 'state': trip.state, 'lane': trip.lane_id.name,
            'vehicle_number': trip.vehicle_number, 'transporter': trip.transporter_id.name,
            'eta': trip.eta, 'progress_pct': round(trip.progress_pct, 1),
            'last_position': {'latitude': trip.last_latitude, 'longitude': trip.last_longitude,
                              'time': trip.last_ping_time},
            'epod_state': trip.epod_state, 'tracking_url': trip.tracking_url,
        })

    @staticmethod
    def _ts(value):
        if not value:
            return None
        try:
            if isinstance(value, (int, float)):
                from datetime import datetime, timezone
                return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
            return fields.Datetime.to_datetime(str(value).replace('T', ' ').replace('Z', '')[:19])
        except (ValueError, OverflowError):
            return None
