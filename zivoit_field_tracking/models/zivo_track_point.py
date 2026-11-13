# -*- coding: utf-8 -*-
import hashlib
import logging
from datetime import timedelta

from odoo import api, fields, models

from . import geo
from .zivo_track_device import get_param

_logger = logging.getLogger(__name__)

MAX_BATCH = 1000


class ZivoTrackPoint(models.Model):
    _name = 'zivo.track.point'
    _description = 'Field Location Point'
    _order = 'fix_time desc, id desc'
    _rec_name = 'fix_time'

    device_id = fields.Many2one('zivo.track.device', required=True, index=True, ondelete='cascade')
    user_id = fields.Many2one('res.users', string='Salesperson', required=True, index=True, ondelete='cascade')
    session_id = fields.Many2one('zivo.track.session', string='Day Log', index=True, ondelete='cascade')
    company_id = fields.Many2one('res.company', required=True, index=True)
    latitude = fields.Float(digits=(10, 7), required=True)
    longitude = fields.Float(digits=(10, 7), required=True)
    accuracy = fields.Float(string='Accuracy (m)')
    altitude = fields.Float(string='Altitude (m)')
    speed_kmh = fields.Float(string='Speed (km/h)', digits=(6, 1))
    heading = fields.Float(string='Heading (°)')
    battery = fields.Float(string='Battery (%)', digits=(5, 0))
    is_charging = fields.Boolean()
    fix_time = fields.Datetime(string='GPS Time', required=True, index=True,
                               help='Time the phone recorded the position (not the upload time)')
    received_time = fields.Datetime(string='Received', required=True, default=fields.Datetime.now)
    delay_minutes = fields.Float(string='Upload Delay (min)', digits=(10, 1),
                                 help='How long the point stayed on the phone before it reached Odoo')
    captured_offline = fields.Boolean(string='Captured Offline', index=True,
                                      help='Recorded while the phone had no network and synced later')
    low_accuracy = fields.Boolean(help='Accuracy is worse than the configured limit; excluded from distance')
    is_mock = fields.Boolean(string='Mock Location', help='Phone reported a fake / mock GPS provider')
    source = fields.Selection(related='device_id.source', store=True)
    event = fields.Char()
    client_uid = fields.Char(required=True, help='Unique id generated on the phone, used to ignore re-sent points')

    _device_uid_unique = models.Constraint('UNIQUE(device_id, client_uid)', 'Duplicate location point.')
    _user_fix_idx = models.Index('(user_id, fix_time DESC)')

    # ------------------------------------------------------------------
    # normalisation of incoming payloads
    # ------------------------------------------------------------------
    @api.model
    def _normalize_pwa(self, item):
        """Item sent by the ZivoIT offline web app / REST API."""
        return {
            'uid': item.get('uid'),
            'lat': item.get('lat', item.get('latitude')),
            'lon': item.get('lon', item.get('longitude')),
            'ts': item.get('ts', item.get('timestamp')),
            'accuracy': geo.to_float(item.get('accuracy')),
            'altitude': geo.to_float(item.get('altitude')),
            'speed_kmh': (geo.to_float(item.get('speed'), -1) * geo.MPS_TO_KMH)
            if geo.to_float(item.get('speed'), -1) >= 0 else None,
            'heading': geo.to_float(item.get('heading')),
            'battery': geo.to_float(item.get('battery')),
            'charging': geo.to_bool(item.get('charging', False)),
            'mock': geo.to_bool(item.get('mock', False)),
            'offline': geo.to_bool(item.get('offline', False)),
            'event': item.get('event'),
        }

    @api.model
    def _normalize_osmand_params(self, params):
        """OsmAnd protocol: query string or form parameters (speed in knots)."""
        lat, lon = params.get('lat'), params.get('lon')
        if (lat is None or lon is None) and params.get('location'):
            parts = str(params['location']).split(',')
            if len(parts) >= 2:
                lat, lon = parts[0], parts[1]
        speed = geo.to_float(params.get('speed'))
        return {
            'uid': None,
            'lat': lat, 'lon': lon,
            'ts': params.get('timestamp'),
            'accuracy': geo.to_float(params.get('accuracy')),
            'altitude': geo.to_float(params.get('altitude')),
            'speed_kmh': speed * geo.KNOTS_TO_KMH if speed is not None and speed >= 0 else None,
            'heading': geo.to_float(params.get('bearing', params.get('heading'))),
            'battery': geo.to_float(params.get('batt')),
            'charging': geo.to_bool(params.get('charge', False)),
            'mock': geo.to_bool(params.get('mock', False)),
            'offline': False,
            'event': params.get('event'),
            'valid': params.get('valid'),
        }

    @api.model
    def _normalize_traccar_json(self, payload):
        """Traccar Client (v9+) JSON body: {device_id, location: {timestamp, coords, battery, ...}}."""
        location = payload.get('location') or {}
        coords = location.get('coords') or {}
        battery = location.get('battery') or {}
        speed = geo.to_float(coords.get('speed'))
        level = geo.to_float(battery.get('level'))
        return {
            'uid': location.get('uuid'),
            'lat': coords.get('latitude'),
            'lon': coords.get('longitude'),
            'ts': location.get('timestamp'),
            'accuracy': geo.to_float(coords.get('accuracy')),
            'altitude': geo.to_float(coords.get('altitude')),
            'speed_kmh': speed * geo.MPS_TO_KMH if speed is not None and speed >= 0 else None,
            'heading': geo.to_float(coords.get('heading')) if (geo.to_float(coords.get('heading'), -1) >= 0) else None,
            'battery': level * 100 if level is not None and level >= 0 else None,
            'charging': geo.to_bool(battery.get('is_charging', False)),
            'mock': geo.to_bool(location.get('mock', False)),
            'offline': False,
            'event': location.get('event'),
        }

    # ------------------------------------------------------------------
    # ingestion
    # ------------------------------------------------------------------
    @api.model
    def _ingest(self, device, items):
        """Store a batch of normalised points for ``device`` (sudo record).

        Idempotent: points already stored (same device + uid) are acknowledged
        again, so a phone that lost the server's reply can safely re-send.
        Returns ``{'accepted': [uid], 'duplicates': [uid], 'rejected': [{uid, reason}]}``.
        """
        result = {'accepted': [], 'duplicates': [], 'rejected': []}
        if not device:
            return result
        device = device.sudo()
        user = device.user_id
        now = fields.Datetime.now()
        max_future = now + timedelta(minutes=10)
        offline_threshold = get_param(self.env, 'offline_threshold_min')
        max_acc = get_param(self.env, 'max_accuracy_m')

        prepared = []
        for item in items[:MAX_BATCH]:
            uid = item.get('uid')
            if str(item.get('valid', 'true')).lower() in ('false', '0'):
                result['rejected'].append({'uid': uid, 'reason': 'invalid_fix'})
                continue
            if not geo.valid_coordinates(item.get('lat'), item.get('lon')):
                result['rejected'].append({'uid': uid, 'reason': 'invalid_coordinates'})
                continue
            lat, lon = float(item['lat']), float(item['lon'])
            fix_time = geo.parse_timestamp(item.get('ts')) or now
            if fix_time > max_future:
                fix_time = now  # phone clock is wrong; trust the server
            if not uid:
                raw = '%s|%s|%.6f|%.6f' % (device.id, fix_time.isoformat(), lat, lon)
                uid = 'h-' + hashlib.sha1(raw.encode()).hexdigest()[:24]
            uid = str(uid)[:64]
            delay = max(0.0, (now - fix_time).total_seconds() / 60.0)
            prepared.append((uid, {
                'device_id': device.id,
                'user_id': user.id,
                'company_id': user.company_id.id,
                'latitude': lat,
                'longitude': lon,
                'accuracy': item.get('accuracy'),
                'altitude': item.get('altitude'),
                'speed_kmh': item.get('speed_kmh'),
                'heading': item.get('heading'),
                'battery': item.get('battery'),
                'is_charging': bool(item.get('charging')),
                'is_mock': bool(item.get('mock')),
                'event': (item.get('event') or '')[:64] or False,
                'fix_time': fix_time,
                'received_time': now,
                'delay_minutes': round(delay, 1),
                'captured_offline': bool(item.get('offline')) or delay > offline_threshold,
                'low_accuracy': bool(max_acc and item.get('accuracy') and item['accuracy'] > max_acc),
                'client_uid': uid,
            }))

        if not prepared:
            return result

        # de-duplicate against the database and inside the batch itself
        uids = [uid for uid, _vals in prepared]
        existing = set(self.sudo().search([('device_id', '=', device.id), ('client_uid', 'in', uids)])
                       .mapped('client_uid'))
        to_create, seen, session_cache = [], set(), {}
        Session = self.env['zivo.track.session']
        for uid, vals in prepared:
            if uid in existing or uid in seen:
                result['duplicates'].append(uid)
                continue
            seen.add(uid)
            day = Session._local_date(user, vals['fix_time'])
            if day not in session_cache:
                session_cache[day] = Session._get_for(user, vals['fix_time']).id
            vals['session_id'] = session_cache[day]
            to_create.append(vals)
            result['accepted'].append(uid)

        if to_create:
            records = self.sudo().create(to_create)
            records.mapped('session_id')._recompute_stats()
            latest = max(records, key=lambda r: r.fix_time)
            device_vals = {'last_seen': now}
            if not device.last_fix_time or latest.fix_time >= device.last_fix_time:
                device_vals.update({
                    'last_fix_time': latest.fix_time,
                    'last_latitude': latest.latitude,
                    'last_longitude': latest.longitude,
                    'last_battery': latest.battery or device.last_battery,
                })
            if any(records.mapped('is_mock')):
                device_vals['mock_detected'] = True
            device.write(device_vals)
        else:
            device.write({'last_seen': now})
        return result

    # ------------------------------------------------------------------
    # live board
    # ------------------------------------------------------------------
    @api.model
    def get_live_board(self):
        """Last known position and status of every visible salesperson."""
        Session = self.env['zivo.track.session']
        now = fields.Datetime.now()
        live_min = get_param(self.env, 'live_minutes')
        delayed_min = get_param(self.env, 'delayed_minutes')
        users = self.env['res.users'].search([('zivo_tracking_enabled', '=', True)])
        if not self.env.user.has_group('zivoit_field_tracking.group_field_manager'):
            users = users.filtered(lambda u: u == self.env.user)
        board = []
        for user in users:
            last = self.search([('user_id', '=', user.id)], order='fix_time desc, id desc', limit=1)
            today = Session._local_date(user, now)
            session = Session.search([('user_id', '=', user.id), ('date', '=', today)], limit=1)
            if session and session.state == 'closed':
                status = 'ended'
            elif not last or not session:
                status = 'notstarted'
            else:
                age = (now - last.fix_time).total_seconds() / 60.0
                status = 'live' if age <= live_min else ('delayed' if age <= delayed_min else 'nosignal')
            trail = []
            if session:
                self.env.cr.execute("""SELECT latitude, longitude FROM zivo_track_point
                                        WHERE session_id = %s AND low_accuracy IS NOT TRUE ORDER BY fix_time, id""",
                                    [session.id])
                rows = self.env.cr.fetchall()
                step = max(1, len(rows) // 300)
                trail = [[r[0], r[1]] for r in rows[::step]]
                if rows and trail[-1] != [rows[-1][0], rows[-1][1]]:
                    trail.append([rows[-1][0], rows[-1][1]])
            board.append({
                'trail': trail,
                'user_id': user.id,
                'name': user.name,
                'status': status,
                'lat': last.latitude if last else False,
                'lon': last.longitude if last else False,
                'fix_time': fields.Datetime.to_string(last.fix_time) if last else False,
                'received_time': fields.Datetime.to_string(last.received_time) if last else False,
                'accuracy': last.accuracy if last else 0,
                'speed_kmh': last.speed_kmh if last else 0,
                'battery': last.battery if last else 0,
                'is_mock': last.is_mock if last else False,
                'session_id': session.id if session else False,
                'distance_km': session.distance_km if session else 0,
                'offline_points': session.offline_point_count if session else 0,
                'point_count': session.point_count if session else 0,
                'visit_count': session.visit_count if session else 0,
            })
        order = {'live': 0, 'delayed': 1, 'nosignal': 2, 'ended': 3, 'notstarted': 4}
        board.sort(key=lambda r: (order[r['status']], r['name']))
        return {
            'users': board,
            'server_time': fields.Datetime.to_string(now),
            'tile_url': get_param(self.env, 'tile_url'),
            'tile_attribution': get_param(self.env, 'tile_attribution'),
            'is_manager': self.env.user.has_group('zivoit_field_tracking.group_field_manager'),
        }

    @api.model
    def _cron_purge_old_points(self):
        days = get_param(self.env, 'retention_days')
        if not days or days <= 0:
            return True
        limit_date = fields.Datetime.now() - timedelta(days=days)
        old = self.sudo().search([('fix_time', '<', limit_date)], limit=50000)
        sessions = old.mapped('session_id')
        count = len(old)
        old.unlink()
        sessions.exists()._recompute_stats()
        _logger.info('ZivoIT Field Tracking: purged %s location points older than %s days', count, days)
        return True
