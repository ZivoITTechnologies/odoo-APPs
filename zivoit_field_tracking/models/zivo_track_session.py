# -*- coding: utf-8 -*-
from datetime import datetime, time, timedelta

import pytz

from odoo import _, api, fields, models

from . import geo
from .zivo_track_device import get_param


class ZivoTrackSession(models.Model):
    """One field day of one salesperson (local calendar date of the user)."""
    _name = 'zivo.track.session'
    _description = 'Field Day Log'
    _inherit = ['mail.thread']
    _order = 'date desc, user_id'
    _rec_name = 'name'

    name = fields.Char(compute='_compute_name', store=True)
    user_id = fields.Many2one('res.users', string='Salesperson', required=True, index=True, ondelete='cascade')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    date = fields.Date(required=True, index=True)
    state = fields.Selection([('active', 'In Field'), ('closed', 'Day Ended')], default='active',
                             required=True, tracking=True)
    start_time = fields.Datetime(string='Day Started', tracking=True)
    end_time = fields.Datetime(string='Day Ended', tracking=True)
    point_ids = fields.One2many('zivo.track.point', 'session_id', string='Points')
    visit_ids = fields.One2many('zivo.track.visit', 'session_id', string='Visits')
    point_count = fields.Integer(readonly=True)
    offline_point_count = fields.Integer(string='Captured Offline', readonly=True,
                                         help='Points recorded on the phone while it had no network '
                                              'and uploaded later')
    first_fix = fields.Datetime(readonly=True)
    last_fix = fields.Datetime(readonly=True)
    distance_km = fields.Float(string='Distance (km)', digits=(10, 2), readonly=True)
    duration_hours = fields.Float(string='Tracked Hours', readonly=True)
    max_delay_minutes = fields.Float(string='Longest Offline Gap (min)', readonly=True)
    visit_count = fields.Integer(compute='_compute_visit_count')
    stop_count = fields.Integer(readonly=True)

    _user_date_unique = models.Constraint('UNIQUE(user_id, date)', 'Only one day log per salesperson and day.')

    @api.depends('user_id', 'date')
    def _compute_name(self):
        for session in self:
            session.name = '%s - %s' % (session.user_id.name or '', session.date or '')

    @api.depends('visit_ids')
    def _compute_visit_count(self):
        for session in self:
            session.visit_count = len(session.visit_ids)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @api.model
    def _local_date(self, user, utc_dt):
        tz = pytz.timezone(user.tz or 'UTC')
        return pytz.utc.localize(utc_dt).astimezone(tz).date()

    @api.model
    def _get_for(self, user, utc_dt):
        """Find or create (sudo) the day log of ``user`` containing ``utc_dt``."""
        day = self._local_date(user, utc_dt)
        Session = self.sudo()
        session = Session.search([('user_id', '=', user.id), ('date', '=', day)], limit=1)
        if not session:
            session = Session.create({
                'user_id': user.id,
                'date': day,
                'company_id': user.company_id.id,
            })
        return session

    def _day_bounds_utc(self):
        self.ensure_one()
        tz = pytz.timezone(self.user_id.tz or 'UTC')
        start = tz.localize(datetime.combine(self.date, time.min)).astimezone(pytz.utc).replace(tzinfo=None)
        return start, start + timedelta(days=1)

    def _ordered_points(self):
        self.ensure_one()
        self.env.cr.execute("""
            SELECT fix_time, latitude, longitude, COALESCE(accuracy, 0), captured_offline, delay_minutes
              FROM zivo_track_point
             WHERE session_id = %s
             ORDER BY fix_time, id
        """, [self.id])
        return self.env.cr.fetchall()

    def _recompute_stats(self):
        max_acc = get_param(self.env, 'max_accuracy_m')
        max_speed = get_param(self.env, 'max_speed_kmh')
        radius = get_param(self.env, 'stop_radius_m')
        minutes = get_param(self.env, 'stop_minutes')
        for session in self.sudo():
            rows = session._ordered_points()
            if not rows:
                session.write({'point_count': 0, 'offline_point_count': 0, 'distance_km': 0,
                               'first_fix': False, 'last_fix': False, 'duration_hours': 0,
                               'max_delay_minutes': 0, 'stop_count': 0})
                continue
            pts = [(r[0], r[1], r[2], r[3]) for r in rows]
            good = [p for p in pts if not (max_acc and p[3] and p[3] > max_acc)]
            distance = geo.route_distance_m(pts, max_acc, max_speed)
            stops = geo.detect_stops(good, radius, minutes)
            session.write({
                'point_count': len(rows),
                'offline_point_count': sum(1 for r in rows if r[4]),
                'distance_km': round(distance / 1000.0, 3),
                'first_fix': rows[0][0],
                'last_fix': rows[-1][0],
                'duration_hours': round((rows[-1][0] - rows[0][0]).total_seconds() / 3600.0, 2),
                'max_delay_minutes': round(max((r[5] or 0) for r in rows), 1),
                'stop_count': len(stops),
            })

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def action_end_day(self):
        for session in self:
            session.write({'state': 'closed', 'end_time': session.end_time or fields.Datetime.now()})
        return True

    def action_reopen(self):
        self.write({'state': 'active', 'end_time': False})
        return True

    def action_recompute(self):
        self._recompute_stats()
        return True

    def action_open_route(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'zivo_track_live_map',
            'name': _('Route Replay'),
            'params': {'session_id': self.id},
        }

    def get_route_data(self):
        """Data for the route replay map (respects access rights)."""
        self.ensure_one()
        self.check_access('read')
        rows = self._ordered_points()
        radius = get_param(self.env, 'stop_radius_m')
        minutes = get_param(self.env, 'stop_minutes')
        max_acc = get_param(self.env, 'max_accuracy_m')
        good = [(r[0], r[1], r[2], r[3]) for r in rows if not (max_acc and r[3] and r[3] > max_acc)]
        stops = geo.detect_stops(good, radius, minutes)
        visits = self.visit_ids.sudo()
        return {
            'session': {
                'id': self.id, 'name': self.name, 'user': self.user_id.name,
                'distance_km': self.distance_km, 'point_count': self.point_count,
                'offline_point_count': self.offline_point_count, 'state': self.state,
                'first_fix': fields.Datetime.to_string(self.first_fix) if self.first_fix else False,
                'last_fix': fields.Datetime.to_string(self.last_fix) if self.last_fix else False,
            },
            'points': [{
                'lat': r[1], 'lon': r[2], 'time': fields.Datetime.to_string(r[0]),
                'accuracy': r[3], 'offline': bool(r[4]), 'delay': r[5] or 0,
            } for r in rows],
            'stops': [{
                'lat': s['lat'], 'lon': s['lon'], 'minutes': s['minutes'],
                'start': fields.Datetime.to_string(s['start']), 'end': fields.Datetime.to_string(s['end']),
            } for s in stops],
            'visits': [{
                'id': v.id, 'partner': v.partner_id.display_name, 'lat': v.check_in_latitude,
                'lon': v.check_in_longitude, 'check_in': fields.Datetime.to_string(v.check_in_time),
                'within_geofence': v.within_geofence, 'distance_m': v.distance_from_customer_m,
            } for v in visits if v.check_in_latitude or v.check_in_longitude],
        }

    @api.model
    def _cron_close_stale_days(self):
        """Close day logs of previous days that were never ended from the phone."""
        today = fields.Date.context_today(self)
        stale = self.sudo().search([('state', '=', 'active'), ('date', '<', today)])
        for session in stale:
            session.write({'state': 'closed', 'end_time': session.last_fix or fields.Datetime.now()})
        return True
