import json
import logging

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ..lib import queries as Q

_logger = logging.getLogger(__name__)

TOPICS = [
    ("ORDERS_CREATE", "Order Created"),
    ("ORDERS_UPDATED", "Order Updated"),
    ("ORDERS_PAID", "Order Paid"),
    ("ORDERS_CANCELLED", "Order Cancelled"),
    ("ORDERS_EDITED", "Order Edited"),
    ("ORDERS_RISK_ASSESSMENT_CHANGED", "Order Risk Changed"),
    ("FULFILLMENTS_CREATE", "Fulfilment Created"),
    ("FULFILLMENTS_UPDATE", "Fulfilment Updated"),
    ("REFUNDS_CREATE", "Refund Created"),
    ("RETURNS_REQUEST", "Return Requested"),
    ("RETURNS_APPROVE", "Return Approved"),
    ("RETURNS_CLOSE", "Return Closed"),
    ("PRODUCTS_CREATE", "Product Created"),
    ("PRODUCTS_UPDATE", "Product Updated"),
    ("PRODUCTS_DELETE", "Product Deleted"),
    ("COLLECTIONS_CREATE", "Collection Created"),
    ("COLLECTIONS_UPDATE", "Collection Updated"),
    ("COLLECTIONS_DELETE", "Collection Deleted"),
    ("LOCALES_CREATE", "Language Added"),
    ("LOCALES_UPDATE", "Language Updated"),
    ("CUSTOMERS_CREATE", "Customer Created"),
    ("CUSTOMERS_UPDATE", "Customer Updated"),
    ("INVENTORY_LEVELS_UPDATE", "Inventory Level Updated"),
    ("CHECKOUTS_CREATE", "Checkout Created"),
    ("CHECKOUTS_UPDATE", "Checkout Updated"),
    ("LOCATIONS_CREATE", "Location Created"),
    ("LOCATIONS_UPDATE", "Location Updated"),
    ("APP_UNINSTALLED", "App Uninstalled"),
]
DEFAULT_OFF = {"ORDERS_RISK_ASSESSMENT_CHANGED", "CHECKOUTS_CREATE", "CHECKOUTS_UPDATE", "CUSTOMERS_CREATE", "CUSTOMERS_UPDATE"}


class ZivoShopifyWebhook(models.Model):
    _name = "zivo.shopify.webhook"
    _description = "Shopify Webhook Subscription"
    _order = "topic"

    instance_id = fields.Many2one("zivo.shopify.instance", string="Store", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    topic = fields.Selection(TOPICS, required=True)
    enabled = fields.Boolean(default=True)
    remote_gid = fields.Char(string="Shopify ID", readonly=True, copy=False)
    received_count = fields.Integer(readonly=True, default=0)
    last_received_at = fields.Datetime(readonly=True)

    _topic_unique = models.Constraint("UNIQUE(instance_id, topic)", "This topic already exists on the store.")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("topic") in DEFAULT_OFF:
                vals.setdefault("enabled", False)
        return super().create(vals_list)

    def _sync_remote(self):
        """Reconcile Shopify subscriptions with the toggles. Disabling keeps the row and its counters."""
        for instance in self.instance_id:
            client = instance._client()
            remote = {
                node["topic"]: node
                for node in client.iter_connection(Q.WEBHOOKS, {}, ["webhookSubscriptions"])
                if node.get("uri") == instance.webhook_url
            }
            for webhook in self.filtered(lambda w: w.instance_id == instance):
                existing = remote.get(webhook.topic)
                if webhook.enabled and not existing:
                    created = client.mutate(Q.WEBHOOK_CREATE, {
                        "topic": webhook.topic,
                        "sub": {"uri": instance.webhook_url, "format": "JSON"},
                    }, "webhookSubscriptionCreate")
                    webhook.remote_gid = created["webhookSubscription"]["id"]
                elif webhook.enabled:
                    webhook.remote_gid = existing["id"]
                elif existing:
                    client.mutate(Q.WEBHOOK_DELETE, {"id": existing["id"]}, "webhookSubscriptionDelete")
                    webhook.remote_gid = False

    def action_toggle(self):
        for webhook in self:
            webhook.enabled = not webhook.enabled
        self._sync_remote()
        return True

    @api.model
    def _dispatch(self, instance, topic, body, event_id=None):
        """Turn a verified webhook into queue jobs. Payloads are only used to find the record:
        the job re-reads Shopify, so late or out-of-order deliveries cannot apply stale data."""
        Webhook = self.sudo()
        row = Webhook.search([("instance_id", "=", instance.id), ("topic", "=", topic)], limit=1)
        if row:
            row.write({"received_count": row.received_count + 1, "last_received_at": fields.Datetime.now()})
            if not row.enabled:
                return False
        instance = instance.sudo()
        gid = body.get("admin_graphql_api_id")

        if topic == "APP_UNINSTALLED":
            instance.state = "uninstalled"
            return True
        if topic.startswith("ORDERS_"):
            order_gid = gid or (body.get("order_id") and f"gid://shopify/Order/{body['order_id']}")
            return self._queue_order(instance, order_gid, topic)
        if topic.startswith(("FULFILLMENTS_", "REFUNDS_")):
            return self._queue_order(instance, f"gid://shopify/Order/{body['order_id']}", topic)
        if topic.startswith("RETURNS_"):
            order = body.get("order") or {}
            order_gid = order.get("admin_graphql_api_id") or (order.get("id") and f"gid://shopify/Order/{order['id']}")
            return self._queue_order(instance, order_gid, topic)
        if topic in ("PRODUCTS_CREATE", "PRODUCTS_UPDATE"):
            instance._enqueue("import_product", f"Webhook {topic}: {body.get('title') or gid}", {"gid": gid},
                              dedupe_key=f"product:{gid}", source="webhook", external_ref=gid, priority=7)
            return True
        if topic == "PRODUCTS_DELETE":
            product_gid = f"gid://shopify/Product/{body['id']}"
            instance._enqueue("product_deleted", f"Product deleted: {product_gid}", {"gid": product_gid},
                              source="webhook", external_ref=product_gid)
            return True
        if topic in ("CUSTOMERS_CREATE", "CUSTOMERS_UPDATE"):
            instance._enqueue("import_customer", f"Webhook customer {gid}", {"gid": gid},
                              dedupe_key=f"customer:{gid}", source="webhook", external_ref=gid)
            return True
        if topic == "INVENTORY_LEVELS_UPDATE":
            self.env["zivo.shopify.variant.level"].sudo()._apply_webhook(instance, body)
            return True
        if topic.startswith("CHECKOUTS_"):
            instance._enqueue("import_checkouts", "Webhook checkout refresh", {},
                              dedupe_key="import_checkouts", source="webhook", delay=300)
            return True
        if topic in ("COLLECTIONS_CREATE", "COLLECTIONS_UPDATE"):
            instance._enqueue("import_collections", "Webhook collection refresh", {},
                              dedupe_key="import_collections", source="webhook", delay=60)
            return True
        if topic == "COLLECTIONS_DELETE":
            self.env["zivo.shopify.collection"].sudo().search([
                ("instance_id", "=", instance.id), ("shopify_gid", "=", f"gid://shopify/Collection/{body['id']}"),
            ]).unlink()
            return True
        if topic.startswith("LOCALES_"):
            instance._sync_languages()
            return True
        if topic.startswith("LOCATIONS_"):
            instance._sync_locations()
            return True
        return False

    @api.model
    def _queue_order(self, instance, gid, topic):
        if not gid:
            return False
        instance._enqueue("import_order", f"Webhook {topic}: {gid.rsplit('/', 1)[-1]}", {"gid": gid},
                          dedupe_key=f"order:{gid}", source="webhook", external_ref=gid, priority=5)
        return True


class ZivoShopifyWebhookReceipt(models.Model):
    _name = "zivo.shopify.webhook.receipt"
    _description = "Processed Shopify Webhook Delivery"

    instance_id = fields.Many2one("zivo.shopify.instance", string="Store", required=True, ondelete="cascade", index=True)
    event_id = fields.Char(required=True)
    topic = fields.Char()

    _event_unique = models.Constraint("UNIQUE(instance_id, event_id)", "Delivery already processed.")


class ZivoShopifyWorkflow(models.Model):
    _name = "zivo.shopify.workflow"
    _description = "Shopify Order Workflow"
    _order = "sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    instance_id = fields.Many2one("zivo.shopify.instance", string="Store", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    gateways = fields.Char(help="Comma separated Shopify gateway names, for example shopify_payments, manual. Empty: any.")
    required_status = fields.Selection([
        ("any", "Any Financial Status"),
        ("paid", "Paid or Partially Paid"),
    ], default="any", required=True, string="Run When")
    confirm_order = fields.Boolean(default=True)
    create_invoice = fields.Boolean()
    post_invoice = fields.Boolean(string="Validate Invoice")
    register_payment = fields.Boolean()
    payment_journal_id = fields.Many2one("account.journal", string="Payment Journal", check_company=True,
                                         domain="[('type', 'in', ('bank', 'cash'))]")
    validate_delivery = fields.Boolean(string="Auto-validate Delivery", help="For digital goods or dropshipping.")

    @api.constrains("register_payment", "payment_journal_id")
    def _check_journal(self):
        for workflow in self:
            if workflow.register_payment and not workflow.payment_journal_id:
                raise ValidationError(self.env._("Choose a payment journal to register payments."))

    def _matches(self, node):
        self.ensure_one()
        wanted = {g.strip().lower() for g in (self.gateways or "").split(",") if g.strip()}
        names = {g.lower() for g in node.get("paymentGatewayNames") or []}
        if wanted and not wanted & names:
            return False
        if self.required_status == "paid":
            return node.get("displayFinancialStatus") in ("PAID", "PARTIALLY_PAID", "PARTIALLY_REFUNDED")
        return True


class ZivoShopifyMarket(models.Model):
    _name = "zivo.shopify.market"
    _description = "Shopify Market"
    _order = "name"

    name = fields.Char(required=True)
    instance_id = fields.Many2one("zivo.shopify.instance", string="Store", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    shopify_gid = fields.Char(required=True, index=True, readonly=True)
    handle = fields.Char(readonly=True)
    status = fields.Char(readonly=True)
    market_type = fields.Char(readonly=True)
    country_codes = fields.Char(readonly=True, help="ISO country codes covered by this market.")
    price_list_gid = fields.Char(readonly=True)
    price_list_currency = fields.Char(readonly=True)
    pricelist_id = fields.Many2one("product.pricelist", check_company=True, help="Prices exported to this market.")
    warehouse_id = fields.Many2one("stock.warehouse", check_company=True, help="Warehouse for orders of this market.")

    _gid_unique = models.Constraint("UNIQUE(instance_id, shopify_gid)", "Market already imported.")

    def action_export_prices(self):
        for market in self:
            market.instance_id._enqueue("export_market_prices", f"Market prices: {market.name}", {"market": market.id})
        return True


class ZivoShopifyRoutingRule(models.Model):
    _name = "zivo.shopify.routing.rule"
    _description = "Shopify Smart Warehouse Routing Rule"
    _order = "sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    instance_id = fields.Many2one("zivo.shopify.instance", string="Store", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    country_ids = fields.Many2many("res.country", string="Countries")
    state_ids = fields.Many2many("res.country.state", string="States")
    zip_from = fields.Char(string="ZIP From")
    zip_to = fields.Char(string="ZIP To")
    zip_prefixes = fields.Char(help="Comma separated prefixes, for example 500, 501.")
    warehouse_id = fields.Many2one("stock.warehouse", required=True, check_company=True)

    def _matches(self, country_code, state_code, zip_code):
        self.ensure_one()
        if self.country_ids and country_code not in self.country_ids.mapped("code"):
            return False
        if self.state_ids and state_code not in self.state_ids.mapped("code"):
            return False
        zip_code = (zip_code or "").strip().upper()
        prefixes = [p.strip().upper() for p in (self.zip_prefixes or "").split(",") if p.strip()]
        if prefixes and not any(zip_code.startswith(prefix) for prefix in prefixes):
            return False
        if self.zip_from and not self._zip_gte(zip_code, self.zip_from):
            return False
        if self.zip_to and not self._zip_gte(self.zip_to, zip_code):
            return False
        return True

    @staticmethod
    def _zip_gte(left, right):
        left, right = left.strip().upper(), right.strip().upper()
        if left.isdigit() and right.isdigit():
            return int(left) >= int(right)
        return left >= right


METAFIELD_MODELS = {
    "product": "product.template",
    "variant": "product.product",
    "customer": "res.partner",
    "order": "sale.order",
}
METAFIELD_FIELD_TYPES = ("char", "text", "html", "integer", "float", "boolean", "date", "datetime")


class ZivoShopifyMetafieldMapping(models.Model):
    _name = "zivo.shopify.metafield.mapping"
    _description = "Shopify Metafield Mapping"
    _order = "owner_type, namespace, key"

    instance_id = fields.Many2one("zivo.shopify.instance", string="Store", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    active = fields.Boolean(default=True)
    owner_type = fields.Selection([
        ("product", "Product"), ("variant", "Variant"), ("customer", "Customer"), ("order", "Order"),
    ], required=True, default="product")
    namespace = fields.Char(required=True, default="custom")
    key = fields.Char(required=True)
    value_type = fields.Char(required=True, default="single_line_text_field")
    field_id = fields.Many2one("ir.model.fields", required=True, ondelete="cascade",
                               domain=[("ttype", "in", METAFIELD_FIELD_TYPES), ("readonly", "=", False), ("store", "=", True)])
    direction = fields.Selection([
        ("both", "Both Directions"), ("import", "Shopify to Odoo"), ("export", "Odoo to Shopify"),
    ], default="both", required=True)

    @api.constrains("owner_type", "field_id")
    def _check_model(self):
        for mapping in self:
            if mapping.field_id.model != METAFIELD_MODELS[mapping.owner_type]:
                raise ValidationError(self.env._(
                    "Field %(field)s does not belong to %(model)s.",
                    field=mapping.field_id.name, model=METAFIELD_MODELS[mapping.owner_type],
                ))

    @api.model
    def _apply_import(self, instance, owner_type, record, nodes):
        """Write mapped Shopify metafields on an Odoo record."""
        mappings = instance.metafield_mapping_ids.filtered(
            lambda m: m.active and m.owner_type == owner_type and m.direction in ("both", "import"))
        if not mappings or not record:
            return
        values = {(n["namespace"], n["key"]): n.get("value") for n in nodes or []}
        vals = {}
        for mapping in mappings:
            key = (mapping.namespace, mapping.key)
            if key in values:
                vals[mapping.field_id.name] = mapping._convert(values[key])
        if vals:
            record.write(vals)

    def _convert(self, value):
        self.ensure_one()
        kind = self.field_id.ttype
        if value in (None, ""):
            return False
        if kind == "integer":
            return int(float(value))
        if kind == "float":
            return float(json.loads(value)["amount"] if str(value).startswith("{") else value)
        if kind == "boolean":
            return str(value).lower() == "true"
        return value

    @api.model
    def _build_export(self, instance, owner_type, record):
        mappings = instance.metafield_mapping_ids.filtered(
            lambda m: m.active and m.owner_type == owner_type and m.direction in ("both", "export"))
        result = []
        for mapping in mappings:
            value = record[mapping.field_id.name]
            if value is False or value is None or value == "":
                continue
            result.append({
                "namespace": mapping.namespace,
                "key": mapping.key,
                "type": mapping.value_type,
                "value": str(value).lower() if isinstance(value, bool) else str(value),
            })
        return result
