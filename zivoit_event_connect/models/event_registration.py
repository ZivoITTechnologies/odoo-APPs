# -*- coding: utf-8 -*-
from odoo import api, models


class EventRegistration(models.Model):
    _inherit = 'event.registration'

    def _zivo_api_payload(self):
        self.ensure_one()
        reg = self.sudo()
        return {
            'id': reg.id,
            'event_id': reg.event_id.id,
            'event_name': reg.event_id.name,
            'name': reg.name,
            'email': reg.email,
            'state': reg.state,
            'ticket': reg.event_ticket_id.name or None,
            'created_at': reg.event_id._zivo_dt(reg.create_date),
        }

    @api.model_create_multi
    def create(self, vals_list):
        registrations = super().create(vals_list)
        Invitation = self.env['zivo.event.invitation'].sudo()
        for reg in registrations:
            if reg.event_id.zivo_access_mode != 'invite':
                continue
            domain = [('event_id', '=', reg.event_id.id), ('state', '!=', 'revoked'), ('registration_id', '=', False)]
            invitation = Invitation.search(domain + [('partner_id', '=', reg.partner_id.id)], limit=1) if reg.partner_id else Invitation
            if not invitation and reg.email:
                invitation = Invitation.search(domain + [('email', '=ilike', reg.email)], limit=1)
            if invitation:
                invitation.write({'state': 'registered', 'registration_id': reg.id})
        published = registrations.filtered(lambda r: r.event_id.zivo_api_published)
        if published:
            self.env['zivo.webhook']._zivo_enqueue_raw(
                'registration.created', [r._zivo_api_payload() for r in published])
        return registrations
