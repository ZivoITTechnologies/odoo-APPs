# -*- coding: utf-8 -*-
"""UPS REST APIs (OAuth 2.0 client credentials).

Production ``https://onlinetools.ups.com``, sandbox (CIE) ``https://wwwcie.ups.com``.
Token: POST /security/v1/oauth/token (HTTP Basic client_id:client_secret)
Rating: POST /api/rating/{version}/Shoptimeintransit (all services + transit days)
Ship:   POST /api/shipments/{version}/ship        Void: DELETE /api/shipments/{version}/void/cancel/{id}
Track:  GET  /api/track/v1/details/{inquiryNumber}
Every path can be overridden from Extra Settings {"paths": {...}} and the API version with {"api_version": "v2409"}.
"""
import base64
import uuid
from datetime import timedelta

from .base import (BaseProvider, ProviderError, RateOption, ShipmentResult, TrackingEvent, TrackingResult,
                   b64_to_bytes, image_to_pdf, parse_datetime, register)

SERVICES = {
    '01': 'UPS Next Day Air', '02': 'UPS 2nd Day Air', '03': 'UPS Ground', '07': 'UPS Worldwide Express',
    '08': 'UPS Worldwide Expedited', '11': 'UPS Standard', '12': 'UPS 3 Day Select', '13': 'UPS Next Day Air Saver',
    '14': 'UPS Next Day Air Early', '54': 'UPS Worldwide Express Plus', '59': 'UPS 2nd Day Air A.M.',
    '65': 'UPS Worldwide Saver', '71': 'UPS Worldwide Express Freight Midday', '93': 'UPS Ground Saver',
    '96': 'UPS Worldwide Express Freight',
}
DEFAULT_DAYS = {'01': 1, '13': 1, '14': 1, '02': 2, '59': 2, '12': 3, '03': 5, '93': 6, '11': 5,
                '07': 2, '54': 2, '65': 3, '08': 5}
IMPERIAL = {'US', 'PR'}


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


@register
class UPSProvider(BaseProvider):
    code = 'ups'
    name = 'UPS'
    country = 'INTL'
    production_url = 'https://onlinetools.ups.com'
    sandbox_url = 'https://wwwcie.ups.com'
    tracking_url = 'https://www.ups.com/track?tracknum={awb}'
    capabilities = {'auth', 'rate', 'create', 'label', 'track', 'cancel'}
    credential_fields = ('client_id', 'client_secret', 'account_number')
    setup_help = ('Create an app on developer.ups.com (products: Authorization, Rating, Shipping, Tracking). '
                  'Client ID / Client Secret come from the app, Account Number is your 6 character UPS shipper number. '
                  'Negotiated (contract) rates are used automatically when your account has them.')

    # activity.status.type, then a few currentStatus descriptions
    STATUS_MAP = {
        'm': 'booked', 'mv': 'booked', 'p': 'picked_up', 'i': 'in_transit', 'w': 'in_transit',
        'o': 'out_for_delivery', 'd': 'delivered', 'dd': 'delivered', 'x': 'exception', 'rs': 'rto_in_transit',
        'do': 'delivered', 'na': None,
        'shipment ready for ups': 'booked', 'label created': 'booked', 'picked up': 'picked_up',
        'on the way': 'in_transit', 'in transit': 'in_transit', 'out for delivery': 'out_for_delivery',
        'out for delivery today': 'out_for_delivery', 'delivered': 'delivered', 'returned to sender': 'rto_delivered',
        'voided information received': 'cancelled', 'delivery attempted': 'ndr',
    }

    @property
    def version(self):
        return (self.config.extra or {}).get('api_version') or 'v2409'

    def normalize_status(self, raw_status, description=''):
        text = (description or '').lower()
        key = str(raw_status or '').strip().lower()
        if 'out for delivery' in text:
            return 'out_for_delivery'
        if key == 'x' and ('attempt' in text or 'not available' in text or 'receiver' in text):
            return 'ndr'
        if ('returned to' in text and 'sender' in text) or 'returning to sender' in text:
            return 'rto_in_transit' if 'returning' in text else 'rto_delivered'
        if key in self.STATUS_MAP:
            return self.STATUS_MAP[key]
        return self.STATUS_MAP.get(text)

    # ------------------------------------------------------------------ auth
    def authenticate(self):
        self.require_fields('client_id', 'client_secret')
        basic = base64.b64encode(('%s:%s' % (self.config.client_id, self.config.client_secret)).encode()).decode()
        headers = {'Authorization': 'Basic %s' % basic, 'Content-Type': 'application/x-www-form-urlencoded'}
        if self.config.account_number:
            headers['x-merchant-id'] = self.config.account_number
        data = self.http.request('POST', self.url(self.path('auth', '/security/v1/oauth/token')), operation='auth',
                                 data={'grant_type': 'client_credentials'}, headers=headers)
        token = isinstance(data, dict) and data.get('access_token')
        if not token:
            raise ProviderError('UPS OAuth failed: %s' % self.http.extract_error(data))
        return token, timedelta(seconds=max(int(data.get('expires_in') or 14399) - 300, 300))

    def _headers(self):
        return {'Authorization': 'Bearer %s' % self.get_token(), 'Content-Type': 'application/json',
                'transId': uuid.uuid4().hex[:32], 'transactionSrc': 'ZivoIT'}

    # ------------------------------------------------------------------ builders
    def _imperial(self, request):
        return request.pickup.country_code in IMPERIAL

    @staticmethod
    def _address(a):
        lines = [s[:35] for s in (a.street, a.street2) if s][:3] or ['-']
        addr = {'AddressLine': lines, 'City': (a.city or '')[:30], 'PostalCode': a.zip or '',
                'CountryCode': a.country_code or 'IN'}
        if a.state_code:
            addr['StateProvinceCode'] = a.state_code[:5]
        return addr

    def _party(self, a, shipper=False):
        party = {'Name': (a.company or a.name or '-')[:35], 'AttentionName': (a.name or a.company or '-')[:35],
                 'Address': self._address(a)}
        if a.clean_phone:
            party['Phone'] = {'Number': a.clean_phone[:15]}
        if a.email:
            party['EMailAddress'] = a.email
        if shipper:
            party['ShipperNumber'] = self.config.account_number
        return party

    def _packages(self, request, ship=False):
        imperial = self._imperial(request)
        packages = []
        for p in request.parcels:
            weight = p.weight_kg * 2.20462 if imperial else p.weight_kg
            dims = [(d / 2.54 if imperial else d) for d in (p.length_cm, p.width_cm, p.height_cm)]
            pkg = {
                ('Packaging' if ship else 'PackagingType'): {'Code': '02', 'Description': 'Package'},
                'Dimensions': {'UnitOfMeasurement': {'Code': 'IN' if imperial else 'CM'},
                               'Length': '%d' % max(round(dims[0]), 1), 'Width': '%d' % max(round(dims[1]), 1),
                               'Height': '%d' % max(round(dims[2]), 1)},
                'PackageWeight': {'UnitOfMeasurement': {'Code': 'LBS' if imperial else 'KGS'},
                                  'Weight': '%.1f' % max(weight, 0.1)},
            }
            insured = p.declared_value if (request.extra or {}).get('insure') else 0.0
            if insured:
                pkg['PackageServiceOptions'] = {'DeclaredValue': {'CurrencyCode': request.currency,
                                                                  'MonetaryValue': '%.2f' % insured}}
            if ship and (p.reference or request.reference):
                pkg['ReferenceNumber'] = [{'Value': (p.reference or request.reference)[:35]}]
            packages.append(pkg)
        return packages

    def _payment(self):
        return {'ShipmentCharge': [{'Type': '01', 'BillShipper': {'AccountNumber': self.config.account_number}}]}

    # ------------------------------------------------------------------ rating
    def get_rates(self, request):
        self.require_fields('account_number')
        shipment = {
            'Shipper': self._party(request.pickup, shipper=True), 'ShipFrom': self._party(request.pickup),
            'ShipTo': self._party(request.delivery), 'PaymentDetails': self._payment(),
            'ShipmentRatingOptions': {'NegotiatedRatesIndicator': 'Y'},
            'DeliveryTimeInformation': {'PackageBillType': '03',
                                        'Pickup': {'Date': request.order_date.strftime('%Y%m%d')}},
            'NumOfPieces': str(len(request.parcels)),
            'Package': self._packages(request),
        }
        if request.pickup.country_code != request.delivery.country_code:
            shipment['InvoiceLineTotal'] = {'CurrencyCode': request.currency, 'MonetaryValue': '%.2f' % request.declared_value}
            shipment['ShipmentTotalWeight'] = {
                'UnitOfMeasurement': {'Code': 'LBS' if self._imperial(request) else 'KGS'},
                'Weight': '%.1f' % (request.total_weight * (2.20462 if self._imperial(request) else 1))}
        body = {'RateRequest': {'Request': {'TransactionReference': {'CustomerContext': request.reference[:35]}},
                                'Shipment': shipment}}
        data = self.http.request('POST', self.url(self.path('rate', '/api/rating/%s/Shoptimeintransit' % self.version)),
                                 operation='rate', json_body=body, headers=self._headers())
        rates = []
        for rated in as_list(((data or {}).get('RateResponse') or {}).get('RatedShipment')):
            code = (rated.get('Service') or {}).get('Code') or ''
            negotiated = ((rated.get('NegotiatedRateCharges') or {}).get('TotalCharge') or {})
            total = negotiated if negotiated.get('MonetaryValue') else (rated.get('TotalCharges') or {})
            summary = ((rated.get('TimeInTransit') or {}).get('ServiceSummary') or {})
            days = ((summary.get('EstimatedArrival') or {}).get('BusinessDaysInTransit')
                    or (rated.get('GuaranteedDelivery') or {}).get('BusinessDaysInTransit') or DEFAULT_DAYS.get(code, 0))
            rates.append(RateOption(
                service_code=code, service_name=SERVICES.get(code, 'UPS %s' % code), courier_name='UPS',
                price=float(total.get('MonetaryValue') or 0.0), currency=total.get('CurrencyCode') or request.currency,
                tat_days=float(days or 0), cod_available=False,
                extra={'negotiated': bool(negotiated.get('MonetaryValue')),
                       'list_price': float((rated.get('TotalCharges') or {}).get('MonetaryValue') or 0.0)}))
        if not rates:
            raise ProviderError('UPS returned no rates: %s' % self.http.extract_error(data), payload=data)
        return rates

    # ------------------------------------------------------------------ shipping
    def create_shipment(self, request):
        self.require_fields('account_number')
        service = request.service_code or self.config.default_service_code or (
            '03' if request.pickup.country_code == request.delivery.country_code else '65')
        shipment = {
            'Description': (', '.join(i.name for i in request.items) or 'Goods')[:50],
            'Shipper': self._party(request.pickup, shipper=True), 'ShipFrom': self._party(request.pickup),
            'ShipTo': self._party(request.delivery), 'PaymentInformation': self._payment(),
            'Service': {'Code': service, 'Description': SERVICES.get(service, service)},
            'ShipmentRatingOptions': {'NegotiatedRatesIndicator': 'Y'},
            'Package': self._packages(request, ship=True),
        }
        if request.pickup.country_code != request.delivery.country_code:
            shipment['InvoiceLineTotal'] = {'CurrencyCode': request.currency, 'MonetaryValue': '%.2f' % request.declared_value}
            shipment['ShipmentServiceOptions'] = {'InternationalForms': self._invoice(request)}
        if request.is_return:
            shipment['ReturnService'] = {'Code': '9'}  # print return label
        body = {'ShipmentRequest': {
            'Request': {'RequestOption': 'nonvalidate', 'TransactionReference': {'CustomerContext': request.reference[:35]}},
            'Shipment': shipment,
            'LabelSpecification': {'LabelImageFormat': {'Code': 'GIF'}, 'HTTPUserAgent': 'Mozilla/4.5',
                                   'LabelStockSize': {'Height': '6', 'Width': '4'}},
        }}
        data = self.http.request('POST', self.url(self.path('create', '/api/shipments/%s/ship' % self.version)),
                                 operation='create', json_body=body, headers=self._headers())
        results = ((data or {}).get('ShipmentResponse') or {}).get('ShipmentResults') or {}
        ident = results.get('ShipmentIdentificationNumber')
        if not ident:
            raise ProviderError('UPS shipment failed: %s' % self.http.extract_error(data), payload=data)
        packages = as_list(results.get('PackageResults'))
        images = [b64_to_bytes((pkg.get('ShippingLabel') or {}).get('GraphicImage')) for pkg in packages]
        negotiated = ((results.get('NegotiatedRateCharges') or {}).get('TotalCharge') or {})
        total = negotiated if negotiated.get('MonetaryValue') else ((results.get('ShipmentCharges') or {}).get('TotalCharges') or {})
        tracking = packages[0].get('TrackingNumber') if packages and packages[0].get('TrackingNumber') else ident
        return ShipmentResult(tracking_number=tracking, carrier_reference=ident,
                              price=float(total.get('MonetaryValue') or 0.0), currency=total.get('CurrencyCode') or request.currency,
                              service_code=service, courier_name='UPS', label_pdf=image_to_pdf([i for i in images if i]),
                              raw={'packages': [p.get('TrackingNumber') for p in packages]})

    @staticmethod
    def _invoice(request):
        products = [{'Description': (i.name or 'Goods')[:35], 'CommodityCode': i.hsn or '',
                     'OriginCountryCode': i.origin_country or request.pickup.country_code,
                     'Unit': {'Number': '%d' % max(int(i.qty or 1), 1), 'Value': '%.2f' % i.unit_price,
                              'UnitOfMeasurement': {'Code': 'PCS'}}}
                    for i in request.items] or [{'Description': 'Goods', 'OriginCountryCode': request.pickup.country_code,
                                                 'Unit': {'Number': '1', 'Value': '%.2f' % request.declared_value,
                                                          'UnitOfMeasurement': {'Code': 'PCS'}}}]
        return {'FormType': '01', 'InvoiceNumber': request.invoice_number or request.reference[:35],
                'InvoiceDate': request.order_date.strftime('%Y%m%d'), 'ReasonForExport': 'SALE',
                'CurrencyCode': request.currency, 'Product': products}

    def cancel(self, tracking_number, carrier_reference=''):
        ident = carrier_reference or tracking_number
        data = self.http.request('DELETE', self.url(self.path('cancel', '/api/shipments/%s/void/cancel/%s' % (self.version, ident))),
                                 operation='cancel', headers=self._headers())
        status = ((((data or {}).get('VoidShipmentResponse') or {}).get('Response') or {}).get('ResponseStatus') or {})
        if str(status.get('Code')) != '1':
            raise ProviderError('UPS void refused: %s' % self.http.extract_error(data))
        return True

    # ------------------------------------------------------------------ tracking
    def track(self, tracking_numbers):
        results = {}
        for awb in tracking_numbers:
            try:
                data = self.http.request('GET', self.url(self.path('track', '/api/track/v1/details/%s' % awb)),
                                         operation='track', params={'locale': 'en_US', 'returnSignature': 'false'},
                                         headers=self._headers())
            except ProviderError as exc:
                if exc.status_code == 404:
                    results[awb] = TrackingResult(tracking_number=awb, found=False, description=exc.message)
                    continue
                raise
            shipment = (((data or {}).get('trackResponse') or {}).get('shipment') or [{}])[0]
            package = (shipment.get('package') or [{}])[0]
            if not package:
                results[awb] = TrackingResult(tracking_number=awb, found=False,
                                              description=str(((shipment.get('warnings') or [{}])[0]).get('message', '')))
                continue
            events = []
            for act in package.get('activity') or []:
                status = act.get('status') or {}
                address = ((act.get('location') or {}).get('address') or {})
                stamp = (act.get('date') or '') + (act.get('time') or '000000')
                events.append(TrackingEvent(
                    time=parse_datetime(stamp[:14]) if len(stamp) >= 14 else parse_datetime(act.get('date')),
                    raw_status=status.get('type') or status.get('code') or '', description=status.get('description') or '',
                    location=', '.join(x for x in (address.get('city'), address.get('stateProvince'), address.get('countryCode')) if x)))
            for ev in events:
                ev.normalized = self.normalize_status(ev.raw_status, ev.description)
            current = package.get('currentStatus') or {}
            latest = max((e for e in events if e.time), key=lambda e: e.time, default=None)
            edd = None
            for dd in package.get('deliveryDate') or []:
                if dd.get('date'):
                    edd = parse_datetime(dd['date'])
            raw = latest.raw_status if latest else (current.get('code') or '')
            desc = current.get('description') or (latest.description if latest else '')
            result = self._result_from_events(awb, events, raw_status=raw, description=desc, edd=edd,
                                              ndr_reason=desc if latest and latest.normalized == 'ndr' else '')
            if result.normalized is None and latest:
                result.normalized = latest.normalized
            results[awb] = result
        return results
