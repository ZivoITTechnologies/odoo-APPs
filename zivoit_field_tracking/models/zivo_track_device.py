# -*- coding: utf-8 -*-
import secrets

from odoo import _, api, fields, models
from odoo.exceptions import UserError

PARAM_PREFIX = 'zivoit_field_tracking.'
DEFAULTS = {
    'interval_sec': 60,
    'min_distance_m': 25,
    'max_accuracy_m': 150,
    'offline_threshold_min': 3,
    'live_minutes': 5,
    'delayed_minutes': 30,
    'geofence_radius_m': 200,
    'retention_days': 180,
    'max_speed_kmh': 250,
    'stop_radius_m': 100,
    'stop_minutes': 10,
    'tile_url': 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    'tile_attribution': '&copy; OpenStreetMap contributors',
}


def get_param(env, key):
    """Typed read of a module setting (falls back to DEFAULTS)."""
    default = DEFAULTS[key]
    raw = env['ir.config_parameter'].sudo().get_param(PARAM_PREFIX + key)
    if raw in (None, False, ''):
        return default
    if isinstance(default, int):
        try:
            return int(float(raw))
        except ValueError:
            return default
    return raw


class ZivoTrackDevice(models.Model):
    _name = 'zivo.track.device'
    _description = 'Field Tracking Device'
    _inherit = ['mail.thread']
    _order = 'last_seen desc, id desc'

    name = fields.Char(required=True, default=lambda self: _('Phone'), tracking=True)
    user_id = fields.Many2one('res.users', string='Salesperson', required=True, index=True,
                              default=lambda self: self.env.user, tracking=True, ondelete='cascade')
    company_id = fields.Many2one(related='user_id.company_id', store=True)
    active = fields.Boolean(default=True, tracking=True)
    token = fields.Char(string='Device Identifier', required=True, copy=False, index='btree',
                        default=lambda self: self._generate_token(), groups='zivoit_field_tracking.group_field_user',
                        help='Secret identifier of this phone. Enter it as "Device identifier" '
                             'in Traccar Client / OsmAnd. Keep it private.')
    source = fields.Selection([
        ('pwa', 'ZivoIT Offline Web App'),
        ('traccar', 'Traccar Client'),
        ('osmand', 'OsmAnd / other OsmAnd-protocol app'),
        ('api', 'Custom app (REST API)'),
    ], default='pwa', required=True, tracking=True)
    user_agent = fields.Char(readonly=True)
    last_seen = fields.Datetime(readonly=True, help='Last time the server received data from this device')
    last_fix_time = fields.Datetime(readonly=True)
    last_latitude = fields.Float(digits=(10, 7), readonly=True)
    last_longitude = fields.Float(digits=(10, 7), readonly=True)
    last_battery = fields.Float(string='Last Battery (%)', digits=(5, 0), readonly=True)
    mock_detected = fields.Boolean(readonly=True, help='The phone reported a mock / fake GPS location')
    point_count = fields.Integer(compute='_compute_point_count')
    server_url = fields.Char(compute='_compute_urls')
    traccar_url = fields.Char(string='Traccar / OsmAnd Server URL', compute='_compute_urls')

    _token_unique = models.Constraint('UNIQUE(token)', 'The device identifier must be unique.')

    @api.model
    def _generate_token(self):
        return 'zv-' + secrets.token_hex(12)

    def _compute_point_count(self):
        groups = self.env['zivo.track.point']._read_group(
            [('device_id', 'in', self.ids)], ['device_id'], ['__count'])
        counts = {device.id: count for device, count in groups}
        for device in self:
            device.point_count = counts.get(device.id, 0)

    def _compute_urls(self):
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '').rstrip('/')
        for device in self:
            device.server_url = base
            device.traccar_url = '%s/zivoit_tracking/osmand' % base

    def action_regenerate_token(self):
        for device in self:
            device.sudo().token = self._generate_token()
        return True

    def action_view_points(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Location Points'),
            'res_model': 'zivo.track.point',
            'view_mode': 'list,form,pivot,graph',
            'domain': [('device_id', '=', self.id)],
        }

    @api.model
    def _authenticate(self, token):
        """Return the active device for ``token`` (sudo) or an empty recordset."""
        if not token or not isinstance(token, str) or len(token) > 128:
            return self.sudo().browse()
        device = self.sudo().search([('token', '=', token.strip()), ('active', '=', True)], limit=1)
        if device and not device.user_id.active:
            return self.sudo().browse()
        if device and not device.user_id.zivo_tracking_enabled:
            return self.sudo().browse()
        return device

    @api.model
    def _get_or_create_pwa_device(self, user, user_agent=None):
        """Device used by the built-in offline web app for ``user``."""
        if not user.zivo_tracking_enabled:
            raise UserError(_('Location tracking is not enabled for %s.', user.name))
        device = self.sudo().search([('user_id', '=', user.id), ('source', '=', 'pwa'), ('active', '=', True)],
                                    limit=1)
        if not device:
            device = self.sudo().create({
                'name': _('%s - Offline Web App', user.name),
                'user_id': user.id,
                'source': 'pwa',
                'user_agent': (user_agent or '')[:250],
            })
        elif user_agent:
            device.user_agent = user_agent[:250]
        return device
