# -*- coding: utf-8 -*-
import hmac
import secrets

from odoo import api, fields, models


def _new_key():
    return 'zec_' + secrets.token_urlsafe(32)


class ZivoApiClient(models.Model):
    _name = 'zivo.api.client'
    _inherit = ['mail.thread']
    _description = 'Headless API Client'
    _order = 'name'

    name = fields.Char(required=True, help='e.g. "Corporate WordPress site"')
    active = fields.Boolean(default=True, tracking=True)
    api_key = fields.Char(string='API Key', required=True, copy=False, default=_new_key,
                          groups='zivoit_event_connect.group_event_connect_manager', tracking=True)
    key_preview = fields.Char(compute='_compute_key_preview', string='Key')
    allowed_origins = fields.Text(
        string='Allowed CORS Origins',
        help='One origin per line (e.g. https://www.example.com). Use * to allow any origin. '
             'Leave empty for server-to-server use only.')
    include_restricted = fields.Boolean(
        string='Include Restricted Events',
        help='Also return IP-restricted and invitation-only events (use only for intranet sites).')
    include_unpublished = fields.Boolean(string='Include Unpublished Events')
    allow_registration = fields.Boolean(
        string='Allow Registrations', default=True,
        help='Allow this client to create attendee registrations through the API.')
    department_ids = fields.Many2many('zivo.event.department', string='Limit to Departments',
                                      help='Restrict this client to events of these departments.')
    website_id = fields.Many2one('website', string='Website',
                                 help='Restrict this client to events of this website (plus events with no website).')
    call_count = fields.Integer(readonly=True, default=0)
    last_call = fields.Datetime(readonly=True)
    last_ip = fields.Char(readonly=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    note = fields.Text()

    def _compute_key_preview(self):
        for client in self:
            key = client.sudo().api_key or ''
            client.key_preview = (key[:8] + '…' + key[-4:]) if key else False

    def action_regenerate_key(self):
        for client in self:
            client.sudo().api_key = _new_key()
        return True

    @api.model
    def _zivo_authenticate(self, key):
        if not key:
            return self.browse()
        clients = self.sudo().search([('active', '=', True)])
        for client in clients:
            if client.api_key and hmac.compare_digest(client.api_key, key):
                return client
        return self.browse()

    def _zivo_origin_allowed(self, origin):
        self.ensure_one()
        if not origin:
            return False
        origins = [o.strip().rstrip('/') for o in (self.allowed_origins or '').splitlines() if o.strip()]
        return '*' in origins or origin.rstrip('/') in origins

    def _zivo_event_domain(self):
        self.ensure_one()
        domain = [('zivo_api_published', '=', True)]
        if not self.include_unpublished:
            domain.append(('website_published', '=', True))
        if not self.include_restricted:
            domain.append(('zivo_access_mode', 'in', ('open', False)))
            domain.append(('website_visibility', '=', 'public'))
        if self.department_ids:
            domain.append(('zivo_department_ids', 'in', self.department_ids.ids))
        if self.website_id:
            domain += ['|', ('website_id', '=', False), ('website_id', '=', self.website_id.id)]
        if self.company_id:
            domain += ['|', ('company_id', '=', False), ('company_id', '=', self.company_id.id)]
        return domain

    def _zivo_touch(self, ip=None):
        for client in self.sudo():
            self.env.cr.execute(
                "UPDATE zivo_api_client SET call_count = COALESCE(call_count, 0) + 1, last_call = now() at time zone 'UTC', "
                "last_ip = %s WHERE id = %s", (ip, client.id))
