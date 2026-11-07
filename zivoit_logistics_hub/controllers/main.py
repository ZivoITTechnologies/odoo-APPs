# -*- coding: utf-8 -*-
import hmac
import json
import logging

from odoo import http
from odoo.http import request

from ..providers.base import NORMALIZED_STATUSES, NotSupported, ProviderError

_logger = logging.getLogger(__name__)

PROGRESS = ['booked', 'picked_up', 'in_transit', 'out_for_delivery', 'delivered']


class LogisticsHubController(http.Controller):

    # ------------------------------------------------------------------ webhooks
    @http.route('/zivo_logistics/webhook/<int:account_id>/<string:token>', type='http', auth='public',
                methods=['POST'], csrf=False, save_session=False)
    def webhook(self, account_id, token, **kwargs):
        account = request.env['zivo.logistics.account'].sudo().browse(account_id).exists()
        if not account or not account.webhook_token or not hmac.compare_digest(account.webhook_token, token):
            return request.make_json_response({'status': 'forbidden'}, status=403)
        raw = request.httprequest.get_data(as_text=True) or '{}'
        try:
            payload = json.loads(raw)
        except ValueError:
            return request.make_json_response({'status': 'error', 'message': 'invalid JSON'}, status=400)
        payloads = payload if isinstance(payload, list) else [payload]
        provider = account._get_provider('zivo.logistics.account', account.id)
        Shipment = request.env['zivo.logistics.shipment'].sudo()
        updated, unknown = 0, []
        for item in payloads:
            try:
                results = provider.parse_webhook(item, dict(request.httprequest.headers))
            except NotSupported:
                return request.make_json_response({'status': 'error', 'message': 'webhooks not supported'}, status=400)
            except (ProviderError, ValueError, KeyError, TypeError) as exc:
                _logger.warning('ZivoIT webhook parse error for %s: %s', account.name, exc)
                continue
            for result in results:
                shipment = Shipment.search([('account_id', '=', account.id),
                                            ('tracking_number', '=', result.tracking_number)], limit=1)
                if not shipment:
                    unknown.append(result.tracking_number)
                    continue
                shipment.with_company(shipment.company_id)._apply_tracking(result, source='webhook')
                updated += 1
        account._log_api_call({'provider': account.provider, 'operation': 'webhook', 'method': 'POST',
                               'url': request.httprequest.path.rsplit('/', 1)[0] + '/***',
                               'request_body': raw[:20000], 'response_status': 200, 'success': True,
                               'response_body': json.dumps({'updated': updated, 'unknown': unknown})})
        return request.make_json_response({'status': 'ok', 'updated': updated, 'unknown': unknown})

    # ------------------------------------------------------------------ public tracking
    @http.route('/logistics/track/<string:token>', type='http', auth='public', website=False, sitemap=False)
    def tracking_page(self, token, **kwargs):
        shipment = request.env['zivo.logistics.shipment'].sudo().search([('access_token', '=', token)], limit=1)
        if not shipment or len(token) < 16:
            return request.render('zivoit_logistics_hub.tracking_lookup', {'error': 'Shipment not found.'}, status=404)
        return request.render('zivoit_logistics_hub.tracking_page', self._tracking_values(shipment))

    @http.route('/logistics/track', type='http', auth='public', methods=['GET', 'POST'], csrf=False, sitemap=False)
    def tracking_lookup(self, awb=None, phone=None, **kwargs):
        if not awb:
            return request.render('zivoit_logistics_hub.tracking_lookup', {'error': False})
        awb = awb.strip()
        phone_digits = ''.join(ch for ch in (phone or '') if ch.isdigit())
        shipment = request.env['zivo.logistics.shipment'].sudo().search([('tracking_number', '=', awb)], limit=1)
        if not shipment or len(phone_digits) < 4 or not (shipment.dest_phone or '').endswith(phone_digits[-4:]):
            return request.render('zivoit_logistics_hub.tracking_lookup',
                                  {'error': 'No shipment matches this AWB and phone number.', 'awb': awb})
        return request.redirect('/logistics/track/%s' % shipment.access_token)

    @staticmethod
    def _tracking_values(shipment):
        labels = dict(NORMALIZED_STATUSES)
        state = shipment.state
        if state in PROGRESS:
            reached = PROGRESS.index(state)
        elif state in ('pickup_scheduled',):
            reached = 0
        elif state in ('ndr', 'exception'):
            reached = 3
        else:
            reached = -1
        steps = [{'label': labels[s], 'done': i <= reached, 'current': i == reached} for i, s in enumerate(PROGRESS)]
        promise = shipment.edd or shipment.ai_edd
        return {
            'shipment': shipment, 'company': shipment.company_id, 'state_label': labels.get(state, state),
            'steps': steps, 'events': shipment.event_ids.sorted('event_time', reverse=True),
            'labels': labels, 'promise': promise,
            'alert': state in ('ndr', 'exception') or state.startswith('rto') or state in ('cancelled', 'lost'),
        }
