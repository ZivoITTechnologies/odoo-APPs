# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
import difflib
import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.modules import module as odoo_module

from ..lib import crypto
from ..lib.qbo_client import QBOClient, QBOError, QBOAuthError, parse_webhook
from .qb_entity_config import ENTITIES, ENTITY_INFO, ENTITY_CANONICAL

_logger = logging.getLogger(__name__)

CDC_MAX_DAYS = 29
TXN_ENTITIES = ("Invoice", "CreditMemo", "SalesReceipt", "Bill", "VendorCredit", "JournalEntry",
                "Payment", "BillPayment", "Estimate", "PurchaseOrder")
DEFAULT_DIRECTIONS = {"TaxCode": "import", "PaymentMethod": "import", "SalesReceipt": "import",
                      "Account": "import", "Term": "import"}
DEFAULT_AUTO_OFF = {"JournalEntry"}


class QbConnectionToken(models.Model):
    """Encrypted OAuth tokens, kept in their own table so they can be saved
    from an independent cursor the instant Intuit rotates them (never lose a
    rotated refresh token because the business transaction rolled back)."""
    _name = "qb.connection.token"
    _description = "QuickBooks OAuth Tokens"

    connection_id = fields.Many2one("qb.connection", required=True, ondelete="cascade", index=True)
    access_token = fields.Char(groups="base.group_system")
    refresh_token = fields.Char(groups="base.group_system")
    access_expiry = fields.Datetime()
    refresh_expiry = fields.Datetime()

    _conn_uniq = models.Constraint("UNIQUE(connection_id)", "One token record per connection.")


class QbConnection(models.Model):
    _name = "qb.connection"
    _description = "QuickBooks Online Connection"
    _inherit = ["mail.thread"]
    _order = "sequence, id"

    # ---------------------------------------------------------- identity
    name = fields.Char(required=True, default="QuickBooks Online", tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    color = fields.Integer()
    company_id = fields.Many2one("res.company", required=True, default=lambda s: s.env.company,
                                 index=True, tracking=True)
    state = fields.Selection([("draft", "Not Connected"), ("connected", "Connected"),
                              ("expired", "Re-authorization Needed"), ("error", "Error")],
                             default="draft", required=True, tracking=True, copy=False)
    environment = fields.Selection([("sandbox", "Sandbox (testing)"), ("production", "Production")],
                                   default="sandbox", required=True, tracking=True)
    client_id = fields.Char("Client ID", groups="zivoit_quickbooks_connector.group_qb_manager", copy=False)
    client_secret = fields.Char("Client Secret", groups="zivoit_quickbooks_connector.group_qb_manager",
                                copy=False)
    redirect_uri = fields.Char(compute="_compute_urls",
                               help="Copy this URL into Intuit Developer → Keys & credentials → Redirect URIs")
    webhook_url = fields.Char(compute="_compute_urls",
                              help="Copy this URL into Intuit Developer → Webhooks → Endpoint URL")
    webhook_verifier_token = fields.Char(groups="zivoit_quickbooks_connector.group_qb_manager", copy=False,
                                         help="Verifier token shown in Intuit Developer → Webhooks")
    realm_id = fields.Char("Company ID (Realm)", copy=False, tracking=True, index=True)
    oauth_nonce = fields.Char(copy=False, groups="base.group_system")
    token_ids = fields.One2many("qb.connection.token", "connection_id", groups="base.group_system")
    access_expiry = fields.Datetime(compute="_compute_token_info")
    refresh_expiry = fields.Datetime(compute="_compute_token_info")
    minor_version = fields.Char(default="75", required=True,
                                help="QuickBooks API minor version (75 or higher is required by Intuit).")

    # ------------------------------------------------- QuickBooks company
    qb_company_name = fields.Char("QuickBooks Company", readonly=True, copy=False)
    qb_country = fields.Char("QuickBooks Country", readonly=True, copy=False)
    qb_home_currency = fields.Char("Home Currency", readonly=True, copy=False)
    qb_multicurrency = fields.Boolean("Multi-currency enabled in QuickBooks", readonly=True, copy=False)
    is_us_company = fields.Boolean(compute="_compute_is_us", store=True)
    last_error = fields.Text(readonly=True, copy=False)

    # ---------------------------------------------------------- behaviour
    auto_sync = fields.Boolean("Automatic Sync", default=True, tracking=True,
                               help="Run incremental sync (CDC) and queued exports on schedule.")
    sync_interval = fields.Integer("Sync every (minutes)", default=15)
    next_sync = fields.Datetime(copy=False)
    last_cdc_sync = fields.Datetime("Last Incremental Sync", copy=False)
    last_full_import = fields.Datetime(copy=False)
    batch_size = fields.Integer("Jobs per Run", default=200)
    max_attempts = fields.Integer("Max Retries", default=5)
    conflict_policy = fields.Selection([
        ("newest", "Most recent change wins"),
        ("qb", "QuickBooks always wins"),
        ("odoo", "Odoo always wins"),
        ("manual", "Ask me (hold as conflict)"),
    ], default="newest", required=True, tracking=True)
    deletion_policy = fields.Selection([
        ("mirror", "Mirror: archive / cancel the Odoo record"),
        ("flag", "Flag only: keep Odoo record, mark mapping"),
    ], default="mirror", required=True,
        help="What to do in Odoo when a record is deleted in QuickBooks.")
    void_policy = fields.Selection([("cancel", "Cancel the Odoo document"), ("flag", "Flag only")],
                                   default="cancel", required=True)
    odoo_delete_policy = fields.Selection([
        ("propagate", "Delete / deactivate in QuickBooks"), ("ignore", "Do nothing")],
        default="propagate", required=True,
        help="What to do in QuickBooks when a synced record is deleted or cancelled in Odoo.")
    match_existing = fields.Boolean("Smart Duplicate Matching", default=True,
                                    help="Before creating, match existing records by e-mail, VAT, "
                                         "SKU, account number or name to avoid duplicates on both sides.")
    import_start_date = fields.Date(default=lambda s: fields.Date.today() - timedelta(days=365),
                                    help="Transactions dated before this are not imported.")
    export_start_date = fields.Date(default=fields.Date.today,
                                    help="Odoo documents dated before this are not exported automatically.")
    auto_post = fields.Boolean("Post imported documents", default=True)
    keep_qb_numbers = fields.Boolean("Use QuickBooks document numbers", default=True,
                                     help="Imported invoices/bills keep their QuickBooks number.")
    send_doc_number = fields.Boolean("Send Odoo numbers to QuickBooks", default=True)
    auto_export_on_post = fields.Boolean("Export immediately on validation", default=True)
    confirm_accepted_estimates = fields.Boolean(default=True,
                                                help="Confirm Odoo quotation when the estimate is accepted.")
    confirm_open_pos = fields.Boolean("Confirm imported open POs", default=True)
    share_partners = fields.Boolean("Partners shared between companies", default=True)
    vendor_suffix = fields.Char(default=" (V)",
                                help="QuickBooks forbids the same display name on a customer and a vendor. "
                                     "This suffix is appended to the vendor name when needed.")
    us_send_tax_total = fields.Boolean("Send Odoo tax total (US)", default=True,
                                       help="US companies: override QuickBooks automated sales tax with the "
                                            "tax computed by Odoo so both totals match.")
    fetch_pdf = fields.Boolean("Attach QuickBooks PDF on import", default=False)
    log_api_calls = fields.Boolean("Log every API call (debug)", default=False)
    log_retention_days = fields.Integer(default=60)

    # -------------------------------------------------- default accounts
    sale_journal_id = fields.Many2one("account.journal", domain="[('type','=','sale'),('company_id','=',company_id)]")
    purchase_journal_id = fields.Many2one("account.journal",
                                          domain="[('type','=','purchase'),('company_id','=',company_id)]")
    misc_journal_id = fields.Many2one("account.journal",
                                      domain="[('type','=','general'),('company_id','=',company_id)]")
    payment_journal_id = fields.Many2one("account.journal", string="Default Payment Journal",
                                         domain="[('type','in',('bank','cash','credit')),('company_id','=',company_id)]")
    income_account_id = fields.Many2one("account.account", string="Default Income Account")
    expense_account_id = fields.Many2one("account.account", string="Default Expense Account")
    sales_tax_account_id = fields.Many2one("account.account", string="US Sales Tax Payable Account",
                                           help="Imported US sales tax is booked on this account.")
    discount_account_id = fields.Many2one("account.account", string="Discount Account")
    class_plan_id = fields.Many2one("account.analytic.plan", string="Analytic Plan for Classes")
    location_plan_id = fields.Many2one("account.analytic.plan", string="Analytic Plan for Locations")

    entity_config_ids = fields.One2many("qb.entity.config", "connection_id", string="Entities")

    # -------------------------------------------------------- dashboard
    pending_count = fields.Integer(compute="_compute_stats")
    failed_count = fields.Integer(compute="_compute_stats")
    conflict_count = fields.Integer(compute="_compute_stats")
    mapping_count = fields.Integer(compute="_compute_stats")
    unmapped_count = fields.Integer(compute="_compute_stats")
    done_today_count = fields.Integer(compute="_compute_stats")
    health = fields.Selection([("ok", "Healthy"), ("warn", "Attention"), ("ko", "Action needed")],
                              compute="_compute_stats")

    _realm_company_uniq = models.Constraint(
        "UNIQUE(realm_id, company_id)",
        "This QuickBooks company is already connected to this Odoo company.")

    # ================================================================ compute
    def _compute_urls(self):
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/")
        for rec in self:
            rec.redirect_uri = "%s/zivoit_qbo/oauth/callback" % base
            rec.webhook_url = "%s/zivoit_qbo/webhook" % base

    @api.depends("qb_country")
    def _compute_is_us(self):
        for rec in self:
            rec.is_us_company = (rec.qb_country or "").upper() in ("US", "USA", "UNITED STATES")

    def _compute_token_info(self):
        for rec in self:
            tok = self.env["qb.connection.token"].sudo().search([("connection_id", "=", rec.id)], limit=1)
            rec.access_expiry = tok.access_expiry
            rec.refresh_expiry = tok.refresh_expiry

    def _compute_stats(self):
        Job = self.env["qb.sync.job"]
        Mapping = self.env["qb.mapping"]
        today = fields.Datetime.now() - timedelta(hours=24)
        for rec in self:
            if not rec.id:
                rec.update(dict.fromkeys(("pending_count", "failed_count", "conflict_count", "mapping_count",
                                          "unmapped_count", "done_today_count"), 0), health="ok")
                continue
            counts = dict(Job._read_group([("connection_id", "=", rec.id)], ["state"], ["__count"]))
            rec.pending_count = counts.get("pending", 0) + counts.get("running", 0)
            rec.failed_count = counts.get("failed", 0)
            rec.conflict_count = counts.get("conflict", 0)
            rec.mapping_count = Mapping.search_count([("connection_id", "=", rec.id)])
            rec.unmapped_count = Mapping.search_count([("connection_id", "=", rec.id), ("state", "=", "unmapped")])
            rec.done_today_count = Job.search_count([("connection_id", "=", rec.id), ("state", "=", "done"),
                                                     ("date_done", ">=", today)])
            if rec.state in ("expired", "error") or rec.failed_count > 20:
                rec.health = "ko"
            elif rec.state != "connected" or rec.failed_count or rec.conflict_count or rec.unmapped_count:
                rec.health = "warn"
            else:
                rec.health = "ok"

    # ================================================================== CRUD
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            rec._ensure_entity_configs()
            rec._ensure_defaults()
        return records

    def _ensure_entity_configs(self):
        for rec in self:
            existing = set(rec.entity_config_ids.mapped("entity"))
            vals = []
            for ent in ENTITIES:
                if ent[0] in existing:
                    continue
                vals.append({"connection_id": rec.id, "entity": ent[0],
                             "direction": DEFAULT_DIRECTIONS.get(ent[0], "both"),
                             "auto_sync": ent[0] not in DEFAULT_AUTO_OFF})
            self.env["qb.entity.config"].create(vals)

    def _ensure_defaults(self):
        Journal = self.env["account.journal"]
        Plan = self.env["account.analytic.plan"]
        for rec in self:
            dom = [("company_id", "=", rec.company_id.id)]
            vals = {}
            if not rec.sale_journal_id:
                vals["sale_journal_id"] = Journal.search(dom + [("type", "=", "sale")], limit=1).id
            if not rec.purchase_journal_id:
                vals["purchase_journal_id"] = Journal.search(dom + [("type", "=", "purchase")], limit=1).id
            if not rec.misc_journal_id:
                vals["misc_journal_id"] = Journal.search(dom + [("type", "=", "general")], limit=1).id
            if not rec.payment_journal_id:
                vals["payment_journal_id"] = Journal.search(dom + [("type", "=", "bank")], limit=1).id
            if not rec.class_plan_id:
                plan = Plan.search([("name", "=", "QuickBooks Classes")], limit=1) or \
                    Plan.create({"name": "QuickBooks Classes"})
                vals["class_plan_id"] = plan.id
            if not rec.location_plan_id:
                plan = Plan.search([("name", "=", "QuickBooks Locations")], limit=1) or \
                    Plan.create({"name": "QuickBooks Locations"})
                vals["location_plan_id"] = plan.id
            if vals:
                rec.write(vals)

    def _config(self, entity):
        self.ensure_one()
        cfg = self.entity_config_ids.filtered(lambda c: c.entity == entity)
        if not cfg and entity in ENTITY_INFO:
            self._ensure_entity_configs()
            cfg = self.entity_config_ids.filtered(lambda c: c.entity == entity)
        return cfg[:1]

    def _allows(self, entity, direction):
        cfg = self._config("TaxCode" if entity == "TaxCodePurchase" else entity)
        return bool(cfg) and cfg.allows(direction)

    # ============================================================= tokens
    def _get_tokens(self):
        self.ensure_one()
        tok = self.env["qb.connection.token"].sudo().search([("connection_id", "=", self.id)], limit=1)
        secret = self._crypt_secret()
        return (crypto.decrypt(secret, tok.access_token), crypto.decrypt(secret, tok.refresh_token),
                tok.access_expiry)

    def _crypt_secret(self):
        return self.env["ir.config_parameter"].sudo().get_param("database.secret") or self.env.cr.dbname

    def _save_tokens(self, body):
        """Persist rotated tokens in an independent transaction."""
        self.ensure_one()
        secret = self._crypt_secret()
        now = fields.Datetime.now()
        vals = {
            "access_token": crypto.encrypt(secret, body.get("access_token")),
            "access_expiry": now + timedelta(seconds=int(body.get("expires_in") or 3600)),
        }
        if body.get("refresh_token"):
            vals["refresh_token"] = crypto.encrypt(secret, body["refresh_token"])
            vals["refresh_expiry"] = now + timedelta(
                seconds=int(body.get("x_refresh_token_expires_in") or 100 * 86400))
        conn_id = self.id

        def _write(env):
            Token = env["qb.connection.token"].sudo()
            tok = Token.search([("connection_id", "=", conn_id)], limit=1)
            if tok:
                tok.write(vals)
            else:
                Token.create(dict(vals, connection_id=conn_id))

        if odoo_module.current_test or self.env.context.get("qb_same_cursor_tokens"):
            _write(self.env)
            return
        with self.env.registry.cursor() as cr:
            _write(api.Environment(cr, self.env.uid, {}))
        # make the business transaction see the fresh values too
        self.env["qb.connection.token"].invalidate_model()

    def _get_client(self, require_realm=True):
        self.ensure_one()
        conn = self.sudo()
        if not (conn.client_id and conn.client_secret):
            raise UserError(_("Please fill the QuickBooks Client ID and Client Secret first."))
        if require_realm and not conn.realm_id:
            raise UserError(_("This connection is not authorized yet. Click 'Connect to QuickBooks'."))
        access, refresh, expiry = conn._get_tokens()
        client = QBOClient(
            conn.client_id, conn.client_secret, realm_id=conn.realm_id,
            environment=conn.environment, access_token=access, refresh_token=refresh,
            token_saver=conn._save_tokens, minor_version=conn.minor_version,
            logger_cb=conn._api_logger if conn.log_api_calls else None,
        )
        if refresh and (not access or not expiry or expiry < fields.Datetime.now() + timedelta(minutes=5)):
            client.refresh()
        return client

    def _api_logger(self, method, url, params, payload, status, body, duration, tid):
        self.env["qb.sync.log"]._log(
            self, "%s %s → %s" % (method, url.split("/v3/company/")[-1], status), level="api",
            request_method=method, request_url=url, request_body=payload, response_status=status,
            response_body=body, duration_ms=int(duration * 1000), intuit_tid=tid)

    # ============================================================== OAuth
    def _oauth_state(self):
        self.ensure_one()
        nonce = secrets.token_urlsafe(16)
        self.sudo().oauth_nonce = nonce
        sig = hmac.new(self._crypt_secret().encode(), ("%s:%s" % (self.id, nonce)).encode(),
                       hashlib.sha256).hexdigest()[:32]
        return "%s:%s:%s" % (self.id, nonce, sig)

    @api.model
    def _check_oauth_state(self, state):
        try:
            conn_id, nonce, sig = (state or "").split(":")
            conn = self.sudo().browse(int(conn_id)).exists()
        except (ValueError, TypeError):
            return self.browse()
        if not conn or not conn.oauth_nonce or not hmac.compare_digest(conn.oauth_nonce, nonce):
            return self.browse()
        expected = hmac.new(conn._crypt_secret().encode(), ("%s:%s" % (conn.id, nonce)).encode(),
                            hashlib.sha256).hexdigest()[:32]
        return conn if hmac.compare_digest(expected, sig) else self.browse()

    def action_connect(self):
        self.ensure_one()
        conn = self.sudo()
        if not (conn.client_id and conn.client_secret):
            raise UserError(_("Please fill the QuickBooks Client ID and Client Secret first."))
        url = QBOClient.authorization_url(conn.client_id, self.redirect_uri, self._oauth_state())
        return {"type": "ir.actions.act_url", "url": url, "target": "self"}

    def _oauth_callback(self, code, realm_id):
        self.ensure_one()
        conn = self.sudo()
        conn.realm_id = realm_id
        client = QBOClient(conn.client_id, conn.client_secret, realm_id=realm_id,
                           environment=conn.environment, token_saver=conn._save_tokens)
        client.exchange_code(code, self.redirect_uri)
        conn.oauth_nonce = False
        conn._after_connect()

    def _after_connect(self):
        self.ensure_one()
        conn = self.sudo()
        conn.write({"state": "connected", "last_error": False})
        try:
            conn._refresh_company_info()
        except QBOError as exc:
            conn.write({"state": "error", "last_error": str(exc)})
            raise
        if not conn.last_cdc_sync:
            conn.last_cdc_sync = fields.Datetime.now()
        conn.message_post(body=_("Connected to QuickBooks company %s (realm %s).",
                                 conn.qb_company_name or "?", conn.realm_id))

    def _refresh_company_info(self):
        client = self._get_client()
        info = client.company_info()
        prefs = {}
        try:
            prefs = client.preferences()
        except QBOError:
            _logger.info("Could not read QuickBooks preferences", exc_info=True)
        currency_prefs = prefs.get("CurrencyPrefs") or {}
        self.sudo().write({
            "qb_company_name": info.get("CompanyName"),
            "qb_country": info.get("Country") or (info.get("CompanyAddr") or {}).get("Country"),
            "qb_home_currency": (currency_prefs.get("HomeCurrency") or {}).get("value"),
            "qb_multicurrency": bool(currency_prefs.get("MultiCurrencyEnabled")),
        })
        return info

    def action_test_connection(self):
        self.ensure_one()
        info = self._refresh_company_info()
        self.write({"state": "connected", "last_error": False})
        return self._notify(_("Connection OK"), _("Connected to %s.", info.get("CompanyName")), "success")

    def action_disconnect(self):
        for rec in self:
            try:
                rec._get_client(require_realm=False).revoke()
            except (QBOError, UserError):
                _logger.info("QuickBooks revoke failed (ignored)", exc_info=True)
            self.env["qb.connection.token"].sudo().search([("connection_id", "=", rec.id)]).unlink()
            rec.write({"state": "draft"})
            rec.message_post(body=_("Disconnected from QuickBooks; tokens revoked and deleted."))
        return True

    # =============================================================== queue
    def _enqueue(self, direction, entity, qb_id=None, record=None, payload=None, operation=None,
                 source="manual", priority_boost=False):
        """Create a job unless an identical pending one exists (dedup)."""
        self.ensure_one()
        Job = self.env["qb.sync.job"].sudo()
        domain = [("connection_id", "=", self.id), ("direction", "=", direction), ("qb_entity", "=", entity),
                  ("state", "in", ("pending", "conflict"))]
        if qb_id:
            domain.append(("qb_id", "=", str(qb_id)))
        if record:
            domain += [("res_model", "=", record._name), ("res_id", "=", record.id)]
        existing = Job.search(domain, limit=1)
        if existing:
            vals = {}
            if payload is not None:
                vals["payload"] = payload
            if operation:
                vals["operation"] = operation
            if existing.state == "conflict" and self.env.context.get("qb_force"):
                vals["state"] = "pending"
            if vals:
                existing.write(vals)
            return existing
        return Job.create({
            "connection_id": self.id, "direction": direction, "qb_entity": entity,
            "qb_id": str(qb_id) if qb_id else False,
            "res_model": record._name if record else False, "res_id": record.id if record else False,
            "payload": payload, "operation": operation, "source": source,
            "max_attempts": self.max_attempts or 5,
            "priority": Job._default_priority(entity, priority_boost),
        })

    def _process_queue(self, limit=None):
        """Run pending jobs synchronously (used by buttons, tests and wizards)."""
        self.ensure_one()
        jobs = self.env["qb.sync.job"].search([("connection_id", "=", self.id), ("state", "=", "pending"),
                                                ("next_attempt", "<=", fields.Datetime.now())],
                                               order="priority, id", limit=limit or self.batch_size or 200)
        return jobs._process(limit=len(jobs))

    def _notify_queue_action(self):
        pending = self.env["qb.sync.job"].search_count([("connection_id", "in", self.ids), ("state", "=", "pending")])
        return self._notify(_("Queued"), _("%s job(s) waiting in the QuickBooks queue. They run in the "
                                           "background; use 'Run Queue Now' to process immediately.", pending),
                            "info")

    @api.model
    def _notify(self, title, message, ntype="info"):
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": title, "message": message, "type": ntype, "sticky": False,
                           "next": {"type": "ir.actions.act_window_close"}}}

    def action_run_queue(self):
        total = 0
        for rec in self:
            total += rec._process_queue()
        return self._notify(_("Queue processed"), _("%s job(s) processed.", total), "success")

    # ========================================================= full import
    def _import_query_where(self, entity, date_from=None, date_to=None):
        cfg = self._config(entity)
        clauses = []
        if entity in TXN_ENTITIES:
            date_from = date_from or self.import_start_date
            if date_from:
                clauses.append("TxnDate >= '%s'" % fields.Date.to_string(date_from))
            if date_to:
                clauses.append("TxnDate <= '%s'" % fields.Date.to_string(date_to))
        if entity == "ItemCategory":
            clauses.append("Type = 'Category'")
        if cfg and cfg.import_filter:
            clauses.append("(%s)" % cfg.import_filter)
        return " AND ".join(clauses) or None

    def _full_import(self, entities=None, date_from=None, date_to=None, source="manual"):
        """Pull every record of the given entities and enqueue import jobs in
        dependency order. Returns the number of jobs queued."""
        self.ensure_one()
        client = self._get_client()
        order = [e[0] for e in ENTITIES]
        entities = [e for e in order if e in (entities or order) and self._allows(e, "import")]
        queued = 0
        for entity in entities:
            qb_name = "Item" if entity == "ItemCategory" else entity
            where = self._import_query_where(entity, date_from, date_to)
            for rec in client.query_all(qb_name, where=where):
                if entity == "Item" and rec.get("Type") == "Category":
                    continue
                self._enqueue("import", entity, qb_id=rec.get("Id"), payload=rec, source=source)
                queued += 1
            cfg = self._config(entity)
            if cfg:
                cfg.last_import = fields.Datetime.now()
        self.last_full_import = fields.Datetime.now()
        return queued

    # ======================================================== incremental
    def _cdc_entities(self):
        return [ENTITY_INFO[e][6] for e in ENTITY_INFO
                if ENTITY_INFO[e][6] and self._allows(e, "import") and self._config(e).auto_sync]

    def _incremental_sync(self, source="cdc"):
        """Use QuickBooks Change Data Capture to fetch everything that changed
        since the last run – including deletions – in one call per 30 days."""
        self.ensure_one()
        client = self._get_client()
        since = self.last_cdc_sync or (fields.Datetime.now() - timedelta(days=1))
        started = fields.Datetime.now()
        entities = self._cdc_entities()
        if not entities:
            return 0
        if since < started - timedelta(days=CDC_MAX_DAYS):
            # CDC look-back is limited to 30 days: fall back to a dated full import
            queued = self._full_import(date_from=since.date(), source=source)
            self.last_cdc_sync = started
            return queued
        changed = {}
        # Intuit recommends small entity lists per CDC call
        for i in range(0, len(entities), 10):
            for ent, records in client.cdc(entities[i:i + 10], since.strftime("%Y-%m-%dT%H:%M:%SZ")).items():
                changed.setdefault(ent, []).extend(records)
        queued = 0
        for ent, records in changed.items():
            for rec in records:
                entity = ent
                if ent == "Item" and rec.get("Type") == "Category":
                    entity = "ItemCategory"
                deleted = rec.get("status") == "Deleted"
                self._enqueue("import", entity, qb_id=rec.get("Id"),
                              payload=None if deleted else rec,
                              operation="Delete" if deleted else "Update", source=source)
                queued += 1
        self.last_cdc_sync = started
        return queued

    # ============================================================ exports
    def _export_candidates(self, entity, force=False):
        """Return Odoo records that should be exported for ``entity``."""
        self.ensure_one()
        env = self.env
        company = self.company_id
        start = self.export_start_date
        exp = env["qb.exporter"]
        domain = None
        model = ENTITY_INFO[entity][2]
        if entity == "Customer":
            domain = [("customer_rank", ">", 0), ("parent_id", "=", False),
                      ("company_id", "in", (False, company.id))]
        elif entity == "Vendor":
            domain = [("supplier_rank", ">", 0), ("parent_id", "=", False),
                      ("company_id", "in", (False, company.id))]
        elif entity == "Item":
            domain = [("company_id", "in", (False, company.id)), "|", ("sale_ok", "=", True),
                      ("purchase_ok", "=", True)]
        elif entity in ("Invoice", "CreditMemo", "Bill", "VendorCredit"):
            mtype = exp.MOVE_TYPES_BY_ENTITY[entity]
            domain = [("move_type", "=", mtype), ("state", "=", "posted"), ("company_id", "=", company.id)]
            if start and not force:
                domain.append(("invoice_date", ">=", start))
        elif entity == "JournalEntry":
            domain = [("move_type", "=", "entry"), ("state", "=", "posted"), ("company_id", "=", company.id),
                      ("journal_id.type", "=", "general"), ("origin_payment_id", "=", False),
                      ("statement_line_id", "=", False)]
            if company.currency_exchange_journal_id:
                domain.append(("journal_id", "!=", company.currency_exchange_journal_id.id))
            if start and not force:
                domain.append(("date", ">=", start))
        elif entity == "Estimate":
            domain = [("company_id", "=", company.id), ("state", "in", ("draft", "sent", "sale"))]
            if start and not force:
                domain.append(("date_order", ">=", fields.Datetime.to_datetime(start)))
        elif entity == "PurchaseOrder":
            domain = [("company_id", "=", company.id), ("state", "in", ("purchase", "done"))]
            if start and not force:
                domain.append(("date_order", ">=", fields.Datetime.to_datetime(start)))
        elif entity in ("Payment", "BillPayment"):
            ptype = "inbound" if entity == "Payment" else "outbound"
            partner_type = "customer" if entity == "Payment" else "supplier"
            domain = [("company_id", "=", company.id), ("payment_type", "=", ptype),
                      ("partner_type", "=", partner_type), ("state", "not in", ("draft", "canceled", "rejected"))]
            if start and not force:
                domain.append(("date", ">=", start))
        elif force and entity == "Account":
            domain = [("company_ids", "in", company.ids)]
        elif force and entity == "Term":
            domain = [("company_id", "in", (False, company.id))]
        elif force and entity in ("Class", "Department"):
            plan = self.class_plan_id if entity == "Class" else self.location_plan_id
            domain = [("plan_id", "=", plan.id)]
        if domain is None or model not in env:
            return env[model].browse() if model in env else None
        Model = env[model].with_context(active_test=True)
        # exclude records already synced (mapping without dirty flag)
        mapped = env["qb.mapping"].search([("connection_id", "=", self.id), ("qb_entity", "=", entity),
                                           ("res_model", "=", model), ("state", "not in", ("dirty",))])
        ids_done = set(mapped.mapped("res_id"))
        records = Model.search(domain)
        return records.filtered(lambda r: r.id not in ids_done)

    def _queue_pending_exports(self, entities=None, force=False, source="cron"):
        self.ensure_one()
        queued = 0
        for ent in ENTITIES:
            entity = ent[0]
            if entities and entity not in entities:
                continue
            if not self._allows(entity, "export"):
                continue
            if not force and not self._config(entity).auto_sync:
                continue
            records = self._export_candidates(entity, force=force)
            for rec in records or []:
                self._enqueue("export", entity, record=rec, source=source)
                queued += 1
            cfg = self._config(entity)
            if cfg and records:
                cfg.last_export = fields.Datetime.now()
        return queued

    # ============================================================== crons
    @api.model
    def _cron_sync(self):
        now = fields.Datetime.now()
        for conn in self.search([("state", "=", "connected"), ("auto_sync", "=", True)]):
            if conn.next_sync and conn.next_sync > now:
                continue
            try:
                conn._incremental_sync()
                conn._queue_pending_exports()
                conn.next_sync = now + timedelta(minutes=max(conn.sync_interval or 15, 1))
            except QBOAuthError as exc:
                conn.write({"state": "expired", "last_error": str(exc)})
                self.env["qb.sync.log"]._log(conn, str(exc), level="error")
            except Exception as exc:  # pylint: disable=broad-except
                _logger.exception("QuickBooks scheduled sync failed for %s", conn.name)
                self.env["qb.sync.log"]._log(conn, str(exc), level="error")
            if self.env.context.get("ir_cron_progress_id"):
                self.env["ir.cron"]._commit_progress(1)

    @api.model
    def _cron_keep_tokens_alive(self):
        """Refresh daily: Intuit rotates refresh tokens and expires idle ones."""
        for conn in self.search([("state", "in", ("connected", "expired")), ("realm_id", "!=", False)]):
            try:
                conn._get_client().refresh()
                if conn.state == "expired":
                    conn.state = "connected"
            except (QBOError, UserError) as exc:
                conn.write({"state": "expired", "last_error": str(exc)})
                conn.message_post(body=_("QuickBooks authorization expired: %s. Please reconnect.", exc))

    def action_sync_now(self):
        total = 0
        for rec in self:
            total += rec._incremental_sync(source="manual")
            total += rec._queue_pending_exports(source="manual")
            rec._process_queue()
        return self._notify(_("Synchronization done"), _("%s change(s) processed.", total), "success")

    # =========================================================== webhooks
    @api.model
    def _handle_webhook(self, raw_body, signature, payload):
        """Verify + enqueue. Must be fast (Intuit expects a 200 within 3 s)."""
        from ..lib.qbo_client import verify_webhook_signature
        events = parse_webhook(payload)
        realms = {e["realm"] for e in events}
        accepted = 0
        for realm in realms:
            for conn in self.sudo().search([("realm_id", "=", realm), ("state", "=", "connected")]):
                if not verify_webhook_signature(raw_body, signature, conn.webhook_verifier_token):
                    self.env["qb.sync.log"]._log(conn, "Rejected webhook: invalid intuit-signature",
                                                 level="warning")
                    continue
                for ev in (e for e in events if e["realm"] == realm):
                    entity = ENTITY_CANONICAL.get((ev["entity"] or "").lower())
                    if not entity or not conn._allows(entity, "import") or not conn._config(entity).auto_sync:
                        continue
                    if ev["operation"] == "Merge" and ev.get("deleted_id"):
                        conn._enqueue("import", entity, qb_id=ev["deleted_id"], operation="Merge",
                                      payload={"merged_into": ev["id"]}, source="webhook")
                    conn._enqueue("import", entity, qb_id=ev["id"], operation=ev["operation"],
                                  source="webhook", priority_boost=True)
                    accepted += 1
        return accepted

    # ======================================================== smart match
    def action_smart_match(self):
        """Fuzzy auto-mapping for accounts, taxes and payment methods that
        exist in QuickBooks but are not linked to an Odoo record yet."""
        self.ensure_one()
        client = self._get_client()
        made = 0
        made += self._smart_match_accounts(client)
        made += self._smart_match_taxes(client)
        made += self._smart_match_payment_methods(client)
        return self._notify(_("Smart Match"), _("%s mapping(s) proposed or confirmed. Review them in "
                                                "Configuration → Mappings.", made), "success")

    @staticmethod
    def _similar(a, b):
        return difflib.SequenceMatcher(None, (a or "").lower().strip(), (b or "").lower().strip()).ratio()

    def _smart_match_accounts(self, client):
        Mapping = self.env["qb.mapping"]
        accounts = self.env["account.account"].search([("company_ids", "in", self.company_id.ids)])
        made = 0
        for qacc in client.query_all("Account"):
            if Mapping._find(self, "Account", qacc["Id"]).res_id:
                continue
            best, score = None, 0.0
            num = qacc.get("AcctNum")
            for acc in accounts:
                s = 1.0 if num and acc.code == num else self._similar(acc.name, qacc.get("Name"))
                if s > score:
                    best, score = acc, s
            if best and score >= 0.82:
                m = Mapping._link(self, "Account", qacc, best)
                m.suggestion_score = round(score * 100, 1)
                made += 1
            else:
                Mapping._link(self, "Account", qacc, None)
        return made

    def _smart_match_taxes(self, client):
        Mapping = self.env["qb.mapping"]
        made = 0
        rates = {r["Id"]: float(r.get("RateValue") or 0) for r in client.query_all("TaxRate")}
        taxes = self.env["account.tax"].search([("company_id", "=", self.company_id.id)])
        for code in client.query_all("TaxCode"):
            for ttype, entity, key in (("sale", "TaxCode", "SalesTaxRateList"),
                                       ("purchase", "TaxCodePurchase", "PurchaseTaxRateList")):
                details = ((code.get(key) or {}).get("TaxRateDetail") or [])
                if not details or Mapping._find(self, entity, code["Id"]).res_id:
                    continue
                rate = sum(rates.get((d.get("TaxRateRef") or {}).get("value"), 0) for d in details)
                cands = taxes.filtered(lambda t: t.type_tax_use == ttype and t.amount_type == "percent"
                                       and abs(t.amount - rate) < 0.0001)
                best = max(cands, key=lambda t: self._similar(t.name, code.get("Name")), default=None)
                if best:
                    m = Mapping._link(self, entity, code, best)
                    m.suggestion_score = round(70 + 30 * self._similar(best.name, code.get("Name")), 1)
                    made += 1
        return made

    def _smart_match_payment_methods(self, client):
        Mapping = self.env["qb.mapping"]
        journals = self.env["account.journal"].search([("company_id", "=", self.company_id.id),
                                                       ("type", "in", ("bank", "cash", "credit"))])
        made = 0
        for pm in client.query_all("PaymentMethod"):
            if Mapping._find(self, "PaymentMethod", pm["Id"]).res_id:
                continue
            best = max(journals, key=lambda j: self._similar(j.name, pm.get("Name")), default=None)
            score = best and self._similar(best.name, pm.get("Name")) or 0
            if best and score >= 0.6:
                m = Mapping._link(self, "PaymentMethod", pm, best)
                m.suggestion_score = round(score * 100, 1)
                made += 1
            else:
                Mapping._link(self, "PaymentMethod", pm, None)
        return made

    # ======================================================= view actions
    def _action(self, xmlid, domain=None, context=None):
        action = self.env["ir.actions.act_window"]._for_xml_id(xmlid)
        action["domain"] = domain or [("connection_id", "in", self.ids)]
        if context:
            action["context"] = context
        return action

    def action_view_jobs(self):
        return self._action("zivoit_quickbooks_connector.action_qb_sync_job")

    def action_view_failed(self):
        return self._action("zivoit_quickbooks_connector.action_qb_sync_job",
                            [("connection_id", "in", self.ids), ("state", "in", ("failed", "conflict"))])

    def action_view_pending(self):
        return self._action("zivoit_quickbooks_connector.action_qb_sync_job",
                            [("connection_id", "in", self.ids), ("state", "in", ("pending", "running"))])

    def action_view_logs(self):
        return self._action("zivoit_quickbooks_connector.action_qb_sync_log")

    def action_view_mappings(self):
        return self._action("zivoit_quickbooks_connector.action_qb_mapping")

    def action_view_unmapped(self):
        return self._action("zivoit_quickbooks_connector.action_qb_mapping",
                            [("connection_id", "in", self.ids), ("state", "=", "unmapped")])

    def action_open_import_wizard(self):
        return {"type": "ir.actions.act_window", "res_model": "qb.import.wizard", "view_mode": "form",
                "target": "new", "context": {"default_connection_id": self.id}}

    def action_open_export_wizard(self):
        return {"type": "ir.actions.act_window", "res_model": "qb.export.wizard", "view_mode": "form",
                "target": "new", "context": {"default_connection_id": self.id}}

    def action_open_integrity_wizard(self):
        return {"type": "ir.actions.act_window", "res_model": "qb.integrity.wizard", "view_mode": "form",
                "target": "new", "context": {"default_connection_id": self.id}}

    def action_set_manual_tokens(self):
        """For local/offline setups: paste realm + refresh token obtained from
        the Intuit OAuth 2.0 Playground."""
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "qb.token.wizard", "view_mode": "form",
                "target": "new", "context": {"default_connection_id": self.id}}

    @api.model
    def _get_for_company(self, company):
        return self.search([("company_id", "=", company.id), ("state", "=", "connected")], limit=1)
