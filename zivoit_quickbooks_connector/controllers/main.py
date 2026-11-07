# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
import json
import logging

from werkzeug.utils import redirect

from odoo import http
from odoo.http import request

from ..lib.qbo_client import QBOError

_logger = logging.getLogger(__name__)


class QuickBooksController(http.Controller):

    @http.route("/zivoit_qbo/oauth/callback", type="http", auth="user", methods=["GET"])
    def oauth_callback(self, code=None, state=None, realmId=None, error=None, **kw):
        Connection = request.env["qb.connection"]
        conn = Connection._check_oauth_state(state)
        if not conn:
            return request.make_response("Invalid or expired authorization state. Please retry from Odoo.",
                                         status=400)
        url = "/odoo/quickbooks/%s" % conn.id
        if error or not code or not realmId:
            conn.sudo().write({"state": "error", "last_error": error or "Authorization was cancelled."})
            return redirect(url)
        try:
            conn._oauth_callback(code, realmId)
        except QBOError as exc:
            _logger.warning("QuickBooks OAuth callback failed: %s", exc)
            conn.sudo().write({"state": "error", "last_error": str(exc)})
        return redirect(url)

    @http.route("/zivoit_qbo/webhook", type="http", auth="public", methods=["POST"], csrf=False,
                save_session=False)
    def webhook(self, **kw):
        raw = request.httprequest.get_data()
        signature = request.httprequest.headers.get("intuit-signature")
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return request.make_response("bad payload", status=400)
        try:
            request.env["qb.connection"].sudo()._handle_webhook(raw, signature, payload)
        except Exception:  # pylint: disable=broad-except
            # Always acknowledge: Intuit retries aggressively; CDC will catch up anyway.
            _logger.exception("QuickBooks webhook processing failed")
        return request.make_response("OK", status=200)
