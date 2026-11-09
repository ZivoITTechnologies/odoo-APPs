# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
from odoo import api, fields, models

from ..services import registry


class ZivoMcpTool(models.Model):
    _name = 'zivo.mcp.tool'
    _description = 'MCP Tool'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    technical_name = fields.Char(required=True, index=True, help="Name the AI assistant sees in tools/list.")
    category = fields.Selection([
        ('core', 'Data Access'),
        ('finance', 'Reports & Analytics'),
        ('write', 'Write (create / update)'),
    ], required=True, default='core')
    is_write = fields.Boolean(string="Changes Data")
    default_enabled = fields.Boolean(default=True, help="Enabled on new connectors.")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    description = fields.Text(compute='_compute_description', string="Description for the AI")
    description_override = fields.Text(
        string="Custom Description",
        help="Optional text that replaces the built-in description sent to the AI assistant.")
    input_schema = fields.Text(compute='_compute_description', string="Parameters (JSON Schema)")
    call_count = fields.Integer(compute='_compute_call_count', string="Calls (30 days)")
    available = fields.Boolean(compute='_compute_available', string="App Installed",
                               help="Tools that need an app (Accounting, Sales, Inventory, CRM…) appear only once it is installed.")
    required_apps = fields.Char(compute='_compute_available', string="Needs")

    _technical_name_uniq = models.Constraint('unique(technical_name)', 'Each MCP tool name must be unique.')

    @api.depends('technical_name', 'description_override')
    def _compute_description(self):
        from ..services.utils import dumps
        for tool in self:
            spec = registry.TOOLS.get(tool.technical_name)
            tool.description = tool.description_override or (spec and spec['description']) or ''
            tool.input_schema = dumps(spec['inputSchema'], indent=2) if spec else ''

    def _compute_available(self):
        labels = {'account.move.line': 'Invoicing', 'sale.report': 'Sales', 'account.invoice.report': 'Invoicing',
                  'purchase.report': 'Purchase', 'stock.quant': 'Inventory', 'crm.lead': 'CRM'}
        for tool in self:
            tool.available = tool._is_available()
            requires = (registry.TOOLS.get(tool.technical_name) or {}).get('requires') or ()
            tool.required_apps = ' or '.join(dict.fromkeys(labels.get(m, m) for m in requires))

    def _is_available(self):
        self.ensure_one()
        return registry.is_available(self.technical_name, self.env)

    def _compute_call_count(self):
        since = fields.Datetime.subtract(fields.Datetime.now(), days=30)
        data = dict(self.env['zivo.mcp.log']._read_group(
            [('tool_name', 'in', self.mapped('technical_name')), ('create_date', '>=', since), ('method', '=', 'tools/call')],
            ['tool_name'], ['__count']))
        for tool in self:
            tool.call_count = data.get(tool.technical_name, 0)

    def action_view_calls(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_mcp_connector.action_zivo_mcp_log')
        action['domain'] = [('tool_name', '=', self.technical_name)]
        return action
