# -*- coding: utf-8 -*-
import hmac
import json
import logging

import pytz
from markupsafe import Markup

from odoo import http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from ..providers.base import NORMALIZED_STATUSES, NotSupported, ProviderError

_logger = logging.getLogger(__name__)

PROGRESS = ['booked', 'picked_up', 'in_transit', 'out_for_delivery', 'delivered']
DOCTYPE = Markup('<!DOCTYPE html>')


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
            return request.render('zivoit_logistics_hub.tracking_lookup', {'error': 'Shipment not found.', 'doctype': DOCTYPE}, status=404)
        return request.render('zivoit_logistics_hub.tracking_page', self._tracking_values(shipment))

    @http.route('/logistics/track', type='http', auth='public', methods=['GET', 'POST'], csrf=False, sitemap=False)
    def tracking_lookup(self, awb=None, phone=None, **kwargs):
        if not awb:
            return request.render('zivoit_logistics_hub.tracking_lookup', {'error': False, 'doctype': DOCTYPE})
        awb = awb.strip()
        phone_digits = ''.join(ch for ch in (phone or '') if ch.isdigit())
        shipment = request.env['zivo.logistics.shipment'].sudo().search([('tracking_number', '=', awb)], limit=1)
        if not shipment or len(phone_digits) < 4 or not (shipment.dest_phone or '').endswith(phone_digits[-4:]):
            return request.render('zivoit_logistics_hub.tracking_lookup',
                                  {'error': 'No shipment matches this AWB and phone number.', 'awb': awb, 'doctype': DOCTYPE})
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
        tz = pytz.timezone(shipment.company_id.partner_id.tz or shipment.user_id.tz or 'Asia/Kolkata')

        def local(dt, fmt):
            return pytz.utc.localize(dt).astimezone(tz).strftime(fmt) if dt else ''
        promise = shipment.edd or shipment.ai_edd
        events = [{'label': labels.get(ev.normalized_status) or ev.raw_status, 'location': ev.location,
                   'description': ev.description if (ev.description or '').lower() != (labels.get(ev.normalized_status) or '').lower() else '',
                   'when': local(ev.event_time, '%d %b %Y, %I:%M %p')}
                  for ev in shipment.event_ids.sorted('event_time', reverse=True)]
        return {
            'doctype': DOCTYPE, 'shipment': shipment, 'company': shipment.company_id, 'state_label': labels.get(state, state),
            'steps': steps, 'events': events, 'labels': labels, 'promise': local(promise, '%a, %d %b %Y'),
            'delivered_on': local(shipment.delivered_at, '%a, %d %b %Y'),
            'alert': state in ('ndr', 'exception') or state.startswith('rto') or state in ('cancelled', 'lost'),
        }


class LogisticsReturnsController(http.Controller):
    """Branded customer returns portal: lookup order -> choose products & reason -> RMA with reverse pickup."""

    @staticmethod
    def _contact_ok(order, contact):
        contact = (contact or '').strip().lower()
        if not contact:
            return False
        partners = order.partner_id | order.partner_shipping_id | order.partner_invoice_id
        if '@' in contact:
            return any((p.email or '').strip().lower() == contact for p in partners)
        digits = ''.join(ch for ch in contact if ch.isdigit())
        if len(digits) < 4:
            return False
        phones = [''.join(ch for ch in (p.phone or '') if ch.isdigit()) for p in partners]
        return any(ph and ph.endswith(digits[-4:]) for ph in phones)

    @staticmethod
    def _order_from_token(order_id, access):
        order = request.env['sale.order'].sudo().browse(int(order_id or 0)).exists()
        if not order or not access or not order.access_token or not hmac.compare_digest(order.access_token, access):
            return None
        return order

    def _render_lookup(self, error=False, order_ref=''):
        return request.render('zivoit_logistics_hub.returns_lookup', {'doctype': DOCTYPE, 'error': error, 'order_ref': order_ref})

    def _render_select(self, order, error=False):
        Return = request.env['zivo.logistics.return'].sudo()
        returnable = Return._returnable_lines(order)
        from ..models.operations import RESOLUTIONS, RETURN_REASONS
        return request.render('zivoit_logistics_hub.returns_select', {
            'doctype': DOCTYPE, 'order': order, 'company': order.company_id, 'error': error,
            'lines': [{'line': line, 'max': qty} for line, qty in returnable.items()], 'reasons': RETURN_REASONS,
            'resolutions': RESOLUTIONS, 'access': order._portal_ensure_token(),
            'window': Return._policy()['window'],
            'existing': Return.search([('sale_id', '=', order.id)], order='id desc'),
            'state_labels': dict(Return._fields['state'].selection),
        })

    @http.route('/logistics/returns', type='http', auth='public', methods=['GET', 'POST'], csrf=False, sitemap=False)
    def returns_lookup(self, order=None, contact=None, **kwargs):
        if request.httprequest.method == 'GET' or not order:
            return self._render_lookup()
        ref = order.strip()
        so = request.env['sale.order'].sudo().search([('name', '=ilike', ref), ('state', '=', 'sale')], limit=1)
        if not so or not self._contact_ok(so, contact):
            return self._render_lookup('We could not find an order with this number and email / phone.', ref)
        return self._render_select(so)

    @http.route('/logistics/returns/submit', type='http', auth='public', methods=['POST'], csrf=True, sitemap=False)
    def returns_submit(self, order_id=None, access=None, reason=None, comment=None, resolution=None, **kwargs):
        so = self._order_from_token(order_id, access)
        if not so:
            return self._render_lookup('Your session expired, please look up the order again.')
        quantities = {}
        for key, value in kwargs.items():
            if key.startswith('qty_') and key[4:].isdigit():
                try:
                    quantities[int(key[4:])] = float(value or 0)
                except ValueError:
                    continue
        try:
            ret = request.env['zivo.logistics.return'].sudo()._portal_submit(so, quantities, reason, (comment or '')[:2000],
                                                                           resolution)
        except Exception as exc:  # noqa: BLE001 - shown to the customer
            if not isinstance(exc, (UserError, ValidationError)):
                _logger.exception('ZivoIT return portal error')
                exc = 'The request could not be created. Please contact us.'
            return self._render_select(so, str(exc))
        return request.redirect('/logistics/returns/status/%s' % ret.access_token)

    @http.route('/logistics/returns/status/<string:token>', type='http', auth='public', sitemap=False)
    def returns_status(self, token, **kwargs):
        ret = request.env['zivo.logistics.return'].sudo().search([('access_token', '=', token)], limit=1)
        if not ret or len(token) < 16:
            return self._render_lookup('Return request not found.')
        labels = dict(ret._fields['state'].selection)
        return request.render('zivoit_logistics_hub.returns_status', {
            'doctype': DOCTYPE, 'ret': ret, 'company': ret.company_id, 'state_label': labels.get(ret.state),
            'reason_label': dict(ret._fields['reason'].selection).get(ret.reason),
            'resolution_label': dict(ret._fields['resolution'].selection).get(ret.resolution),
            'has_label': bool(ret.shipment_id.label_attachment_id),
        })

    @http.route('/logistics/returns/label/<string:token>', type='http', auth='public', sitemap=False)
    def returns_label(self, token, **kwargs):
        ret = request.env['zivo.logistics.return'].sudo().search([('access_token', '=', token)], limit=1)
        attachment = ret.shipment_id.label_attachment_id if ret and len(token) >= 16 else None
        if not attachment:
            raise request.not_found()
        return request.make_response(attachment.raw, headers=[
            ('Content-Type', 'application/pdf'),
            ('Content-Disposition', 'inline; filename="Return-%s.pdf"' % (ret.name or 'label').replace('/', '-'))])
