# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
import ipaddress
import json
from datetime import timedelta

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services import utils

# Models offered by the "Finance & Operations" preset (only those installed are added).
PRESET_MODELS = [
    'res.partner', 'res.company', 'res.currency', 'res.currency.rate', 'res.country',
    'account.account', 'account.journal', 'account.move', 'account.move.line', 'account.payment',
    'account.payment.term', 'account.tax', 'account.analytic.account', 'account.analytic.line',
    'account.invoice.report', 'account.fiscal.year', 'account.bank.statement.line',
    'product.product', 'product.template', 'product.category', 'uom.uom',
    'sale.order', 'sale.order.line', 'sale.report', 'crm.team',
    'purchase.order', 'purchase.order.line', 'purchase.report',
    'stock.quant', 'stock.picking', 'stock.move', 'stock.warehouse', 'stock.location', 'stock.lot',
    'crm.lead', 'crm.stage', 'project.project', 'project.task', 'hr.employee.public', 'calendar.event',
    'helpdesk.ticket', 'mrp.production', 'account.asset',
]
# Groups given to an integration user created from the connector (only those installed).
INTEGRATION_GROUPS = [
    'base.group_user', 'account.group_account_readonly', 'account.group_account_invoice',
    'sales_team.group_sale_salesman_all_leads', 'purchase.group_purchase_user', 'stock.group_stock_user',
]


class ZivoMcpConnector(models.Model):
    _name = 'zivo.mcp.connector'
    _description = 'AI Connector (MCP)'
    _inherit = ['mail.thread']
    _order = 'sequence, name'

    name = fields.Char(required=True, tracking=True, help="e.g. 'US Entity – Claude via AnythingMCP'.")
    entity_label = fields.Char(
        string="Entity Name", tracking=True,
        help="How the AI should call this entity in its answers, e.g. 'US Entity'. Defaults to the company name.")
    sequence = fields.Integer(default=10)
    color = fields.Integer()
    active = fields.Boolean(default=True)
    state = fields.Selection([('draft', 'Draft'), ('active', 'Active'), ('paused', 'Paused')],
                             default='draft', required=True, tracking=True, copy=False)
    client_type = fields.Selection([
        ('claude', 'Claude (web, desktop, mobile)'),
        ('claude_code', 'Claude Code'),
        ('chatgpt', 'ChatGPT'),
        ('anythingmcp', 'AnythingMCP'),
        ('claude_desktop', 'Claude Desktop (local config)'),
        ('other', 'Other MCP client'),
    ], string="Main Client", default='claude', required=True)
    auth_mode = fields.Selection([
        ('oauth', 'Sign in with Odoo (each person, OAuth)'),
        ('api_key', 'Integration user + API key'),
    ], string="Authentication", default='oauth', required=True, tracking=True,
        help="Sign in with Odoo: people add the MCP address in Claude, ChatGPT or another AI app and log in with "
             "their own Odoo account; their own access rights apply.\n"
             "Integration user + API key: one technical user and a key, for gateways (AnythingMCP), scripts and agents.")
    allowed_user_ids = fields.Many2many(
        'res.users', 'zivo_mcp_connector_allowed_user_rel', 'connector_id', 'user_id', string="Allowed Users",
        domain="[('share', '=', False)]",
        help="Sign in with Odoo: who may connect. Empty means every user with the 'AI Connector / User' right.")
    oauth_session_count = fields.Integer(compute='_compute_oauth_session_count', string="Signed-in Sessions")

    # ------------------------------------------------------------------ identity & scope
    user_id = fields.Many2one(
        'res.users', string="Integration User", tracking=True, copy=False, ondelete='restrict',
        domain="[('share', '=', False)]",
        help="Dedicated Odoo user the AI acts as. Its access rights and record rules apply to every request.")
    company_ids = fields.Many2many(
        'res.company', string="Companies", default=lambda self: self.env.company,
        help="Companies the AI may report on. Empty: every company of the signed-in user.")
    access_mode = fields.Selection([
        ('read_only', 'Read only'),
        ('read_write', 'Read & write (approved models only)'),
    ], default='read_only', required=True, tracking=True)
    restrict_models = fields.Boolean(
        string="Allow-listed Models Only", default=True, tracking=True,
        help="When set, the AI may only read the models listed under Model Access. When unset, it may read any "
             "business model the integration user can read (technical and security models stay blocked).")
    model_access_ids = fields.One2many('zivo.mcp.model.access', 'connector_id', string="Model Access", copy=True)
    excluded_field_ids = fields.Many2many(
        'ir.model.fields', string="Hidden Fields", copy=True,
        help="Fields the AI must never read or filter on (passwords, keys and binary fields are always hidden).")
    tool_ids = fields.Many2many(
        'zivo.mcp.tool', string="Enabled Tools", copy=True,
        default=lambda self: self.env['zivo.mcp.tool'].search([('default_enabled', '=', True), ('is_write', '=', False)]))
    instructions = fields.Text(
        string="Extra Instructions for the AI",
        help="Added to the server instructions, e.g. 'Fiscal year ends 31 March. Report amounts in thousands.'")

    # ------------------------------------------------------------------ security
    max_records = fields.Integer(string="Max Records per Call", default=200)
    rate_limit = fields.Integer(string="Rate Limit (calls / minute)", default=120)
    allowed_ips = fields.Char(
        string="Allowed IP Addresses",
        help="Comma-separated IPs or networks (CIDR). Empty allows any address.")
    allow_url_key = fields.Boolean(
        string="Allow Key in URL",
        help="Lets clients that cannot send headers use /zivoit/mcp/<key>. Keys in URLs can end up in logs: "
             "prefer the Authorization header.")
    expiry_date = fields.Date(string="Valid Until", tracking=True)

    # ------------------------------------------------------------------ computed
    endpoint_url = fields.Char(compute='_compute_urls', string="MCP Endpoint")
    odoo_url = fields.Char(compute='_compute_urls', string="Odoo Address (ODOO_URL)")
    key_count = fields.Integer(compute='_compute_key_info')
    key_summary = fields.Html(compute='_compute_key_info', sanitize=False)
    snippet_anythingmcp = fields.Text(compute='_compute_snippets')
    snippet_claude_desktop = fields.Text(compute='_compute_snippets')
    snippet_claude_code = fields.Text(compute='_compute_snippets')
    snippet_curl = fields.Text(compute='_compute_snippets')
    snippet_claude_web = fields.Text(compute='_compute_snippets')
    snippet_chatgpt = fields.Text(compute='_compute_snippets')
    call_count_today = fields.Integer(compute='_compute_stats')
    call_count_30d = fields.Integer(compute='_compute_stats')
    error_count_7d = fields.Integer(compute='_compute_stats')
    success_rate = fields.Float(compute='_compute_stats', string="Success Rate (30 days)")
    last_call_date = fields.Datetime(compute='_compute_stats')
    avg_duration_ms = fields.Float(compute='_compute_stats', string="Avg. Response (ms)")
    tool_count = fields.Integer(compute='_compute_tool_count')
    model_count = fields.Integer(compute='_compute_tool_count')

    _user_uniq = models.Constraint('unique(user_id)', 'An integration user can belong to one connector only.')

    # ------------------------------------------------------------------ computes
    def _public_base_url(self):
        url = self.env['ir.config_parameter'].sudo().get_param('zivoit_mcp.public_url') or self.get_base_url()
        return (url or '').rstrip('/')

    def _compute_urls(self):
        for rec in self:
            base = rec._public_base_url()
            rec.odoo_url = base
            rec.endpoint_url = base + '/mcp'

    def _compute_key_info(self):
        Keys = self.env['res.users.apikeys'].sudo()
        for rec in self:
            keys = Keys.search([('user_id', '=', rec.user_id.id)]) if rec.user_id else Keys
            rec.key_count = len(keys)
            if not keys:
                rec.key_summary = Markup('<span class="text-muted">%s</span>') % _('No API key yet.')
                continue
            rows = Markup('').join(
                Markup('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>') % (
                    k.name, k.scope or _('All (RPC & MCP)'), fields.Date.to_string(k.create_date.date()),
                    fields.Date.to_string(k.expiration_date.date()) if k.expiration_date else _('Never'))
                for k in keys)
            rec.key_summary = Markup(
                '<table class="table table-sm o_zivo_keys"><thead><tr><th>%s</th><th>%s</th><th>%s</th><th>%s</th>'
                '</tr></thead><tbody>%s</tbody></table>') % (_('Key'), _('Scope'), _('Created'), _('Expires'), rows)

    def _compute_oauth_session_count(self):
        data = dict(self.env['zivo.mcp.oauth.token'].sudo()._read_group(
            [('connector_id', 'in', self.ids), ('active_session', '=', True)], ['connector_id'], ['__count']))
        for rec in self:
            rec.oauth_session_count = data.get(rec._origin or rec, 0)

    @api.depends('name', 'user_id', 'client_type', 'allow_url_key', 'auth_mode', 'entity_label')
    def _compute_snippets(self):
        for rec in self:
            base = rec._public_base_url()
            endpoint = base + '/mcp'
            slug = 'odoo-%s' % ''.join(c if c.isalnum() else '-' for c in (rec.entity_label or rec.name or 'odoo').lower()).strip('-')
            slug = slug[:40].strip('-')
            rec.snippet_claude_web = '\n'.join([
                _('Claude (claude.ai, Claude Desktop, mobile):'),
                _('1. Settings > Connectors > Add custom connector'),
                _('2. Name: Odoo   URL: %s', endpoint),
                _('3. Click Connect, log in to Odoo and click Allow.'),
                _('Team / Enterprise plans: an owner adds it once under Organization settings > Connectors.'),
            ])
            rec.snippet_chatgpt = '\n'.join([
                _('ChatGPT (developer mode connectors):'),
                _('Settings > Apps & Connectors > Create'),
                _('MCP Server URL: %s', endpoint),
                _('Authentication: OAuth  ->  log in to Odoo and click Allow.'),
            ])
            if rec.auth_mode == 'oauth':
                rec.snippet_anythingmcp = _('AnythingMCP and other gateways need a connector with '
                                            '"Integration user + API key" authentication.')
                rec.snippet_claude_desktop = rec.snippet_claude_web
                rec.snippet_claude_code = ('claude mcp add --transport http %s %s\n'
                                           '# then run /mcp in Claude Code and choose Authenticate' % (slug, endpoint))
                rec.snippet_curl = _('Sign-in connectors use OAuth tokens. To test with cURL, create a connector '
                                     'with "Integration user + API key" authentication.')
                continue
            rec.snippet_anythingmcp = '\n'.join([
                _('AnythingMCP > Connectors > Store > "Odoo (MCP server)"'),
                'ODOO_URL          = %s' % base,
                'ODOO_MCP_API_KEY  = <the 40-character key generated for this connector>',
                '',
                _('Or: AnythingMCP > Connectors > New > MCP server'),
                'URL   = %s' % endpoint,
                'Auth  = Bearer token  (the same key)',
            ])
            rec.snippet_claude_desktop = json.dumps({'mcpServers': {slug: {
                'command': 'npx',
                'args': ['-y', 'mcp-remote', endpoint, '--header', 'Authorization:Bearer ${ODOO_MCP_KEY}'],
                'env': {'ODOO_MCP_KEY': '<your key>'},
            }}}, indent=2)
            rec.snippet_claude_code = ('claude mcp add --transport http %s %s \\\n'
                                       '  --header "Authorization: Bearer <your key>"' % (slug, endpoint))
            rec.snippet_curl = ('curl -s %s \\\n  -H "Authorization: Bearer <your key>" \\\n'
                                '  -H "Content-Type: application/json" \\\n  -H "Accept: application/json, text/event-stream" \\\n'
                                '  -d \'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\'' % endpoint)

    def _compute_stats(self):
        Log = self.env['zivo.mcp.log'].sudo()
        now = fields.Datetime.now()
        start_today = fields.Datetime.to_datetime(fields.Date.context_today(self))
        since_30 = now - timedelta(days=30)
        since_7 = now - timedelta(days=7)
        base = [('connector_id', 'in', self.ids)]
        today = dict(Log._read_group(base + [('create_date', '>=', start_today)], ['connector_id'], ['__count']))
        month = {c: (n, d) for c, n, d in Log._read_group(base + [('create_date', '>=', since_30), ('method', '=', 'tools/call')],
                                                          ['connector_id'], ['__count', 'duration_ms:avg'])}
        month_ok = dict(Log._read_group(base + [('create_date', '>=', since_30), ('method', '=', 'tools/call'),
                                                ('status', '=', 'success')], ['connector_id'], ['__count']))
        errors = dict(Log._read_group(base + [('create_date', '>=', since_7), ('status', '!=', 'success')],
                                      ['connector_id'], ['__count']))
        last = dict(Log._read_group(base, ['connector_id'], ['create_date:max']))
        for rec in self:
            key = rec._origin or rec
            calls, avg = month.get(key, (0, 0.0))
            rec.call_count_today = today.get(key, 0)
            rec.call_count_30d = calls
            rec.error_count_7d = errors.get(key, 0)
            rec.success_rate = (100.0 * month_ok.get(key, 0) / calls) if calls else 0.0
            rec.avg_duration_ms = avg or 0.0
            rec.last_call_date = last.get(key, False)

    @api.depends('tool_ids', 'model_access_ids')
    def _compute_tool_count(self):
        for rec in self:
            rec.tool_count = len(rec._enabled_tools())
            rec.model_count = len(rec.model_access_ids.filtered('perm_read'))

    # ------------------------------------------------------------------ constraints
    @api.constrains('user_id', 'auth_mode')
    def _check_user(self):
        allow_admin = utils.as_bool(self.env['ir.config_parameter'].sudo().get_param('zivoit_mcp.allow_admin_user'), False)
        for rec in self.filtered(lambda r: r.user_id and r.auth_mode == 'api_key'):
            if rec.user_id.share:
                raise ValidationError(_("The integration user must be an internal user."))
            if not allow_admin and (rec.user_id._is_superuser() or rec.user_id.has_group('base.group_system')):
                raise ValidationError(_(
                    "For security, the integration user cannot be an Administrator (Settings rights). "
                    "Create a dedicated user with only the access the AI needs, e.g. with the "
                    "'Create Integration User' button."))

    @api.constrains('max_records', 'rate_limit')
    def _check_limits(self):
        for rec in self:
            if not 1 <= rec.max_records <= 2000:
                raise ValidationError(_("Max records per call must be between 1 and 2000."))
            if rec.rate_limit < 0:
                raise ValidationError(_("The rate limit cannot be negative."))

    @api.constrains('allowed_ips')
    def _check_ips(self):
        for rec in self.filtered('allowed_ips'):
            for item in rec.allowed_ips.split(','):
                try:
                    ipaddress.ip_network(item.strip(), strict=False)
                except ValueError:
                    raise ValidationError(_("%s is not a valid IP address or network.", item.strip())) from None

    # ------------------------------------------------------------------ CRUD
    def _message_auto_subscribe_followers(self, updated_values, default_subtype_ids):
        # The integration user is a technical account: never subscribe or notify it.
        return []

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records.filtered(lambda r: not r.model_access_ids):
            rec._load_preset()
        return records

    # ------------------------------------------------------------------ actions
    def action_activate(self):
        for rec in self:
            if rec.auth_mode == 'api_key' and not rec.user_id:
                raise UserError(_("Set an integration user before activating '%s'.", rec.name))
            if not rec._enabled_tools():
                raise UserError(_("Enable at least one tool on '%s'.", rec.name))
        self.state = 'active'

    def action_pause(self):
        self.state = 'paused'

    def action_reset_draft(self):
        self.state = 'draft'

    def action_load_preset(self):
        for rec in self:
            rec._load_preset()
        return True

    def _load_preset(self):
        self.ensure_one()
        existing = set(self.model_access_ids.mapped('model_id.model'))
        IrModel = self.env['ir.model'].sudo()
        lines = []
        for model_name in PRESET_MODELS:
            if model_name in existing or model_name not in self.env.registry:
                continue
            model = IrModel._get(model_name)
            if model:
                lines.append((0, 0, {'model_id': model.id, 'perm_read': True}))
        if lines:
            self.write({'model_access_ids': lines})

    def action_generate_key(self):
        self.ensure_one()
        if not self.user_id:
            raise UserError(_("Set or create the integration user first."))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Generate API Key'),
            'res_model': 'zivo.mcp.key.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_connector_id': self.id},
        }

    def action_revoke_keys(self):
        for rec in self.filtered('user_id'):
            keys = self.env['res.users.apikeys'].sudo().search([('user_id', '=', rec.user_id.id)])
            count = len(keys)
            keys._remove()
            rec.message_post(body=_("%(count)s API key(s) of %(user)s revoked.", count=count, user=rec.user_id.name))
        return True

    def action_create_integration_user(self):
        self.ensure_one()
        if self.user_id:
            raise UserError(_("This connector already has an integration user."))
        label = self.entity_label or self.name
        login = 'mcp-%s-%s' % (''.join(c if c.isalnum() else '-' for c in label.lower()).strip('-')[:30], self.id)
        groups = self.env['res.groups']
        for xmlid in INTEGRATION_GROUPS:
            group = self.env.ref(xmlid, raise_if_not_found=False)
            if group:
                groups |= group
        user = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': _('AI Integration – %s', label),
            'login': login,
            'company_id': self.company_ids[:1].id,
            'company_ids': [(6, 0, self.company_ids.ids)],
            'group_ids': [(6, 0, groups.ids)],
        })
        self.user_id = user
        self.message_post(body=_("Integration user %s created with read access to Accounting, Sales, Purchase and "
                                 "Inventory.", user.login))
        return True

    def action_test_connection(self):
        self.ensure_one()
        from ..services.protocol import McpSession
        if self.auth_mode == 'api_key':
            if not self.user_id:
                raise UserError(_("Set the integration user first."))
            user = self.user_id
        else:
            user = self.env.user
            if not self._is_user_allowed(user):
                raise UserError(_("You are not allowed to sign in with this connector. Add yourself to Allowed "
                                  "Users or give yourself the 'AI Connector / User' right."))
        env = self.env(user=user.id, context=dict(self.env.context, allowed_company_ids=self._effective_company_ids(user)),
                       su=False)
        session = McpSession(env, self.sudo().with_env(env).sudo(), {'base_url': self._public_base_url()})
        init = session.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                               'params': {'protocolVersion': '2025-06-18', 'clientInfo': {'name': 'Odoo test'}}})
        listed = session.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        ctx_call = session.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                                   'params': {'name': 'ai_tool_mcp_retrieve_initial_context', 'arguments': {}}})
        self.env['zivo.mcp.log'].sudo()._log_events(self, user.id, session.events, 'odoo', 'Test connection', True)
        ok = 'result' in init and 'result' in listed and not ctx_call.get('result', {}).get('isError', True)
        if ok:
            data = ctx_call['result']['structuredContent']
            message = _("Connected as %(user)s. %(tools)s tools, %(models)s readable models, companies: %(companies)s.",
                        user=user.login, tools=len(listed['result']['tools']), models=len(data['models']),
                        companies=', '.join(c['name'] for c in data['companies']))
        else:
            err = ctx_call.get('result', {}).get('structuredContent', {}).get('error') or ctx_call.get('error', {}).get('message')
            message = _("Test failed: %s", err)
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('Connection OK') if ok else _('Connection failed'), 'message': message,
            'type': 'success' if ok else 'danger', 'sticky': not ok}}

    def action_view_logs(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_mcp_connector.action_zivo_mcp_log')
        action['domain'] = [('connector_id', '=', self.id)]
        action['context'] = {'search_default_last_7_days': 0}
        return action

    def action_view_errors(self):
        action = self.action_view_logs()
        action['domain'] = [('connector_id', '=', self.id), ('status', '!=', 'success')]
        return action

    def action_view_sessions(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_mcp_connector.action_zivo_mcp_oauth_token')
        action['domain'] = [('connector_id', '=', self.id)]
        return action

    def action_enable_all_read_tools(self):
        for rec in self:
            rec.tool_ids = [(4, t.id) for t in self.env['zivo.mcp.tool'].search([('is_write', '=', False)])]

    # ------------------------------------------------------------------ runtime helpers (used by the endpoint)
    def _today(self):
        return fields.Date.context_today(self)

    def _effective_company_ids(self, user=None):
        self.ensure_one()
        user = user or self.user_id
        if not user:
            return self.company_ids.ids or self.env.company.ids
        companies = (self.company_ids & user.company_ids) if self.company_ids else user.company_ids
        companies = companies or user.company_id
        main = user.company_id if user.company_id in companies else companies[:1]
        return (main | companies).ids

    def _is_user_allowed(self, user):
        self.ensure_one()
        if self.auth_mode != 'oauth' or not user or user.share or not user.active:
            return False
        if self.allowed_user_ids:
            return user in self.allowed_user_ids
        return user.has_group('zivoit_mcp_connector.group_zivo_mcp_user')

    @api.model
    def _oauth_connectors_for(self, user):
        connectors = self.sudo().search([('auth_mode', '=', 'oauth'), ('state', '=', 'active')])
        return connectors.filtered(lambda c: c._is_user_allowed(user) and
                                   (not c.expiry_date or c.expiry_date >= c._today()))

    def _enabled_tools(self):
        self.ensure_one()
        tools = self.tool_ids.filtered(lambda t: t.active and t._is_available())
        if self.access_mode != 'read_write':
            tools = tools.filtered(lambda t: not t.is_write)
        return tools.sorted(lambda t: (t.sequence, t.id))

    def _is_model_allowed(self, model_name, operation='read'):
        self.ensure_one()
        if utils.is_model_blocked(model_name):
            return False
        if model_name in utils.ALLOWED_TECHNICAL_MODELS and operation == 'read':
            return True
        line = self.model_access_ids.filtered(lambda l: l.model_id.model == model_name)
        if operation == 'read':
            return bool(line.perm_read) if self.restrict_models else (not line or line.perm_read)
        if self.access_mode != 'read_write':
            return False
        return bool(line.filtered('perm_%s' % operation))

    def _allowed_model_summary(self):
        self.ensure_one()
        if self.restrict_models:
            lines = self.model_access_ids.filtered(lambda l: l.perm_read and l.model_id.model in self.env.registry)
            return [{'model': l.model_id.model, 'name': l.model_id.name,
                     'create': self.access_mode == 'read_write' and l.perm_create,
                     'write': self.access_mode == 'read_write' and l.perm_write} for l in lines]
        return 'Any business model the signed-in Odoo user can read (call ai_tool_get_models).'

    def _assistant_instructions(self):
        self.ensure_one()
        entity = self.entity_label or (self.company_ids[:1].name if self.company_ids else 'Odoo')
        text = [
            "You are connected to the Odoo ERP of %s through the ZivoIT MCP Connector." % entity,
            "Data comes live from Odoo with the access rights of %s." % (
                "the signed-in Odoo user" if self.auth_mode == 'oauth' else "a dedicated integration user"),
            "Start with ai_tool_mcp_retrieve_initial_context. Prefer the zivo_* report tools (statements, ageing, "
            "sales, purchases, stock, CRM pipeline, KPIs) over raw queries when they are available.",
            "Amounts are in each company's currency; never add amounts of companies with different currencies.",
            "When several companies are returned, label every figure with its company (entity).",
            "Use ai_tool_read_group for totals instead of summing search results.",
        ]
        if self.access_mode == 'read_write':
            text.append("Before any create or update tool, show the user exactly what will change and ask for confirmation.")
        else:
            text.append("This connection is read-only.")
        if self.instructions:
            text.append(self.instructions)
        return '\n'.join(text)

    def _rate_limited(self, user_id=None):
        self.ensure_one()
        if not self.rate_limit:
            return False
        since = fields.Datetime.now() - timedelta(seconds=60)
        domain = [('connector_id', '=', self.id), ('create_date', '>=', since)]
        if user_id:
            domain.append(('user_id', '=', user_id))
        count = self.env['zivo.mcp.log'].sudo().search_count(domain, limit=self.rate_limit + 1)
        return count >= self.rate_limit

    def _ip_allowed(self, ip):
        self.ensure_one()
        if not self.allowed_ips:
            return True
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(address in ipaddress.ip_network(item.strip(), strict=False)
                   for item in self.allowed_ips.split(',') if item.strip())


class ZivoMcpModelAccess(models.Model):
    _name = 'zivo.mcp.model.access'
    _description = 'AI Connector Model Access'
    _order = 'model_name'

    connector_id = fields.Many2one('zivo.mcp.connector', required=True, ondelete='cascade', index=True)
    model_id = fields.Many2one('ir.model', string="Model", required=True, ondelete='cascade',
                               domain="[('transient', '=', False)]")
    model_name = fields.Char(related='model_id.model', store=True, string="Technical Name")
    perm_read = fields.Boolean(string="Read", default=True)
    perm_create = fields.Boolean(string="Create")
    perm_write = fields.Boolean(string="Update")

    _model_uniq = models.Constraint('unique(connector_id, model_id)', 'Each model can be listed once per connector.')

    @api.constrains('model_id')
    def _check_model(self):
        for line in self:
            if utils.is_model_blocked(line.model_id.model):
                raise ValidationError(_("The model %s is blocked for AI access for security reasons.", line.model_id.model))


class ZivoMcpDashboard(models.AbstractModel):
    _name = 'zivo.mcp.dashboard'
    _description = 'AI Connector Dashboard'

    @api.model
    def get_dashboard_data(self):
        Log = self.env['zivo.mcp.log']
        Connector = self.env['zivo.mcp.connector']
        now = fields.Datetime.now()
        today = fields.Date.context_today(self)
        start_today = fields.Datetime.to_datetime(today)
        since_30 = now - timedelta(days=30)
        since_7 = now - timedelta(days=7)
        calls = [('method', '=', 'tools/call')]
        connectors = Connector.search([])
        total_30 = Log.search_count(calls + [('create_date', '>=', since_30)])
        ok_30 = Log.search_count(calls + [('create_date', '>=', since_30), ('status', '=', 'success')])
        avg_ms = Log._read_group(calls + [('create_date', '>=', since_30)], [], ['duration_ms:avg'])[0][0] or 0.0
        start_14 = fields.Datetime.to_datetime(today - timedelta(days=13))
        series = {}
        for day, status, count in Log.with_context(tz='UTC')._read_group(calls + [('create_date', '>=', start_14)],
                                                  ['create_date:day', 'status'], ['__count']):
            key = day.date() if hasattr(day, 'date') else day
            bucket = series.setdefault(key, {'ok': 0, 'error': 0})
            bucket['ok' if status == 'success' else 'error'] += count
        days = []
        for i in range(14):
            d = today - timedelta(days=13 - i)
            b = series.get(d, {'ok': 0, 'error': 0})
            days.append({'label': d.strftime('%d %b'), 'ok': b['ok'], 'error': b['error'], 'total': b['ok'] + b['error']})
        top_tools = [{'name': t or '-', 'count': c} for t, c in Log._read_group(
            calls + [('create_date', '>=', since_30)], ['tool_name'], ['__count'], order='__count desc', limit=7)]
        tool_names = dict(self.env['zivo.mcp.tool'].with_context(active_test=False).search([]).mapped(
            lambda t: (t.technical_name, t.name)))
        for t in top_tools:
            t['label'] = tool_names.get(t['name'], t['name'])
        recent = Log.search([('method', '=', 'tools/call')], limit=8, order='create_date desc, id desc')
        return {
            'endpoint': Connector._public_base_url() + '/mcp',
            'kpis': {
                'active_connectors': len(connectors.filtered(lambda c: c.state == 'active')),
                'connectors': len(connectors),
                'entities': len(connectors.mapped('company_ids')) or len(self.env['res.company'].sudo().search([])),
                'sessions': self.env['zivo.mcp.oauth.token'].sudo().search_count([('active_session', '=', True)]),
                'calls_today': Log.search_count(calls + [('create_date', '>=', start_today)]),
                'calls_30d': total_30,
                'success_rate': round(100.0 * ok_30 / total_30, 1) if total_30 else 0,
                'avg_ms': round(avg_ms),
                'errors_7d': Log.search_count([('create_date', '>=', since_7), ('status', '!=', 'success')]),
            },
            'days': days,
            'max_day': max([d['total'] for d in days] + [1]),
            'top_tools': top_tools,
            'max_tool': max([t['count'] for t in top_tools] + [1]),
            'connectors': [{
                'id': c.id, 'name': c.name, 'entity': c.entity_label or c.company_ids[:1].name or _('All companies'),
                'state': c.state, 'auth': 'Sign in with Odoo' if c.auth_mode == 'oauth' else 'API key',
                'state_label': dict(c._fields['state'].selection).get(c.state),
                'mode': c.access_mode, 'client': dict(c._fields['client_type'].selection).get(c.client_type),
                'calls_today': c.call_count_today, 'calls_30d': c.call_count_30d, 'errors': c.error_count_7d,
                'tools': c.tool_count,
            } for c in connectors],
            'recent': [{
                'id': l.id, 'time': fields.Datetime.context_timestamp(self, l.create_date).strftime('%d %b %H:%M:%S'),
                'connector': l.connector_id.name or '-', 'tool': l.tool_name or l.method, 'status': l.status,
                'status_label': dict(l._fields['status'].selection).get(l.status),
                'duration': round(l.duration_ms or 0), 'count': l.result_count,
            } for l in recent],
        }
