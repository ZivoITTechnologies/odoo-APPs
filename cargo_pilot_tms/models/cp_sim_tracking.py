# -*- coding: utf-8 -*-
"""SIM (telecom) tracking for market trucks without GPS: the driver approves location sharing once
by SMS, then the provider returns the phone's network location.

Providers (for example Telenity, Masters India, Fretron, LocoNav) differ in paths and field names,
so the account maps them: consent request, consent status and location calls, and the JSON keys
for latitude, longitude, time and consent state.
"""
import math
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .cp_integration_base import india_phone


def _dig(payload, path):
    """Read 'data.location.lat' style paths from nested JSON."""
    value = payload
    for part in (path or '').split('.'):
        if not part:
            continue
        if isinstance(value, list):
            value = value[0] if value else None
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


class CpSimProvider(models.Model):
    _name = 'cp.sim.provider'
    _description = 'SIM Tracking Provider'
    _inherit = ['cp.integration.mixin']
    _cp_service = 'sim'
    _cp_secret_fields = ('api_key',)

    name = fields.Char(required=True, default=lambda self: _('SIM tracking'))
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    provider = fields.Selection([('http', 'HTTP JSON provider'), ('simulator', 'Simulator (no real calls)')],
                                default='http', required=True)
    base_url = fields.Char(string='API Address')
    api_key = fields.Char(groups='cargo_pilot_tms.group_cp_manager')
    auth_header = fields.Char(default='Authorization', help='Header that carries the key.')
    auth_prefix = fields.Char(default='Bearer ', help='Text before the key, for example "Bearer ".')
    consent_path = fields.Char(default='/consent', help='POST {phone, reference}: sends the consent SMS.')
    status_path = fields.Char(default='/consent/{phone}', help='GET: consent state for {phone}.')
    location_path = fields.Char(default='/location/{phone}', help='GET: latest location of {phone}.')
    key_lat = fields.Char(string='Latitude Key', default='latitude')
    key_lon = fields.Char(string='Longitude Key', default='longitude')
    key_time = fields.Char(string='Time Key', default='timestamp')
    key_consent = fields.Char(string='Consent Key', default='consent',
                              help='Value granted / approved / Y means the driver agreed.')
    interval_minutes = fields.Integer(string='Poll Every (min)', default=30)
    state = fields.Selection([('new', 'Not checked'), ('ok', 'Connected'), ('error', 'Problem')], default='new',
                             readonly=True, copy=False)
    last_message = fields.Char(readonly=True, copy=False)

    def action_test(self):
        """Check the settings and that the provider answers (no consent SMS is sent)."""
        self.ensure_one()
        if self.provider == 'simulator':
            msg = _('Simulator ready.')
        else:
            if not self.sudo().api_key:
                raise UserError(_('Enter the API key of the SIM tracking provider.'))
            self._cp_http('test', 'GET', self._url('', ''), headers=self._headers(), expect='text', timeout=15)
            msg = _('Connected to %s.', self.base_url)
        self.write({'state': 'ok', 'last_message': msg})
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'type': 'success', 'title': self.name, 'message': msg}}

    def _headers(self):
        key = self.sudo().api_key or ''
        headers = {'Content-Type': 'application/json'}
        if key:
            headers[self.auth_header or 'Authorization'] = '%s%s' % (self.auth_prefix or '', key)
        return headers

    def _url(self, path, phone):
        if not self.base_url:
            raise UserError(_('Enter the API address of the SIM tracking provider.'))
        return self.base_url.rstrip('/') + (path or '').replace('{phone}', phone)

    @staticmethod
    def _granted(value):
        return str(value or '').strip().lower() in ('granted', 'approved', 'y', 'yes', 'true', '1', 'active',
                                                    'consent_approved')

    def cp_request_consent(self, trip):
        phone = india_phone(trip.driver_phone)
        if self.provider == 'simulator':
            return 'requested'
        data = self._cp_http('consent', 'POST', self._url(self.consent_path, phone), headers=self._headers(),
                             json_body={'phone': phone, 'reference': trip.name}, record=trip)
        return 'granted' if self._granted(_dig(data, self.key_consent)) else 'requested'

    def cp_consent_state(self, trip):
        phone = india_phone(trip.driver_phone)
        if self.provider == 'simulator':
            return 'granted'
        data = self._cp_http('consent_status', 'GET', self._url(self.status_path, phone), headers=self._headers(),
                             record=trip)
        value = _dig(data, self.key_consent)
        if str(value or '').lower() in ('denied', 'rejected', 'n', 'no'):
            return 'denied'
        return 'granted' if self._granted(value) else 'requested'

    def cp_location(self, trip):
        phone = india_phone(trip.driver_phone)
        if self.provider == 'simulator':
            o, d = trip.origin_id, trip.destination_id
            start = trip.actual_dispatch or fields.Datetime.now()
            share = min((fields.Datetime.now() - start).total_seconds() / 3600.0 / max(trip.lane_id.transit_hours or 24, 1),
                        0.95)
            return {'lat': o.latitude + (d.latitude - o.latitude) * share,
                    'lon': o.longitude + (d.longitude - o.longitude) * share, 'time': fields.Datetime.now()}
        data = self._cp_http('location', 'GET', self._url(self.location_path, phone), headers=self._headers(),
                             record=trip)
        try:
            lat, lon = float(_dig(data, self.key_lat)), float(_dig(data, self.key_lon))
        except (TypeError, ValueError):
            return None
        if math.isclose(lat, 0.0) and math.isclose(lon, 0.0):
            return None
        stamp = _dig(data, self.key_time)
        when = None
        if isinstance(stamp, (int, float)):
            from datetime import datetime, timezone
            when = datetime.fromtimestamp(stamp / 1000 if stamp > 1e11 else stamp, timezone.utc).replace(tzinfo=None)
        elif stamp:
            when = fields.Datetime.to_datetime(str(stamp).replace('T', ' ')[:19])
        return {'lat': lat, 'lon': lon, 'time': when or fields.Datetime.now()}

    @api.model
    def cp_for(self, company):
        return self.search([('company_id', '=', company.id)], limit=1)

    @api.model
    def _cron_sim(self):
        Trip = self.env['cp.trip']
        for provider in self.search([]):
            due = fields.Datetime.now() - timedelta(minutes=provider.interval_minutes or 30)
            trips = Trip.search([('company_id', '=', provider.company_id.id), ('tracking_method', '=', 'sim'),
                                 ('state', 'in', ('assigned', 'reported', 'loading', 'in_transit')),
                                 ('sim_consent', 'in', ('requested', 'granted')),
                                 '|', ('sim_last_sync', '=', False), ('sim_last_sync', '<', due)], limit=300)
            for trip in trips:
                try:
                    trip._cp_sim_sync(provider)
                except UserError:
                    continue
        return True


class CpTrip(models.Model):
    _inherit = 'cp.trip'

    sim_consent = fields.Selection([('none', 'Not asked'), ('requested', 'SMS sent'), ('granted', 'Approved'),
                                    ('denied', 'Refused')], default='none', string='SIM Consent', copy=False)
    sim_last_sync = fields.Datetime(readonly=True, copy=False)

    def action_sim_request_consent(self):
        for rec in self:
            provider = self.env['cp.sim.provider'].cp_for(rec.company_id)
            if not provider:
                raise UserError(_('Add a SIM tracking provider first (Setup > India Integrations).'))
            if not rec.driver_phone:
                raise UserError(_('Enter the driver phone number of %s.', rec.name))
            rec.sim_consent = provider.cp_request_consent(rec)
            rec.tracking_method = 'sim'
            rec._cp_log('tracking', _('SIM tracking consent requested from %s.', rec.driver_phone))
        return True

    def _cp_sim_sync(self, provider=None):
        self.ensure_one()
        provider = provider or self.env['cp.sim.provider'].cp_for(self.company_id)
        if not provider:
            raise UserError(_('Add a SIM tracking provider first.'))
        if self.sim_consent != 'granted':
            self.sim_consent = provider.cp_consent_state(self)
            if self.sim_consent != 'granted':
                self.sim_last_sync = fields.Datetime.now()
                return False
        point = provider.cp_location(self)
        self.sim_last_sync = fields.Datetime.now()
        if not point:
            return False
        self.cp_add_ping(point['lat'], point['lon'], ping_time=point['time'], source='sim')
        return True

    def action_sim_sync(self):
        for rec in self:
            rec._cp_sim_sync()
        return True

    def _cp_after_assign(self):
        res = super()._cp_after_assign()
        for rec in self.filtered(lambda t: t.tracking_method == 'sim' and t.driver_phone and t.sim_consent == 'none'):
            try:
                rec.action_sim_request_consent()
            except UserError:
                continue
        return res
