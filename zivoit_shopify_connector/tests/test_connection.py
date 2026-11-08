import base64
import hashlib
import hmac
import json

from odoo.exceptions import UserError
from odoo.tests import HttpCase, tagged
from odoo.tools import mute_logger

from ..lib.queries import ALL_DOCUMENTS
from .common import ZivoShopifyCommon
from .shopify_mock import SCHEMA, estimate_cost, make_order

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


@tagged("post_install", "-at_install")
class TestConnection(ZivoShopifyCommon):

    def test_connect_loads_shop_and_locations(self):
        self.assertEqual(self.instance.state, "connected")
        self.assertEqual(self.instance.shop_name, "Acme Store")
        self.assertEqual(self.instance.shop_currency_id.name, "USD")
        self.assertEqual(self.location.name, "Main Hub")
        self.assertEqual(self.location.warehouse_ids, self.warehouse, "first location maps the default warehouse")
        self.assertEqual(self.instance.publication_data[0]["name"], "Online Store")

    def test_token_cached_then_renewed(self):
        self.assertEqual(self.shop.token_requests, 1)
        self.instance._client().execute("query ZivoShop { shop { id } }")
        self.assertEqual(self.shop.token_requests, 1, "valid token is reused")
        self.instance.token_expires_at = "2020-01-01 00:00:00"
        self.instance._client().execute("query ZivoShop { shop { id } }")
        self.assertEqual(self.shop.token_requests, 2)

    def test_domain_is_normalised(self):
        store = self.env["zivo.shopify.instance"].create({"name": "B", "shop_domain": "https://Other-Store.myshopify.com/admin"})
        self.assertEqual(store.shop_domain, "other-store.myshopify.com")
        short = self.env["zivo.shopify.instance"].create({"name": "C", "shop_domain": "shorty"})
        self.assertEqual(short.shop_domain, "shorty.myshopify.com")

    def test_foreign_domain_is_refused(self):
        store = self.env["zivo.shopify.instance"].create({"name": "B", "shop_domain": "evil.example.com"})
        with self.assertRaises(UserError):
            store._client()

    def test_webhook_registration_respects_toggles(self):
        self.instance.action_register_webhooks()
        registered = {hook["topic"] for hook in self.shop.webhooks}
        self.assertIn("ORDERS_CREATE", registered)
        self.assertNotIn("CHECKOUTS_CREATE", registered, "off by default")
        row = self.instance.webhook_ids.filtered(lambda w: w.topic == "ORDERS_CREATE")
        self.assertTrue(row.remote_gid)
        row.enabled = False
        self.instance.action_register_webhooks()
        self.assertNotIn("ORDERS_CREATE", {hook["topic"] for hook in self.shop.webhooks})
        self.assertFalse(row.remote_gid)
        self.assertTrue(row.exists(), "toggling keeps the configuration")

    def test_webhook_registration_is_idempotent(self):
        self.instance.action_register_webhooks()
        count = len(self.shop.webhooks)
        self.instance.action_register_webhooks()
        self.assertEqual(len(self.shop.webhooks), count)

    def test_setup_wizard(self):
        wizard = self.env["zivo.shopify.setup.wizard"].create({
            "name": "New", "shop_domain": "new-shop", "client_id": "a", "client_secret": "b",
        })
        wizard.action_connect()
        self.assertEqual(wizard.step, "automation")
        wizard.action_finish()
        self.assertEqual(wizard.step, "done")
        self.assertTrue(wizard.instance_id.clearing_journal_id)
        self.assertTrue(wizard.instance_id.fee_account_id)

    def test_every_document_matches_official_schema(self):
        if SCHEMA is None:
            self.skipTest("Shopify schema not provided")
        from graphql import parse, validate
        for name, document in ALL_DOCUMENTS.items():
            self.assertEqual(validate(SCHEMA, parse(document)), [], name)

    def test_every_document_fits_the_cost_limit(self):
        """Shopify refuses a single query whose requested cost is over 1000 points."""
        if SCHEMA is None:
            self.skipTest("Shopify schema not provided")
        over = {name: cost for name, document in ALL_DOCUMENTS.items() if (cost := estimate_cost(SCHEMA, document)) > 1000}
        self.assertEqual(over, {})

    def test_large_order_query_is_not_one_document(self):
        self.stock_product()
        node = make_order()
        self.shop.orders[node["id"]] = node
        self.shop.calls.clear()
        self.instance._fetch_order(node["id"])
        self.assertEqual([call[0] for call in self.shop.calls], ["ZivoOrderOne", "ZivoOrderLines"], "no extra calls for a simple order")


@tagged("post_install", "-at_install")
class TestWebhookEndpoint(HttpCase):

    def setUp(self):
        super().setUp()
        self.store = self.env["zivo.shopify.instance"].create({
            "name": "Hooks", "shop_domain": "hooks.myshopify.com", "client_id": "c", "client_secret": "topsecret", "state": "connected",
        })
        self.store._ensure_webhook_rows()
        self.url = f"/zivoit/shopify/webhook/{self.store.webhook_token}"

    def send(self, body, topic="orders/create", event="evt-1", secret="topsecret", url=None):
        raw = json.dumps(body).encode()
        signature = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
        return self.url_open(url or self.url, data=raw, headers={
            "X-Shopify-Hmac-Sha256": signature, "X-Shopify-Topic": topic, "X-Shopify-Event-Id": event, "Content-Type": "application/json",
        })

    def open_jobs(self):
        return self.env["zivo.shopify.job"].search([("instance_id", "=", self.store.id)])

    def test_valid_webhook_creates_job(self):
        response = self.send({"admin_graphql_api_id": "gid://shopify/Order/77"})
        self.assertEqual(response.status_code, 200)
        job = self.open_jobs()
        self.assertEqual(job.job_type, "import_order")
        self.assertEqual(job.payload["gid"], "gid://shopify/Order/77")
        self.assertEqual(job.source, "webhook")

    @mute_logger("odoo.addons.zivoit_shopify_connector.controllers.main")
    def test_bad_signature_rejected(self):
        response = self.send({"admin_graphql_api_id": "gid://shopify/Order/77"}, secret="wrong")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.open_jobs())

    def test_unknown_endpoint(self):
        response = self.send({}, url="/zivoit/shopify/webhook/" + "0" * 32)
        self.assertEqual(response.status_code, 404)

    def test_redelivery_is_ignored(self):
        self.send({"admin_graphql_api_id": "gid://shopify/Order/77"})
        self.open_jobs().state = "done"
        response = self.send({"admin_graphql_api_id": "gid://shopify/Order/77"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.open_jobs()), 1)

    def test_burst_of_events_collapses_to_one_job(self):
        for index in range(4):
            self.send({"admin_graphql_api_id": "gid://shopify/Order/77"}, event=f"e{index}", topic="orders/updated")
        self.assertEqual(len(self.open_jobs()), 1)

    def test_refund_webhook_targets_order(self):
        self.send({"order_id": 55, "id": 9}, topic="refunds/create", event="r1")
        self.assertEqual(self.open_jobs().payload["gid"], "gid://shopify/Order/55")

    def test_uninstall(self):
        self.send({}, topic="app/uninstalled", event="u1")
        self.assertEqual(self.store.state, "uninstalled")

    def test_disabled_topic_counts_but_does_nothing(self):
        row = self.store.webhook_ids.filtered(lambda w: w.topic == "ORDERS_CREATE")
        row.enabled = False
        self.send({"admin_graphql_api_id": "gid://shopify/Order/77"})
        self.assertFalse(self.open_jobs())
        self.assertEqual(row.received_count, 1)

    def test_signed_image_url(self):
        template = self.env["product.template"].create({"name": "Img", "image_1920": PNG})
        signature = self.env["zivo.shopify.product"]._image_signature(template.id)
        self.assertEqual(self.url_open(f"/zivoit/shopify/image/{template.id}/{signature}").status_code, 200)
        self.assertEqual(self.url_open(f"/zivoit/shopify/image/{template.id}/{'0' * 64}").status_code, 404)
