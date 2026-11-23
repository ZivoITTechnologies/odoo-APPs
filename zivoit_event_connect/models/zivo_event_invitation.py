# -*- coding: utf-8 -*-
import uuid

from odoo import api, fields, models
from odoo.exceptions import UserError


class ZivoEventInvitation(models.Model):
    _name = 'zivo.event.invitation'
    _inherit = ['mail.thread', 'zivo.audit.mixin']
    _description = 'Event Invitation'
    _order = 'id desc'
    _rec_name = 'email'

    event_id = fields.Many2one('event.event', required=True, ondelete='cascade', index=True)
    partner_id = fields.Many2one('res.partner', string='Contact', index=True)
    name = fields.Char(string='Invitee Name', compute='_compute_from_partner', store=True, readonly=False, precompute=True)
    email = fields.Char(compute='_compute_from_partner', store=True, readonly=False, required=True, precompute=True)
    token = fields.Char(required=True, copy=False, readonly=True, index=True,
                        default=lambda self: uuid.uuid4().hex)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('sent', 'Sent'),
        ('opened', 'Opened'),
        ('registered', 'Registered'),
        ('revoked', 'Revoked'),
    ], default='draft', required=True, tracking=True)
    sent_date = fields.Datetime(readonly=True)
    opened_date = fields.Datetime(readonly=True)
    open_count = fields.Integer(readonly=True)
    registration_id = fields.Many2one('event.registration', readonly=True)
    invite_url = fields.Char(compute='_compute_invite_url')
    company_id = fields.Many2one(related='event_id.company_id', store=True)

    _token_uniq = models.Constraint('unique(token)', 'Invitation token must be unique.')
    _event_email_uniq = models.Constraint('unique(event_id, email)', 'This email is already invited to the event.')

    @api.depends('partner_id')
    def _compute_from_partner(self):
        for inv in self:
            if inv.partner_id:
                inv.name = inv.partner_id.name
                inv.email = inv.partner_id.email

    def _compute_invite_url(self):
        for inv in self:
            base = inv.event_id.get_base_url() if inv.event_id else self.get_base_url()
            inv.invite_url = '%s/event/invite/%s' % (base, inv.token)

    def action_send(self):
        template = self.env.ref('zivoit_event_connect.mail_template_event_invitation')
        for inv in self:
            if inv.state == 'revoked':
                raise UserError(self.env._('Invitation for %s is revoked.', inv.email))
            if not inv.email:
                raise UserError(self.env._('Invitation has no email.'))
            template.send_mail(inv.id, force_send=False, email_layout_xmlid='mail.mail_notification_light')
            inv.write({'state': 'sent' if inv.state in ('draft', 'sent') else inv.state,
                       'sent_date': fields.Datetime.now()})
        return True

    def action_revoke(self):
        self.write({'state': 'revoked'})

    def action_reset(self):
        self.write({'state': 'draft', 'token': uuid.uuid4().hex})

    def _mark_opened(self):
        for inv in self:
            vals = {'open_count': inv.open_count + 1}
            if inv.state in ('draft', 'sent'):
                vals.update(state='opened', opened_date=fields.Datetime.now())
            inv.sudo().with_context(zivo_audit_skip=True).write(vals)
