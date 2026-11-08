# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
"""Thin, dependency-free QuickBooks Online (Intuit) REST client.

Kept free of Odoo imports so it can be unit tested and reused. Handles:

* OAuth 2.0 authorization-code flow, token refresh (rotating refresh tokens)
  and revocation.
* Per-realm client-side rate limiting (Intuit: 500 req/min/realm and
  10 concurrent req/s) plus server-side 429 / 5xx retry with back-off.
* Query paging (MAXRESULTS capped at 1000), CDC, batch (30 ops/request),
  create / sparse update / delete / void, PDF download and e-mail send.
"""
import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from collections import deque
from urllib.parse import urlencode, quote

import requests

try:  # inside Odoo: QuickBooks errors are shown as clean user messages, never as tracebacks
    from odoo.exceptions import UserError as _ErrorBase
except ImportError:  # pragma: no cover - library used outside Odoo
    _ErrorBase = Exception

_logger = logging.getLogger(__name__)

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
API_BASE = {
    "production": "https://quickbooks.api.intuit.com",
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
}
SCOPE_ACCOUNTING = "com.intuit.quickbooks.accounting"
DEFAULT_MINOR_VERSION = "75"
MAX_RESULTS = 1000
BATCH_SIZE = 30
REQUESTS_PER_MINUTE = 450          # keep a safety margin under Intuit's 500/min
DEFAULT_TIMEOUT = 60

# Indirections so tests can swap transport / clock without monkey-patching requests.
SESSION_FACTORY = requests.Session


def _sleep(seconds):
    time.sleep(seconds)


class QBOError(_ErrorBase):
    """Generic QuickBooks API error carrying the parsed Fault payload."""

    def __init__(self, message, status=None, code=None, detail=None, payload=None):
        super().__init__(message)
        self.status = status
        self.code = str(code) if code is not None else None
        self.detail = detail
        self.payload = payload

    @property
    def is_stale_object(self):
        # Error 5010 = Stale Object Error (SyncToken mismatch)
        return self.code == "5010"

    @property
    def is_duplicate_name(self):
        # 6240 = Duplicate Name Exists Error
        return self.code == "6240"

    @property
    def is_transient(self):
        return self.status in (429, 500, 502, 503, 504) or self.status is None


class QBOAuthError(QBOError):
    """Token is invalid/expired/revoked: the user must re-authorize."""


class _RateLimiter:
    """Sliding-window limiter shared by every client of the same realm."""

    _lock = threading.Lock()
    _windows = {}
    enabled = True

    @classmethod
    def acquire(cls, realm, per_minute=REQUESTS_PER_MINUTE, sleep=None):
        if not cls.enabled:
            return
        sleep = sleep or _sleep
        while True:
            with cls._lock:
                now = time.monotonic()
                window = cls._windows.setdefault(realm, deque())
                while window and now - window[0] > 60:
                    window.popleft()
                if len(window) < per_minute:
                    window.append(now)
                    return
                wait = 60 - (now - window[0]) + 0.05
            _logger.info("QuickBooks rate limit reached for realm %s, waiting %.1fs", realm, wait)
            sleep(wait)


def verify_webhook_signature(payload_bytes, signature, verifier_token):
    """Intuit signs webhook bodies: base64(HMAC-SHA256(verifier_token, body))."""
    if not (signature and verifier_token):
        return False
    digest = hmac.new(verifier_token.encode(), payload_bytes, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature.strip())


CLOUDEVENT_OPERATIONS = {"created": "Create", "updated": "Update", "deleted": "Delete", "merged": "Merge",
                         "voided": "Void", "emailed": "Emailed", "create": "Create", "update": "Update",
                         "delete": "Delete", "merge": "Merge", "void": "Void"}


def parse_webhook(payload):
    """Normalize both webhook formats into a list of event dicts.

    Returns ``[{"realm": str, "entity": "Invoice", "id": "123",
    "operation": "Update", "deleted_id": None}]``.

    * Legacy format: ``{"eventNotifications": [{"realmId", "dataChangeEvent":
      {"entities": [{"name", "id", "operation", "lastUpdated", "deletedId"}]}}]}``
    * CloudEvents format (mandatory since 31 July 2026): a JSON array of
      envelopes with ``type = qbo.<entity>.<operation>.v1``,
      ``intuitentityid`` and ``intuitaccountid``.
    """
    events = []
    if isinstance(payload, dict) and "eventNotifications" in payload:
        for notif in payload.get("eventNotifications") or []:
            realm = str(notif.get("realmId") or "")
            for ent in (notif.get("dataChangeEvent") or {}).get("entities") or []:
                events.append({
                    "realm": realm,
                    "entity": ent.get("name"),
                    "id": str(ent.get("id") or ""),
                    "operation": ent.get("operation") or "Update",
                    "deleted_id": ent.get("deletedId"),
                })
        return events
    envelopes = payload if isinstance(payload, list) else [payload]
    for env in envelopes:
        if not isinstance(env, dict):
            continue
        etype = env.get("type") or ""
        parts = etype.split(".")
        if len(parts) < 3 or parts[0] != "qbo":
            continue
        data = env.get("data") or {}
        events.append({
            "realm": str(env.get("intuitaccountid") or ""),
            "entity": parts[1],          # lower-case, normalized by caller
            "id": str(env.get("intuitentityid") or ""),
            "operation": CLOUDEVENT_OPERATIONS.get(parts[2].lower(), parts[2].capitalize()),
            "deleted_id": data.get("deletedid") if isinstance(data, dict) else None,
        })
    return events


class QBOClient:
    """Stateless client. Token persistence is delegated to ``token_saver``."""

    def __init__(self, client_id, client_secret, realm_id=None, environment="sandbox",
                 access_token=None, refresh_token=None, token_saver=None,
                 minor_version=DEFAULT_MINOR_VERSION, session=None, logger_cb=None,
                 max_retries=4, sleep=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.realm_id = realm_id
        self.environment = environment or "sandbox"
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_saver = token_saver
        self.minor_version = minor_version or DEFAULT_MINOR_VERSION
        self.session = session or SESSION_FACTORY()
        self.logger_cb = logger_cb
        self.max_retries = max_retries
        self.sleep = sleep or (lambda seconds: _sleep(seconds))

    # ------------------------------------------------------------------ OAuth
    @staticmethod
    def authorization_url(client_id, redirect_uri, state, scopes=(SCOPE_ACCOUNTING,)):
        params = {
            "client_id": client_id,
            "response_type": "code",
            "scope": " ".join(scopes),
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return "%s?%s" % (AUTH_URL, urlencode(params, quote_via=quote))

    def _basic_auth(self):
        raw = ("%s:%s" % (self.client_id, self.client_secret)).encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _token_request(self, data):
        try:
            resp = self.session.post(
                TOKEN_URL, data=data, timeout=DEFAULT_TIMEOUT,
                headers={"Authorization": self._basic_auth(), "Accept": "application/json",
                         "Content-Type": "application/x-www-form-urlencoded"},
            )
        except requests.RequestException as exc:
            # network problem, not an authorization problem: keep the tokens, retry later
            raise QBOError("Could not reach QuickBooks (Intuit) to refresh the connection. Check the server's "
                           "internet access and try again. Details: %s" % exc) from exc
        try:
            body = resp.json()
        except ValueError:
            body = {"error": resp.text}
        if resp.status_code != 200:
            raise QBOAuthError(
                "QuickBooks token request failed: %s" % (body.get("error_description") or body.get("error")),
                status=resp.status_code, code=body.get("error"), payload=body)
        self.access_token = body.get("access_token")
        # Intuit rotates refresh tokens: ALWAYS persist the newest one.
        self.refresh_token = body.get("refresh_token") or self.refresh_token
        if self.token_saver:
            self.token_saver(body)
        return body

    def exchange_code(self, code, redirect_uri):
        return self._token_request({"grant_type": "authorization_code", "code": code,
                                    "redirect_uri": redirect_uri})

    def refresh(self):
        if not self.refresh_token:
            raise QBOAuthError("No refresh token: please connect to QuickBooks again.")
        return self._token_request({"grant_type": "refresh_token",
                                    "refresh_token": self.refresh_token})

    def revoke(self):
        token = self.refresh_token or self.access_token
        if not token:
            return True
        try:
            resp = self.session.post(
                REVOKE_URL, json={"token": token}, timeout=DEFAULT_TIMEOUT,
                headers={"Authorization": self._basic_auth(), "Accept": "application/json"})
        except requests.RequestException as exc:
            raise QBOError("Could not reach QuickBooks (Intuit) to revoke the connection: %s" % exc) from exc
        return resp.status_code in (200, 204)

    # --------------------------------------------------------------- Requests
    @property
    def base_url(self):
        return "%s/v3/company/%s" % (API_BASE[self.environment], self.realm_id)

    @staticmethod
    def _parse_fault(body):
        fault = (body or {}).get("Fault") or (body or {}).get("fault") or {}
        errors = fault.get("Error") or fault.get("error") or []
        if errors:
            err = errors[0]
            return (err.get("Message") or err.get("message") or "QuickBooks error",
                    err.get("code"), err.get("Detail") or err.get("detail"))
        return None, None, None

    def request(self, method, path, params=None, payload=None, raw=False, accept="application/json",
                content_type="application/json"):
        if not self.realm_id:
            raise QBOAuthError("QuickBooks company (realm) is not connected.")
        url = path if path.startswith("http") else "%s/%s" % (self.base_url, path.lstrip("/"))
        params = dict(params or {})
        params.setdefault("minorversion", self.minor_version)
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            _RateLimiter.acquire(self.realm_id, sleep=self.sleep)
            headers = {"Authorization": "Bearer %s" % (self.access_token or ""),
                       "Accept": accept}
            data = None
            if payload is not None:
                headers["Content-Type"] = content_type
                data = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
            started = time.monotonic()
            try:
                resp = self.session.request(method, url, params=params, data=data,
                                            headers=headers, timeout=DEFAULT_TIMEOUT)
            except requests.RequestException as exc:
                if attempt <= self.max_retries:
                    self.sleep(min(2 ** attempt, 30))
                    continue
                raise QBOError("Could not reach QuickBooks. Check the server's internet access and try "
                               "again. Details: %s" % exc) from exc
            duration = time.monotonic() - started
            status = resp.status_code
            if self.logger_cb:
                try:
                    self.logger_cb(method, url, params, payload, status,
                                   None if raw else resp.text[:20000], duration,
                                   resp.headers.get("intuit_tid"))
                except Exception:  # never break a sync because of logging
                    _logger.exception("QuickBooks request logger failed")
            if status == 401 and not refreshed:
                refreshed = True
                self.refresh()
                continue
            if status == 429 or status >= 500:
                if attempt <= self.max_retries:
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        wait = float(retry_after)
                    except (TypeError, ValueError):
                        wait = min(2 ** attempt * 2, 60)
                    self.sleep(wait)
                    continue
            if raw and status == 200:
                return resp.content
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if status >= 400 or (isinstance(body, dict) and body.get("Fault")):
                message, code, detail = self._parse_fault(body)
                exc_cls = QBOAuthError if status in (401, 403) else QBOError
                raise exc_cls("%s%s" % (message or "HTTP %s" % status,
                                        (": %s" % detail) if detail else ""),
                              status=status, code=code, detail=detail, payload=body)
            return body

    # ------------------------------------------------------------- Helpers
    def company_info(self):
        body = self.request("GET", "companyinfo/%s" % self.realm_id)
        return body.get("CompanyInfo", {})

    def preferences(self):
        return self.request("GET", "preferences").get("Preferences", {})

    def read(self, entity, qb_id, params=None):
        body = self.request("GET", "%s/%s" % (entity.lower(), qb_id), params=params)
        return body.get(entity)

    def query(self, statement):
        body = self.request("GET", "query", params={"query": statement})
        return body.get("QueryResponse", {})

    def query_all(self, entity, where=None, order_by=None, page_size=MAX_RESULTS, extra=None):
        """Yield every record of ``entity`` handling STARTPOSITION paging."""
        start = 1
        while True:
            stmt = "SELECT * FROM %s" % entity
            if where:
                stmt += " WHERE %s" % where
            if order_by:
                stmt += " ORDERBY %s" % order_by
            stmt += " STARTPOSITION %d MAXRESULTS %d" % (start, page_size)
            resp = self.query(stmt)
            records = resp.get(entity) or []
            for rec in records:
                yield rec
            if len(records) < page_size:
                break
            start += page_size

    def count(self, entity, where=None):
        stmt = "SELECT COUNT(*) FROM %s" % entity + (" WHERE %s" % where if where else "")
        return int(self.query(stmt).get("totalCount") or 0)

    def create(self, entity, payload):
        return self.request("POST", entity.lower(), payload=payload).get(entity)

    def update(self, entity, payload, sparse=True):
        payload = dict(payload)
        if sparse:
            payload["sparse"] = True
        return self.request("POST", entity.lower(), payload=payload).get(entity)

    def delete(self, entity, qb_id, sync_token):
        return self.request("POST", entity.lower(), params={"operation": "delete"},
                            payload={"Id": qb_id, "SyncToken": sync_token})

    def void(self, entity, qb_id, sync_token):
        # Invoices use operation=void; payments/sales receipts use include=void
        if entity in ("Payment", "SalesReceipt", "BillPayment"):
            params = {"operation": "update", "include": "void"}
            payload = {"Id": qb_id, "SyncToken": sync_token, "sparse": True}
        else:
            params = {"operation": "void"}
            payload = {"Id": qb_id, "SyncToken": sync_token}
        return self.request("POST", entity.lower(), params=params, payload=payload).get(entity)

    def cdc(self, entities, changed_since):
        """Change Data Capture. ``changed_since`` must be an ISO-8601 string
        no older than 30 days. Returns ``{entity: [records]}``; deleted records
        carry ``status == 'Deleted'``."""
        body = self.request("GET", "cdc", params={"entities": ",".join(entities),
                                                  "changedSince": changed_since})
        out = {}
        for cdc in body.get("CDCResponse") or []:
            for qr in cdc.get("QueryResponse") or []:
                for key, value in qr.items():
                    if isinstance(value, list):
                        out.setdefault(key, []).extend(value)
        return out

    def batch(self, operations):
        """Run up to 30 operations per call. ``operations`` is a list of
        ``{"bId": str, "operation": "create|update|delete", "<Entity>": {...}}``
        or query items. Returns a dict bId -> item response."""
        results = {}
        for i in range(0, len(operations), BATCH_SIZE):
            chunk = operations[i:i + BATCH_SIZE]
            body = self.request("POST", "batch", payload={"BatchItemRequest": chunk})
            for item in body.get("BatchItemResponse") or []:
                results[item.get("bId")] = item
        return results

    def pdf(self, entity, qb_id):
        return self.request("GET", "%s/%s/pdf" % (entity.lower(), qb_id), raw=True,
                            accept="application/pdf")

    def send(self, entity, qb_id, email=None):
        params = {"sendTo": email} if email else None
        return self.request("POST", "%s/%s/send" % (entity.lower(), qb_id), params=params,
                            payload="", content_type="application/octet-stream").get(entity)
