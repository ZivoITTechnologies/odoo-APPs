# -*- coding: utf-8 -*-
from odoo import fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    zivo_tracking_enabled = fields.Boolean(
        string='Field Location Tracking',
        compute='_compute_zivo_tracking_enabled', search='_search_zivo_tracking_enabled',
        compute_sudo=True,
        help='The user belongs to the "Field Tracking / Salesperson" group, so the phone can send locations.')
    zivo_device_ids = fields.One2many('zivo.track.device', 'user_id', string='Tracking Devices')

    def _zivo_group(self):
        return self.env.ref('zivoit_field_tracking.group_field_user', raise_if_not_found=False)

    def _compute_zivo_tracking_enabled(self):
        group = self._zivo_group()
        for user in self:
            user.zivo_tracking_enabled = bool(group) and group in user.all_group_ids

    def _search_zivo_tracking_enabled(self, operator, value):
        group = self._zivo_group()
        if not group:
            return [('id', '=', 0)]
        if operator not in ('=', '!=', 'in', 'not in'):
            return NotImplemented
        positive = (operator in ('=', 'in')) == bool(value if not isinstance(value, (list, tuple)) else True in value)
        return [('all_group_ids', 'in' if positive else 'not in', group.ids)]
