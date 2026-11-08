import base64
import hmac
import json
import logging

from psycopg2 import IntegrityError

from odoo import http
from odoo.http import request, Response
from odoo.tools.mimetypes import guess_mimetype

_logger = logging.getLogger(__name__)

MAX_BODY = 5 * 1024 * 1024


class ZivoShopifyController(http.Controller):

    @http.route("/zivoit/shopify/webhook/<string:token>", type="http", auth="public", methods=["POST"], csrf=False, save_session=False)
    def webhook(self, token, **_kwargs):
        env = request.env(su=True)
        instance = env["zivo.shopify.instance"].search([("webhook_token", "=", token)], limit=1) if len(token) == 32 else None
        if not instance:
            return Response("Unknown endpoint", status=404)
        raw = request.httprequest.get_data(cache=False, as_text=False)
        if len(raw) > MAX_BODY:
            return Response("Payload too large", status=413)
        if not instance._verify_webhook(raw, request.httprequest.headers.get("X-Shopify-Hmac-Sha256")):
            _logger.warning("Rejected Shopify webhook with a bad signature for store %s", instance.id)
            return Response("Invalid signature", status=401)
        headers = request.httprequest.headers
        topic = (headers.get("X-Shopify-Topic") or "").upper().replace("/", "_")
        event_id = headers.get("X-Shopify-Event-Id") or headers.get("X-Shopify-Webhook-Id")
        if event_id:
            Receipt = env["zivo.shopify.webhook.receipt"]
            if Receipt.search_count([("instance_id", "=", instance.id), ("event_id", "=", event_id)], limit=1):
                return Response("Duplicate", status=200)
            try:
                with env.cr.savepoint():
                    Receipt.create({"instance_id": instance.id, "event_id": event_id, "topic": topic})
            except IntegrityError:
                return Response("Duplicate", status=200)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return Response("Invalid JSON", status=400)
        env["zivo.shopify.webhook"]._dispatch(instance, topic, body, event_id)
        return Response("OK", status=200)

    @http.route("/zivoit/shopify/image/<int:template_id>/<string:signature>", type="http", auth="public", methods=["GET"], csrf=False, save_session=False)
    def image(self, template_id, signature, **_kwargs):
        env = request.env(su=True)
        expected = env["zivo.shopify.product"]._image_signature(template_id)
        if not hmac.compare_digest(expected, signature):
            return Response("Not found", status=404)
        template = env["product.template"].browse(template_id).exists()
        if not template or not template.image_1920:
            return Response("Not found", status=404)
        content = base64.b64decode(template.image_1920)
        return Response(content, status=200, content_type=guess_mimetype(content, default="image/png"))
