# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
import base64
import hashlib
import hmac


from odoo.tests import BaseCase, tagged

from ..lib import crypto, qbo_client
from ..lib.qbo_client import QBOAuthError, QBOClient, QBOError, parse_webhook, verify_webhook_signature
from .common import FakeQBO, FakeResponse


@tagged("post_install", "-at_install", "qbo")
class TestQboClient(BaseCase):

    def setUp(self):
        super().setUp()
        self.fake = FakeQBO()
        self.saved = []
        self.slept = []
        qbo_client._RateLimiter.enabled = False
        self.addCleanup(setattr, qbo_client._RateLimiter, "enabled", True)
        self.client = QBOClient("cid", "secret", realm_id="123", access_token="at-0", refresh_token="rt-0",
                                session=self.fake.session(), token_saver=self.saved.append,
                                sleep=self.slept.append)

    def test_authorization_url(self):
        url = QBOClient.authorization_url("cid", "https://odoo.example.com/cb", "1:n:s")
        self.assertTrue(url.startswith(qbo_client.AUTH_URL))
        self.assertIn("scope=com.intuit.quickbooks.accounting", url)
        self.assertIn("redirect_uri=https%3A%2F%2Fodoo.example.com%2Fcb", url)
        self.assertIn("state=1%3An%3As", url)

    def test_minor_version_always_sent(self):
        self.client.company_info()
        method, url, params, _data = self.fake.calls[-1]
        self.assertEqual(params["minorversion"], "75")

    def test_401_refreshes_and_rotates_refresh_token(self):
        self.client.access_token = "expired"
        info = self.client.company_info()
        self.assertEqual(info["CompanyName"], "Fake Co CA")
        self.assertEqual(self.client.refresh_token, "rt-1", "rotated refresh token must be kept")
        self.assertEqual(self.saved[-1]["refresh_token"], "rt-1", "rotated token must be persisted")

    def test_invalid_refresh_raises_auth_error(self):
        self.client.access_token = "expired"
        self.client.refresh_token = "revoked"
        with self.assertRaises(QBOAuthError):
            self.client.company_info()

    def test_429_retry_with_retry_after(self):
        self.fake.fail_next(lambda m, u, p: "companyinfo" in u,
                            FakeResponse(429, {"Fault": {"Error": [{"Message": "Throttle", "code": "003001"}]}},
                                         headers={"Retry-After": "7"}))
        self.client.company_info()
        self.assertEqual(self.slept, [7.0])

    def test_5xx_gives_up_after_retries(self):
        for _i in range(6):
            self.fake.fail_next(lambda m, u, p: "companyinfo" in u, FakeResponse(503, {}))
        with self.assertRaises(QBOError) as cm:
            self.client.company_info()
        self.assertTrue(cm.exception.is_transient)
        self.assertEqual(len(self.slept), self.client.max_retries)

    def test_fault_parsing_and_flags(self):
        self.fake.add("Customer", {"Id": "5", "DisplayName": "A"})
        with self.assertRaises(QBOError) as cm:
            self.client.update("Customer", {"Id": "5", "SyncToken": "9", "DisplayName": "B"})
        self.assertTrue(cm.exception.is_stale_object)
        with self.assertRaises(QBOError) as cm:
            self.client.create("Customer", {"DisplayName": "A"})
        self.assertTrue(cm.exception.is_duplicate_name)

    def test_query_paging(self):
        for i in range(7):
            self.fake.add("Term", {"Name": "T%s" % i})
        names = [r["Name"] for r in self.client.query_all("Term", page_size=3)]
        self.assertEqual(len(names), 7)
        queries = [c[2]["query"] for c in self.fake.calls if c[2].get("query")]
        self.assertIn("STARTPOSITION 4 MAXRESULTS 3", queries[1])
        self.assertIn("STARTPOSITION 7 MAXRESULTS 3", queries[2])

    def test_cdc_returns_deleted(self):
        self.fake.cdc_feed = {"Invoice": [{"Id": "9", "status": "Deleted"}, {"Id": "10", "SyncToken": "1"}]}
        res = self.client.cdc(["Invoice", "Customer"], "2026-11-01T00:00:00Z")
        self.assertEqual(len(res["Invoice"]), 2)
        self.assertEqual(res["Invoice"][0]["status"], "Deleted")

    def test_void_payment_uses_include_void(self):
        pay = self.fake.add("Payment", {"TotalAmt": 10})
        self.client.void("Payment", pay["Id"], "0")
        _m, _u, params, _d = self.fake.calls[-1]
        self.assertEqual(params.get("include"), "void")
        inv = self.fake.add("Invoice", {"Line": [{"DetailType": "SalesItemLineDetail", "Amount": 5}]})
        self.client.void("Invoice", inv["Id"], "0")
        self.assertEqual(self.fake.calls[-1][2].get("operation"), "void")
        self.assertEqual(self.fake.get("Invoice", inv["Id"])["TotalAmt"], 0)

    def test_pdf_raw(self):
        inv = self.fake.add("Invoice", {"Line": [{"DetailType": "SalesItemLineDetail", "Amount": 5}]})
        self.assertTrue(self.client.pdf("Invoice", inv["Id"]).startswith(b"%PDF"))

    def test_webhook_signature(self):
        body = b'{"eventNotifications": []}'
        sig = base64.b64encode(hmac.new(b"tok", body, hashlib.sha256).digest()).decode()
        self.assertTrue(verify_webhook_signature(body, sig, "tok"))
        self.assertFalse(verify_webhook_signature(body, sig, "other"))
        self.assertFalse(verify_webhook_signature(body + b" ", sig, "tok"))
        self.assertFalse(verify_webhook_signature(body, None, "tok"))

    def test_parse_legacy_webhook(self):
        payload = {"eventNotifications": [{"realmId": "123", "dataChangeEvent": {"entities": [
            {"name": "Invoice", "id": "42", "operation": "Update", "lastUpdated": "2026-11-06T00:00:00Z"},
            {"name": "Customer", "id": "7", "operation": "Merge", "deletedId": "6"}]}}]}
        events = parse_webhook(payload)
        self.assertEqual(events[0], {"realm": "123", "entity": "Invoice", "id": "42", "operation": "Update",
                                     "deleted_id": None})
        self.assertEqual(events[1]["deleted_id"], "6")

    def test_parse_cloudevents_webhook(self):
        payload = [
            {"specversion": "1.0", "id": "x", "type": "qbo.invoice.voided.v1", "intuitentityid": "42",
             "intuitaccountid": "123", "time": "2026-11-06T00:00:00Z"},
            {"specversion": "1.0", "id": "y", "type": "qbo.customer.merged.v1", "intuitentityid": "7",
             "intuitaccountid": "123", "data": {"deletedid": "6"}},
            {"type": "other.thing.v1"},
        ]
        events = parse_webhook(payload)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["entity"], "invoice")
        self.assertEqual(events[0]["operation"], "Void")
        self.assertEqual(events[1]["operation"], "Merge")
        self.assertEqual(events[1]["deleted_id"], "6")

    def test_rate_limiter_waits(self):
        qbo_client._RateLimiter.enabled = True
        waits = []
        qbo_client._RateLimiter._windows.pop("rl-test", None)
        for _i in range(3):
            qbo_client._RateLimiter.acquire("rl-test", per_minute=3, sleep=waits.append)
        self.assertEqual(waits, [])
        # 4th call must wait; the fake sleep frees the window to stop the loop
        def fake_sleep(sec):
            waits.append(sec)
            qbo_client._RateLimiter._windows["rl-test"].clear()
        qbo_client._RateLimiter.acquire("rl-test", per_minute=3, sleep=fake_sleep)
        self.assertEqual(len(waits), 1)
        self.assertGreater(waits[0], 50)

    def test_crypto_roundtrip(self):
        enc = crypto.encrypt("db-secret", "refresh-token-value")
        self.assertTrue(enc.startswith(crypto.PREFIX))
        self.assertNotIn("refresh-token-value", enc)
        self.assertEqual(crypto.decrypt("db-secret", enc), "refresh-token-value")
        self.assertFalse(crypto.decrypt("other-secret", enc))
        self.assertEqual(crypto.decrypt("x", "legacy-plain"), "legacy-plain")
