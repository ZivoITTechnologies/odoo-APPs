# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
from datetime import datetime, time, timedelta

from odoo import _, fields, models
from odoo.exceptions import UserError


class ZivoMcpKeyWizard(models.TransientModel):
    _name = 'zivo.mcp.key.wizard'
    _description = 'Generate MCP API Key'

    connector_id = fields.Many2one('zivo.mcp.connector', required=True, ondelete='cascade')
    key_name = fields.Char(string="Key Name", required=True,
                           default=lambda self: _('AnythingMCP %s', fields.Date.context_today(self)))
    duration = fields.Selection([
        ('30', '30 days'), ('90', '90 days'), ('180', '6 months'), ('365', '1 year'), ('0', 'No expiry'),
    ], default='90', required=True, string="Valid For")
    revoke_previous = fields.Boolean(string="Revoke Previous Keys", default=True,
                                     help="Revoke the integration user's existing keys so only the new one works.")
    key = fields.Char(readonly=True)
    endpoint_url = fields.Char(related='connector_id.endpoint_url')
    odoo_url = fields.Char(related='connector_id.odoo_url')
    snippet = fields.Text(readonly=True)
    state = fields.Selection([('setup', 'Setup'), ('done', 'Done')], default='setup')

    def action_generate(self):
        self.ensure_one()
        connector = self.connector_id
        user = connector.user_id
        if not user:
            raise UserError(_("Set the integration user first."))
        Keys = self.env['res.users.apikeys'].sudo()
        if self.revoke_previous:
            Keys.search([('user_id', '=', user.id)])._remove()
        days = int(self.duration)
        expiration = datetime.combine(fields.Date.context_today(self) + timedelta(days=days), time.max) if days else False
        key = Keys.with_user(user).sudo()._generate('mcp', self.key_name, expiration)
        endpoint = connector.endpoint_url
        self.write({
            'key': key,
            'state': 'done',
            'snippet': '\n'.join([
                'ODOO_URL=%s' % connector.odoo_url,
                'ODOO_MCP_API_KEY=%s' % key,
                '',
                'MCP endpoint: %s' % endpoint,
                'Header: Authorization: Bearer %s' % key,
            ]),
        })
        connector.message_post(body=_("New MCP API key '%(name)s' generated for %(user)s (%(validity)s).",
                                      name=self.key_name, user=user.login,
                                      validity=dict(self._fields['duration'].selection)[self.duration]))
        return {
            'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
            'view_mode': 'form', 'target': 'new', 'name': _('Copy your API key now'),
        }
