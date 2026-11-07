# -*- coding: utf-8 -*-
"""Xpressbees shipping API (``https://shipment.xpressbees.com``).

Auth: POST /api/users/login (email/password) -> bearer token.
"""
from datetime import timedelta

from .base import (BaseProvider, ProviderError, RateOption, ServiceabilityResult, ShipmentResult,
                   TrackingEvent, TrackingResult, parse_datetime, register)


@register
class XpressbeesProvider(BaseProvider):
    code = 'xpressbees'
    name = 'Xpressbees'
    production_url = 'https://shipment.xpressbees.com'
    sandbox_url = 'https://shipment.xpressbees.com'
    tracking_url = 'https://www.xpressbees.com/shipment/tracking?awbNo={awb}'
    capabilities = {'auth', 'serviceability', 'rate', 'create', 'track', 'cancel', 'ndr'}
    credential_fields = ('username', 'password')
    token_lifetime = timedelta(hours=11)

    STATUS_MAP = {
        'booked': 'booked', 'pending pickup': 'pickup_scheduled', 'pp': 'pickup_scheduled',
        'picked up': 'picked_up', 'pickedup': 'picked_up', 'in transit': 'in_transit', 'it': 'in_transit',
        'out for delivery': 'out_for_delivery', 'ofd': 'out_for_delivery', 'delivered': 'delivered', 'dl': 'delivered',
        'exception': 'ndr', 'ex': 'ndr', 'undelivered': 'ndr',
        'rto': 'rto_initiated', 'rt': 'rto_in_transit', 'rto in transit': 'rto_in_transit',
        'rto delivered': 'rto_delivered', 'rto-dl': 'rto_delivered', 'lost': 'lost', 'lt': 'lost',
        'cancelled': 'cancelled', 'canceled': 'cancelled',
    }

    def authenticate(self):
        self.require_fields('username', 'password')
        data = self.http.request('POST', self.url('/api/users/login'), operation='auth',
                                 json_body={'email': self.config.username, 'password': self.config.password})
        token = isinstance(data, dict) and data.get('data')
        if not token or not data.get('status', True):
            raise ProviderError('Xpressbees login failed: %s' % self.http.extract_error(data))
        return token if isinstance(token, str) else token.get('token'), self.token_lifetime

    def _headers(self):
        return {'Authorization': 'Bearer %s' % self.get_token(), 'Content-Type': 'application/json'}

    def _serviceability(self, req_or_args):
        req = req_or_args
        length, width, height = req.max_dimensions
        data = self.http.request('POST', self.url('/api/courier/serviceability'), operation='serviceability', json_body={
            'origin': req.pickup.zip, 'destination': req.delivery.zip,
            'payment_type': 'cod' if req.is_cod else 'prepaid',
            'order_amount': round(req.cod_amount if req.is_cod else req.declared_value, 2),
            'weight': int(round(req.chargeable_weight * 1000)), 'length': length, 'breadth': width, 'height': height,
        }, headers=self._headers())
        if not isinstance(data, dict) or data.get('status') is False:
            return []
        return data.get('data') or []

    def check_serviceability(self, pickup_zip, delivery_zip, weight_kg=0.5, cod=False):
        from .base import Address, Parcel, ShipmentRequest
        req = ShipmentRequest(reference='serviceability', pickup=Address(zip=pickup_zip), delivery=Address(zip=delivery_zip),
                              parcels=[Parcel(weight_kg=weight_kg)], payment_mode='cod' if cod else 'prepaid',
                              cod_amount=1.0 if cod else 0.0, declared_value=1.0)
        rows = self._serviceability(req)
        return ServiceabilityResult(zip=delivery_zip, serviceable=bool(rows), cod=bool(rows) and cod,
                                    prepaid=bool(rows), pickup=bool(rows))

    def get_rates(self, request):
        return [RateOption(service_code=str(r.get('id')), service_name=r.get('name') or '', courier_name='Xpressbees',
                           price=float(r.get('total_charges') or r.get('freight_charges') or 0.0),
                           tat_days=float(r.get('tat') or 0) or 0.0, cod_available=True)
                for r in self._serviceability(request)]

    def create_shipment(self, request):
        d, p, (length, width, height) = request.delivery, request.pickup, request.max_dimensions
        body = {
            'order_number': request.reference,
            'payment_type': 'cod' if request.is_cod else 'prepaid',
            'order_amount': round(request.declared_value, 2),
            'collectable_amount': round(request.cod_amount, 2) if request.is_cod else 0,
            'package_weight': int(round(request.total_weight * 1000)),
            'package_length': length, 'package_breadth': width, 'package_height': height,
            'request_auto_pickup': 'yes',
            'consignee': {'name': d.name, 'address': d.street or d.full_street, 'address_2': d.street2 or '',
                          'city': d.city, 'state': d.state, 'pincode': d.zip, 'phone': d.clean_phone},
            'pickup': {'warehouse_name': request.pickup_location_code or self.config.pickup_location_code or p.company or p.name,
                       'name': p.name, 'address': p.street or p.full_street, 'address_2': p.street2 or '',
                       'city': p.city, 'state': p.state, 'pincode': p.zip, 'phone': p.clean_phone},
            'order_items': [{'name': i.name[:100], 'qty': int(i.qty or 1), 'price': round(i.unit_price, 2), 'sku': i.sku or ''}
                            for i in request.items],
            'courier_id': request.service_code or '',
        }
        data = self.http.request('POST', self.url('/api/shipments2'), operation='create', json_body=body,
                                 headers=self._headers())
        row = (data or {}).get('data') or {}
        if not (data or {}).get('status') or not row.get('awb_number'):
            raise ProviderError('Xpressbees booking failed: %s' % self.http.extract_error(data), payload=data)
        return ShipmentResult(tracking_number=str(row['awb_number']), carrier_reference=str(row.get('shipment_id') or row.get('order_id') or ''),
                              service_code=str(row.get('courier_id') or request.service_code or ''),
                              courier_name=row.get('courier_name') or 'Xpressbees', label_url=row.get('label') or '', raw=data)

    def get_label(self, tracking_number, carrier_reference=''):
        raise ProviderError('Xpressbees returns the label URL at booking time; re-use the stored label link.')

    def track(self, tracking_numbers):
        results = {}
        for awb in tracking_numbers:
            data = self.http.request('GET', self.url('/api/shipments2/track/%s' % awb), operation='track',
                                     headers=self._headers())
            row = (data or {}).get('data') or {}
            if not row:
                results[awb] = TrackingResult(tracking_number=awb, found=False)
                continue
            events = [TrackingEvent(time=parse_datetime(h.get('event_time')), raw_status=h.get('status_code') or h.get('message') or '',
                                    description=h.get('message') or '', location=h.get('location') or '')
                      for h in row.get('history') or []]
            results[awb] = self._result_from_events(awb, events, raw_status=row.get('status') or '',
                                                    description=row.get('status') or '',
                                                    edd=parse_datetime(row.get('edd')))
        return results

    def cancel(self, tracking_number, carrier_reference=''):
        data = self.http.request('POST', self.url('/api/shipments2/cancel'), operation='cancel',
                                 json_body={'awb': tracking_number}, headers=self._headers())
        if isinstance(data, dict) and data.get('status') is False:
            raise ProviderError('Xpressbees cancellation refused: %s' % self.http.extract_error(data))
        return True

    def ndr_action(self, tracking_number, action, data=None):
        act = {'reattempt': 're-attempt', 'change_address': 'change_address', 'change_phone': 'change_phone'}.get(action, action)
        payload = [{'awb': tracking_number, 'action': act, 'action_data': data or {}}]
        self.http.request('POST', self.url('/api/ndr/create'), operation='ndr', json_body=payload, headers=self._headers())
        return True
