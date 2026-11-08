"""Shopify Admin GraphQL transport.

Pure Python on purpose: no Odoo import, so it can be unit-tested in isolation
and so the rate-limit logic is not tangled with business code.
"""
import logging
import time

import requests

_logger = logging.getLogger(__name__)

DEFAULT_API_VERSION = "2026-10"
DEFAULT_TIMEOUT = 40
MAX_ATTEMPTS = 6
MAX_BACKOFF_SECONDS = 30


class ShopifyError(Exception):
    """Base class. ``transient`` tells the queue whether a retry can help."""

    transient = False

    def __init__(self, message, code=None, details=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details


class ShopifyTransientError(ShopifyError):
    """Network failure, 5xx or throttling that outlived the retries."""

    transient = True


class ShopifyAuthError(ShopifyError):
    """Invalid or expired credentials."""


class ShopifyAccessDenied(ShopifyError):
    """The app is missing an access scope (or protected customer data approval)."""


class ShopifyGraphQLError(ShopifyError):
    """The server rejected the document itself."""


class ShopifyUserError(ShopifyError):
    """A mutation answered with userErrors: a business validation failure."""


class ShopifyClient:
    """One client per store and per job batch.

    :param shop: ``my-store.myshopify.com``
    :param token_provider: callable returning a valid token. Called with
        ``force=True`` once when Shopify answers 401, so that expiring
        client-credentials tokens are renewed transparently.
    :param on_cost: optional callback receiving the ``throttleStatus`` dict.
    """

    def __init__(self, shop, token_provider, api_version=DEFAULT_API_VERSION, on_cost=None,
                 timeout=DEFAULT_TIMEOUT, sleep=time.sleep):
        self.shop = shop
        self.token_provider = token_provider
        self.api_version = api_version
        self.on_cost = on_cost
        self.timeout = timeout
        self.sleep = sleep
        self.session = requests
        self.calls = 0
        self._available = None
        self._restore_rate = None

    @property
    def endpoint(self):
        return f"https://{self.shop}/admin/api/{self.api_version}/graphql.json"

    # ------------------------------------------------------------------
    # Throttling
    # ------------------------------------------------------------------
    def _remember_cost(self, cost):
        status = (cost or {}).get("throttleStatus") or {}
        if not status:
            return
        self._available = status.get("currentlyAvailable")
        self._restore_rate = status.get("restoreRate") or 50
        if self.on_cost:
            self.on_cost(status)

    def _wait_for_budget(self, expected_cost):
        """Sleep *before* the call when the bucket clearly cannot serve it."""
        if self._available is None or self._available >= expected_cost:
            return
        missing = expected_cost - self._available
        wait = min(missing / (self._restore_rate or 50), MAX_BACKOFF_SECONDS)
        _logger.debug("Shopify %s: waiting %.1fs for query budget", self.shop, wait)
        self.sleep(wait)
        self._available = expected_cost

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------
    def execute(self, query, variables=None, expected_cost=100):
        """Run a document and return ``data``; retries what is worth retrying."""
        self._wait_for_budget(expected_cost)
        auth_retried = False
        last_error = None
        attempt = 0
        while attempt < MAX_ATTEMPTS:
            attempt += 1
            try:
                response = self.session.post(
                    self.endpoint,
                    json={"query": query, "variables": variables or {}},
                    headers={
                        "X-Shopify-Access-Token": self.token_provider(),
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as error:
                last_error = ShopifyTransientError(f"Network error: {error}", code="NETWORK")
                self.sleep(self._backoff(attempt))
                continue
            self.calls += 1
            status = response.status_code

            if status == 401:
                if not auth_retried:
                    auth_retried = True
                    attempt -= 1
                    self.token_provider(force=True)
                    continue
                raise ShopifyAuthError("Shopify rejected the access token (401).", code="UNAUTHORIZED")
            if status == 403:
                raise ShopifyAccessDenied(
                    "Shopify refused the request (403). Check the app access scopes.", code="FORBIDDEN"
                )
            if status == 402:
                raise ShopifyError("The Shopify store is frozen (402 Payment Required).", code="PAYMENT_REQUIRED")
            if status == 404:
                raise ShopifyError(
                    f"Store or API version not found (404): {self.endpoint}", code="NOT_FOUND"
                )
            if status == 429 or status >= 500:
                last_error = ShopifyTransientError(f"Shopify answered HTTP {status}", code=str(status))
                self.sleep(self._retry_after(response, attempt))
                continue
            if status >= 400:
                raise ShopifyError(f"Shopify answered HTTP {status}: {response.text[:500]}", code=str(status))

            try:
                payload = response.json()
            except ValueError as error:
                last_error = ShopifyTransientError(f"Invalid JSON from Shopify: {error}", code="BAD_JSON")
                self.sleep(self._backoff(attempt))
                continue

            self._remember_cost((payload.get("extensions") or {}).get("cost"))
            errors = payload.get("errors")
            if not errors:
                return payload.get("data") or {}

            if isinstance(errors, str):
                raise ShopifyGraphQLError(errors)
            codes = {((e.get("extensions") or {}).get("code") or "") for e in errors}
            message = "; ".join(e.get("message", "") for e in errors)
            if "THROTTLED" in codes:
                last_error = ShopifyTransientError("Shopify query budget exhausted", code="THROTTLED")
                self.sleep(self._throttle_wait(payload, expected_cost, attempt))
                continue
            if "ACCESS_DENIED" in codes:
                raise ShopifyAccessDenied(message, code="ACCESS_DENIED", details=errors)
            if "INTERNAL_SERVER_ERROR" in codes:
                last_error = ShopifyTransientError(message, code="INTERNAL_SERVER_ERROR")
                self.sleep(self._backoff(attempt))
                continue
            raise ShopifyGraphQLError(message, code=",".join(sorted(codes)), details=errors)
        raise last_error or ShopifyTransientError("Shopify request failed", code="UNKNOWN")

    def mutate(self, query, variables, payload_key, expected_cost=50):
        """Run a mutation and raise when its payload carries userErrors."""
        data = self.execute(query, variables, expected_cost=expected_cost)
        payload = data.get(payload_key) or {}
        errors = payload.get("userErrors") or []
        if errors:
            message = "; ".join(
                f"{'.'.join(str(p) for p in (error.get('field') or []))}: {error.get('message')}".lstrip(": ")
                for error in errors
            )
            raise ShopifyUserError(message, code=errors[0].get("code"), details=errors)
        return payload

    def iter_connection(self, query, variables, path, max_pages=None, expected_cost=500):
        """Yield nodes of a paginated connection found at ``path`` in the result."""
        variables = dict(variables or {})
        pages = 0
        while True:
            data = self.execute(query, variables, expected_cost=expected_cost)
            connection = data
            for key in path:
                connection = (connection or {}).get(key)
            if not connection:
                return
            yield from connection.get("nodes") or []
            info = connection.get("pageInfo") or {}
            pages += 1
            if not info.get("hasNextPage") or (max_pages and pages >= max_pages):
                return
            variables["cursor"] = info.get("endCursor")

    def node_with_pages(self, query, gid, connection_key, expected_cost=500):
        """Read a node and finish one of its connections when it has more pages."""
        variables = {"id": gid}
        node = self.execute(query, variables, expected_cost=expected_cost).get("node")
        if not node:
            return None
        connection = node.get(connection_key) or {}
        items = list(connection.get("nodes") or [])
        info = connection.get("pageInfo") or {}
        while info.get("hasNextPage"):
            variables["cursor"] = info.get("endCursor")
            page = self.execute(query, variables, expected_cost=expected_cost)["node"][connection_key]
            items += page.get("nodes") or []
            info = page.get("pageInfo") or {}
        node[connection_key] = {"nodes": items}
        return node

    @staticmethod
    def _backoff(attempt):
        return min(2 ** attempt, MAX_BACKOFF_SECONDS)

    def _retry_after(self, response, attempt):
        try:
            return min(float(response.headers.get("Retry-After")), MAX_BACKOFF_SECONDS)
        except (TypeError, ValueError):
            return self._backoff(attempt)

    def _throttle_wait(self, payload, expected_cost, attempt):
        cost = (payload.get("extensions") or {}).get("cost") or {}
        status = cost.get("throttleStatus") or {}
        needed = cost.get("requestedQueryCost") or expected_cost
        available = status.get("currentlyAvailable") or 0
        rate = status.get("restoreRate") or 50
        return min(max(needed - available, 1) / rate + 0.25, MAX_BACKOFF_SECONDS) or self._backoff(attempt)


def request_client_credentials_token(shop, client_id, client_secret, timeout=30):
    """Exchange Dev Dashboard credentials for a 24 hour Admin API token."""
    try:
        response = requests.post(
            f"https://{shop}/admin/oauth/access_token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
    except (requests.ConnectionError, requests.Timeout) as error:
        raise ShopifyTransientError(f"Network error: {error}", code="NETWORK") from error
    if response.status_code >= 500:
        raise ShopifyTransientError(f"Shopify answered HTTP {response.status_code}", code=str(response.status_code))
    if response.status_code != 200:
        raise ShopifyAuthError(
            f"Could not obtain a Shopify token (HTTP {response.status_code}): {response.text[:300]}",
            code=str(response.status_code),
        )
    payload = response.json()
    return payload["access_token"], int(payload.get("expires_in") or 86399)


def gid_tail(gid):
    """``gid://shopify/Order/123?x=1`` -> ``123``."""
    return (gid or "").rsplit("/", 1)[-1].split("?", 1)[0]
