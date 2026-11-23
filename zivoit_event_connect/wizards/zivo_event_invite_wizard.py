# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError


class ZivoEventInviteWizard(models.TransientModel):
    _name = 'zivo.event.invite.wizard'
    _description = 'Invite Contacts to Event'

    event_id = fields.Many2one('event.event', required=True)
    partner_ids = fields.Many2many('res.partner', string='Contacts')
    department_ids = fields.Many2many('zivo.event.department', string='Invite Department Members')
    emails = fields.Text(string='Additional Emails', help='One email per line (for people not yet in Contacts).')
    send_now = fields.Boolean(string='Send Invitations Now', default=True)

    def _collect(self):
        self.ensure_one()
        result = []
        partners = self.partner_ids | self.department_ids.member_ids.partner_id
        for partner in partners:
            if partner.email:
                result.append({'partner_id': partner.id, 'email': partner.email.strip().lower(), 'name': partner.name})
        for line in (self.emails or '').replace(',', '\n').splitlines():
            email = line.strip().lower()
            if email and '@' in email:
                result.append({'partner_id': False, 'email': email, 'name': email.split('@')[0]})
        return result

    def action_invite(self):
        self.ensure_one()
        entries = self._collect()
        if not entries:
            raise UserError(self.env._('Please select at least one contact with an email address.'))
        Invitation = self.env['zivo.event.invitation']
        existing = set(Invitation.search([('event_id', '=', self.event_id.id)]).mapped(lambda i: i.email.lower()))
        vals_list, seen = [], set()
        for entry in entries:
            if entry['email'] in existing or entry['email'] in seen:
                continue
            seen.add(entry['email'])
            vals_list.append(dict(entry, event_id=self.event_id.id))
        invitations = Invitation.create(vals_list)
        if self.send_now and invitations:
            invitations.action_send()
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'type': 'success',
                'message': self.env._('%(n)s invitation(s) created, %(s)s skipped (already invited).',
                                      n=len(invitations), s=len(entries) - len(invitations)),
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
