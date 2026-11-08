# -*- coding: utf-8 -*-
"""Shiprocket (aggregator: 25+ couriers incl. Delhivery, Blue Dart, DTDC, Xpressbees, Ekart, Shree Maruti...).

API reference: https://apidocs.shiprocket.in - base ``https://apiv2.shiprocket.in/v1/external``.
Auth: POST /auth/login with an *API user* (Settings > API > Configure). Token valid 240 h.
"""
from datetime import timedelta

from .base import (BaseProvider, LabelResult, ProviderError, RateOption, ServiceabilityResult,
                   ShipmentResult, TrackingEvent, parse_datetime, register)


@register
class ShiprocketProvider(BaseProvider):
    code = 'shiprocket'
    name = 'Shiprocket'
    aggregator = True
    production_url = 'https://apiv2.shiprocket.in/v1/external'
    sandbox_url = 'https://apiv2.shiprocket.in/v1/external'
    tracking_url = 'https://shiprocket.co/tracking/{awb}'
    capabilities = {'auth', 'serviceability', 'rate', 'create', 'label', 'track', 'cancel', 'pickup', 'ndr', 'webhook'}
    credential_fields = ('username', 'password', 'pickup_location_code')
    token_lifetime = timedelta(days=9)  # 240 h validity, refresh a day early

    # Shiprocket numeric "shipment_status" ids + textual labels.
    STATUS_MAP = {
        'new': 'booked', 'awb assigned': 'booked', '1': 'booked', 'label generated': 'booked',
        'pickup scheduled': 'pickup_scheduled', 'pickup generated': 'pickup_scheduled', 'pickup queued': 'pickup_scheduled',
        'out for pickup': 'pickup_scheduled', '19': 'pickup_scheduled', '4': 'pickup_scheduled',
        'pickup exception': 'exception', 'pickup error': 'exception',
        'picked up': 'picked_up', '42': 'picked_up', '6': 'in_transit', 'shipped': 'in_transit',
        'in transit': 'in_transit', '18': 'in_transit', 'reached at destination hub': 'in_transit',
        'reached destination hub': 'in_transit', 'misrouted': 'exception', 'delayed': 'in_transit',
        'out for delivery': 'out_for_delivery', '17': 'out_for_delivery',
        'delivered': 'delivered', '7': 'delivered',
        'undelivered': 'ndr', '21': 'ndr', 'ndr': 'ndr',
        'rto initiated': 'rto_initiated', 'rto_initiated': 'rto_initiated', '9': 'rto_initiated', '15': 'rto_initiated',
        'rto in transit': 'rto_in_transit', 'rto in intransit': 'rto_in_transit', 'rto ofd': 'rto_in_transit',
        'rto delivered': 'rto_delivered', '10': 'rto_delivered', '40': 'rto_delivered',
        'canceled': 'cancelled', 'cancelled': 'cancelled', '8': 'cancelled', 'cancellation requested': 'cancelled',
        'lost': 'lost', '12': 'lost', 'damaged': 'lost', 'destroyed': 'lost', '24': 'lost',
        'shipment booked': 'booked',
    }

    def _headers(self):
        return {'Authorization': 'Bearer %s' % self.get_token(), 'Content-Type': 'application/json'}

    def authenticate(self):
        self.require_fields('username', 'password')
        data = self.http.request('POST', self.url('/auth/login'), operation='auth',
                                 json_body={'email': self.config.username, 'password': self.config.password})
        token = isinstance(data, dict) and data.get('token')
        if not token:
            raise ProviderError('Shiprocket login failed: %s' % self.http.extract_error(data))
        return token, self.token_lifetime

    # ----------------------------------------------------------- rates
    def _serviceability(self, pickup_zip, delivery_zip, weight_kg, cod, declared_value=0.0, dims=None):
        params = {'pickup_postcode': pickup_zip, 'delivery_postcode': delivery_zip,
                  'weight': round(max(weight_kg, 0.01), 3), 'cod': 1 if cod else 0}
        if declared_value:
            params['declared_value'] = round(declared_value, 2)
        if dims:
            params.update({'length': dims[0], 'breadth': dims[1], 'height': dims[2]})
        data = self.http.request('GET', self.url('/courier/serviceability/'), operation='serviceability',
                                 params=params, headers=self._headers(), ok_statuses=(404,))
        if not isinstance(data, dict):
            return []
        return (data.get('data') or {}).get('available_courier_companies') or []

    def check_serviceability(self, pickup_zip, delivery_zip, weight_kg=0.5, cod=False):
        couriers = self._serviceability(pickup_zip, delivery_zip, weight_kg, cod)
        if not couriers:
            return ServiceabilityResult(zip=delivery_zip, serviceable=False, message='No courier available')
        tat = min(float(c.get('estimated_delivery_days') or 0) or 99 for c in couriers)
        return ServiceabilityResult(zip=delivery_zip, serviceable=True, cod=any(int(c.get('cod') or 0) for c in couriers),
                                    prepaid=True, pickup=True, tat_days=0 if tat == 99 else tat,
                                    message='%d couriers available' % len(couriers))

    def get_rates(self, request):
        couriers = self._serviceability(request.pickup.zip, request.delivery.zip, request.chargeable_weight,
                                        request.is_cod, request.declared_value, request.max_dimensions)
        rates = []
        for c in couriers:
            if request.is_cod and not int(c.get('cod') or 0):
                continue
            price = c.get('rate') if c.get('rate') is not None else (c.get('freight_charge') or 0) + (c.get('cod_charges') or 0)
            rates.append(RateOption(
                service_code=str(c.get('courier_company_id')),
                service_name=c.get('courier_name') or '',
                courier_name=c.get('courier_name') or '',
                price=float(price or 0.0),
                tat_days=float(c.get('estimated_delivery_days') or 0) or 0.0,
                cod_available=bool(int(c.get('cod') or 0)),
                extra={'rating': c.get('rating'), 'etd': c.get('etd'),
                       'delivery_performance': c.get('delivery_performance'),
                       'rto_performance': c.get('rto_performance')},
            ))
        return rates

    # ----------------------------------------------------------- shipment
    def _order_payload(self, req):
        d, length, width, height = req.delivery, *req.max_dimensions
        name_parts = (d.name or 'Customer').split(' ', 1)
        items = req.items or []
        return {
            'order_id': req.reference,
            'order_date': req.order_date.strftime('%Y-%m-%d %H:%M'),
            'pickup_location': req.pickup_location_code or self.config.pickup_location_code or 'Primary',
            'billing_customer_name': name_parts[0],
            'billing_last_name': name_parts[1] if len(name_parts) > 1 else '',
            'billing_address': d.street or d.full_street,
            'billing_address_2': d.street2 or '',
            'billing_city': d.city, 'billing_pincode': d.zip, 'billing_state': d.state,
            'billing_country': 'India' if d.country_code == 'IN' else d.country_code,
            'billing_email': d.email or '', 'billing_phone': d.clean_phone,
            'shipping_is_billing': True,
            'order_items': [{'name': i.name[:100], 'sku': i.sku or i.name[:40], 'units': int(i.qty or 1),
                             'selling_price': round(i.unit_price, 2), 'hsn': i.hsn or ''} for i in items]
            or [{'name': 'Goods', 'sku': req.reference, 'units': 1, 'selling_price': round(req.declared_value, 2)}],
            'payment_method': 'COD' if req.is_cod else 'Prepaid',
            'sub_total': round(req.cod_amount if req.is_cod else req.declared_value, 2),
            'length': length, 'breadth': width, 'height': height,
            'weight': req.chargeable_weight,
            'invoice_number': req.invoice_number or '',
            'ewaybill_no': req.ewaybill or '',
        }

    def create_shipment(self, request):
        headers = self._headers()
        order = self.http.request('POST', self.url('/orders/create/adhoc'), operation='create',
                                  json_body=self._order_payload(request), headers=headers)
        shipment_id = isinstance(order, dict) and order.get('shipment_id')
        if not shipment_id:
            raise ProviderError('Shiprocket order creation failed: %s' % self.http.extract_error(order), payload=order)
        payload = {'shipment_id': shipment_id}
        if request.service_code:
            payload['courier_id'] = request.service_code
        awb_resp = self.http.request('POST', self.url('/courier/assign/awb'), operation='create',
                                     json_body=payload, headers=headers)
        data = ((awb_resp or {}).get('response') or {}).get('data') or {}
        awb = data.get('awb_code')
        if not awb:
            raise ProviderError('Shiprocket AWB assignment failed: %s' % self.http.extract_error(awb_resp),
                                payload=awb_resp, retryable=False)
        label = LabelResult()
        try:
            label = self.get_label(awb, str(shipment_id))
        except ProviderError:
            pass  # label can be fetched later
        return ShipmentResult(
            tracking_number=awb, carrier_reference='%s/%s' % (order.get('order_id'), shipment_id),
            price=float(data.get('freight_charges') or data.get('applied_weight_amount') or 0.0),
            service_code=str(data.get('courier_company_id') or request.service_code or ''),
            courier_name=data.get('courier_name') or '', label_url=label.url, raw={'order': order, 'awb': awb_resp})

    @staticmethod
    def _shipment_id(carrier_reference):
        return (carrier_reference or '').split('/')[-1]

    def get_label(self, tracking_number, carrier_reference=''):
        data = self.http.request('POST', self.url('/courier/generate/label'), operation='label',
                                 json_body={'shipment_id': [self._shipment_id(carrier_reference)]},
                                 headers=self._headers())
        url = isinstance(data, dict) and data.get('label_url')
        if not url:
            raise ProviderError('Shiprocket label not available: %s' % self.http.extract_error(data))
        return LabelResult(url=url, filename='Label-%s.pdf' % tracking_number)

    def schedule_pickup(self, tracking_numbers, pickup_date, carrier_references=None, package_count=1):
        ids = [self._shipment_id(r) for r in (carrier_references or []) if r]
        data = self.http.request('POST', self.url('/courier/generate/pickup'), operation='pickup',
                                 json_body={'shipment_id': ids, 'pickup_date': [pickup_date.strftime('%Y-%m-%d')]},
                                 headers=self._headers())
        return (data or {}).get('response', {}).get('pickup_token_number') or 'scheduled'

    def cancel(self, tracking_number, carrier_reference=''):
        self.http.request('POST', self.url('/orders/cancel/shipment/awbs'), operation='cancel',
                          json_body={'awbs': [tracking_number]}, headers=self._headers())
        return True

    def ndr_action(self, tracking_number, action, data=None):
        sr_action = {'reattempt': 're-attempt', 'rto': 'return'}.get(action, action)
        body = {'action': sr_action, 'comments': (data or {}).get('comments') or 'Action from Odoo'}
        if data and data.get('phone'):
            body['phone'] = data['phone']
        if data and data.get('address'):
            body['address1'] = data['address']
        self.http.request('POST', self.url('/ndr/%s/action' % tracking_number), operation='ndr',
                          json_body=body, headers=self._headers())
        return True

    # ----------------------------------------------------------- tracking
    def track(self, tracking_numbers):
        results = {}
        for awb in tracking_numbers:
            data = self.http.request('GET', self.url('/courier/track/awb/%s' % awb), operation='track',
                                     headers=self._headers())
            results[awb] = self._parse_tracking(awb, (data or {}).get('tracking_data') or {})
        return results

    def _parse_tracking(self, awb, td):
        if not td or td.get('error'):
            from .base import TrackingResult
            return TrackingResult(tracking_number=awb, found=False, description=(td or {}).get('error', ''))
        events = [TrackingEvent(time=parse_datetime(a.get('date')),
                                raw_status=a.get('sr-status-label') or a.get('status') or a.get('activity') or '',
                                description=a.get('activity') or '', location=a.get('location') or '')
                  for a in td.get('shipment_track_activities') or []]
        track = (td.get('shipment_track') or [{}])[0]
        raw = track.get('current_status') or str(td.get('shipment_status') or '')
        return self._result_from_events(
            awb, events, raw_status=raw, description=raw,
            edd=parse_datetime(track.get('edd') or td.get('etd')),
            delivered_at=parse_datetime(track.get('delivered_date')),
            charged_weight=float(track.get('weight') or 0.0))

    def parse_webhook(self, payload, headers=None):
        awb = str(payload.get('awb') or '')
        if not awb:
            return []
        events = [TrackingEvent(time=parse_datetime(s.get('date')),
                                raw_status=s.get('sr-status-label') or s.get('status') or s.get('activity') or '',
                                description=s.get('activity') or '', location=s.get('location') or '')
                  for s in payload.get('scans') or []]
        raw = payload.get('current_status') or payload.get('shipment_status') or ''
        return [self._result_from_events(awb, events, raw_status=raw, description=raw,
                                         edd=parse_datetime(payload.get('etd')))]
