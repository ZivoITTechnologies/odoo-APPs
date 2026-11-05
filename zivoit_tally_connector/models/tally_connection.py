# -*- coding: utf-8 -*-
import logging
import secrets
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from ..lib import tally_xml as TX
from ..lib.transport import TallyConnectionError, post_xml

_logger = logging.getLogger(__name__)

SYNC_DIR = [('none', 'Off'), ('pull', 'Tally → Odoo'), ('push', 'Odoo → Tally'),
            ('both', 'Two-way')]
AGENT_ONLINE_SECONDS = 180


class TallyConnection(models.Model):
    _name = 'tally.connection'
    _description = 'Tally Connection'
    _inherit = ['mail.thread']
    _order = 'sequence, id'

    name = fields.Char(required=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company)
    currency_id = fields.Many2one(related='company_id.currency_id')
    tally_company = fields.Char('Tally Company Name', required=True, tracking=True,
                                help='Exact company name as shown in TallyPrime (F3).')
    state = fields.Selection([('draft', 'Not tested'), ('connected', 'Connected'),
                              ('error', 'Error')], default='draft', tracking=True)
    last_test_message = fields.Text(readonly=True)

    # ---- transport ------------------------------------------------------
    transport = fields.Selection([
        ('direct', 'Direct (Odoo reaches Tally on LAN/VPN)'),
        ('agent', 'Bridge Agent (Odoo Online / Odoo.sh / any cloud)'),
        ('file', 'Offline XML files'),
    ], default='agent', required=True, tracking=True)
    host = fields.Char(default='localhost')
    port = fields.Integer(default=9000)
    use_https = fields.Boolean('HTTPS', help='Only when Tally sits behind a TLS reverse proxy.')
    timeout = fields.Integer(default=60)
    schema = fields.Selection([('prime3', 'TallyPrime 3.0 and later (7.x ready)'),
                               ('legacy', 'Tally.ERP 9 / TallyPrime 1.x-2.x')],
                              default='prime3', required=True)
    batch_size = fields.Integer(default=50, help='Objects per Tally import envelope.')

    # ---- agent ------------------------------------------------------------
    agent_token = fields.Char(copy=False, groups='zivoit_tally_connector.group_tally_manager')
    agent_last_seen = fields.Datetime(readonly=True, copy=False)
    agent_hostname = fields.Char(readonly=True, copy=False)
    agent_version = fields.Char(readonly=True, copy=False)
    agent_tally_ok = fields.Boolean('Tally reachable from agent', readonly=True, copy=False)
    agent_online = fields.Boolean(compute='_compute_agent_online')

    # ---- scope & direction ---------------------------------------------
    sync_parties = fields.Selection(SYNC_DIR, default='both', required=True)
    sync_ledgers = fields.Selection(SYNC_DIR[:2], default='pull', required=True,
                                    help='Non-party ledgers are pulled for mapping to accounts.')
    sync_items = fields.Selection(SYNC_DIR, default='both', required=True)
    sync_sales = fields.Selection(SYNC_DIR, 'Sales & Credit Notes', default='push', required=True)
    sync_purchases = fields.Selection(SYNC_DIR, 'Purchases & Debit Notes', default='push',
                                      required=True)
    sync_payments = fields.Selection(SYNC_DIR, 'Receipts & Payments', default='both',
                                     required=True)
    sync_journals = fields.Selection(SYNC_DIR, 'Journals & Contra', default='pull', required=True)
    sync_cost_centres = fields.Selection(SYNC_DIR, 'Cost centres', default='both', required=True,
                                         help='Tally cost centres ↔ Odoo analytic accounts, '
                                              'including allocations on vouchers.')
    primary_plan_id = fields.Many2one(
        'account.analytic.plan', 'Plan for "Primary Cost Category"',
        help="Analytic plan matching Tally's built-in Primary Cost Category. Cost centres "
             "pulled from Tally are created here unless their category maps to another plan.")
    detect_deletions = fields.Boolean('Detect deletions in Tally', default=True)
    census_interval_hours = fields.Integer('Deletion check every (hours)', default=6)
    last_census_date = fields.Datetime(readonly=True, copy=False)
    deletion_guard_pct = fields.Integer(
        'Mass-deletion guard (%)', default=30,
        help='If more than this share of known vouchers/masters seems deleted in one check, '
             'nothing is applied and an alert is logged (protects against a wrong company or '
             'a restored backup).')
    push_on_post = fields.Boolean('Push automatically on posting', default=True)
    auto_post_imported = fields.Boolean('Post imported vouchers', default=True)
    pull_from_date = fields.Date('Pull vouchers from',
                                 default=lambda s: fields.Date.today().replace(month=4, day=1)
                                 if fields.Date.today().month >= 4 else
                                 fields.Date.today().replace(year=fields.Date.today().year - 1,
                                                             month=4, day=1))
    voucher_mode = fields.Selection([('accounting', 'Accounting invoice (ledgers only)'),
                                     ('item', 'Item invoice (stock items)')],
                                    default='item', required=True)
    conflict_policy = fields.Selection([('manual', 'Hold for review'),
                                        ('tally', 'Tally wins'), ('odoo', 'Odoo wins')],
                                       default='manual', required=True, tracking=True)
    amount_tolerance = fields.Monetary(default=1.0,
                                       help='Max difference between Tally and Odoo totals '
                                            'accepted when rebuilding invoices with Odoo taxes.')
    fallback_to_entry = fields.Boolean(
        'Fallback to journal entry', default=True,
        help='If Odoo taxes cannot reproduce the Tally total, import the voucher as an exact '
             'journal entry instead of holding it.')
    auto_create_ledgers = fields.Boolean('Create missing Tally masters', default=True,
                                         help='Push parties, items, units, tax and GL ledgers '
                                              'to Tally before the vouchers that need them.')
    auto_heal = fields.Boolean('AI auto-heal failed pushes', default=True)
    max_attempts = fields.Integer(default=5)

    # ---- Tally naming --------------------------------------------------
    vt_sales = fields.Char('Sales', default='Sales', required=True)
    vt_purchase = fields.Char('Purchase', default='Purchase', required=True)
    vt_credit_note = fields.Char('Credit Note', default='Credit Note', required=True)
    vt_debit_note = fields.Char('Debit Note', default='Debit Note', required=True)
    vt_receipt = fields.Char('Receipt', default='Receipt', required=True)
    vt_payment = fields.Char('Payment', default='Payment', required=True)
    vt_journal = fields.Char('Journal', default='Journal', required=True)
    vt_contra = fields.Char('Contra', default='Contra', required=True)
    group_customers = fields.Char(default='Sundry Debtors', required=True)
    group_vendors = fields.Char(default='Sundry Creditors', required=True)
    round_off_ledger = fields.Char(default='Round Off')

    # ---- Odoo defaults --------------------------------------------------
    sale_journal_id = fields.Many2one('account.journal', domain="[('type','=','sale')]")
    purchase_journal_id = fields.Many2one('account.journal', domain="[('type','=','purchase')]")
    misc_journal_id = fields.Many2one('account.journal', domain="[('type','=','general')]")
    bank_journal_id = fields.Many2one('account.journal', 'Default bank journal',
                                      domain="[('type','in',('bank','cash'))]")
    suspense_account_id = fields.Many2one(
        'account.account', help='Used for unmapped Tally ledgers when importing as entry. '
                                'Leave empty to hold such vouchers instead.')

    # ---- incremental cursors -------------------------------------------
    last_alter_group = fields.Integer(copy=False)
    last_alter_ledger = fields.Integer(copy=False)
    last_alter_item = fields.Integer(copy=False)
    last_alter_voucher = fields.Integer(copy=False)
    last_alter_vchtype = fields.Integer(copy=False)
    last_alter_costcentre = fields.Integer(copy=False)
    last_alter_godown = fields.Integer(copy=False)
    last_pull_date = fields.Datetime(readonly=True, copy=False)
    last_push_date = fields.Datetime(readonly=True, copy=False)

    # ---- AI -----------------------------------------------------------------
    ai_provider = fields.Selection([('none', 'Built-in engine only (no external AI)'),
                                    ('anthropic', 'Anthropic Claude'),
                                    ('openai', 'OpenAI'),
                                    ('gemini', 'Google Gemini'),
                                    ('openai_compatible', 'OpenAI-compatible (Azure, Ollama, '
                                                          'Groq, OpenRouter…)')],
                                   default='none', required=True)
    ai_api_key = fields.Char(groups='zivoit_tally_connector.group_tally_manager', copy=False)
    ai_model = fields.Char()
    ai_base_url = fields.Char()
    ai_send_amounts = fields.Boolean(
        'Share amounts with AI', help='Off: only names and error texts are sent to the AI '
                                      'provider. On: amounts are included for richer insights.')
    ai_match_threshold = fields.Float('Auto-confirm matches ≥', default=0.92)
    ai_suggest_threshold = fields.Float('Suggest matches ≥', default=0.6)
    anomaly_check = fields.Boolean('AI anomaly guard', default=True)
    anomaly_block = fields.Boolean('Hold blocking anomalies', default=True,
                                   help='Vouchers with blocking findings are not posted/pushed.')
    z_threshold = fields.Float('Outlier z-score', default=4.0)
    large_amount = fields.Monetary(default=100000.0)
    ai_summary = fields.Html('AI health summary', readonly=True, sanitize=True)

    # ---- stats ------------------------------------------------------------
    request_ids = fields.One2many('tally.request', 'connection_id')
    mapping_ids = fields.One2many('tally.mapping', 'connection_id')
    voucher_ids = fields.One2many('tally.voucher', 'connection_id')
    count_pending = fields.Integer(compute='_compute_counts')
    count_failed = fields.Integer(compute='_compute_counts')
    count_vouchers_review = fields.Integer(compute='_compute_counts')
    count_mapping_review = fields.Integer(compute='_compute_counts')
    count_done_today = fields.Integer(compute='_compute_counts')

    _tally_company_uniq = models.Constraint(
        'unique(company_id, tally_company, active)',
        'A connection for this Tally company already exists.')

    # ------------------------------------------------------------------
    @api.depends('agent_last_seen')
    def _compute_agent_online(self):
        limit = fields.Datetime.now() - timedelta(seconds=AGENT_ONLINE_SECONDS)
        for rec in self:
            rec.agent_online = bool(rec.agent_last_seen and rec.agent_last_seen >= limit)

    def _compute_counts(self):
        Req = self.env['tally.request']
        today = fields.Datetime.now().replace(hour=0, minute=0, second=0)
        for rec in self:
            dom = [('connection_id', '=', rec.id)]
            rec.count_pending = Req.search_count(dom + [('state', 'in', ('pending', 'sent'))])
            rec.count_failed = Req.search_count(dom + [('state', '=', 'failed')])
            rec.count_done_today = Req.search_count(dom + [('state', '=', 'done'),
                                                          ('write_date', '>=', today)])
            rec.count_vouchers_review = self.env['tally.voucher'].search_count(
                dom + [('state', 'in', ('error', 'anomaly', 'conflict'))])
            rec.count_mapping_review = self.env['tally.mapping'].search_count(
                dom + [('state', 'in', ('suggested', 'conflict'))])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _remote_prefix(self):
        uuid = self.env['ir.config_parameter'].sudo().get_param('database.uuid') or 'odoo'
        return 'ZIVO-%s-' % uuid.replace('-', '')[:8].upper()

    def _remote_id(self, record):
        short = {'account.move': 'MV', 'res.partner': 'RP', 'product.product': 'PP',
                 'account.account': 'AA', 'account.tax': 'TX'}.get(record._name, 'X')
        return '%s%s-%s' % (self._remote_prefix(), short, record.id)

    def _is_own_remote(self, guid):
        return bool(guid) and guid.startswith(self._remote_prefix())

    def _vch_type_kind(self):
        return {
            self.vt_sales: 'sale', self.vt_purchase: 'purchase',
            self.vt_credit_note: 'credit_note', self.vt_debit_note: 'debit_note',
            self.vt_receipt: 'receipt', self.vt_payment: 'payment',
            self.vt_journal: 'journal', self.vt_contra: 'contra',
        }

    def _kind_vch_type(self, kind):
        return {v: k for k, v in self._vch_type_kind().items()}.get(kind)

    def _dir_allows(self, field, direction):
        val = self[field]
        return val == 'both' or val == direction

    def _check_manager(self):
        if not self.env.user.has_group('zivoit_tally_connector.group_tally_manager') \
                and not self.env.su:
            raise AccessError(_('Only Tally connector managers can do this.'))

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _post_direct(self, xml):
        self.ensure_one()
        return post_xml(self.host, self.port, xml, timeout=self.timeout or 60,
                        use_https=self.use_https)

    def action_test_connection(self):
        self.ensure_one()
        if self.transport == 'file':
            self.write({'state': 'connected',
                        'last_test_message': _('Offline file mode: nothing to test.')})
            return self._notify(_('Offline mode — use the XML import/export wizards.'))
        if self.transport == 'agent':
            if not self.agent_online:
                self.write({'state': 'error', 'last_test_message': _(
                    'Bridge agent has not checked in for %s seconds. Start the ZivoIT Tally '
                    'Agent on the Tally computer.') % AGENT_ONLINE_SECONDS})
                return self._notify(self.last_test_message, 'warning')
            self.env['tally.request']._enqueue(self, 'test', TX.build_company_list_request(),
                                               priority=1)
            self.write({'state': 'connected', 'last_test_message': _(
                'Agent %(host)s online (v%(ver)s). Tally reachable: %(ok)s. A company-list '
                'probe was queued.') % {'host': self.agent_hostname, 'ver': self.agent_version,
                                        'ok': self.agent_tally_ok}})
            return self._notify(self.last_test_message)
        try:
            raw = self._post_direct(TX.build_company_list_request())
            companies = [c.get('@name') or c.get('name') for c in
                         TX.parse_collection(raw, 'COMPANY')]
        except (TallyConnectionError, TX.TallyProtocolError) as e:
            self.write({'state': 'error', 'last_test_message': str(e)})
            return self._notify(str(e), 'danger')
        if self.tally_company not in companies:
            msg = _('Connected, but company "%(c)s" is not open in Tally. Open companies: '
                    '%(l)s') % {'c': self.tally_company, 'l': ', '.join(filter(None, companies))
                                or '-'}
            self.write({'state': 'error', 'last_test_message': msg})
            return self._notify(msg, 'warning')
        self.write({'state': 'connected', 'last_test_message': _(
            'Connected to TallyPrime. Company "%s" is open.') % self.tally_company})
        return self._notify(self.last_test_message, 'success')

    def _notify(self, message, ntype='info'):
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': _('Tally'), 'message': message, 'type': ntype,
                           'sticky': ntype == 'danger',
                           'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'}}}

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    def action_sync_now(self):
        for rec in self:
            rec._run_cycle()
        return self._notify(_('Sync cycle completed. Check the Activity log for details.'),
                            'success')

    def _run_cycle(self):
        self.ensure_one()
        if self.transport == 'file':
            return
        self._queue_pulls()
        self._queue_census(only_if_due=True)
        self.env['tally.request']._process_queue(self)
        self.env['tally.voucher'].search([('connection_id', '=', self.id),
                                          ('state', '=', 'draft')])._process()

    @api.model
    def _cron_sync(self):
        for conn in self.search([('transport', '!=', 'file')]):
            try:
                with self.env.cr.savepoint():
                    conn._run_cycle()
            except Exception:  # never let one connection break the cron
                _logger.exception('Tally sync failed for connection %s', conn.name)
        self.env['tally.request']._gc()

    def action_generate_agent_token(self):
        self._check_manager()
        for rec in self:
            rec.sudo().agent_token = secrets.token_urlsafe(32)
        return True

    def action_view_requests(self):
        return self._open('tally.request', _('Activity'))

    def action_view_failed(self):
        return self._open('tally.request', _('Failed'), [('state', '=', 'failed')])

    def action_view_vouchers(self):
        return self._open('tally.voucher', _('Tally Vouchers'))

    def action_view_vouchers_review(self):
        return self._open('tally.voucher', _('Vouchers to review'),
                          [('state', 'in', ('error', 'anomaly', 'conflict'))])

    def action_view_mappings(self):
        return self._open('tally.mapping', _('Mappings'))

    def action_view_mappings_review(self):
        return self._open('tally.mapping', _('Mappings to review'),
                          [('state', 'in', ('suggested', 'conflict'))])

    def _open(self, model, name, domain=None):
        return {'type': 'ir.actions.act_window', 'name': name, 'res_model': model,
                'view_mode': 'list,form',
                'domain': [('connection_id', '=', self.id)] + (domain or []),
                'context': {'default_connection_id': self.id}}

    # ------------------------------------------------------------------
    # Bridge agent API (called through Odoo's JSON-2 API with a user API key)
    # ------------------------------------------------------------------
    @api.model
    def _agent_connection(self, token):
        if not self.env.user.has_group('zivoit_tally_connector.group_tally_agent'):
            raise AccessError(_('This user is not allowed to act as a Tally agent.'))
        if not token:
            raise AccessError(_('Missing agent token.'))
        conns = self.sudo().search([('transport', '=', 'agent'), ('agent_token', '!=', False)])
        conn = conns.filtered(lambda c: secrets.compare_digest(c.agent_token, token))
        if not conn:
            raise AccessError(_('Invalid agent token.'))
        return conn[:1]

    @api.model
    def agent_poll(self, token, hostname=None, version=None, tally_ok=None, limit=20):
        """Heartbeat + fetch the next Tally requests to execute locally."""
        conn = self._agent_connection(token)
        conn.write({'agent_last_seen': fields.Datetime.now(), 'agent_hostname': hostname,
                    'agent_version': version, 'agent_tally_ok': bool(tally_ok)})
        conn._queue_pulls(only_if_due=True)
        conn._queue_census(only_if_due=True)
        jobs = self.env['tally.request'].sudo()._agent_checkout(conn, limit=limit) \
            if limit and limit > 0 else self.env['tally.request']
        return {'connection': conn.name, 'company': conn.tally_company,
                'host': conn.host or 'localhost', 'port': conn.port or 9000,
                'poll_seconds': 5 if jobs else 15,
                'jobs': [{'id': j.id, 'xml': j.request_xml, 'timeout': conn.timeout}
                         for j in jobs]}

    @api.model
    def agent_submit(self, token, results):
        """results: [{'id': int, 'ok': bool, 'response': str, 'error': str}]"""
        conn = self._agent_connection(token)
        Req = self.env['tally.request'].sudo()
        done = 0
        for res in results or []:
            req = Req.search([('id', '=', int(res.get('id') or 0)),
                              ('connection_id', '=', conn.id), ('state', '=', 'sent')], limit=1)
            if not req:
                continue
            if res.get('ok'):
                req._handle_response(res.get('response') or '')
            else:
                req._mark_failed(res.get('error') or _('Agent could not reach Tally'),
                                 retry=True)
            done += 1
        # Staged vouchers can be processed straight away
        self.env['tally.voucher'].sudo().search([('connection_id', '=', conn.id),
                                                 ('state', '=', 'draft')], limit=200)._process()
        return {'accepted': done}
