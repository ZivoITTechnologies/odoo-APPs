from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from ..lib.client import ShopifyAccessDenied, ShopifyAuthError, ShopifyClient, ShopifyGraphQLError, ShopifyTransientError, ShopifyUserError, gid_tail
from .shopify_mock import MockResponse


@tagged("post_install", "-at_install")
class TestClient(TransactionCase):

    def make(self, responses):
        self.tokens = []

        def provider(force=False):
            self.tokens.append(force)
            return "t"
        self.slept = []
        client = ShopifyClient("a.myshopify.com", provider, sleep=self.slept.append)
        self.responses = list(responses)
        patcher = patch("requests.post", lambda *a, **k: self.responses.pop(0))
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def test_endpoint_uses_pinned_version(self):
        self.assertEqual(self.make([]).endpoint, "https://a.myshopify.com/admin/api/2026-10/graphql.json")

    def test_success_records_budget(self):
        client = self.make([MockResponse(200, {"data": {"x": 1}, "extensions": {"cost": {"throttleStatus": {"currentlyAvailable": 50, "restoreRate": 100}}}})])
        self.assertEqual(client.execute("q"), {"x": 1})
        self.assertEqual(client._available, 50)

    def test_preemptive_wait_when_budget_is_low(self):
        client = self.make([MockResponse(200, {"data": {}})])
        client._available, client._restore_rate = 100, 100
        client.execute("q", expected_cost=500)
        self.assertEqual(self.slept, [4.0])

    def test_throttled_then_success(self):
        throttled = {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}], "extensions": {"cost": {"requestedQueryCost": 300, "throttleStatus": {"currentlyAvailable": 100, "restoreRate": 100}}}}
        client = self.make([MockResponse(200, throttled), MockResponse(200, {"data": {"ok": True}})])
        self.assertEqual(client.execute("q"), {"ok": True})
        self.assertEqual(self.slept, [2.25])

    def test_429_uses_retry_after(self):
        client = self.make([MockResponse(429, {}, {"Retry-After": "3"}), MockResponse(200, {"data": {"ok": 1}})])
        client.execute("q")
        self.assertEqual(self.slept, [3.0])

    def test_5xx_exhausts_to_transient(self):
        client = self.make([MockResponse(503, {}) for _ in range(6)])
        with self.assertRaises(ShopifyTransientError) as caught:
            client.execute("q")
        self.assertTrue(caught.exception.transient)
        self.assertEqual(len(self.slept), 6)

    def test_401_refreshes_token_once(self):
        client = self.make([MockResponse(401, {}), MockResponse(200, {"data": {"ok": 1}})])
        self.assertEqual(client.execute("q"), {"ok": 1})
        self.assertIn(True, self.tokens)

    def test_401_twice_is_auth_error(self):
        client = self.make([MockResponse(401, {}), MockResponse(401, {})])
        with self.assertRaises(ShopifyAuthError):
            client.execute("q")

    def test_access_denied_is_permanent(self):
        client = self.make([MockResponse(200, {"errors": [{"message": "Denied", "extensions": {"code": "ACCESS_DENIED"}}]})])
        with self.assertRaises(ShopifyAccessDenied) as caught:
            client.execute("q")
        self.assertFalse(caught.exception.transient)

    def test_graphql_error(self):
        client = self.make([MockResponse(200, {"errors": [{"message": "Field x missing"}]})])
        with self.assertRaises(ShopifyGraphQLError):
            client.execute("q")

    def test_user_errors_raise(self):
        client = self.make([MockResponse(200, {"data": {"m": {"userErrors": [{"field": ["a", "b"], "message": "bad", "code": "X"}]}}})])
        with self.assertRaises(ShopifyUserError) as caught:
            client.mutate("m", {}, "m")
        self.assertIn("a.b: bad", str(caught.exception))

    def test_pagination(self):
        client = self.make([
            MockResponse(200, {"data": {"c": {"nodes": [1], "pageInfo": {"hasNextPage": True, "endCursor": "A"}}}}),
            MockResponse(200, {"data": {"c": {"nodes": [2], "pageInfo": {"hasNextPage": False}}}}),
        ])
        self.assertEqual(list(client.iter_connection("q", {}, ["c"])), [1, 2])

    def test_node_with_pages_finishes_connection(self):
        first = {"data": {"node": {"id": "x", "rows": {"nodes": [1], "pageInfo": {"hasNextPage": True, "endCursor": "A"}}}}}
        second = {"data": {"node": {"id": "x", "rows": {"nodes": [2], "pageInfo": {"hasNextPage": False}}}}}
        client = self.make([MockResponse(200, first), MockResponse(200, second)])
        self.assertEqual(client.node_with_pages("q", "x", "rows")["rows"]["nodes"], [1, 2])

    def test_gid_tail(self):
        self.assertEqual(gid_tail("gid://shopify/Order/12?x=1"), "12")
