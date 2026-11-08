import json
from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError
from odoo.fields import Command
from odoo.tests import tagged, new_test_user
from odoo.tools import mute_logger

from ..lib import ai as ai_lib
from ..lib.client import ShopifyTransientError
from .common import ZivoShopifyCommon
from .shopify_mock import make_line, make_order, make_product, make_variant


@tagged("post_install", "-at_install")
class TestLocalAi(ZivoShopifyCommon):

    def test_score_exact_identifiers(self):
        self.assertEqual(ai_lib.score_product_match("A1", None, "x", "A1", None, "y")[0], 1.0)
        self.assertEqual(ai_lib.score_product_match(None, "99", "x", None, "99", "y")[0], 1.0)

    def test_score_normalised_sku(self):
        self.assertEqual(ai_lib.score_product_match("TS-100/b", None, "x", "ts100b", None, "y")[0], 0.97)

    def test_unrelated_titles_score_low(self):
        self.assertLess(ai_lib.score_product_match(None, None, "Red running shoe", None, None, "Oak dining table")[0], 0.35)

    def test_matcher_suggests_and_accepts(self):
        self.instance.auto_create_products = False
        product = self.env["product.product"].create({"name": "Classic Cotton Tee Blue", "default_code": "OLD-77"})
        self.shop.products["p"] = make_product("500", "Classic Cotton Tee", [make_variant("501", "NEW-1")])
        self.instance.action_import_products()
        self.run_queue()
        match = self.env["zivo.shopify.match"].search([("product_id", "=", product.id)])
        self.assertTrue(match)
        self.assertLess(match.confidence, self.instance.ai_auto_match_threshold)
        self.assertFalse(match.variant_id.product_id, "not linked without review")
        match.action_accept()
        self.assertEqual(match.variant_id.product_id, product)
        self.assertEqual(match.variant_id.shopify_product_id.product_tmpl_id, product.product_tmpl_id)

    def test_high_confidence_auto_links(self):
        self.instance.auto_create_products = False
        product = self.env["product.product"].create({"name": "Zeta Lamp", "default_code": "ZL-100"})
        self.shop.products["p"] = make_product("510", "Zeta Lamp", [make_variant("511", "zl100")])
        self.instance.action_import_products()
        self.run_queue()
        variant = self.env["zivo.shopify.variant"].search([("shopify_gid", "=", "gid://shopify/ProductVariant/511")])
        self.assertEqual(variant.product_id, product)

    def order_risk(self, **overrides):
        self.stock_product(gid="11") if not self.env["zivo.shopify.variant"].search([("instance_id", "=", self.instance.id), ("shopify_gid", "=", "gid://shopify/ProductVariant/11")]) else None
        order, _ = self.import_order(make_order(**overrides))
        return order

    def test_low_risk_order(self):
        order = self.order_risk()
        self.assertEqual(order.zivo_risk_level, "low")
        self.assertFalse(order.zivo_risk_hold)
        self.assertEqual(order.state, "sale")

    def test_high_risk_order_is_held(self):
        risk = {"recommendation": "CANCEL", "assessments": [{"riskLevel": "HIGH", "facts": []}]}
        order = self.order_risk(risk=risk, email="x@mailinator.com")
        self.assertGreaterEqual(order.zivo_risk_score, 70)
        self.assertTrue(order.zivo_risk_hold)
        self.assertEqual(order.state, "draft", "held orders are not confirmed")
        self.assertIn("Shopify risk level is HIGH", order.zivo_risk_reasons)
        self.assertIn("Disposable", order.zivo_risk_reasons)
        self.assertTrue(order.activity_ids)

    def test_release_hold_resumes_workflow(self):
        risk = {"recommendation": "CANCEL", "assessments": [{"riskLevel": "HIGH", "facts": []}]}
        order = self.order_risk(risk=risk)
        order.action_zivo_release_hold()
        self.shop.orders[order.zivo_shopify_gid]["risk"] = {"recommendation": "ACCEPT", "assessments": []}
        self.run_queue()
        self.assertFalse(self.jobs("failed"), self.jobs("failed").mapped("error_message"))
        self.assertEqual(order.state, "sale")

    def test_score_is_capped_and_explainable(self):
        score, reasons = ai_lib.score_order_risk({"shopify_level": "HIGH", "shopify_recommendation": "CANCEL", "disposable_email": True, "address_mismatch": True, "country_mismatch": True})
        self.assertEqual(score, 100)
        self.assertEqual(len(reasons), 5)

    def test_forecast(self):
        product = self.stock_product(qty=10)
        variant = self.env["zivo.shopify.variant"].search([("product_id", "=", product.id)])
        order = self.env["sale.order"].create({
            "partner_id": self.partner_a.id, "zivo_instance_id": self.instance.id, "zivo_shopify_gid": "x-1",
            "date_order": fields.Datetime.now() - timedelta(days=1),
            "order_line": [Command.create({"product_id": product.id, "product_uom_qty": 8})],
        })
        order.with_context(mail_notrack=True).action_confirm()
        Forecast = self.env["zivo.shopify.forecast"]
        Forecast._recompute(self.instance)
        forecast = Forecast.search([("variant_id", "=", variant.id)])
        self.assertGreater(forecast.daily_velocity, 0)
        self.assertEqual(forecast.stock_qty, 2)
        self.assertIn(forecast.risk, ("critical", "warning", "ok"))
        self.assertGreaterEqual(forecast.suggested_reorder, 0)
        Forecast._recompute(self.instance)
        self.assertEqual(Forecast.search_count([("variant_id", "=", variant.id)]), 1)

    def test_ewma_favours_recent(self):
        Forecast = self.env["zivo.shopify.forecast"]
        self.assertGreater(Forecast._ewma([0, 0, 0, 10, 10]), Forecast._ewma([10, 10, 0, 0, 0]))

    def test_diagnose_rules(self):
        self.assertEqual(ai_lib.diagnose_error("Access denied for orders field")[0], "Missing Shopify access scope")
        self.assertTrue(ai_lib.diagnose_error("Throttled by Shopify")[2])
        self.assertEqual(ai_lib.diagnose_error("zzz")[0], "Unclassified error")

    def test_extract_json(self):
        self.assertEqual(ai_lib.extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(ai_lib.extract_json('Sure! {"a": 2} done'), {"a": 2})
        self.assertIsNone(ai_lib.extract_json("nope"))


@tagged("post_install", "-at_install")
class TestLlm(ZivoShopifyCommon):

    def enable(self, provider="openai"):
        self.instance.write({"ai_enabled": True, "ai_provider": provider, "ai_api_key": "k"})

    def test_providers(self):
        for provider in ("openai", "anthropic", "gemini"):
            self.enable(provider)
            self.assertEqual(self.instance._ai_complete("t", "s", "p"), self.shop.llm_answer)
            self.assertEqual(self.shop.llm_requests[-1][0], provider)
        logs = self.env["zivo.shopify.ai.log"].search([("instance_id", "=", self.instance.id)])
        self.assertEqual(len(logs), 3)
        self.assertEqual(sum(logs.mapped("input_tokens")), 33)

    def test_no_llm_without_key(self):
        self.assertFalse(self.instance._ai_available())
        self.instance.ai_enabled = True
        self.assertFalse(self.instance._ai_available())

    def test_job_doctor_llm(self):
        self.enable()
        self.shop.llm_answer = json.dumps({"root_cause": "Bad SKU", "fix": ["Step 1", "Step 2"]})
        job = self.instance._enqueue("import_order", "x", {"gid": "g"})
        job.write({"state": "failed", "error_message": "boom"})
        job.action_ai_diagnose()
        self.assertEqual(job.ai_root_cause, "Bad SKU")
        self.assertIn("Step 2", job.ai_fix)
        self.assertEqual(job.ai_source, "llm")

    def test_rerank_cannot_auto_link(self):
        self.enable()
        self.instance.auto_create_products = False
        product = self.env["product.product"].create({"name": "Classic Cotton Tee", "default_code": "OLD-77"})
        self.shop.llm_answer = json.dumps({"index": 0, "confidence": 1.0, "reason": "Same product"})
        self.shop.products["p"] = make_product("520", "Classic Cotton Tee Blue", [make_variant("521", "NEW-1")])
        self.instance.action_import_products()
        self.run_queue()
        match = self.env["zivo.shopify.match"].search([("product_id", "=", product.id)])
        self.assertEqual(match.engine, "llm")
        self.assertEqual(match.confidence, 0.96)
        self.assertFalse(match.variant_id.product_id, "still requires a human")

    def test_copilot_sends_aggregates_only(self):
        self.enable()
        self.stock_product()
        self.import_order(make_order())
        self.shop.llm_answer = "Two orders."
        result = self.env["zivo.shopify.dashboard"].ask_copilot("How many orders?", self.instance.id)
        self.assertEqual(result, {"answer": "Two orders.", "engine": "llm"})
        body = json.dumps(self.shop.llm_requests[-1][3])
        self.assertNotIn("asha@example.com", body)
        self.assertNotIn("Asha", body)

    def test_copilot_local_answer(self):
        self.stock_product()
        self.import_order(make_order())
        result = self.env["zivo.shopify.dashboard"].ask_copilot("What is revenue today?", self.instance.id)
        self.assertEqual(result["engine"], "local")
        self.assertIn("orders", result["answer"])

    def test_llm_failure_falls_back(self):
        self.enable()
        import requests
        from unittest.mock import patch
        with patch("requests.post", side_effect=requests.ConnectionError("down")):
            result = self.env["zivo.shopify.dashboard"].ask_copilot("failed jobs?", self.instance.id)
        self.assertEqual(result["engine"], "local")

    def content(self):
        product = self.stock_product()
        record = self.env["zivo.shopify.variant"].search([("product_id", "=", product.id)]).shopify_product_id
        return record, self.env["zivo.shopify.content.wizard"].create({"shopify_product_id": record.id})

    def test_content_local(self):
        record, wizard = self.content()
        wizard.action_generate()
        self.assertTrue(wizard.generated)
        self.assertIn("Local", wizard.engine)

    def test_content_llm_and_apply(self):
        self.enable()
        record, wizard = self.content()
        self.shop.llm_answer = json.dumps({"title": "Better Tee", "description_html": "<p>Soft</p>", "seo_title": "T" * 90, "seo_description": "D", "tags": ["a", "b"]})
        wizard.action_generate()
        self.assertEqual(len(wizard.seo_title), 70)
        wizard.action_apply()
        self.assertEqual(record.name, "Better Tee")
        self.assertEqual(record.tags, "a, b")
        self.assertTrue(self.jobs().filtered(lambda j: j.job_type == "export_product"), "push queued")


@tagged("post_install", "-at_install")
class TestQueue(ZivoShopifyCommon):

    def failing_job(self, error):
        job = self.instance._enqueue("import_stock", "x", {"location": self.location.id})
        original = type(self.instance)._job_import_stock
        from unittest.mock import patch

        def boom(_self, _job):
            raise error
        return job, patch.object(type(self.instance), "_job_import_stock", boom), original

    def test_dedupe_returns_pending_job(self):
        first = self.instance._enqueue("import_order", "a", {"gid": "1"}, dedupe_key="k")
        second = self.instance._enqueue("import_order", "b", {"gid": "2"}, dedupe_key="k")
        self.assertEqual(first, second)
        self.assertEqual(first.payload["gid"], "2")

    def test_transient_error_backs_off(self):
        job, patcher, _ = self.failing_job(ShopifyTransientError("Shopify answered HTTP 503"))
        with patcher:
            job._run()
        self.assertEqual(job.state, "pending")
        self.assertEqual(job.attempts, 1)
        self.assertGreater(job.next_attempt_at, fields.Datetime.now())

    def test_transient_gives_up_after_max(self):
        job, patcher, _ = self.failing_job(ShopifyTransientError("Shopify answered HTTP 503"))
        job.attempts = 5
        with patcher:
            job._run()
        self.assertEqual(job.state, "failed")

    def test_future_job_not_run(self):
        job = self.instance._enqueue("import_markets", "later", {}, delay=3600)
        self.run_queue()
        self.assertEqual(job.state, "pending")

    def test_permanent_error_fails_with_fix(self):
        from ..lib.client import ShopifyAccessDenied
        job, patcher, _ = self.failing_job(ShopifyAccessDenied("Access denied: read_orders", code="ACCESS_DENIED"))
        with patcher:
            job._run()
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.ai_root_cause, "Missing Shopify access scope")
        job.action_retry()
        self.assertEqual((job.state, job.attempts), ("pending", 0))

    def test_unexpected_crash_is_contained(self):
        job, patcher, _ = self.failing_job(RuntimeError("database surprise"))
        with patcher, mute_logger("odoo.addons.zivoit_shopify_connector.models.job"):
            job._run()
        self.assertEqual(job.state, "failed")
        self.assertIn("RuntimeError", job.error_message)

    def test_one_bad_job_does_not_stop_others(self):
        self.stock_product()
        bad = make_order(gid="1", lines=[make_line("1", "999", sku="NOPE")])
        good = make_order(gid="2")
        self.shop.orders.update({bad["id"]: bad, good["id"]: good})
        self.instance._enqueue("import_order", "bad", {"gid": bad["id"]})
        self.instance._enqueue("import_order", "good", {"gid": good["id"]})
        with mute_logger("odoo.addons.zivoit_shopify_connector.models.job"):
            self.run_queue()
        self.assertEqual(len(self.jobs("failed")), 1)
        self.assertTrue(self.env["sale.order"].search([("zivo_shopify_gid", "=", good["id"])]))

    def test_stale_running_job_released(self):
        job = self.instance._enqueue("import_markets", "x", {})
        job.write({"state": "running", "started_at": fields.Datetime.now() - timedelta(hours=2)})
        self.env["zivo.shopify.job"]._release_stale()
        self.assertEqual(job.state, "pending")

    def test_schedulers_queue_jobs(self):
        self.instance.write({"auto_import_products": True, "auto_import_checkouts": True})
        Instance = self.env["zivo.shopify.instance"]
        Instance._cron_import_orders()
        Instance._cron_import_products()
        Instance._cron_import_checkouts()
        Instance._cron_import_payouts()
        self.assertEqual(set(self.jobs("pending").mapped("job_type")), {"import_orders", "import_products", "import_checkouts", "import_payouts"})

    def test_dashboard_with_legacy_timezone_name(self):
        data = self.env["zivo.shopify.dashboard"].with_context(tz="Asia/Calcutta").get_data(self.instance.id)
        self.assertEqual(len(data["series"]), 14)

    def test_dashboard_payload(self):
        self.stock_product()
        self.import_order(make_order())
        data = self.env["zivo.shopify.dashboard"].get_data(self.instance.id)
        self.assertEqual(data["kpis"]["orders_today"] >= 0, True)
        self.assertEqual(len(data["series"]), 14)
        self.assertTrue(data["insights"])


@tagged("post_install", "-at_install")
class TestSecurity(ZivoShopifyCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(cls.env, "zivo_user", groups="base.group_user,zivoit_shopify_connector.group_user,sales_team.group_sale_salesman")
        cls.manager = new_test_user(cls.env, "zivo_mgr", groups="base.group_user,zivoit_shopify_connector.group_manager,sales_team.group_sale_salesman")
        cls.plain = new_test_user(cls.env, "zivo_plain", groups="base.group_user")

    def test_user_cannot_read_secrets(self):
        store = self.instance.with_user(self.user)
        self.assertEqual(store.name, "Acme")
        for field in ("client_secret", "access_token", "client_id", "webhook_token", "ai_api_key"):
            with self.assertRaises(AccessError):
                store.read([field])

    def test_manager_reads_secrets(self):
        self.assertEqual(self.instance.with_user(self.manager).client_secret, "secret")

    def test_user_cannot_edit_store(self):
        with self.assertRaises(AccessError):
            self.instance.with_user(self.user).name = "Hacked"

    def test_plain_user_has_no_access(self):
        with self.assertRaises(AccessError):
            self.env["zivo.shopify.instance"].with_user(self.plain).search([]).read(["name"]) or self.instance.with_user(self.plain).read(["name"])

    def test_multi_company_isolation(self):
        other = self.env["res.company"].create({"name": "Other Co"})
        store = self.env["zivo.shopify.instance"].create({"name": "Theirs", "shop_domain": "theirs.myshopify.com", "company_id": other.id})
        found = self.env["zivo.shopify.instance"].with_user(self.user).with_context(allowed_company_ids=self.user.company_ids.ids).search([])
        self.assertNotIn(store, found)

    def test_user_can_queue_export_through_action(self):
        product = self.stock_product()
        record = self.env["zivo.shopify.variant"].search([("product_id", "=", product.id)]).shopify_product_id
        record.with_user(self.user).action_export()
        self.assertTrue(self.jobs().filtered(lambda j: j.job_type == "export_product"))

    def test_dashboard_for_user(self):
        data = self.env["zivo.shopify.dashboard"].with_user(self.user).get_data()
        self.assertIn("kpis", data)

    def test_webhook_token_not_guessable(self):
        self.assertEqual(len(self.instance.webhook_token), 32)
