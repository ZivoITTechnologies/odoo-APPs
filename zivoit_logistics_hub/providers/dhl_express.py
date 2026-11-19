# -*- coding: utf-8 -*-
"""DHL Express - MyDHL API (REST, HTTP Basic auth).

Production ``https://express.api.dhl.com/mydhlapi``, test ``https://express.api.dhl.com/mydhlapi/test``.
Rates: GET /rates | Ship: POST /shipments | Track: GET /shipments/{id}/tracking | Pickup: POST /pickups
MyDHL has no void call: labels that are never scanned are not billed, so cancelling only cancels in Odoo
(and the courier pickup when one was booked).
"""
import base64
from datetime import datetime, timedelta

from .base import (BaseProvider, ProviderError, RateOption, ShipmentResult, TrackingEvent, TrackingResult,
                   b64_to_bytes, parse_datetime, register)


@register
class DHLExpressProvider(BaseProvider):
    code = 'dhl_express'
    name = 'DHL Express'
    country = 'INTL'
    production_url = 'https://express.api.dhl.com/mydhlapi'
    sandbox_url = 'https://express.api.dhl.com/mydhlapi/test'
    tracking_url = 'https://www.dhl.com/global-en/home/tracking/tracking-express.html?tracking-id={awb}'
    capabilities = {'rate', 'create', 'label', 'track', 'cancel', 'pickup'}
    credential_fields = ('username', 'password', 'account_number')
    setup_help = ('Request MyDHL API access on developer.dhl.com. Username / Password are the API key and secret, '
                  'Account Number is your DHL Express import or export account. Unused labels are never billed, '
                  'so cancelling simply voids the shipment in Odoo.')

    STATUS_MAP = {
        'pu': 'picked_up', 'pl': 'in_transit', 'df': 'in_transit', 'af': 'in_transit', 'ar': 'in_transit',
        'tr': 'in_transit', 'rr': 'in_transit', 'cr': 'in_transit', 'cc': 'ndr', 'ic': 'in_transit', 'ud': 'in_transit',
        'wc': 'out_for_delivery', 'ok': 'delivered', 'dd': 'delivered', 'nh': 'ndr', 'ba': 'ndr', 'ca': 'ndr',
        'cm': 'ndr', 'rd': 'ndr', 'rt': 'rto_in_transit', 'oh': 'exception', 'hp': 'exception', 'md': 'exception',
        'sa': 'booked', 'transit': 'in_transit', 'delivered': 'delivered', 'failure': 'exception',
        'pre-transit': 'booked',
    }

    def _headers(self):
        self.require_fields('username', 'password')
        basic = base64.b64encode(('%s:%s' % (self.config.username, self.config.password)).encode()).decode()
        return {'Authorization': 'Basic %s' % basic, 'Content-Type': 'application/json',
                'Message-Reference': datetime.now().strftime('ZIVO%Y%m%d%H%M%S%f')[:36]}

    def test_connection(self):
        self.http.request('GET', self.url(self.path('address', '/address-validate')), operation='auth', params={
            'type': 'delivery', 'countryCode': 'DE', 'postalCode': '53113', 'cityName': 'Bonn'}, headers=self._headers())
        return 'DHL Express: credentials accepted.'

    @staticmethod
    def _customs(request):
        return request.pickup.country_code != request.delivery.country_code

    def get_rates(self, request):
        self.require_fields('account_number')
        length, width, height = request.max_dimensions
        params = {
            'accountNumber': self.config.account_number,
            'originCountryCode': request.pickup.country_code, 'originPostalCode': request.pickup.zip,
            'originCityName': request.pickup.city, 'destinationCountryCode': request.delivery.country_code,
            'destinationPostalCode': request.delivery.zip, 'destinationCityName': request.delivery.city,
            'weight': round(max(request.total_weight, 0.1), 3), 'length': round(length, 1), 'width': round(width, 1),
            'height': round(height, 1), 'plannedShippingDate': (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d'),
            'isCustomsDeclarable': 'true' if self._customs(request) else 'false', 'unitOfMeasurement': 'metric',
        }
        data = self.http.request('GET', self.url(self.path('rate', '/rates')), operation='rate', params=params,
                                 headers=self._headers())
        rates = []
        for product in (data or {}).get('products') or []:
            prices = product.get('totalPrice') or []
            billing = next((p for p in prices if p.get('currencyType') == 'BILLC'), prices[0] if prices else {})
            caps = product.get('deliveryCapabilities') or {}
            rates.append(RateOption(service_code=product.get('productCode') or '',
                                    service_name='DHL %s' % (product.get('productName') or product.get('productCode')).title(),
                                    courier_name='DHL Express', price=float(billing.get('price') or 0.0),
                                    currency=billing.get('priceCurrency') or request.currency,
                                    tat_days=float(caps.get('totalTransitDays') or 0), cod_available=False,
                                    extra={'edd': caps.get('estimatedDeliveryDateAndTime')}))
        if not rates:
            raise ProviderError('DHL Express returned no products: %s' % self.http.extract_error(data), payload=data)
        return rates

    @staticmethod
    def _party(a):
        addr = {'postalCode': a.zip or '', 'cityName': a.city or '', 'countryCode': a.country_code,
                'addressLine1': (a.street or '-')[:45]}
        if a.street2:
            addr['addressLine2'] = a.street2[:45]
        if a.state_code:
            addr['provinceCode'] = a.state_code[:35]
        return {'postalAddress': addr,
                'contactInformation': {'phone': a.clean_phone or '0000000000', 'companyName': (a.company or a.name or '-')[:60],
                                       'fullName': (a.name or a.company or '-')[:255], 'email': a.email or ''}}

    def create_shipment(self, request):
        self.require_fields('account_number')
        customs = self._customs(request)
        product = request.service_code or self.config.default_service_code or ('P' if customs else 'N')
        content = {
            'packages': [{'weight': round(max(p.weight_kg, 0.1), 3),
                          'dimensions': {'length': max(round(p.length_cm), 1), 'width': max(round(p.width_cm), 1),
                                         'height': max(round(p.height_cm), 1)},
                          'customerReferences': [{'value': (p.reference or request.reference)[:35], 'typeCode': 'CU'}]}
                         for p in request.parcels],
            'isCustomsDeclarable': customs, 'description': (', '.join(i.name for i in request.items) or 'Goods')[:70],
            'incoterm': (request.extra or {}).get('incoterm') or 'DAP', 'unitOfMeasurement': 'metric',
        }
        if customs:
            content['declaredValue'] = round(request.declared_value, 2)
            content['declaredValueCurrency'] = request.currency
            content['exportDeclaration'] = {
                'lineItems': [{'number': n, 'description': (i.name or 'Goods')[:70], 'price': round(i.unit_price, 2),
                               'quantity': {'value': max(int(i.qty or 1), 1), 'unitOfMeasurement': 'PCS'},
                               'commodityCodes': [{'typeCode': 'outbound', 'value': i.hsn}] if i.hsn else [],
                               'manufacturerCountry': i.origin_country or request.pickup.country_code,
                               'weight': {'netValue': round(max(i.weight_kg, 0.01), 3),
                                          'grossValue': round(max(i.weight_kg, 0.01), 3)}}
                              for n, i in enumerate(request.items, 1)] or [{
                    'number': 1, 'description': 'Goods', 'price': round(request.declared_value, 2),
                    'quantity': {'value': 1, 'unitOfMeasurement': 'PCS'},
                    'manufacturerCountry': request.pickup.country_code,
                    'weight': {'netValue': request.total_weight, 'grossValue': request.total_weight}}],
                'invoice': {'number': request.invoice_number or request.reference[:35],
                            'date': request.order_date.strftime('%Y-%m-%d')},
                'exportReason': 'sale',
            }
        body = {
            'plannedShippingDateAndTime': (datetime.now() + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%S GMT+00:00'),
            'pickup': {'isRequested': False}, 'productCode': product,
            'accounts': [{'typeCode': 'shipper', 'number': self.config.account_number}],
            'customerDetails': {'shipperDetails': self._party(request.pickup), 'receiverDetails': self._party(request.delivery)},
            'content': content,
            'outputImageProperties': {'imageOptions': [{'typeCode': 'label', 'templateName': 'ECOM26_84_001'}]},
        }
        if (request.extra or {}).get('insure') and request.declared_value:
            body['valueAddedServices'] = [{'serviceCode': 'II', 'value': round(request.declared_value, 2),
                                           'currency': request.currency}]
        data = self.http.request('POST', self.url(self.path('create', '/shipments')), operation='create', json_body=body,
                                 headers=self._headers())
        awb = (data or {}).get('shipmentTrackingNumber')
        if not awb:
            raise ProviderError('DHL Express shipment failed: %s' % self.http.extract_error(data), payload=data)
        label = None
        for doc in data.get('documents') or []:
            if doc.get('typeCode') == 'label' and doc.get('content'):
                label = b64_to_bytes(doc['content'])
                break
        charges = data.get('shipmentCharges') or [{}]
        billing = next((c for c in charges if c.get('currencyType') == 'BILLC'), charges[0])
        return ShipmentResult(tracking_number=awb, carrier_reference=data.get('dispatchConfirmationNumber') or '',
                              price=float(billing.get('price') or 0.0), currency=billing.get('priceCurrency') or request.currency,
                              service_code=product, courier_name='DHL Express', label_pdf=label,
                              edd=parse_datetime((data.get('estimatedDeliveryDate') or {}).get('estimatedDeliveryDate')))

    def cancel(self, tracking_number, carrier_reference=''):
        if carrier_reference:  # a courier pickup was booked: cancel it
            self.http.request('DELETE', self.url(self.path('cancel', '/pickups/%s' % carrier_reference)), operation='cancel',
                              params={'requestorName': 'ZivoIT', 'reason': 'not needed'}, headers=self._headers(),
                              ok_statuses=(200, 204, 404))
        return True

    def schedule_pickup(self, tracking_numbers, pickup_date, carrier_references=None, package_count=1):
        extra = self.config.extra or {}
        location = extra.get('pickup_address') or {}
        if not location:
            raise ProviderError('DHL Express: add the pickup address to Extra Settings as {"pickup_address": '
                                '{"postalCode": "...", "cityName": "...", "countryCode": "IN", "addressLine1": "...", '
                                '"phone": "...", "companyName": "...", "fullName": "..."}}.')
        contact = {k: location.get(k, '') for k in ('phone', 'companyName', 'fullName', 'email')}
        address = {k: v for k, v in location.items() if k not in contact}
        body = {'plannedPickupDateAndTime': pickup_date.strftime('%Y-%m-%dT%H:%M:%S GMT+00:00'),
                'accounts': [{'typeCode': 'shipper', 'number': self.config.account_number}],
                'customerDetails': {'shipperDetails': {'postalAddress': address, 'contactInformation': contact}},
                'shipmentDetails': [{'productCode': self.config.default_service_code or 'N', 'isCustomsDeclarable': False,
                                     'unitOfMeasurement': 'metric',
                                     'packages': [{'weight': 1, 'dimensions': {'length': 10, 'width': 10, 'height': 10}}] * max(package_count, 1)}]}
        data = self.http.request('POST', self.url(self.path('pickup', '/pickups')), operation='pickup', json_body=body,
                                 headers=self._headers())
        numbers = (data or {}).get('dispatchConfirmationNumbers') or []
        if not numbers:
            raise ProviderError('DHL Express pickup failed: %s' % self.http.extract_error(data))
        return numbers[0]

    def track(self, tracking_numbers):
        results = {}
        for awb in tracking_numbers:
            try:
                data = self.http.request('GET', self.url(self.path('track', '/shipments/%s/tracking' % awb)), operation='track',
                                         params={'trackingView': 'all-checkpoints', 'levelOfDetail': 'all'},
                                         headers=self._headers())
            except ProviderError as exc:
                if exc.status_code == 404:
                    results[awb] = TrackingResult(tracking_number=awb, found=False, description=exc.message)
                    continue
                raise
            shipment = ((data or {}).get('shipments') or [{}])[0]
            events = []
            for ev in shipment.get('events') or []:
                area = (ev.get('serviceArea') or [{}])[0]
                events.append(TrackingEvent(time=parse_datetime('%s %s' % (ev.get('date', ''), ev.get('time', '00:00:00'))),
                                            raw_status=ev.get('typeCode') or '', description=ev.get('description') or '',
                                            location=area.get('description') or ''))
            latest = max((e for e in events if e.time), key=lambda e: e.time, default=None)
            results[awb] = self._result_from_events(
                awb, events, raw_status=latest.raw_status if latest else shipment.get('status', ''),
                description=latest.description if latest else shipment.get('description', ''),
                edd=parse_datetime(shipment.get('estimatedDeliveryDate')),
                ndr_reason=latest.description if latest and self.normalize_status(latest.raw_status) == 'ndr' else '')
        return results
