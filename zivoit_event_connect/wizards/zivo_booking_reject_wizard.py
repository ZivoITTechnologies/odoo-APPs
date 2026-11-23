# -*- coding: utf-8 -*-
from odoo import fields, models


class ZivoBookingRejectWizard(models.TransientModel):
    _name = 'zivo.booking.reject.wizard'
    _description = 'Reject Equipment Booking'

    booking_ids = fields.Many2many('zivo.equipment.booking', required=True)
    reason = fields.Text(required=True)

    def action_confirm(self):
        self.booking_ids.action_reject(reason=self.reason)
        return {'type': 'ir.actions.act_window_close'}
