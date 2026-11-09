# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
from datetime import timedelta

from odoo import api, fields, models

from ..services import utils


class ZivoMcpLog(models.Model):
    _name = 'zivo.mcp.log'
    _description = 'AI Connector Audit Log'
    _order = 'create_date desc, id desc'
    _rec_name = 'method'

    connector_id = fields.Many2one('zivo.mcp.connector', string="Connector", index=True, ondelete='cascade')
    user_id = fields.Many2one('res.users', string="Integration User", ondelete='set null')
    method = fields.Char(string="MCP Method", index=True)
    tool_name = fields.Char(string="Tool", index=True)
    model_name = fields.Char(string="Model")
    status = fields.Selection([
        ('success', 'Success'),
        ('error', 'Error'),
        ('denied', 'Denied'),
        ('auth_failed', 'Authentication Failed'),
        ('rate_limited', 'Rate Limited'),
    ], required=True, default='success', index=True)
    message = fields.Text()
    arguments = fields.Text()
    duration_ms = fields.Float(string="Duration (ms)", aggregator='avg')
    result_count = fields.Integer(string="Records")
    call_count = fields.Integer(default=1, string="Calls")
    ip_address = fields.Char(string="IP Address")
    user_agent = fields.Char(string="Client")

    @api.model
    def _log_simple(self, connector_id, status, message, ip, user_agent, uid=False):
        return self.sudo().create({
            'connector_id': connector_id, 'status': status, 'message': message, 'method': 'auth',
            'ip_address': ip, 'user_agent': user_agent, 'user_id': uid or False,
        })

    @api.model
    def _log_events(self, connector, uid, events, ip, user_agent, with_payload=True):
        vals = []
        for event in events:
            vals.append({
                'connector_id': connector.id,
                'user_id': uid,
                'method': event.get('method'),
                'tool_name': event.get('tool') or False,
                'model_name': event.get('model') or False,
                'status': event.get('status') or 'success',
                'message': utils.truncate(event.get('message') or '', 2000) or False,
                'arguments': event.get('arguments') if with_payload else False,
                'duration_ms': event.get('duration_ms') or 0.0,
                'result_count': event.get('count') or 0,
                'ip_address': ip,
                'user_agent': user_agent,
            })
        return self.sudo().create(vals) if vals else self.browse()

    @api.autovacuum
    def _gc_old_logs(self):
        days = int(self.env['ir.config_parameter'].sudo().get_param('zivoit_mcp.log_retention_days') or 90)
        if days > 0:
            limit = fields.Datetime.now() - timedelta(days=days)
            self.sudo().search([('create_date', '<', limit)], limit=50000).unlink()
