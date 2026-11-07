# -*- coding: utf-8 -*-
"""Cargo Pilot REST API v2 (JSON). Same key as v1: header ``X-CP-API-Key``.

    GET  /cp/api/v2/shipments                     list (filters: state, mode, channel, updated_since, limit, offset)
    POST /cp/api/v2/shipments                     create a load request with orders (optionally auto-allocate)
    GET  /cp/api/v2/shipments/<name>              one shipment with orders, position and timeline
    POST /cp/api/v2/shipments/<name>/events       assign | accept | decline | report | dispatch | arrive | deliver | cancel
    POST /cp/api/v2/shipments/<name>/pod          proof of delivery (signature / photos as base64)
    GET  /cp/api/v2/orders/<order_ref>/freight    freight cost of an order across shipments
    GET  /cp/api/v2/freight-bills                 freight bills (filter: state)
    GET  /cp/api/v2/transporters/scorecard        transporter scorecards
    GET  /cp/api/v2/openapi.json                  this API as OpenAPI 3 (no key needed)
"""
import base64
import json

from odoo import fields, http, _
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from .api import CargoPilotApi


def _ok(data, status=200):
    return request.make_response(json.dumps(data, default=str), status=status,
                                 headers=[('Content-Type', 'application/json')])


def _err(message, status=400, code='bad_request'):
    return _ok({'error': {'code': code, 'message': message}}, status)


class CargoPilotApiV2(CargoPilotApi):

    def _env(self):
        company = self._authenticate()
        return company.env if company else None

    # ------------------------------------------------------------------
    @http.route('/cp/api/v2/shipments', type='http', auth='public', methods=['GET', 'POST'], csrf=False)
    def v2_shipments(self, **kw):
        env = self._env()
        if env is None:
            return _err('Missing or wrong X-CP-API-Key', 401, 'unauthorized')
        if request.httprequest.method == 'POST':
            return self._v2_create(env)
        company_id = env.context['allowed_company_ids'][0]
        domain = [('company_id', '=', company_id)]
        for key in ('state', 'channel'):
            if kw.get(key):
                domain.append((key, '=', kw[key]))
        if kw.get('mode'):
            domain.append(('shipment_mode', '=', kw['mode']))
        if kw.get('updated_since'):
            domain.append(('write_date', '>=', kw['updated_since']))
        try:
            limit = min(int(kw.get('limit') or 50), 200)
            offset = int(kw.get('offset') or 0)
        except ValueError:
            return _err('limit and offset must be numbers')
        Trip = env['cp.trip']
        trips = Trip.search(domain, limit=limit, offset=offset, order='write_date desc, id desc')
        return _ok({'count': Trip.search_count(domain), 'limit': limit, 'offset': offset,
                    'results': [t.cp_api_dict() for t in trips]})

    def _v2_create(self, env):
        data = self._payload()
        if data is None:
            return _err('Body must be a JSON object')
        Location = env['cp.location']
        company_id = env.context['allowed_company_ids'][0]

        def location(code):
            return Location.search(['|', ('code', '=', code), ('name', '=', code), ('company_id', 'in', [company_id, False])],
                                   limit=1) if code else Location
        origin, dest = location(data.get('origin')), location(data.get('destination'))
        if not origin or not dest:
            return _err('origin and destination must be existing location codes', 422, 'unknown_location')
        lane = env['cp.lane'].search([('origin_id', '=', origin.id), ('destination_id', '=', dest.id)], limit=1)
        if not lane:
            lane = env['cp.lane'].create({'origin_id': origin.id, 'destination_id': dest.id})
            lane._onchange_points()
        mode = data.get('mode') or 'ftl'
        vtype = env['cp.vehicle.type'].search([('name', '=ilike', data.get('vehicle_type') or '')], limit=1) \
            if data.get('vehicle_type') else env['cp.vehicle.type']
        if mode == 'ftl' and not vtype:
            return _err('vehicle_type is required for FTL and must match a truck type name', 422, 'unknown_vehicle_type')
        Partner = env['res.partner']
        lines = []
        for order in data.get('orders') or []:
            customer = Partner.search([('name', '=ilike', order.get('customer'))], limit=1) \
                if order.get('customer') else Partner
            serials = order.get('serial_numbers') or []
            lines.append((0, 0, {
                'description': order.get('description') or order.get('sku') or order.get('order_ref'),
                'order_ref': order.get('order_ref'), 'invoice_ref': order.get('invoice_ref'),
                'customer_id': customer.id, 'quantity': order.get('quantity') or 1,
                'weight_ton': float(order.get('weight_kg') or 0.0) / 1000.0,
                'volume_cbm': order.get('volume_cbm') or 0.0, 'order_value': order.get('order_value') or 0.0,
                'serial_numbers': '\n'.join(serials) if isinstance(serials, list) else serials,
                'eway_bill_no': order.get('eway_bill_no'), 'drop_point': order.get('drop_point'),
            }))
        try:
            indent = env['cp.indent'].create({
                'lane_id': lane.id, 'shipment_mode': mode, 'channel': data.get('channel'),
                'vehicle_type_id': vtype.id, 'vehicle_count': 1 if mode == 'ltl' else int(data.get('vehicle_count') or 1),
                'required_date': data.get('required_date') or fields.Datetime.now(),
                'origin': data.get('reference'), 'source_channel': 'api', 'line_ids': lines,
            })
            indent.action_confirm()
            if data.get('auto_allocate'):
                indent.action_auto_allocate()
        except (UserError, ValidationError) as exc:
            return _err(str(exc.args[0]), 422, 'not_processed')
        return _ok({'load_request': indent.name, 'state': indent.state,
                    'shipments': [t.cp_api_dict(full=True) for t in indent.trip_ids]}, 201)

    # ------------------------------------------------------------------
    def _v2_trip(self, env, name):
        company_id = env.context['allowed_company_ids'][0]
        return env['cp.trip'].search([('name', '=', name), ('company_id', '=', company_id)], limit=1)

    @http.route('/cp/api/v2/shipments/<path:name>', type='http', auth='public', methods=['GET'], csrf=False)
    def v2_shipment(self, name, **kw):
        if name.endswith('/events') or name.endswith('/pod'):
            return _err('Use POST for this endpoint', 405, 'method_not_allowed')
        env = self._env()
        if env is None:
            return _err('Missing or wrong X-CP-API-Key', 401, 'unauthorized')
        trip = self._v2_trip(env, name)
        if not trip:
            return _err('Shipment not found', 404, 'not_found')
        return _ok(trip.cp_api_dict(full=True))

    @http.route('/cp/api/v2/shipments/<path:name>/events', type='http', auth='public', methods=['POST'], csrf=False)
    def v2_shipment_event(self, name, **kw):
        env = self._env()
        if env is None:
            return _err('Missing or wrong X-CP-API-Key', 401, 'unauthorized')
        trip = self._v2_trip(env, name)
        if not trip:
            return _err('Shipment not found', 404, 'not_found')
        data = self._payload() or {}
        action = data.get('action')
        vals = {k: data[k] for k in ('vehicle_number', 'driver_name', 'driver_phone', 'docket_number', 'eway_bill_no')
                if data.get(k)}
        try:
            if vals:
                trip.write(vals)
            if action == 'accept':
                trip._cp_accept_pending_offer('api')
            elif action == 'decline':
                offer = trip.offer_ids.filtered(lambda o: o.state == 'pending')[:1]
                if not offer:
                    raise UserError(_('No open offer on this shipment.'))
                offer.action_decline(data.get('reason') or 'other', data.get('note'), 'api')
            elif action:
                method = {'assign': 'action_assign', 'report': 'action_report', 'dispatch': 'action_dispatch',
                          'arrive': 'action_mark_arrived', 'deliver': 'action_mark_delivered',
                          'cancel': 'action_cancel'}.get(action)
                if not method:
                    return _err('Unknown action %s' % action, 422, 'unknown_action')
                getattr(trip, method)()
        except (UserError, ValidationError) as exc:
            return _err(str(exc.args[0]), 409, 'not_allowed')
        return _ok(trip.cp_api_dict(full=True))

    @http.route('/cp/api/v2/shipments/<path:name>/pod', type='http', auth='public', methods=['POST'], csrf=False)
    def v2_shipment_pod(self, name, **kw):
        env = self._env()
        if env is None:
            return _err('Missing or wrong X-CP-API-Key', 401, 'unauthorized')
        trip = self._v2_trip(env, name)
        if not trip:
            return _err('Shipment not found', 404, 'not_found')
        data = self._payload() or {}
        if trip.state not in ('in_transit', 'arrived', 'delivered'):
            return _err('A POD can be recorded once the shipment is in transit', 409, 'not_allowed')
        attachments = env['ir.attachment']
        for index, photo in enumerate(data.get('photos') or []):
            try:
                base64.b64decode(photo, validate=True)
            except (ValueError, TypeError):
                return _err('photos must be base64 strings', 422, 'bad_photo')
            attachments |= attachments.create({'name': '%s-pod-%s.jpg' % (trip.name, index + 1), 'datas': photo,
                                               'res_model': 'cp.epod'})
        serials = data.get('serial_numbers') or []
        epod = env['cp.epod'].create({
            'trip_id': trip.id, 'channel': 'api', 'receiver_name': data.get('receiver_name'),
            'receiver_phone': data.get('receiver_phone'),
            'received_weight_ton': float(data.get('received_weight_kg') or 0.0) / 1000.0 or trip.loaded_weight_ton,
            'received_packages': int(data.get('received_packages') or 0),
            'shortage_packages': int(data.get('shortage_packages') or 0),
            'damaged_packages': int(data.get('damaged_packages') or 0),
            'refused_by_consignee': bool(data.get('refused')), 'remarks': data.get('remarks'),
            'latitude': data.get('latitude') or 0.0, 'longitude': data.get('longitude') or 0.0,
            'signature': data.get('signature') or False, 'grn_number': data.get('grn_number'),
            'scanned_serials': '\n'.join(serials) if isinstance(serials, list) else serials,
            'photo_ids': [(6, 0, attachments.ids)],
        })
        attachments.write({'res_id': epod.id})
        epod.action_submit()
        return _ok({'pod': epod.name, 'state': epod.state, 'issues': epod.exception_reason or None,
                    'shipment': trip.cp_api_dict()}, 201)

    # ------------------------------------------------------------------
    @http.route('/cp/api/v2/orders/<path:order_ref>/freight', type='http', auth='public', methods=['GET'], csrf=False)
    def v2_order_freight(self, order_ref, **kw):
        env = self._env()
        if env is None:
            return _err('Missing or wrong X-CP-API-Key', 401, 'unauthorized')
        company_id = env.context['allowed_company_ids'][0]
        orders = env['cp.trip.order'].search(['|', ('order_ref', '=', order_ref), ('invoice_ref', '=', order_ref),
                                              ('company_id', '=', company_id), ('trip_state', '!=', 'cancel')])
        result = {'order_ref': order_ref, 'freight_cost': round(sum(orders.mapped('freight_cost')), 2),
                  'order_value': sum(orders.mapped('order_value')), 'lines': [o.cp_api_dict() for o in orders]}
        if 'cp.parcel' in env:
            parcels = env['cp.parcel'].search([('reference', '=', order_ref), ('state', '!=', 'cancelled'),
                                               ('company_id', '=', company_id)])
            result['courier'] = [{'parcel': p.name, 'awb': p.awb, 'courier': p.courier_name,
                                  'cost': p.shipping_cost, 'state': p.state} for p in parcels]
            result['freight_cost'] = round(result['freight_cost'] + sum(parcels.mapped('shipping_cost')), 2)
        return _ok(result)

    @http.route('/cp/api/v2/freight-bills', type='http', auth='public', methods=['GET'], csrf=False)
    def v2_freight_bills(self, **kw):
        env = self._env()
        if env is None:
            return _err('Missing or wrong X-CP-API-Key', 401, 'unauthorized')
        company_id = env.context['allowed_company_ids'][0]
        domain = [('company_id', '=', company_id)]
        if kw.get('state'):
            domain.append(('state', '=', kw['state']))
        bills = env['cp.freight.bill'].search(domain, limit=min(int(kw.get('limit') or 50), 200))
        return _ok({'results': [b.cp_api_dict() for b in bills]})

    @http.route('/cp/api/v2/transporters/scorecard', type='http', auth='public', methods=['GET'], csrf=False)
    def v2_scorecard(self, **kw):
        env = self._env()
        if env is None:
            return _err('Missing or wrong X-CP-API-Key', 401, 'unauthorized')
        partners = env['res.partner'].search([('cp_is_transporter', '=', True), ('is_company', '=', True)])
        return _ok({'results': [{
            'name': p.name, 'score': p.cp_score, 'grade': p.cp_score_grade, 'trips': p.cp_trip_count,
            'acceptance_pct': round(p.cp_acceptance_rate, 1), 'placement_on_time_pct': round(p.cp_placement_rate, 1),
            'on_time_pct': round(p.cp_on_time_rate, 1), 'damage_free_pct': round(p.cp_damage_free_rate, 1),
            'pod_turnaround_hours': round(p.cp_pod_tat_hours, 1), 'billing_accuracy_pct': round(p.cp_billing_accuracy, 1),
        } for p in partners]})

    @http.route('/cp/api/v2/openapi.json', type='http', auth='public', methods=['GET'], csrf=False)
    def v2_openapi(self, **kw):
        base = request.httprequest.host_url.rstrip('/')
        shipment = {'type': 'object', 'properties': {k: {'type': 'string'} for k in (
            'name', 'state', 'mode', 'channel', 'lane', 'origin', 'destination', 'transporter', 'vehicle_number',
            'docket_number', 'eway_bill_no', 'eta', 'tracking_url', 'acceptance')}}
        order = {'type': 'object', 'required': ['order_ref'], 'properties': {
            'order_ref': {'type': 'string'}, 'invoice_ref': {'type': 'string'}, 'customer': {'type': 'string'},
            'quantity': {'type': 'number'}, 'weight_kg': {'type': 'number'}, 'volume_cbm': {'type': 'number'},
            'order_value': {'type': 'number'}, 'serial_numbers': {'type': 'array', 'items': {'type': 'string'}},
            'eway_bill_no': {'type': 'string'}, 'drop_point': {'type': 'string'}}}
        key = [{'ApiKey': []}]

        def op(summary, body=None, params=None):
            spec = {'summary': summary, 'security': key, 'responses': {'200': {'description': 'OK'}}}
            if body:
                spec['requestBody'] = {'required': True, 'content': {'application/json': {'schema': body}}}
            if params:
                spec['parameters'] = [{'name': p, 'in': where, 'required': where == 'path', 'schema': {'type': 'string'}}
                                      for p, where in params]
            return spec
        spec = {
            'openapi': '3.0.3',
            'info': {'title': 'Cargo Pilot TMS API', 'version': '2.0'},
            'servers': [{'url': base}],
            'components': {'securitySchemes': {'ApiKey': {'type': 'apiKey', 'in': 'header', 'name': 'X-CP-API-Key'}},
                           'schemas': {'Shipment': shipment, 'Order': order}},
            'paths': {
                '/cp/api/v2/shipments': {
                    'get': op('List shipments', params=[('state', 'query'), ('mode', 'query'), ('channel', 'query'),
                                                        ('updated_since', 'query'), ('limit', 'query'), ('offset', 'query')]),
                    'post': op('Create a load request with its orders', {'type': 'object', 'required': ['origin', 'destination'],
                               'properties': {'origin': {'type': 'string'}, 'destination': {'type': 'string'},
                                              'mode': {'type': 'string', 'enum': ['ftl', 'ltl']},
                                              'channel': {'type': 'string'}, 'vehicle_type': {'type': 'string'},
                                              'vehicle_count': {'type': 'integer'}, 'required_date': {'type': 'string'},
                                              'reference': {'type': 'string'}, 'auto_allocate': {'type': 'boolean'},
                                              'orders': {'type': 'array', 'items': {'$ref': '#/components/schemas/Order'}}}})},
                '/cp/api/v2/shipments/{name}': {'get': op('One shipment', params=[('name', 'path')])},
                '/cp/api/v2/shipments/{name}/events': {'post': op('Move a shipment on', {'type': 'object', 'properties': {
                    'action': {'type': 'string', 'enum': ['assign', 'accept', 'decline', 'report', 'dispatch', 'arrive',
                                                          'deliver', 'cancel']},
                    'vehicle_number': {'type': 'string'}, 'driver_name': {'type': 'string'},
                    'driver_phone': {'type': 'string'}, 'docket_number': {'type': 'string'},
                    'eway_bill_no': {'type': 'string'}, 'reason': {'type': 'string'}}}, [('name', 'path')])},
                '/cp/api/v2/shipments/{name}/pod': {'post': op('Record proof of delivery', {'type': 'object', 'properties': {
                    'receiver_name': {'type': 'string'}, 'received_packages': {'type': 'integer'},
                    'shortage_packages': {'type': 'integer'}, 'damaged_packages': {'type': 'integer'},
                    'serial_numbers': {'type': 'array', 'items': {'type': 'string'}}, 'grn_number': {'type': 'string'},
                    'latitude': {'type': 'number'}, 'longitude': {'type': 'number'},
                    'signature': {'type': 'string', 'format': 'byte'},
                    'photos': {'type': 'array', 'items': {'type': 'string', 'format': 'byte'}}}}, [('name', 'path')])},
                '/cp/api/v2/orders/{order_ref}/freight': {'get': op('Freight cost of one order', params=[('order_ref', 'path')])},
                '/cp/api/v2/freight-bills': {'get': op('Freight bills', params=[('state', 'query')])},
                '/cp/api/v2/transporters/scorecard': {'get': op('Transporter scorecards')},
            },
        }
        return _ok(spec)
