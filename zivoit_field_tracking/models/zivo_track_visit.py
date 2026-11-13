# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from . import geo
from .zivo_track_device import get_param

OUTCOMES = [
    ('order', 'Order Taken'),
    ('follow_up', 'Follow-up Needed'),
    ('collection', 'Payment Collected'),
    ('demo', 'Demo / Presentation'),
    ('no_interest', 'Not Interested'),
    ('closed', 'Shop Closed'),
    ('other', 'Other'),
]


class ZivoTrackVisit(models.Model):
    _name = 'zivo.track.visit'
    _description = 'Customer Visit'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'check_in_time desc, id desc'

    name = fields.Char(compute='_compute_name', store=True)
    user_id = fields.Many2one('res.users', string='Salesperson', required=True, index=True,
                              default=lambda self: self.env.user, tracking=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    partner_id = fields.Many2one('res.partner', string='Customer', required=True, index=True, tracking=True)
    session_id = fields.Many2one('zivo.track.session', string='Day Log', index=True, ondelete='set null')
    device_id = fields.Many2one('zivo.track.device', ondelete='set null')
    state = fields.Selection([('in_progress', 'Checked In'), ('done', 'Checked Out')],
                             default='in_progress', required=True, tracking=True)
    check_in_time = fields.Datetime(required=True, default=fields.Datetime.now, tracking=True)
    check_out_time = fields.Datetime(tracking=True)
    duration_minutes = fields.Float(string='Duration (min)', compute='_compute_duration', store=True, digits=(8, 1))
    check_in_latitude = fields.Float(digits=(10, 7))
    check_in_longitude = fields.Float(digits=(10, 7))
    check_in_accuracy = fields.Float(string='Check-in Accuracy (m)')
    check_out_latitude = fields.Float(digits=(10, 7))
    check_out_longitude = fields.Float(digits=(10, 7))
    distance_from_customer_m = fields.Float(string='Distance from Customer (m)', digits=(10, 0), readonly=True)
    within_geofence = fields.Boolean(string='Inside Geo-fence', readonly=True)
    geofence_status = fields.Selection([
        ('inside', 'Inside geo-fence'),
        ('outside', 'Outside geo-fence'),
        ('unknown', 'Customer has no location'),
    ], compute='_compute_geofence_status', store=True)
    outcome = fields.Selection(OUTCOMES, tracking=True)
    note = fields.Text()
    captured_offline = fields.Boolean(string='Recorded Offline', readonly=True)
    client_uid = fields.Char(index=True, copy=False)

    _device_uid_unique = models.Constraint('UNIQUE(device_id, client_uid)', 'Duplicate visit.')

    @api.depends('partner_id', 'check_in_time')
    def _compute_name(self):
        for visit in self:
            visit.name = '%s - %s' % (visit.partner_id.display_name or _('Visit'),
                                      fields.Datetime.to_string(visit.check_in_time) if visit.check_in_time else '')

    @api.depends('check_in_time', 'check_out_time')
    def _compute_duration(self):
        for visit in self:
            if visit.check_in_time and visit.check_out_time:
                visit.duration_minutes = max(0.0, (visit.check_out_time - visit.check_in_time).total_seconds() / 60.0)
            else:
                visit.duration_minutes = 0.0

    @api.depends('within_geofence', 'distance_from_customer_m', 'partner_id.partner_latitude',
                 'partner_id.partner_longitude')
    def _compute_geofence_status(self):
        for visit in self:
            partner = visit.partner_id
            if not (partner.partner_latitude or partner.partner_longitude):
                visit.geofence_status = 'unknown'
            else:
                visit.geofence_status = 'inside' if visit.within_geofence else 'outside'

    @api.constrains('check_in_time', 'check_out_time')
    def _check_times(self):
        for visit in self:
            if visit.check_out_time and visit.check_in_time and visit.check_out_time < visit.check_in_time:
                raise ValidationError(_('Check-out cannot be before check-in.'))

    def _update_geofence(self):
        radius = get_param(self.env, 'geofence_radius_m')
        for visit in self:
            partner = visit.partner_id
            if not (visit.check_in_latitude or visit.check_in_longitude):
                continue
            if partner.partner_latitude or partner.partner_longitude:
                dist = geo.haversine_m(visit.check_in_latitude, visit.check_in_longitude,
                                       partner.partner_latitude, partner.partner_longitude)
                visit.write({'distance_from_customer_m': round(dist),
                             'within_geofence': dist <= radius})
            elif self.env['ir.config_parameter'].sudo().get_param(
                    'zivoit_field_tracking.learn_customer_location') and \
                    (not visit.check_in_accuracy or visit.check_in_accuracy <= get_param(self.env, 'max_accuracy_m')):
                partner.sudo().write({'partner_latitude': visit.check_in_latitude,
                                      'partner_longitude': visit.check_in_longitude})
                visit.write({'distance_from_customer_m': 0, 'within_geofence': True})

    def action_check_out(self):
        for visit in self.filtered(lambda v: v.state == 'in_progress'):
            visit.write({'state': 'done', 'check_out_time': fields.Datetime.now()})
        return True

    @api.model
    def _ingest(self, device, items):
        """Upsert visits sent by the phone (check-in, then later the check-out)."""
        result = {'accepted': [], 'rejected': []}
        device = device.sudo()
        user = device.user_id
        Partner = self.env['res.partner'].with_user(user)
        offline_threshold = get_param(self.env, 'offline_threshold_min')
        now = fields.Datetime.now()
        for item in items[:500]:
            uid = str(item.get('uid') or '')[:64]
            if not uid:
                result['rejected'].append({'uid': uid, 'reason': 'missing_uid'})
                continue
            try:
                partner = Partner.browse(int(item.get('partner_id') or 0)).exists()
                partner.check_access('read') if partner else None
            except Exception:  # noqa: BLE001 - access error or bad id
                partner = Partner.browse()
            if not partner:
                result['rejected'].append({'uid': uid, 'reason': 'unknown_customer'})
                continue
            check_in = geo.parse_timestamp(item.get('check_in_ts')) or now
            check_out = geo.parse_timestamp(item.get('check_out_ts'))
            if check_out and check_out < check_in:
                check_out = check_in
            vals = {
                'partner_id': partner.id,
                'check_in_time': check_in,
                'check_in_latitude': geo.to_float(item.get('lat'), 0.0),
                'check_in_longitude': geo.to_float(item.get('lon'), 0.0),
                'check_in_accuracy': geo.to_float(item.get('accuracy'), 0.0),
                'outcome': item.get('outcome') if item.get('outcome') in dict(OUTCOMES) else False,
                'note': (item.get('note') or '')[:4000] or False,
                'captured_offline': geo.to_bool(item.get('offline', False))
                or (now - check_in).total_seconds() / 60.0 > offline_threshold,
            }
            if check_out:
                vals.update({
                    'state': 'done',
                    'check_out_time': check_out,
                    'check_out_latitude': geo.to_float(item.get('out_lat'), 0.0),
                    'check_out_longitude': geo.to_float(item.get('out_lon'), 0.0),
                })
            visit = self.sudo().search([('device_id', '=', device.id), ('client_uid', '=', uid)], limit=1)
            if visit:
                visit.write(vals)
            else:
                vals.update({
                    'device_id': device.id,
                    'client_uid': uid,
                    'user_id': user.id,
                    'company_id': user.company_id.id,
                    'session_id': self.env['zivo.track.session']._get_for(user, check_in).id,
                })
                visit = self.sudo().create(vals)
            visit._update_geofence()
            result['accepted'].append(uid)
        return result
