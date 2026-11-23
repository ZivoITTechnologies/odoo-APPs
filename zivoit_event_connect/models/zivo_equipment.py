# -*- coding: utf-8 -*-
from datetime import datetime, time, timedelta

import pytz

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools.date_utils import float_to_time

from odoo.addons.base.models.res_partner import _tz_get

WEEKDAY_FIELDS = ['open_mon', 'open_tue', 'open_wed', 'open_thu', 'open_fri', 'open_sat', 'open_sun']
ACTIVE_BOOKING_STATES = ('submitted', 'approved')


class ZivoEquipmentCategory(models.Model):
    _name = 'zivo.equipment.category'
    _description = 'Equipment Category'
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    color = fields.Integer()
    equipment_ids = fields.One2many('zivo.equipment', 'category_id')


class ZivoEquipment(models.Model):
    _name = 'zivo.equipment'
    _inherit = ['mail.thread', 'image.mixin']
    _description = 'Bookable Equipment'
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True, tracking=True)
    code = fields.Char(string='Reference', tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    category_id = fields.Many2one('zivo.equipment.category', string='Category', tracking=True)
    description = fields.Html(translate=True)
    location = fields.Char(help='Where the equipment is stored / picked up.')
    quantity = fields.Integer(string='Units Available', default=1, required=True, tracking=True,
                              help='How many identical units can be booked at the same time.')
    department_id = fields.Many2one('zivo.event.department', string='Owning Department',
                                    help='The department manager is automatically an approver.')
    requires_approval = fields.Boolean(default=True, tracking=True,
                                       help='When unchecked bookings are confirmed instantly when free.')
    approver_ids = fields.Many2many('res.users', 'zivo_equipment_approver_rel', 'equipment_id', 'user_id',
                                    string='Approvers',
                                    help='Leave empty to route approvals to every Equipment Approver.')
    portal_bookable = fields.Boolean(string='Bookable from Portal', default=True,
                                     help='Visible on the /equipment self-service booking screen.')
    # availability window
    tz = fields.Selection(_tz_get, string='Timezone', required=True,
                          default=lambda self: self.env.user.tz or self.env.company.partner_id.tz or 'UTC')
    slot_duration = fields.Float(string='Slot Duration (h)', default=1.0, required=True)
    open_hour = fields.Float(string='Opens At', default=8.0, required=True)
    close_hour = fields.Float(string='Closes At', default=18.0, required=True)
    open_mon = fields.Boolean('Mon', default=True)
    open_tue = fields.Boolean('Tue', default=True)
    open_wed = fields.Boolean('Wed', default=True)
    open_thu = fields.Boolean('Thu', default=True)
    open_fri = fields.Boolean('Fri', default=True)
    open_sat = fields.Boolean('Sat')
    open_sun = fields.Boolean('Sun')
    min_notice_hours = fields.Float(string='Minimum Notice (h)', default=0.0)
    max_days_ahead = fields.Integer(string='Book Up To (days ahead)', default=90)
    max_slots_per_booking = fields.Integer(string='Max Slots per Booking', default=8)
    booking_ids = fields.One2many('zivo.equipment.booking', 'equipment_id', string='Bookings')
    booking_count = fields.Integer(compute='_compute_booking_count')
    pending_count = fields.Integer(compute='_compute_booking_count')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    color = fields.Integer()

    @api.depends('booking_ids.state')
    def _compute_booking_count(self):
        data = {}
        for equipment, state, count in self.env['zivo.equipment.booking']._read_group(
                [('equipment_id', 'in', self.ids)], ['equipment_id', 'state'], ['__count']):
            data.setdefault(equipment.id, {})[state] = count
        for eq in self:
            d = data.get(eq.id, {})
            eq.booking_count = sum(d.values())
            eq.pending_count = d.get('submitted', 0)

    @api.constrains('quantity', 'slot_duration', 'open_hour', 'close_hour')
    def _check_config(self):
        for eq in self:
            if eq.quantity < 1:
                raise ValidationError(self.env._('Units available must be at least 1.'))
            if eq.slot_duration <= 0:
                raise ValidationError(self.env._('Slot duration must be positive.'))
            if not (0 <= eq.open_hour < eq.close_hour <= 24):
                raise ValidationError(self.env._('Opening hours are invalid for %s.', eq.name))

    # ------------------------------------------------------------------
    # Availability engine
    # ------------------------------------------------------------------
    def _zivo_reserved_peak(self, start, stop, exclude=None):
        """Peak number of units reserved between start and stop (UTC naive datetimes)."""
        self.ensure_one()
        domain = [('equipment_id', '=', self.id), ('state', 'in', ACTIVE_BOOKING_STATES),
                  ('start', '<', stop), ('stop', '>', start)]
        if exclude:
            domain.append(('id', 'not in', exclude.ids))
        bookings = self.env['zivo.equipment.booking'].sudo().search(domain)
        points = []
        for b in bookings:
            points.append((max(b.start, start), 1, b.quantity))
            points.append((min(b.stop, stop), 0, -b.quantity))
        peak = current = 0
        for _dt, _order, delta in sorted(points):
            current += delta
            peak = max(peak, current)
        return peak

    def _zivo_available_qty(self, start, stop, exclude=None):
        self.ensure_one()
        return max(self.quantity - self._zivo_reserved_peak(start, stop, exclude=exclude), 0)

    def _zivo_is_open_day(self, day):
        self.ensure_one()
        return bool(self[WEEKDAY_FIELDS[day.weekday()]])

    def _zivo_local_to_utc(self, local_dt):
        tz = pytz.timezone(self.tz or 'UTC')
        return tz.localize(local_dt).astimezone(pytz.utc).replace(tzinfo=None)

    def _zivo_utc_to_local(self, utc_dt):
        tz = pytz.timezone(self.tz or 'UTC')
        return pytz.utc.localize(utc_dt).astimezone(tz).replace(tzinfo=None)

    def _zivo_get_slots(self, day):
        """Return the list of bookable slots for a local date.

        Each slot: {'start': utc dt, 'stop': utc dt, 'label': 'HH:MM - HH:MM', 'available': int, 'bookable': bool}
        """
        self.ensure_one()
        if not self._zivo_is_open_day(day):
            return []
        now = fields.Datetime.now()
        earliest = now + timedelta(hours=self.min_notice_hours or 0)
        horizon = now + timedelta(days=self.max_days_ahead or 3650)
        slots = []
        cursor = self.open_hour
        step = self.slot_duration
        while cursor + step <= self.close_hour + 1e-6:
            local_start = datetime.combine(day, float_to_time(cursor) if cursor < 24 else time.max)
            local_stop = local_start + timedelta(hours=step)
            start = self._zivo_local_to_utc(local_start)
            stop = self._zivo_local_to_utc(local_stop)
            available = self._zivo_available_qty(start, stop)
            slots.append({
                'start': start,
                'stop': stop,
                'label': '%s - %s' % (local_start.strftime('%H:%M'), local_stop.strftime('%H:%M')),
                'available': available,
                'bookable': available > 0 and start >= earliest and start <= horizon,
            })
            cursor += step
        return slots

    def _zivo_check_window(self, start, stop):
        """Validate a requested interval against the opening window (raises ValidationError)."""
        self.ensure_one()
        local_start = self._zivo_utc_to_local(start)
        local_stop = self._zivo_utc_to_local(stop)
        if local_start.date() != local_stop.date() and not (local_stop.time() == time(0, 0)
                                                            and local_stop.date() == local_start.date() + timedelta(days=1)):
            raise ValidationError(self.env._('A booking of %s must start and end on the same day.', self.name))
        if not self._zivo_is_open_day(local_start.date()):
            raise ValidationError(self.env._('%(eq)s cannot be booked on %(day)s.',
                                             eq=self.name, day=local_start.strftime('%A')))
        start_h = local_start.hour + local_start.minute / 60.0
        stop_h = (local_stop.hour + local_stop.minute / 60.0) or 24.0
        if start_h < self.open_hour - 1e-6 or stop_h > self.close_hour + 1e-6:
            raise ValidationError(self.env._('%(eq)s can only be booked between %(o)s and %(c)s.',
                                             eq=self.name,
                                             o=float_to_time(self.open_hour).strftime('%H:%M'),
                                             c=float_to_time(min(self.close_hour, 23.999)).strftime('%H:%M')))
        slots = (stop - start).total_seconds() / 3600.0 / (self.slot_duration or 1)
        if self.max_slots_per_booking and slots > self.max_slots_per_booking + 1e-6:
            raise ValidationError(self.env._('%(eq)s can be booked for at most %(n)s slot(s) at a time.',
                                             eq=self.name, n=self.max_slots_per_booking))

    def _zivo_get_approvers(self):
        self.ensure_one()
        users = self.approver_ids | self.department_id.manager_id
        if not users:
            group = self.env.ref('zivoit_event_connect.group_equipment_approver')
            users = group.sudo().all_user_ids.filtered(lambda u: u.active and not u.share)
        return users

    def action_view_bookings(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Bookings of %s', self.name),
            'res_model': 'zivo.equipment.booking',
            'view_mode': 'calendar,list,form',
            'domain': [('equipment_id', '=', self.id)],
            'context': {'default_equipment_id': self.id},
        }
