import hashlib
import hmac
import logging
import re
import uuid
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..lib import ai as ai_lib, client as client_lib, queries as Q
from ..lib.client import ShopifyClient, ShopifyError

_logger = logging.getLogger(__name__)

DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.(com|io)$")


class ZivoShopifyInstance(models.Model):
    _name = "zivo.shopify.instance"
    _description = "Shopify Store"
    _inherit = ["mail.thread"]
    _order = "sequence, id"

    name = fields.Char(required=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    color = fields.Integer()
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company, index=True)
    state = fields.Selection([
        ("draft", "Not Connected"),
        ("connected", "Connected"),
        ("error", "Connection Error"),
        ("uninstalled", "App Uninstalled"),
    ], default="draft", required=True, tracking=True)

    # Connection
    shop_domain = fields.Char(string="Shop Domain", required=True, help="your-store.myshopify.com")
    api_version = fields.Char(string="API Version", required=True, default=client_lib.DEFAULT_API_VERSION)
    auth_mode = fields.Selection([
        ("client_credentials", "Dev Dashboard App (Client ID + Secret)"),
        ("admin_token", "Existing Custom App (Admin API Token)"),
    ], default="client_credentials", required=True)
    client_id = fields.Char(string="Client ID", groups="zivoit_shopify_connector.group_manager")
    client_secret = fields.Char(string="Client Secret", groups="zivoit_shopify_connector.group_manager")
    access_token = fields.Char(string="Admin API Token", groups="zivoit_shopify_connector.group_manager", copy=False)
    token_expires_at = fields.Datetime(copy=False, groups="zivoit_shopify_connector.group_manager")
    webhook_secret = fields.Char(
        string="Webhook Signing Secret", groups="zivoit_shopify_connector.group_manager",
        help="Only for Admin API token apps. Dev Dashboard apps sign webhooks with the Client Secret.",
    )
    webhook_token = fields.Char(
        required=True, copy=False, index=True, default=lambda self: uuid.uuid4().hex,
        groups="zivoit_shopify_connector.group_manager",
    )
    webhook_url = fields.Char(compute="_compute_webhook_url")

    # Shop facts
    shop_name = fields.Char(readonly=True)
    shop_email = fields.Char(readonly=True)
    shop_currency_id = fields.Many2one("res.currency", readonly=True)
    shop_timezone = fields.Char(readonly=True)
    shop_plan = fields.Char(readonly=True)
    shop_taxes_included = fields.Boolean(readonly=True)
    shop_url = fields.Char(readonly=True)
    publication_data = fields.Json(default=list)
    last_connected_at = fields.Datetime(readonly=True)
    api_available = fields.Integer(readonly=True)
    api_maximum = fields.Integer(readonly=True)
    api_restore_rate = fields.Integer(readonly=True)

    # Order settings
    warehouse_id = fields.Many2one("stock.warehouse", string="Default Warehouse", check_company=True)
    pricelist_id = fields.Many2one("product.pricelist", string="Default Pricelist", check_company=True)
    team_id = fields.Many2one("crm.team", string="Sales Team")
    user_id = fields.Many2one("res.users", string="Salesperson")
    order_prefix = fields.Char(help="Added before the Shopify order number, for example SHP-.")
    use_shopify_order_name = fields.Boolean(string="Use Shopify Order Number", default=True)
    import_orders_after = fields.Datetime(string="Import Orders Created After")
    order_status_filter = fields.Selection([
        ("all", "All Orders"),
        ("paid", "Paid and Partially Paid Only"),
    ], default="all", required=True, string="Financial Status Filter")
    skip_test_orders = fields.Boolean(default=True)
    import_pos_orders = fields.Boolean(string="Import POS Orders", default=True)
    use_presentment_currency = fields.Boolean(
        string="Use Customer Currency", help="Create orders in the currency the customer paid in.",
    )
    auto_create_products = fields.Boolean(default=True)
    auto_create_taxes = fields.Boolean(default=True)
    update_existing_products = fields.Boolean(
        string="Overwrite Matched Odoo Products", help="Otherwise only products created by the import are updated.",
    )
    sync_images = fields.Boolean(string="Import Images", default=True)
    company_mode = fields.Selection([
        ("individual", "Individual Contacts"),
        ("b2b", "Company + Contact (B2B)"),
    ], default="individual", required=True, string="Customer Mode")
    rounding_tolerance = fields.Float(default=0.05, help="Largest difference absorbed by an adjustment line.")
    shipping_product_id = fields.Many2one("product.product", required=True, default=lambda s: s._default_product("product_shipping"))
    custom_item_product_id = fields.Many2one("product.product", required=True, default=lambda s: s._default_product("product_custom"))
    tip_product_id = fields.Many2one("product.product", required=True, default=lambda s: s._default_product("product_tip"))
    adjustment_product_id = fields.Many2one("product.product", required=True, default=lambda s: s._default_product("product_adjustment"))

    # Product settings
    sync_translations = fields.Boolean(string="Sync Translations", help="Keep product text in step with Shopify's store languages.")
    auto_create_categories = fields.Boolean(string="Auto-create Categories", default=True)
    export_categories = fields.Boolean(
        string="Create Collections from Categories",
        help="When a product is exported, its Odoo category becomes a manual Shopify collection if none is mapped yet.",
    )
    export_as_draft = fields.Boolean(string="Export New Products as Draft", default=True)
    publication_gids = fields.Char(string="Publish To", help="Comma separated publication IDs. Empty: all sales channels.")
    price_pricelist_id = fields.Many2one("product.pricelist", string="Price Pricelist", check_company=True)
    compare_at_pricelist_id = fields.Many2one("product.pricelist", string="Compare-at Pricelist", check_company=True)

    # Stock settings
    stock_source = fields.Selection([
        ("free", "Free to Use"),
        ("on_hand", "On Hand"),
        ("forecast", "Forecasted"),
    ], default="free", required=True, string="Stock Quantity")
    routing_allow_split = fields.Boolean(
        string="Split Across Warehouses",
        help="When no single warehouse can ship the whole order, each line ships from a warehouse holding it.",
    )
    stock_percent = fields.Float(string="Export % of Stock", default=100.0)
    smart_buffer_enabled = fields.Boolean(string="AI Smart Buffer")
    smart_buffer_hours = fields.Integer(string="Buffer Window (hours)", default=24)

    # Fulfilment
    notify_customer_on_fulfillment = fields.Boolean(string="Notify Customer", default=True)

    # Automation
    auto_import_orders = fields.Boolean(string="Scheduled Order Import", default=True)
    auto_import_products = fields.Boolean(string="Scheduled Product Import")
    auto_export_stock = fields.Boolean(string="Realtime Stock Export", default=True)
    auto_export_fulfillment = fields.Boolean(string="Automatic Fulfilment Export", default=True)
    auto_import_checkouts = fields.Boolean(string="Import Abandoned Checkouts")
    auto_import_payouts = fields.Boolean(string="Import Payouts", default=True)
    drift_guard_enabled = fields.Boolean(string="Stock Drift Guard", default=True)
    drift_auto_fix = fields.Boolean(string="Auto-correct Drift")
    last_order_sync = fields.Datetime(readonly=True)
    last_product_sync = fields.Datetime(readonly=True)
    last_customer_sync = fields.Datetime(readonly=True)
    last_checkout_sync = fields.Datetime(readonly=True)

    # Accounting
    clearing_journal_id = fields.Many2one(
        "account.journal", string="Shopify Payments Journal", check_company=True,
        domain="[('type', 'in', ('bank', 'cash'))]",
    )
    payout_journal_id = fields.Many2one(
        "account.journal", string="Payout Bank Journal", check_company=True, domain="[('type', '=', 'bank')]",
    )
    fee_account_id = fields.Many2one("account.account", string="Fees Account", check_company=True)
    adjustment_account_id = fields.Many2one("account.account", string="Adjustments Account", check_company=True)

    # AI
    ai_enabled = fields.Boolean(string="Use LLM", help="Local AI engines stay active even when this is off.")
    ai_provider = fields.Selection([
        ("openai", "OpenAI"),
        ("anthropic", "Anthropic Claude"),
        ("gemini", "Google Gemini"),
        ("compatible", "OpenAI-compatible (Ollama, OpenRouter, Azure, ...)"),
    ], default="openai", string="Provider")
    ai_api_key = fields.Char(string="API Key", groups="zivoit_shopify_connector.group_manager", copy=False)
    ai_model = fields.Char(string="Model", help="Empty: the default model of the provider.")
    ai_base_url = fields.Char(string="Base URL")
    ai_privacy_shield = fields.Boolean(
        string="Privacy Shield", default=True,
        help="Names, emails, phones and addresses are never sent to the LLM.",
    )
    ai_auto_match_threshold = fields.Float(string="Auto-link Confidence", default=0.97)
    risk_hold_threshold = fields.Integer(string="Hold Orders at Risk Score", default=70)

    # Relations
    location_ids = fields.One2many("zivo.shopify.location", "instance_id", string="Locations")
    webhook_ids = fields.One2many("zivo.shopify.webhook", "instance_id", string="Webhooks")
    workflow_ids = fields.One2many("zivo.shopify.workflow", "instance_id", string="Workflows")
    market_ids = fields.One2many("zivo.shopify.market", "instance_id", string="Markets")
    routing_rule_ids = fields.One2many("zivo.shopify.routing.rule", "instance_id", string="Routing Rules")
    language_ids = fields.One2many("zivo.shopify.language", "instance_id", string="Languages")
    collection_ids = fields.One2many("zivo.shopify.collection", "instance_id", string="Collections")
    metafield_mapping_ids = fields.One2many("zivo.shopify.metafield.mapping", "instance_id", string="Metafields")

    product_count = fields.Integer(compute="_compute_counts")
    order_count = fields.Integer(compute="_compute_counts")
    failed_job_count = fields.Integer(compute="_compute_counts")
    pending_job_count = fields.Integer(compute="_compute_counts")

    _shop_unique = models.Constraint("UNIQUE(shop_domain)", "This Shopify store is already configured.")

    def _default_product(self, xmlid):
        return self.env.ref(f"zivoit_shopify_connector.{xmlid}", raise_if_not_found=False)

    @api.depends("webhook_token")
    def _compute_webhook_url(self):
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        for instance in self.sudo():
            instance.webhook_url = f"{base}/zivoit/shopify/webhook/{instance.webhook_token}"

    def _compute_counts(self):
        def count(model, field):
            return dict(self.env[model]._read_group([(field, "in", self.ids)], [field], ["__count"]))
        products = count("zivo.shopify.product", "instance_id")
        orders = count("sale.order", "zivo_instance_id")
        jobs = {
            (instance, state): total
            for instance, state, total in self.env["zivo.shopify.job"]._read_group(
                [("instance_id", "in", self.ids), ("state", "in", ("failed", "pending"))],
                ["instance_id", "state"], ["__count"])
        }
        for instance in self:
            instance.product_count = products.get(instance, 0)
            instance.order_count = orders.get(instance, 0)
            instance.failed_job_count = jobs.get((instance, "failed"), 0)
            instance.pending_job_count = jobs.get((instance, "pending"), 0)

    @api.model
    def _normalize_domain(self, value):
        domain = re.sub(r"^https?://", "", (value or "").strip().lower()).split("/")[0]
        if domain and "." not in domain:
            domain = f"{domain}.myshopify.com"
        return domain

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("shop_domain"):
                vals["shop_domain"] = self._normalize_domain(vals["shop_domain"])
        instances = super().create(vals_list)
        instances._ensure_default_workflow()
        return instances

    def write(self, vals):
        if vals.get("shop_domain"):
            vals["shop_domain"] = self._normalize_domain(vals["shop_domain"])
        if {"client_id", "client_secret", "shop_domain"} & set(vals):
            vals.setdefault("token_expires_at", False)
        return super().write(vals)

    def _ensure_default_workflow(self):
        for instance in self:
            if not instance.workflow_ids:
                self.env["zivo.shopify.workflow"].create({"instance_id": instance.id, "name": "Default"})

    # ------------------------------------------------------------------
    # API access
    # ------------------------------------------------------------------
    def _get_access_token(self, force=False):
        self.ensure_one()
        store = self.sudo()
        if store.auth_mode == "admin_token":
            if not store.access_token:
                raise UserError(self.env._("Enter the Admin API token of store %s.", self.name))
            return store.access_token
        now = fields.Datetime.now()
        if not force and store.access_token and store.token_expires_at and store.token_expires_at > now + timedelta(minutes=5):
            return store.access_token
        if not store.client_id or not store.client_secret:
            raise UserError(self.env._("Enter the Client ID and Client Secret of store %s.", self.name))
        token, expires_in = client_lib.request_client_credentials_token(
            store.shop_domain, store.client_id, store.client_secret,
        )
        store.write({"access_token": token, "token_expires_at": now + timedelta(seconds=expires_in)})
        return token

    def _client(self):
        self.ensure_one()
        if not DOMAIN_RE.match(self.shop_domain or "") and not self.env.context.get("zivo_shopify_allow_any_host"):
            raise UserError(self.env._("%s is not a valid myshopify.com domain.", self.shop_domain))

        def provider(force=False):
            return self._get_access_token(force=force)

        def on_cost(status):
            # Written with SQL so a query-budget gauge never conflicts with the job transaction.
            self.env.cr.execute(
                "UPDATE zivo_shopify_instance SET api_available=%s, api_maximum=%s, api_restore_rate=%s WHERE id=%s",
                (status.get("currentlyAvailable"), status.get("maximumAvailable"), status.get("restoreRate"), self.id),
            )
        return ShopifyClient(self.shop_domain, provider, self.api_version, on_cost=on_cost)

    # ------------------------------------------------------------------
    # Connection and setup
    # ------------------------------------------------------------------
    def action_test_connection(self):
        self.ensure_one()
        try:
            client = self._client()
            shop = client.execute(Q.SHOP)["shop"]
        except (ShopifyError, UserError) as error:
            self.state = "error"
            raise UserError(self.env._("Shopify connection failed: %s", error)) from error
        currency = self.env["res.currency"].with_context(active_test=False).search(
            [("name", "=", shop["currencyCode"])], limit=1)
        if currency and not currency.active:
            currency.active = True
        self.write({
            "state": "connected",
            "shop_name": shop["name"],
            "shop_email": shop.get("email"),
            "shop_currency_id": currency.id,
            "shop_timezone": shop.get("ianaTimezone"),
            "shop_plan": (shop.get("plan") or {}).get("publicDisplayName"),
            "shop_taxes_included": shop.get("taxesIncluded"),
            "shop_url": (shop.get("primaryDomain") or {}).get("url"),
            "last_connected_at": fields.Datetime.now(),
        })
        self._sync_locations(client)
        self._sync_publications(client)
        self._sync_languages(client)
        self._ensure_defaults()
        return self._notify(self.env._("Connected to %s", shop["name"]), "success")

    def _ensure_defaults(self):
        self.ensure_one()
        vals = {}
        company = self.company_id
        if not self.warehouse_id:
            vals["warehouse_id"] = self.env["stock.warehouse"].search([("company_id", "=", company.id)], limit=1).id
        if not self.pricelist_id:
            currency = self.shop_currency_id or company.currency_id
            vals["pricelist_id"] = self.env["sale.order"]._zivo_get_pricelist(self, currency).id
        if vals:
            self.write(vals)
        self._ensure_webhook_rows()

    def _sync_locations(self, client=None):
        self.ensure_one()
        client = client or self._client()
        Location = self.env["zivo.shopify.location"]
        for node in client.iter_connection(Q.LOCATIONS, {}, ["locations"]):
            location = Location.search([("instance_id", "=", self.id), ("shopify_gid", "=", node["id"])], limit=1)
            address = node.get("address") or {}
            vals = {
                "name": node["name"],
                "city": address.get("city"),
                "zip": address.get("zip"),
                "country_code": address.get("countryCode"),
                "active_on_shopify": node.get("isActive", True),
                "fulfills_online": node.get("fulfillsOnlineOrders", True),
                "is_fulfillment_service": node.get("isFulfillmentService", False),
            }
            if location:
                location.write(vals)
            else:
                vals.update(instance_id=self.id, shopify_gid=node["id"])
                if self.warehouse_id and not Location.search_count([("instance_id", "=", self.id)]):
                    vals["warehouse_ids"] = [fields.Command.set(self.warehouse_id.ids)]
                Location.create(vals)

    def _sync_publications(self, client=None):
        self.ensure_one()
        client = client or self._client()
        try:
            self.publication_data = [
                {"id": node["id"], "name": (node.get("catalog") or {}).get("title") or node["id"]}
                for node in client.iter_connection(Q.PUBLICATIONS, {}, ["publications"])
            ]
        except ShopifyError as error:
            _logger.info("Shopify publications unavailable: %s", error)

    def _ensure_webhook_rows(self):
        Webhook = self.env["zivo.shopify.webhook"]
        for instance in self:
            known = set(instance.webhook_ids.mapped("topic"))
            for topic, _label in Webhook._fields["topic"].selection:
                if topic not in known:
                    Webhook.create({"instance_id": instance.id, "topic": topic})

    def action_register_webhooks(self):
        self.ensure_one()
        self.webhook_ids._sync_remote()
        return self._notify(self.env._("Webhook subscriptions are synchronised."), "success")

    def _notify(self, message, kind="info"):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"message": message, "type": kind, "sticky": False,
                       "next": {"type": "ir.actions.client", "tag": "soft_reload"}},
        }

    def _signing_secret(self):
        self.ensure_one()
        store = self.sudo()
        return (store.client_secret if store.auth_mode == "client_credentials" else store.webhook_secret) or ""

    def _verify_webhook(self, raw_body, header_hmac):
        import base64
        secret = self._signing_secret()
        if not secret or not header_hmac:
            return False
        digest = base64.b64encode(hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()).decode()
        return hmac.compare_digest(digest, header_hmac)

    # ------------------------------------------------------------------
    # Manual launchers
    # ------------------------------------------------------------------
    def _enqueue(self, job_type, name, payload=None, **kwargs):
        self.ensure_one()
        # action_* launchers are RPC callable and the queue itself runs with sudo.
        self.check_access("read")
        return self.env["zivo.shopify.job"]._enqueue(self, job_type, name, payload, **kwargs)

    def _action_jobs(self, domain=None):
        action = self.env["ir.actions.act_window"]._for_xml_id("zivoit_shopify_connector.action_jobs")
        action["domain"] = [("instance_id", "in", self.ids)] + (domain or [])
        return action

    def action_open_jobs(self):
        return self._action_jobs()

    def action_open_failed_jobs(self):
        return self._action_jobs([("state", "=", "failed")])

    def action_open_products(self):
        action = self.env["ir.actions.act_window"]._for_xml_id("zivoit_shopify_connector.action_products")
        action["domain"] = [("instance_id", "in", self.ids)]
        return action

    def action_open_orders(self):
        action = self.env["ir.actions.act_window"]._for_xml_id("zivoit_shopify_connector.action_orders")
        action["domain"] = [("zivo_instance_id", "in", self.ids)]
        return action

    def _shopify_since_query(self, last, field="updated_at"):
        if not last:
            return None
        return f"{field}:>='{(last - timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%SZ')}'"

    def _orders_query(self, since=None):
        self.ensure_one()
        parts = []
        if since:
            parts.append(self._shopify_since_query(since))
        if self.import_orders_after:
            parts.append(f"created_at:>='{self.import_orders_after.strftime('%Y-%m-%dT%H:%M:%SZ')}'")
        return " AND ".join(parts) or None

    # ------------------------------------------------------------------
    # Schedulers
    # ------------------------------------------------------------------
    @api.model
    def _connected(self):
        return self.search([("state", "=", "connected")])

    @api.model
    def _cron_import_orders(self):
        for instance in self._connected().filtered("auto_import_orders"):
            instance._enqueue("import_orders", "Scheduled order import",
                              {"query": instance._orders_query(instance.last_order_sync), "stamp": True},
                              source="cron", dedupe_key="import_orders")

    @api.model
    def _cron_import_products(self):
        for instance in self._connected().filtered("auto_import_products"):
            instance._enqueue("import_products", "Scheduled product import",
                              {"query": instance._shopify_since_query(instance.last_product_sync), "stamp": True},
                              source="cron", dedupe_key="import_products")

    @api.model
    def _cron_import_checkouts(self):
        for instance in self._connected().filtered("auto_import_checkouts"):
            instance._enqueue("import_checkouts", "Scheduled checkout import", {}, source="cron", dedupe_key="import_checkouts")

    @api.model
    def _cron_import_payouts(self):
        for instance in self._connected().filtered("auto_import_payouts"):
            instance._enqueue("import_payouts", "Scheduled payout import", {}, source="cron", dedupe_key="import_payouts")

    # ------------------------------------------------------------------
    # AI gateway
    # ------------------------------------------------------------------
    def _ai_available(self):
        self.ensure_one()
        store = self.sudo()
        if not store.ai_enabled:
            return False
        return bool(store.ai_api_key or store.ai_provider == "compatible")

    def _ai_complete(self, feature, system, prompt, max_tokens=900):
        """Call the LLM for this store and keep an auditable usage record."""
        self.ensure_one()
        store = self.sudo()
        if not store._ai_available():
            raise ai_lib.AIError("No LLM is configured for this store.")
        log = self.env["zivo.shopify.ai.log"].sudo()
        try:
            text, usage = ai_lib.complete(
                store.ai_provider, store.ai_api_key, store.ai_model, system, prompt,
                base_url=store.ai_base_url, max_tokens=max_tokens,
            )
        except ai_lib.AIError as error:
            log.create({"instance_id": store.id, "feature": feature, "state": "error", "message": str(error)[:500]})
            raise
        log.create({
            "instance_id": store.id,
            "feature": feature,
            "state": "ok",
            "model": store.ai_model or ai_lib.DEFAULT_MODELS.get(store.ai_provider),
            "input_tokens": usage.get("input", 0),
            "output_tokens": usage.get("output", 0),
        })
        return text
