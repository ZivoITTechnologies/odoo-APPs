# -*- coding: utf-8 -*-
"""Endpoints EasyEcom calls.

Authentication: header ``X-CP-Token: <token>`` (or ``?token=``), shown on the EasyEcom connection.

    POST /cp/easyecom/carrier/create_shipment   EasyEcom books Cargo Pilot as the carrier of an order
    POST /cp/easyecom/carrier/cancel_shipment   EasyEcom cancels it ({"awb": ...} or {"order_id": ...})
    POST /cp/easyecom/webhook/order             order created / updated / cancelled
    POST /cp/easyecom/webhook/inventory         stock changed
"""
import json
import logging

from odoo import http, _
from odoo.exceptions import UserError
from odoo.http import request

_logger = logging.getLogger(__name__)


def _reply(data, status=200):
    return request.make_response(json.dumps(data, default=str), status=status,
                                 headers=[('Content-Type', 'application/json')])


class CpEasyecomController(http.Controller):

    def _account(self, kw):
        token = request.httprequest.headers.get('X-CP-Token') or kw.get('token') or ''
        account = request.env['cp.easyecom.account'].cp_by_token(token)
        if not account:
            return None
        return account.with_company(account.company_id).sudo()

    @staticmethod
    def _body():
        try:
            return json.loads(request.httprequest.get_data(as_text=True) or '{}')
        except ValueError:
            return None

    # ------------------------------------------------------------------
    @http.route('/cp/easyecom/carrier/create_shipment', type='http', auth='public', methods=['POST'], csrf=False)
    def create_shipment(self, **kw):
        account = self._account(kw)
        if account is None:
            return _reply({'success': False, 'message': 'Unauthorized'}, 401)
        body = self._body()
        if not isinstance(body, dict):
            return _reply({'success': False, 'message': 'Send the order as a JSON object'}, 400)
        data = body.get('order') or body.get('data') or body
        order = account.env['cp.channel.order'].cp_upsert(account, data)
        if not order:
            return _reply({'success': False, 'message': 'order_id is missing'}, 422)
        if order.trip_id and order.trip_id.state != 'cancel':
            return _reply(self._shipment_reply(order))
        try:
            with request.env.cr.savepoint():
                if order.state == 'new':
                    order.action_plan_transport()
                indent = order.indent_id
                if indent.state == 'draft':
                    indent.action_confirm()
                try:
                    with request.env.cr.savepoint():
                        indent.action_auto_allocate()
                except UserError as err:
                    indent.message_post(body=_('No contract for this order yet: %s', err.args[0] if err.args else err))
        except UserError as err:
            return _reply({'success': False, 'message': str(err.args[0] if err.args else err)}, 422)
        return _reply(self._shipment_reply(order))

    @staticmethod
    def _shipment_reply(order):
        trip = order.trip_id
        awb = order.awb_number or (trip.docket_number or trip.name if trip else order.indent_id.name)
        if not order.awb_number:
            order.awb_number = awb
        return {'success': True, 'awb': awb, 'order_id': order.external_id, 'reference_code': order.reference,
                'carrier_name': trip.transporter_id.name if trip else '', 'shipment': trip.name if trip else '',
                'load_request': order.indent_id.name or '', 'tracking_url': trip.tracking_url if trip else '',
                'status': trip.state if trip else 'planned',
                'freight': trip.order_ids.filtered(lambda o: o.order_ref == order.reference)[:1].freight_cost
                if trip else 0.0}

    @http.route('/cp/easyecom/carrier/cancel_shipment', type='http', auth='public', methods=['POST'], csrf=False)
    def cancel_shipment(self, **kw):
        account = self._account(kw)
        if account is None:
            return _reply({'success': False, 'message': 'Unauthorized'}, 401)
        body = self._body() or {}
        Order = account.env['cp.channel.order']
        domain = [('account_id', '=', account.id)]
        if body.get('awb'):
            domain.append(('awb_number', '=', str(body['awb'])))
        elif body.get('order_id'):
            domain.append(('external_id', '=', str(body['order_id'])))
        else:
            return _reply({'success': False, 'message': 'Send awb or order_id'}, 400)
        order = Order.search(domain, limit=1)
        if not order:
            return _reply({'success': False, 'message': 'Shipment not found'}, 404)
        trip = order.trip_id
        if trip and trip.state not in ('draft', 'assigned', 'reported', 'cancel'):
            return _reply({'success': False, 'message': 'The goods have already left (%s)' % trip.state}, 409)
        others = trip.order_ids.filtered(lambda o: o.order_ref != order.reference) if trip else []
        if trip and others:
            # shared truck: take only this order off it
            trip.order_ids.filtered(lambda o: o.order_ref == order.reference).unlink()
            trip.message_post(body=_('EasyEcom cancelled order %s; it was taken off this shipment.', order.reference))
        elif trip and trip.state != 'cancel':
            trip.action_cancel()
        indent = order.indent_id
        if indent and indent.state not in ('done', 'cancel') and not indent.trip_ids.filtered(
                lambda t: t.state != 'cancel'):
            indent.action_cancel()
        order.write({'state': 'cancel', 'trip_id': False, 'trip_order_id': False, 'tracking_dirty': False})
        return _reply({'success': True, 'awb': order.awb_number, 'status': 'cancelled'})

    # ------------------------------------------------------------------
    @http.route('/cp/easyecom/webhook/order', type='http', auth='public', methods=['POST'], csrf=False)
    def webhook_order(self, **kw):
        account = self._account(kw)
        if account is None:
            return _reply({'success': False, 'message': 'Unauthorized'}, 401)
        body = self._body()
        if body is None:
            return _reply({'success': False, 'message': 'Bad JSON'}, 400)
        rows = body if isinstance(body, list) else (body.get('orders') or body.get('data') or [body])
        rows = rows if isinstance(rows, list) else [rows]
        saved = account.env['cp.channel.order']
        for row in rows:
            saved |= saved.cp_upsert(account, row)
        return _reply({'success': True, 'orders': len(saved)})

    @http.route('/cp/easyecom/webhook/inventory', type='http', auth='public', methods=['POST'], csrf=False)
    def webhook_inventory(self, **kw):
        account = self._account(kw)
        if account is None:
            return _reply({'success': False, 'message': 'Unauthorized'}, 401)
        body = self._body()
        if body is None:
            return _reply({'success': False, 'message': 'Bad JSON'}, 400)
        count = account.env['cp.channel.stock'].cp_apply(account, body)
        return _reply({'success': True, 'rows': count})
