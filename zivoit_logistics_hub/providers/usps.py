# -*- coding: utf-8 -*-
"""USPS APIs v3 (developers.usps.com, OAuth 2.0 client credentials).

Production ``https://apis.usps.com``, test environment ``https://apis-tem.usps.com``.
Token:   POST /oauth2/v3/token
Prices:  POST /prices/v3/base-rates/search (domestic), /international-prices/v3/base-rates/search
Payment: POST /payments/v3/payment-authorization (CRID / MID / EPS account, needed for labels)
Label:   POST /labels/v3/label (JSON response)       Cancel: DELETE /labels/v3/label/{trackingNumber}
Track:   GET  /tracking/v3/tracking/{trackingNumber}?expand=DETAIL
USPS works in pounds and inches; conversion is automatic.
"""
from datetime import date, timedelta

from .base import (BaseProvider, ProviderError, RateOption, ShipmentResult, TrackingEvent, TrackingResult,
                   b64_to_bytes, parse_datetime, register)

DOMESTIC = [('USPS_GROUND_ADVANTAGE', 'USPS Ground Advantage', 4), ('PRIORITY_MAIL', 'USPS Priority Mail', 2),
            ('PRIORITY_MAIL_EXPRESS', 'USPS Priority Mail Express', 1)]
INTERNATIONAL = [('FIRST-CLASS_PACKAGE_INTERNATIONAL_SERVICE', 'USPS First-Class Package International', 12),
                 ('PRIORITY_MAIL_INTERNATIONAL', 'USPS Priority Mail International', 8),
                 ('PRIORITY_MAIL_EXPRESS_INTERNATIONAL', 'USPS Priority Mail Express International', 4)]
LB, INCH = 2.20462, 2.54


@register
class USPSProvider(BaseProvider):
    code = 'usps'
    name = 'USPS'
    country = 'US'
    production_url = 'https://apis.usps.com'
    sandbox_url = 'https://apis-tem.usps.com'
    tracking_url = 'https://tools.usps.com/go/TrackConfirmAction?tLabels={awb}'
    capabilities = {'auth', 'rate', 'create', 'label', 'track', 'cancel'}
    credential_fields = ('client_id', 'client_secret', 'account_number', 'customer_code')
    setup_help = ('Register an app on developers.usps.com. Client ID / Secret come from the app; Customer Code is '
                  'your CRID and Account Number your EPS account. Put your MID in Extra Settings as '
                  '{"mid": "903xxxxxx"} (and "manifest_mid" if different) to print labels. Rates work with the '
                  'Client ID / Secret alone; "price_type" can be RETAIL, COMMERCIAL or CONTRACT.')

    STATUS_MAP = {
        'pre-shipment': 'booked', 'label created': 'booked', 'shipping label created': 'booked',
        'accepted': 'picked_up', 'usps picked up item': 'picked_up', 'in transit': 'in_transit',
        'out for delivery': 'out_for_delivery', 'delivered': 'delivered', 'delivery attempt': 'ndr',
        'notice left': 'ndr', 'available for pickup': 'ndr', 'alert': 'exception', 'return to sender': 'rto_in_transit',
        'returned to sender': 'rto_delivered', 'delivered to sender': 'rto_delivered',
    }

    def normalize_status(self, raw_status, description=''):
        key = str(raw_status or '').strip().lower()
        if key in self.STATUS_MAP:
            return self.STATUS_MAP[key]
        text = (description or '').lower()
        for phrase, status in (('out for delivery', 'out_for_delivery'), ('return to sender', 'rto_in_transit'),
                               ('notice left', 'ndr'), ('delivery attempt', 'ndr'), ('delivered', 'delivered'),
                               ('accepted', 'picked_up'), ('arrived', 'in_transit'), ('departed', 'in_transit'),
                               ('in transit', 'in_transit'), ('label created', 'booked')):
            if phrase in text or phrase in key:
                return status
        return None

    # ------------------------------------------------------------------ auth
    def authenticate(self):
        self.require_fields('client_id', 'client_secret')
        data = self.http.request('POST', self.url(self.path('auth', '/oauth2/v3/token')), operation='auth', json_body={
            'grant_type': 'client_credentials', 'client_id': self.config.client_id,
            'client_secret': self.config.client_secret,
        })
        token = isinstance(data, dict) and data.get('access_token')
        if not token:
            raise ProviderError('USPS OAuth failed: %s' % self.http.extract_error(data))
        return token, timedelta(seconds=max(int(data.get('expires_in') or 28800) - 600, 300))

    def _headers(self, extra=None):
        headers = {'Authorization': 'Bearer %s' % self.get_token(), 'Content-Type': 'application/json'}
        headers.update(extra or {})
        return headers

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _dims(parcel):
        return {'weight': round(max(parcel.weight_kg * LB, 0.1), 2), 'length': round(max(parcel.length_cm / INCH, 1), 1),
                'width': round(max(parcel.width_cm / INCH, 1), 1), 'height': round(max(parcel.height_cm / INCH, 1), 1)}

    @staticmethod
    def _zip5(value):
        return (value or '').strip()[:5]

    def _price_type(self):
        return (self.config.extra or {}).get('price_type') or 'COMMERCIAL'

    # ------------------------------------------------------------------ rating
    def get_rates(self, request):
        domestic = request.delivery.country_code in ('US', 'PR', 'VI', 'GU', 'AS', 'MP')
        parcel = max(request.parcels, key=lambda p: p.weight_kg)
        dims = self._dims(parcel)
        dims['weight'] = round(max(request.total_weight * LB, 0.1), 2)
        rates, errors = [], []
        for mail_class, label, days in (DOMESTIC if domestic else INTERNATIONAL):
            body = dict(dims, originZIPCode=self._zip5(request.pickup.zip), mailClass=mail_class,
                        processingCategory='MACHINABLE', rateIndicator='SP', destinationEntryFacilityType='NONE',
                        priceType=self._price_type(), mailingDate=date.today().isoformat())
            if domestic:
                body['destinationZIPCode'] = self._zip5(request.delivery.zip)
                path = self.path('rate', '/prices/v3/base-rates/search')
            else:
                body['destinationCountryCode'] = request.delivery.country_code
                body['foreignPostalCode'] = request.delivery.zip or ''
                path = self.path('rate_international', '/international-prices/v3/base-rates/search')
            try:
                data = self.http.request('POST', self.url(path), operation='rate', json_body=body, headers=self._headers())
            except ProviderError as exc:
                errors.append('%s: %s' % (label, exc.message))
                continue
            price = (data or {}).get('totalBasePrice')
            if price is None:
                price = sum(float(r.get('price') or 0.0) for r in (data or {}).get('rates') or [])
            if price:
                rates.append(RateOption(service_code=mail_class, service_name=label, courier_name='USPS',
                                        price=float(price), currency='USD', tat_days=days, cod_available=False))
        if not rates and errors:
            raise ProviderError('USPS returned no rates: %s' % '; '.join(errors)[:500])
        return rates

    # ------------------------------------------------------------------ labels
    def _payment_token(self):
        extra = self.config.extra or {}
        if extra.get('payment_token'):
            return extra['payment_token']
        self.require_fields('customer_code', 'account_number')
        if not extra.get('mid'):
            raise ProviderError('USPS: add your MID to Extra Settings, e.g. {"mid": "903123456"}.')
        role = {'CRID': self.config.customer_code, 'MID': extra['mid'], 'manifestMID': extra.get('manifest_mid') or extra['mid'],
                'accountType': extra.get('account_type') or 'EPS', 'accountNumber': self.config.account_number}
        data = self.http.request('POST', self.url(self.path('payment', '/payments/v3/payment-authorization')),
                                 operation='auth', json_body={'roles': [dict(role, roleName='PAYER'),
                                                                        dict(role, roleName='LABEL_OWNER')]},
                                 headers=self._headers())
        token = isinstance(data, dict) and data.get('paymentAuthorizationToken')
        if not token:
            raise ProviderError('USPS payment authorization failed: %s' % self.http.extract_error(data))
        return token

    @staticmethod
    def _address(a):
        first, _sep, last = (a.name or '').partition(' ')
        addr = {'firstName': first[:50] or '-', 'lastName': last[:50] or '-', 'streetAddress': (a.street or '-')[:50],
                'city': a.city or '', 'state': (a.state_code or '')[:2], 'ZIPCode': (a.zip or '')[:5]}
        if a.street2:
            addr['secondaryAddress'] = a.street2[:50]
        if a.company:
            addr['firm'] = a.company[:50]
        if a.clean_phone:
            addr['phone'] = a.clean_phone[-10:]
        if a.email:
            addr['email'] = a.email
        if len((a.zip or '').replace('-', '')) == 9:
            addr['ZIPPlus4'] = a.zip.replace('-', '')[5:]
        return addr

    def create_shipment(self, request):
        if request.delivery.country_code not in ('US', 'PR', 'VI', 'GU', 'AS', 'MP'):
            raise ProviderError('USPS international labels need the customs form API; use UPS, FedEx or DHL Express '
                                'for international shipments or enable "Extra Settings > paths > create".')
        mail_class = request.service_code or self.config.default_service_code or 'USPS_GROUND_ADVANTAGE'
        parcel = request.parcels[0]
        package = dict(self._dims(parcel), mailClass=mail_class, rateIndicator='SP', weightUOM='lb', dimensionsUOM='in',
                       processingCategory='MACHINABLE', destinationEntryFacilityType='NONE',
                       mailingDate=date.today().isoformat(),
                       customerReference=[{'referenceNumber': request.reference[:30], 'printReferenceNumber': True}])
        if (request.extra or {}).get('insure') and request.declared_value:
            package['extraServices'] = [930 if request.declared_value <= 500 else 931]
            package['packageOptions'] = {'packageValue': round(request.declared_value, 2)}
        body = {'imageInfo': {'imageType': 'PDF', 'labelType': '4X6LABEL', 'receiptOption': 'NONE'},
                'toAddress': self._address(request.delivery), 'fromAddress': self._address(request.pickup),
                'packageDescription': package}
        headers = self._headers({'X-Payment-Authorization-Token': self._payment_token(),
                                 'Accept': 'application/vnd.usps.labels+json'})
        data = self.http.request('POST', self.url(self.path('create', '/labels/v3/label')), operation='create',
                                 json_body=body, headers=headers)
        meta = (data or {}).get('labelMetadata') or {}
        if not meta.get('trackingNumber'):
            raise ProviderError('USPS label failed: %s' % self.http.extract_error(data), payload=data)
        return ShipmentResult(tracking_number=meta['trackingNumber'], carrier_reference=meta.get('SKU') or '',
                              price=float(meta.get('postage') or 0.0), currency='USD', service_code=mail_class,
                              courier_name='USPS', label_pdf=b64_to_bytes((data or {}).get('labelImage')))

    def cancel(self, tracking_number, carrier_reference=''):
        self.http.request('DELETE', self.url(self.path('cancel', '/labels/v3/label/%s' % tracking_number)),
                          operation='cancel', headers=self._headers({'X-Payment-Authorization-Token': self._payment_token()}),
                          ok_statuses=(200, 204))
        return True

    # ------------------------------------------------------------------ tracking
    def track(self, tracking_numbers):
        results = {}
        for awb in tracking_numbers:
            try:
                data = self.http.request('GET', self.url(self.path('track', '/tracking/v3/tracking/%s' % awb)),
                                         operation='track', params={'expand': 'DETAIL'}, headers=self._headers())
            except ProviderError as exc:
                if exc.status_code in (400, 404):
                    results[awb] = TrackingResult(tracking_number=awb, found=False, description=exc.message)
                    continue
                raise
            data = data or {}
            events = [TrackingEvent(time=parse_datetime(ev.get('eventTimestamp')), raw_status=ev.get('eventType') or '',
                                    description=ev.get('eventType') or '',
                                    location=', '.join(x for x in (ev.get('eventCity'), ev.get('eventState')) if x))
                      for ev in data.get('trackingEvents') or []]
            for ev in events:
                ev.normalized = self.normalize_status(ev.raw_status, ev.description)
            category = data.get('statusCategory') or data.get('status') or ''
            result = self._result_from_events(
                awb, events, raw_status=category, description=data.get('statusSummary') or data.get('status') or '',
                edd=parse_datetime(data.get('expectedDeliveryTimeStamp') or data.get('expectedDeliveryDate')),
                ndr_reason=(data.get('statusSummary') or '') if self.normalize_status(category) == 'ndr' else '')
            results[awb] = result
        return results
