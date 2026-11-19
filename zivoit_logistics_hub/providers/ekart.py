# -*- coding: utf-8 -*-
"""Ekart Logistics (Flipkart group) - Ekart Elite seller API.

Base ``https://app.elite.ekartlogistics.in`` (API reference at /api/docs).
Auth:  POST /integrations/v2/auth/token/{client_id} (username / password) -> access_token
Serviceability: GET /api/v2/serviceability/{pincode} | Estimate: POST /data/pricing/estimate
Create: PUT /api/v1/package/create | Label: POST /api/v1/package/label | Cancel: DELETE /api/v1/package/cancel
Track (public): GET /api/v1/track/{tracking_id} | NDR: POST /api/v2/package/ndr
All paths can be overridden in Extra Settings {"paths": {...}} if Ekart publishes a new version.
"""
from datetime import timedelta

from .base import (BaseProvider, LabelResult, ProviderError, RateOption, ServiceabilityResult, ShipmentResult,
                   TrackingEvent, TrackingResult, parse_datetime, register, to_bool)


@register
class EkartProvider(BaseProvider):
    code = 'ekart'
    name = 'Ekart Logistics'
    production_url = 'https://app.elite.ekartlogistics.in'
    sandbox_url = 'https://app.elite.ekartlogistics.in'
    tracking_url = 'https://ekartlogistics.com/shipmenttrack/{awb}'
    capabilities = {'auth', 'serviceability', 'rate', 'create', 'label', 'track', 'cancel', 'ndr'}
    credential_fields = ('client_id', 'username', 'password')
    token_lifetime = timedelta(hours=23)
    setup_help = ('Ekart Elite: Client ID, Username and Password are under Settings > API in the Ekart Elite panel. '
                  'Pickup Location Code is the warehouse alias registered with Ekart.')

    STATUS_MAP = {
        'order placed': 'booked', 'shipment created': 'booked', 'manifested': 'booked', 'pickup scheduled': 'pickup_scheduled',
        'out for pickup': 'pickup_scheduled', 'picked up': 'picked_up', 'shipment picked up': 'picked_up',
        'in transit': 'in_transit', 'reached hub': 'in_transit', 'dispatched': 'in_transit', 'out for delivery': 'out_for_delivery',
        'delivered': 'delivered', 'undelivered': 'ndr', 'delivery attempted': 'ndr', 'not delivered': 'ndr',
        'rto initiated': 'rto_initiated', 'rto in transit': 'rto_in_transit', 'rto delivered': 'rto_delivered',
        'returned to seller': 'rto_delivered', 'cancelled': 'cancelled', 'lost': 'lost', 'damaged': 'lost',
    }

    def authenticate(self):
        self.require_fields('client_id', 'username', 'password')
        data = self.http.request('POST', self.url(self.path('auth', '/integrations/v2/auth/token/%s' % self.config.client_id)),
                                 operation='auth', json_body={'username': self.config.username,
                                                              'password': self.config.password})
        token = isinstance(data, dict) and data.get('access_token')
        if not token:
            raise ProviderError('Ekart login failed: %s' % self.http.extract_error(data))
        return token, timedelta(seconds=max(int(data.get('expires_in') or 86400) - 600, 600))

    def _headers(self):
        return {'Authorization': 'Bearer %s' % self.get_token(), 'Content-Type': 'application/json'}

    def check_serviceability(self, pickup_zip, delivery_zip, weight_kg=0.5, cod=False):
        data = self.http.request('GET', self.url(self.path('serviceability', '/api/v2/serviceability/%s' % delivery_zip)),
                                 operation='serviceability', headers=self._headers())
        data = data or {}
        details = data.get('details') or {}
        ok = to_bool(data.get('status', True)) and to_bool(details.get('forward_drop', True))
        return ServiceabilityResult(zip=delivery_zip, serviceable=ok, cod=to_bool(details.get('cod', False)),
                                    prepaid=ok, pickup=to_bool(details.get('forward_pickup', True)),
                                    tat_days=float(details.get('tat') or 0), message=data.get('remark') or '')

    def get_rates(self, request):
        length, width, height = request.max_dimensions
        body = {'pickupPincode': request.pickup.zip, 'dropPincode': request.delivery.zip,
                'invoiceAmount': round(request.declared_value, 2), 'weight': int(round(request.chargeable_weight * 1000)),
                'length': round(length), 'height': round(height), 'width': round(width), 'serviceType': 'SURFACE',
                'codAmount': round(request.cod_amount, 2) if request.is_cod else 0, 'shippingDirection': 'FORWARD'}
        data = self.http.request('POST', self.url(self.path('rate', '/data/pricing/estimate')), operation='rate',
                                 json_body=body, headers=self._headers())
        data = data or {}
        total = data.get('total') or data.get('totalAmount') or data.get('shippingCharge')
        if not total:
            raise ProviderError('Ekart estimate failed: %s' % self.http.extract_error(data), payload=data)
        return [RateOption(service_code='SURFACE', service_name='Ekart Surface', courier_name='Ekart',
                           price=float(total), currency='INR', tat_days=float(data.get('tat') or 0) or 4.0,
                           cod_available=True)]

    @staticmethod
    def _location(a, name=None):
        return {'name': (name or a.name or a.company or '-')[:60], 'phone': a.clean_phone, 'pin': a.zip,
                'address': a.full_street or '-', 'city': a.city, 'state': a.state, 'country': 'India'}

    def create_shipment(self, request):
        length, width, height = request.max_dimensions
        value = round(request.declared_value, 2)
        body = {
            'seller_name': request.pickup.company or request.pickup.name, 'seller_address': request.pickup.full_street,
            'seller_gst_tin': (self.config.extra or {}).get('gstin', ''), 'consignee_name': request.delivery.name,
            'consignee_alternate_phone': request.delivery.clean_phone, 'order_number': request.reference,
            'invoice_number': request.invoice_number or request.reference,
            'invoice_date': request.invoice_date or request.order_date.strftime('%Y-%m-%d'),
            'products_desc': (', '.join(i.name for i in request.items) or 'Goods')[:200],
            'category_of_goods': (self.config.extra or {}).get('category', 'General'),
            'payment_mode': 'COD' if request.is_cod else 'Prepaid', 'total_amount': value, 'tax_value': 0,
            'taxable_amount': value, 'commodity_value': str(value), 'cod_amount': round(request.cod_amount, 2) if request.is_cod else 0,
            'quantity': int(sum(i.qty for i in request.items) or 1), 'weight': int(round(request.chargeable_weight * 1000)),
            'length': round(length), 'height': round(height), 'width': round(width),
            'drop_location': self._location(request.delivery),
            'pickup_location': self._location(request.pickup, request.pickup_location_code or self.config.pickup_location_code),
            'return_location': self._location(request.return_address or request.pickup,
                                              request.pickup_location_code or self.config.pickup_location_code),
            'ewbn': request.ewaybill or '',
        }
        data = self.http.request('PUT', self.url(self.path('create', '/api/v1/package/create')), operation='create',
                                 json_body=body, headers=self._headers())
        data = data or {}
        awb = data.get('tracking_id') or (data.get('barcodes') or {}).get('wbn')
        if not awb or data.get('status') is False:
            raise ProviderError('Ekart booking failed: %s' % (data.get('remark') or self.http.extract_error(data)), payload=data)
        return ShipmentResult(tracking_number=awb, carrier_reference=data.get('vendor_order_id') or request.reference,
                              price=float(data.get('total_charge') or 0.0), currency='INR', service_code='SURFACE',
                              courier_name='Ekart')

    def get_label(self, tracking_number, carrier_reference=''):
        data = self.http.request('POST', self.url(self.path('label', '/api/v1/package/label')), operation='label',
                                 json_body={'ids': [tracking_number]}, headers=self._headers(), expect='bytes')
        if isinstance(data, bytes) and data[:4] == b'%PDF':
            return LabelResult(pdf=data)
        raise ProviderError('Ekart label not ready: %s' % self.http.extract_error(data))

    def cancel(self, tracking_number, carrier_reference=''):
        data = self.http.request('DELETE', self.url(self.path('cancel', '/api/v1/package/cancel')), operation='cancel',
                                 params={'tracking_id': tracking_number}, headers=self._headers())
        if isinstance(data, dict) and data.get('status') is False:
            raise ProviderError('Ekart cancellation refused: %s' % (data.get('remark') or self.http.extract_error(data)))
        return True

    def ndr_action(self, tracking_number, action, data=None):
        data = data or {}
        body = {'action': 'Re-Attempt' if action != 'rto' else 'RTO', 'wbn': tracking_number}
        if data.get('date'):
            body['date'] = str(data['date'])
        if data.get('phone'):
            body['phone'] = data['phone']
        if data.get('address'):
            body['address'] = data['address']
        res = self.http.request('POST', self.url(self.path('ndr', '/api/v2/package/ndr')), operation='ndr', json_body=body,
                                headers=self._headers())
        if isinstance(res, dict) and res.get('status') is False:
            raise ProviderError('Ekart NDR action refused: %s' % (res.get('remark') or self.http.extract_error(res)))
        return True

    def track(self, tracking_numbers):
        results = {}
        for awb in tracking_numbers:
            try:
                data = self.http.request('GET', self.url(self.path('track', '/api/v1/track/%s' % awb)), operation='track')
            except ProviderError as exc:
                if exc.status_code == 404:
                    results[awb] = TrackingResult(tracking_number=awb, found=False, description=exc.message)
                    continue
                raise
            track = (data or {}).get('track') or {}
            events = [TrackingEvent(time=parse_datetime(d.get('ctime')), raw_status=d.get('status') or '',
                                    description=d.get('desc') or d.get('status') or '', location=d.get('location') or '')
                      for d in track.get('details') or []]
            status = track.get('status') or ''
            results[awb] = self._result_from_events(
                awb, events, raw_status=status, description=track.get('desc') or status,
                edd=parse_datetime((data or {}).get('edd')), ndr_reason=track.get('ndrStatus') or '')
        return results
