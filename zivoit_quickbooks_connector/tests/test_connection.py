# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
import base64
import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from ..lib import crypto
from ..models.qb_entity_config import ENTITIES
from .common import QbCommon


def _sign(body, token="verifier-123"):
    return base64.b64encode(hmac.new(token.encode(), body, hashlib.sha256).digest()).decode()


@tagged("post_install", "-at_install", "qbo")
class TestConnection(QbCommon):

    def test_entity_configs_created_with_defaults(self):
        self.assertEqual(len(self.conn.entity_config_ids), len(ENTITIES))
        tax = self.conn._config("TaxCode")
        self.assertEqual(tax.direction, "import")
        self.assertTrue(tax.allows("import"))
        self.assertFalse(tax.allows("export"))
        self.assertFalse(self.conn._config("JournalEntry").auto_sync)
        self.assertTrue(self.conn._allows("Invoice", "export"))

    def test_company_info_after_connect(self):
        self.assertEqual(self.conn.state, "connected")
        self.assertEqual(self.conn.qb_company_name, "Fake Co CA")
        self.assertFalse(self.conn.is_us_company)
        self.assertTrue(self.conn.qb_multicurrency)
        self.assertTrue(self.conn.sale_journal_id and self.conn.payment_journal_id)
        self.assertTrue(self.conn.class_plan_id and self.conn.location_plan_id)

    def test_tokens_encrypted_at_rest(self):
        tok = self.env["qb.connection.token"].sudo().search([("connection_id", "=", self.conn.id)])
        self.assertTrue(tok.refresh_token.startswith(crypto.PREFIX))
        access, refresh, _exp = self.conn._get_tokens()
        self.assertNotIn(refresh, tok.refresh_token, "plain refresh token must never be stored")
        self.assertNotIn(access, tok.access_token, "plain access token must never be stored")
        self.assertTrue(refresh.startswith("rt-"))
        self.assertTrue(access.startswith("at-"))

    def test_proactive_refresh_when_access_token_expires(self):
        tok = self.env["qb.connection.token"].sudo().search([("connection_id", "=", self.conn.id)])
        tok.access_expiry = fields.Datetime.now() - timedelta(minutes=1)
        before = self.fake.refresh_count
        self.conn._get_client()
        self.assertEqual(self.fake.refresh_count, before + 1)
        _a, refresh, exp = self.conn._get_tokens()
        self.assertEqual(refresh, "rt-%d" % self.fake.refresh_count)
        self.assertGreater(exp, fields.Datetime.now())

    def test_oauth_state_roundtrip_and_tamper(self):
        action = self.conn.action_connect()
        self.assertEqual(action["type"], "ir.actions.act_url")
        state = action["url"].split("state=")[1].replace("%3A", ":")
        self.assertEqual(self.env["qb.connection"]._check_oauth_state(state), self.conn)
        conn_id, nonce, sig = state.split(":")
        self.assertFalse(self.env["qb.connection"]._check_oauth_state("%s:%s:%s" % (conn_id, nonce, "0" * 32)))
        self.assertFalse(self.env["qb.connection"]._check_oauth_state("garbage"))

    def test_oauth_callback_exchanges_code(self):
        conn = self.env["qb.connection"].create({"name": "Second", "client_id": "c2", "client_secret": "s2",
                                                  "company_id": self.company.id})
        self.assertEqual(conn.state, "draft")
        conn._oauth_state()
        conn._oauth_callback("auth-code", "555")
        self.assertEqual(conn.state, "connected")
        self.assertEqual(conn.realm_id, "555")
        self.assertFalse(conn.oauth_nonce, "nonce must be single-use")
        token_call = [c for c in self.fake.calls if "tokens/bearer" in c[1]][-1]
        self.assertEqual(token_call[3]["grant_type"], "authorization_code")
        self.assertEqual(token_call[3]["code"], "auth-code")

    def test_connect_requires_credentials(self):
        conn = self.env["qb.connection"].create({"name": "No keys", "company_id": self.company.id})
        with self.assertRaises(UserError):
            conn.action_connect()

    def test_manual_token_wizard(self):
        conn = self.env["qb.connection"].create({"name": "Manual", "client_id": "c3", "client_secret": "s3",
                                                  "company_id": self.company.id})
        wiz = self.env["qb.token.wizard"].create({"connection_id": conn.id, "realm_id": " 777 ",
                                                  "refresh_token": "rt-playground"})
        wiz.action_apply()
        self.assertEqual(conn.state, "connected")
        self.assertEqual(conn.realm_id, "777")

    def test_disconnect_revokes(self):
        conn = self.env["qb.connection"].create({"name": "Temp", "client_id": "c4", "client_secret": "s4",
                                                  "company_id": self.company.id})
        conn._save_tokens({"access_token": "at-x", "refresh_token": "rt-x"})
        conn.realm_id = "888"
        conn.state = "connected"
        conn.action_disconnect()
        self.assertTrue(self.fake.revoked)
        self.assertEqual(conn.state, "draft")
        self.assertFalse(self.env["qb.connection.token"].sudo().search([("connection_id", "=", conn.id)]))

    def test_auth_failure_marks_connection_expired(self):
        self.fake.add("Customer", {"Id": "301", "DisplayName": "Auth Test"})
        tok = self.env["qb.connection.token"].sudo().search([("connection_id", "=", self.conn.id)])
        tok.write({"access_token": crypto.encrypt(self.conn._crypt_secret(), "bad"),
                   "refresh_token": crypto.encrypt(self.conn._crypt_secret(), "revoked")})
        job = self.conn._enqueue("import", "Customer", qb_id="301")
        job._run_one()
        self.assertEqual(job.state, "pending", "auth errors must not burn retries")
        self.assertEqual(self.conn.state, "expired")
        self.conn.state = "connected"
        self.conn._save_tokens({"access_token": "at-0", "refresh_token": "rt-0"})

    # ------------------------------------------------------------ webhooks
    def test_webhook_legacy_valid_signature_enqueues(self):
        body = json.dumps({"eventNotifications": [{"realmId": self.conn.realm_id, "dataChangeEvent": {"entities": [
            {"name": "Invoice", "id": "4242", "operation": "Create"},
            {"name": "Customer", "id": "77", "operation": "Update"},
            {"name": "Employee", "id": "1", "operation": "Update"}]}}]}).encode()
        n = self.env["qb.connection"]._handle_webhook(body, _sign(body), json.loads(body))
        self.assertEqual(n, 2, "unsupported entities are ignored")
        job = self.Job.search([("qb_entity", "=", "Invoice"), ("qb_id", "=", "4242")])
        self.assertEqual(job.source, "webhook")
        self.assertLess(job.priority, 0, "webhook jobs are boosted")

    def test_webhook_cloudevents(self):
        body = json.dumps([{"specversion": "1.0", "type": "qbo.customer.merged.v1", "intuitentityid": "88",
                            "intuitaccountid": self.conn.realm_id, "data": {"deletedid": "87"}}]).encode()
        self.env["qb.connection"]._handle_webhook(body, _sign(body), json.loads(body))
        merge_job = self.Job.search([("qb_entity", "=", "Customer"), ("qb_id", "=", "87")])
        self.assertEqual(merge_job.operation, "Merge")
        self.assertEqual(merge_job.payload["merged_into"], "88")

    def test_webhook_invalid_signature_rejected(self):
        body = json.dumps({"eventNotifications": [{"realmId": self.conn.realm_id, "dataChangeEvent": {
            "entities": [{"name": "Invoice", "id": "999", "operation": "Create"}]}}]}).encode()
        n = self.env["qb.connection"]._handle_webhook(body, _sign(body, "wrong"), json.loads(body))
        self.assertEqual(n, 0)
        self.assertFalse(self.Job.search([("qb_id", "=", "999")]))
        self.assertTrue(self.env["qb.sync.log"].search([("message", "ilike", "invalid intuit-signature")]))

    def test_webhook_respects_disabled_entity(self):
        self.conn._config("Invoice").direction = "export"
        body = json.dumps({"eventNotifications": [{"realmId": self.conn.realm_id, "dataChangeEvent": {
            "entities": [{"name": "Invoice", "id": "1001", "operation": "Create"}]}}]}).encode()
        self.assertEqual(self.env["qb.connection"]._handle_webhook(body, _sign(body), json.loads(body)), 0)

    def test_enqueue_dedup(self):
        j1 = self.conn._enqueue("import", "Customer", qb_id="55")
        j2 = self.conn._enqueue("import", "Customer", qb_id="55", operation="Update")
        self.assertEqual(j1, j2)
        self.assertEqual(j1.operation, "Update")

    def test_transient_failure_backoff_then_fail(self):
        from .common import FakeResponse
        self.fake.add("Customer", {"Id": "401", "DisplayName": "Flaky"})
        job = self.conn._enqueue("import", "Customer", qb_id="401")
        job.max_attempts = 2
        for _i in range(12):
            self.fake.fail_next(lambda m, u, p: "customer/401" in u, FakeResponse(503, {}))
        job._run_one()
        self.assertEqual(job.state, "pending")
        self.assertGreater(job.next_attempt, fields.Datetime.now())
        job._run_one()
        self.assertEqual(job.state, "failed")
        self.assertTrue(self.env["qb.sync.log"].search([("job_id", "=", job.id), ("level", "=", "error")]))
        self.fake.failures = []
        job.action_retry()
        job._run_one()
        self.assertJobDone(job)

    def test_cron_sync_runs_cdc_and_exports(self):
        self.conn.next_sync = False
        self.fake.cdc_feed = {"Customer": [{"Id": "9001", "DisplayName": "From CDC", "SyncToken": "0"}]}
        self.env["qb.connection"]._cron_sync()
        job = self.Job.search([("qb_id", "=", "9001")])
        self.assertEqual(job.source, "cdc")
        self.assertTrue(self.conn.next_sync)
        cdc_call = [c for c in self.fake.calls if c[1].endswith("/cdc")][-1]
        self.assertNotIn("JournalEntry", cdc_call[2]["entities"], "auto-sync off entities are not polled")

    def test_cdc_lookback_fallback_to_full_import(self):
        self.conn.last_cdc_sync = fields.Datetime.now() - timedelta(days=45)
        with patch.object(type(self.conn), "_full_import", return_value=3) as full:
            self.assertEqual(self.conn._incremental_sync(), 3)
            full.assert_called_once()

    def test_cron_keep_tokens_alive(self):
        before = self.fake.refresh_count
        self.env["qb.connection"]._cron_keep_tokens_alive()
        self.assertGreater(self.fake.refresh_count, before)

    def test_cleanup_cron(self):
        log = self.env["qb.sync.log"]._log(self.conn, "old")
        self.env.cr.execute("UPDATE qb_sync_log SET create_date = now() - interval '400 days' WHERE id = %s",
                            [log.id])
        self.env["qb.sync.log"].invalidate_model()
        self.env["qb.sync.log"]._cron_cleanup()
        self.assertFalse(log.exists())

    def test_dashboard_stats(self):
        self.conn._enqueue("import", "Customer", qb_id="12345")
        self.conn.invalidate_recordset(["pending_count"])
        self.assertGreaterEqual(self.conn.pending_count, 1)
        self.assertIn(self.conn.health, ("ok", "warn", "ko"))
        self.assertEqual(self.conn.action_view_pending()["res_model"], "qb.sync.job")

    # ------------------------------------------------------ error handling
    def _break_network(self):
        import requests
        from .common import FakeQBO
        original = FakeQBO.handle

        def boom(fake, method, url, params, data, headers):
            raise requests.exceptions.ConnectionError("Tunnel connection failed: 403 Forbidden")
        self.fake.handle = boom.__get__(self.fake)
        self.addCleanup(lambda: delattr(self.fake, "handle"))
        tok = self.env["qb.connection.token"].sudo().search([("connection_id", "=", self.conn.id)])
        tok.access_expiry = fields.Datetime.now() - timedelta(minutes=1)  # force a token refresh
        return original

    def test_network_error_is_a_clean_user_message(self):
        self._break_network()
        wiz = self.env["qb.integrity.wizard"].create({"connection_id": self.conn.id})
        with self.assertRaises(UserError) as cm:
            wiz.action_check()
        self.assertIn("Could not reach QuickBooks", str(cm.exception))
        self.assertIs(type(cm.exception), UserError, "the web client only shows a clean dialog for UserError itself")
        with self.assertRaises(UserError):
            self.conn.action_test_connection()
        with self.assertRaises(UserError):
            self.conn.action_smart_match()
        with self.assertRaises(UserError):
            self.conn.action_sync_now()

    def test_network_error_does_not_expire_connection(self):
        self._break_network()
        self.env["qb.connection"]._cron_keep_tokens_alive()
        self.assertEqual(self.conn.state, "connected", "a network outage must not force re-authorization")
        self.assertIn("Could not reach QuickBooks", self.conn.last_error)

    def test_network_error_in_job_is_retried(self):
        self._break_network()
        job = self.conn._enqueue("import", "Customer", qb_id="5150")
        job._run_one()
        self.assertEqual(job.state, "pending", "network errors are transient and retried later")
        self.assertEqual(self.conn.state, "connected")

    def test_disconnect_works_offline(self):
        self._break_network()
        conn = self.conn
        conn.action_disconnect()
        self.assertEqual(conn.state, "draft")

