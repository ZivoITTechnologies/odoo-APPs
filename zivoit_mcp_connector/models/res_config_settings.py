# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    zivoit_mcp_endpoint_disabled = fields.Boolean(
        string="Disable MCP Endpoint", config_parameter='zivoit_mcp.endpoint_disabled',
        help="Emergency switch: every AI request is refused while set.")
    zivoit_mcp_hide_payloads = fields.Boolean(
        string="Do Not Store Tool Arguments", config_parameter='zivoit_mcp.hide_payloads',
        help="The audit log keeps the tool, model, status and timing but not the arguments sent by the AI.")
    zivoit_mcp_log_retention_days = fields.Integer(
        string="Audit Log Retention (days)", config_parameter='zivoit_mcp.log_retention_days', default=90)
    zivoit_mcp_public_url = fields.Char(
        string="Public Odoo URL", config_parameter='zivoit_mcp.public_url',
        help="Address AI platforms use to reach this Odoo (e.g. behind a proxy). Defaults to web.base.url.")
    zivoit_mcp_allowed_origins = fields.Char(
        string="Allowed Browser Origins", config_parameter='zivoit_mcp.allowed_origins',
        help="Comma-separated origins allowed to call the endpoint from a browser. Server-to-server calls "
             "(AnythingMCP, Claude) send no origin and are not affected.")
    zivoit_mcp_oauth_disabled = fields.Boolean(
        string="Disable Sign in with Odoo", config_parameter='zivoit_mcp.oauth_disabled',
        help="Turn off the OAuth sign-in used by Claude, ChatGPT and Cursor. API-key connectors keep working.")
    zivoit_mcp_oauth_dcr_disabled = fields.Boolean(
        string="Disable Automatic App Registration", config_parameter='zivoit_mcp.oauth_dcr_disabled',
        help="When set, AI applications cannot register themselves; create them under Applications (OAuth) instead.")
    zivoit_mcp_oauth_access_ttl = fields.Integer(
        string="Access Token Lifetime (minutes)", config_parameter='zivoit_mcp.oauth_access_ttl_minutes', default=60)
    zivoit_mcp_oauth_refresh_ttl = fields.Integer(
        string="Session Lifetime (days)", config_parameter='zivoit_mcp.oauth_refresh_ttl_days', default=30)
    zivoit_mcp_endpoint_url = fields.Char(string="MCP Endpoint", compute='_compute_zivoit_mcp_endpoint_url')

    @api.depends('zivoit_mcp_public_url')
    def _compute_zivoit_mcp_endpoint_url(self):
        for rec in self:
            base = (rec.zivoit_mcp_public_url or rec.env['ir.config_parameter'].sudo().get_param('web.base.url') or '')
            rec.zivoit_mcp_endpoint_url = base.rstrip('/') + '/mcp'
