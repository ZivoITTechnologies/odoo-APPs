# -*- coding: utf-8 -*-
import hashlib
import hmac
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class CpWhatsappWebhook(http.Controller):

    @http.route('/cp/whatsapp/webhook', type='http', auth='public', methods=['GET'], csrf=False)
    def verify(self, **kw):
        """Meta checks the webhook once with hub.mode=subscribe, hub.verify_token and hub.challenge."""
        token = kw.get('hub.verify_token') or ''
        accounts = request.env['cp.whatsapp.account'].sudo().search([('verify_token', '!=', False)])
        if kw.get('hub.mode') == 'subscribe' and token and any(
                hmac.compare_digest(acc.verify_token, token) for acc in accounts):
            return request.make_response(kw.get('hub.challenge') or '', headers=[('Content-Type', 'text/plain')])
        return request.make_response('Forbidden', status=403)

    @http.route('/cp/whatsapp/webhook', type='http', auth='public', methods=['POST'], csrf=False)
    def receive(self, **kw):
        raw = request.httprequest.get_data() or b''
        Account = request.env['cp.whatsapp.account'].sudo()
        secrets = [s for s in Account.search([]).mapped('app_secret') if s]
        if secrets:
            signature = request.httprequest.headers.get('X-Hub-Signature-256', '')
            expected = ['sha256=' + hmac.new(s.encode(), raw, hashlib.sha256).hexdigest() for s in secrets]
            if not any(hmac.compare_digest(signature, e) for e in expected):
                return request.make_response('Bad signature', status=401)
        try:
            payload = json.loads(raw.decode() or '{}')
        except ValueError:
            return request.make_response('Bad JSON', status=400)
        Message = request.env['cp.whatsapp.message'].sudo()
        updated = 0
        for entry in payload.get('entry') or []:
            for change in entry.get('changes') or []:
                for status in (change.get('value') or {}).get('statuses') or []:
                    errors = status.get('errors') or [{}]
                    if Message.cp_apply_status(status.get('id'), status.get('status'),
                                               errors[0].get('title') or errors[0].get('message')):
                        updated += 1
        return request.make_response(json.dumps({'updated': updated}), headers=[('Content-Type', 'application/json')])
