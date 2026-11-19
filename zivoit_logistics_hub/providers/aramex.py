# -*- coding: utf-8 -*-
"""Aramex Shipping Services API v2 (JSON flavour of the WCF services).

Production ``https://ws.aramex.net/ShippingAPI.V2``, test ``https://ws.dev.aramex.net/ShippingAPI.V2``.
Rate:  /RateCalculator/Service_1_0.svc/json/CalculateRate
Ship:  /Shipping/Service_1_0.svc/json/CreateShipments
Label: /Shipping/Service_1_0.svc/json/PrintLabel
Track: /Tracking/Service_1_0.svc/json/TrackShipments
Pickup:/Shipping/Service_1_0.svc/json/CreatePickup
Every call carries ``ClientInfo`` (user, password, account number / PIN / entity / country).
"""
from datetime import datetime, timedelta, timezone

from ..ai.engine import classify_status
from .base import (BaseProvider, LabelResult, ProviderError, RateOption, ShipmentResult, TrackingEvent, TrackingResult,
                   b64_to_bytes, parse_datetime, register)

DOMESTIC = [('ONP', 'Aramex Overnight Parcel', 1), ('CDS', 'Aramex Credit Card Delivery', 2)]
INTERNATIONAL = [('PPX', 'Aramex Priority Parcel Express', 3), ('EPX', 'Aramex Economy Parcel Express', 6),
                 ('DPX', 'Aramex Deferred Parcel Express', 8)]


def wcf_date(dt):
    return '/Date(%d)/' % int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


@register
class AramexProvider(BaseProvider):
    code = 'aramex'
    name = 'Aramex'
    country = 'INTL'
    production_url = 'https://ws.aramex.net/ShippingAPI.V2'
    sandbox_url = 'https://ws.dev.aramex.net/ShippingAPI.V2'
    tracking_url = 'https://www.aramex.com/track/results?ShipmentNumber={awb}'
    capabilities = {'rate', 'create', 'label', 'track', 'pickup'}
    credential_fields = ('username', 'password', 'account_number', 'license_key', 'customer_code')
    setup_help = ('Aramex shares API credentials with your account: Username / Password, Account Number, '
                  'Licence Key = Account PIN and Customer Code = Account Entity (e.g. BOM, DXB). Put the account '
                  'country in Extra Settings as {"account_country": "IN"}. Aramex has no shipment cancel API; '
                  'cancelling voids the shipment in Odoo only.')

    # Aramex update codes differ per entity, so the update description is classified instead
    # (account specific codes can still be pinned in Configuration > Status Mapping).
    STATUS_MAP = {'record created': 'booked', 'shipment picked up': 'picked_up', 'out for delivery': 'out_for_delivery',
                  'delivered': 'delivered', 'returned to shipper': 'rto_delivered'}

    def normalize_status(self, raw_status, description=''):
        mapped = self.STATUS_MAP.get(str(description or '').strip().lower())
        if mapped:
            return mapped
        status, confidence = classify_status(description)
        return status if confidence >= 0.6 else None

    def _client(self):
        self.require_fields('username', 'password', 'account_number', 'license_key', 'customer_code')
        extra = self.config.extra or {}
        return {'UserName': self.config.username, 'Password': self.config.password, 'Version': 'v1.0',
                'AccountNumber': self.config.account_number, 'AccountPin': self.config.license_key,
                'AccountEntity': self.config.customer_code, 'AccountCountryCode': extra.get('account_country') or 'IN',
                'Source': int(extra.get('source') or 24)}

    def _call(self, op, path, body):
        body = dict(body)
        reference = body.pop('_ref', '')
        body.update(ClientInfo=self._client(), Transaction={'Reference1': reference})
        data = self.http.request('POST', self.url(self.path(op, path)), operation=op, json_body=body,
                                 headers={'Content-Type': 'application/json'})
        if not isinstance(data, dict):
            raise ProviderError('Aramex %s: unexpected response %s' % (op, str(data)[:200]))
        if data.get('HasErrors'):
            raise ProviderError('Aramex %s failed: %s' % (op, self._notes(data)), payload=data)
        return data

    @staticmethod
    def _notes(data):
        notes = list(data.get('Notifications') or [])
        for shp in data.get('Shipments') or []:
            notes += shp.get('Notifications') or []
        return '; '.join('%s %s' % (n.get('Code', ''), n.get('Message', '')) for n in notes)[:500] or 'unknown error'

    @staticmethod
    def _address(a):
        return {'Line1': (a.street or '-')[:50], 'Line2': (a.street2 or '')[:50], 'City': a.city or '',
                'StateOrProvinceCode': a.state_code or '', 'PostCode': a.zip or '', 'CountryCode': a.country_code}

    def _group(self, request):
        return 'DOM' if request.pickup.country_code == request.delivery.country_code else 'EXP'

    def _details(self, request, product_type):
        length, width, height = request.max_dimensions
        details = {
            'Dimensions': {'Length': round(length, 1), 'Width': round(width, 1), 'Height': round(height, 1), 'Unit': 'CM'},
            'ActualWeight': {'Unit': 'KG', 'Value': round(max(request.total_weight, 0.1), 3)},
            'ChargeableWeight': {'Unit': 'KG', 'Value': round(max(request.chargeable_weight, 0.1), 3)},
            'NumberOfPieces': len(request.parcels), 'ProductGroup': self._group(request), 'ProductType': product_type,
            'PaymentType': 'P', 'DescriptionOfGoods': (', '.join(i.name for i in request.items) or 'Goods')[:100],
            'GoodsOriginCountry': request.pickup.country_code,
        }
        if request.is_cod:
            details['Services'] = 'CODS'
            details['CashOnDeliveryAmount'] = {'Value': round(request.cod_amount, 2), 'CurrencyCode': request.currency}
        if self._group(request) == 'EXP':
            details['CustomsValueAmount'] = {'Value': round(request.declared_value, 2), 'CurrencyCode': request.currency}
        if (request.extra or {}).get('insure') and request.declared_value:
            details['InsuranceAmount'] = {'Value': round(request.declared_value, 2), 'CurrencyCode': request.currency}
            details['Services'] = ','.join(s for s in (details.get('Services'), 'INSR') if s)
        return details

    def get_rates(self, request):
        rates, errors = [], []
        products = DOMESTIC if self._group(request) == 'DOM' else INTERNATIONAL
        for product_type, label, days in products:
            try:
                data = self._call('rate', '/RateCalculator/Service_1_0.svc/json/CalculateRate', {
                    'OriginAddress': self._address(request.pickup), 'DestinationAddress': self._address(request.delivery),
                    'ShipmentDetails': self._details(request, product_type), 'PreferredCurrencyCode': request.currency,
                    '_ref': request.reference})
            except ProviderError as exc:
                errors.append(exc.message)
                continue
            amount = data.get('TotalAmount') or {}
            if amount.get('Value'):
                rates.append(RateOption(service_code=product_type, service_name=label, courier_name='Aramex',
                                        price=float(amount['Value']), currency=amount.get('CurrencyCode') or request.currency,
                                        tat_days=days, cod_available=self._group(request) == 'DOM'))
        if not rates and errors:
            raise ProviderError('; '.join(errors)[:500])
        return rates

    def _contact(self, a):
        return {'PersonName': (a.name or '-')[:45], 'CompanyName': (a.company or a.name or '-')[:45],
                'PhoneNumber1': a.clean_phone or '0000000000', 'CellPhone': a.clean_phone or '0000000000',
                'EmailAddress': a.email or ''}

    def create_shipment(self, request):
        product_type = request.service_code or self.config.default_service_code or (
            'ONP' if self._group(request) == 'DOM' else 'PPX')
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        shipment = {
            'Reference1': request.reference[:50],
            'Shipper': {'Reference1': request.reference[:50], 'AccountNumber': self.config.account_number,
                        'PartyAddress': self._address(request.pickup), 'Contact': self._contact(request.pickup)},
            'Consignee': {'Reference1': request.reference[:50], 'PartyAddress': self._address(request.delivery),
                          'Contact': self._contact(request.delivery)},
            'ShippingDateTime': wcf_date(now), 'DueDate': wcf_date(now + timedelta(days=1)),
            'Details': self._details(request, product_type),
        }
        if self._group(request) == 'EXP':
            shipment['Details']['Items'] = [{'PackageType': 'Box', 'Quantity': max(int(i.qty or 1), 1),
                                             'Weight': {'Unit': 'KG', 'Value': round(max(i.weight_kg, 0.01), 3)},
                                             'Comments': (i.name or '')[:50], 'Reference': i.sku or '',
                                             'CommodityCode': i.hsn or '', 'GoodsDescription': (i.name or '')[:50],
                                             'CountryOfOrigin': i.origin_country or request.pickup.country_code,
                                             'CustomsValue': {'Value': round(i.unit_price * (i.qty or 1), 2),
                                                              'CurrencyCode': request.currency}}
                                            for i in request.items]
        data = self._call('create', '/Shipping/Service_1_0.svc/json/CreateShipments', {
            'LabelInfo': {'ReportID': 9201, 'ReportType': 'URL'}, 'Shipments': [shipment], '_ref': request.reference})
        result = (data.get('Shipments') or [{}])[0]
        if result.get('HasErrors') or not result.get('ID'):
            raise ProviderError('Aramex shipment failed: %s' % self._notes(data), payload=data)
        label = result.get('ShipmentLabel') or {}
        return ShipmentResult(tracking_number=str(result['ID']), carrier_reference=str(result['ID']), price=0.0,
                              currency=request.currency, service_code=product_type, courier_name='Aramex',
                              label_pdf=b64_to_bytes(label.get('LabelFileContents')), label_url=label.get('LabelURL') or '')

    def get_label(self, tracking_number, carrier_reference=''):
        data = self._call('label', '/Shipping/Service_1_0.svc/json/PrintLabel', {
            'ShipmentNumber': tracking_number, 'LabelInfo': {'ReportID': 9201, 'ReportType': 'URL'}})
        label = data.get('ShipmentLabel') or {}
        return LabelResult(pdf=b64_to_bytes(label.get('LabelFileContents')), url=label.get('LabelURL') or '')

    def schedule_pickup(self, tracking_numbers, pickup_date, carrier_references=None, package_count=1):
        extra = self.config.extra or {}
        location = extra.get('pickup_address')
        if not location:
            raise ProviderError('Aramex: add {"pickup_address": {"Line1": "...", "City": "...", "PostCode": "...", '
                                '"CountryCode": "IN", "PersonName": "...", "PhoneNumber1": "..."}} to Extra Settings.')
        contact = {k: location.get(k, '') for k in ('PersonName', 'CompanyName', 'PhoneNumber1', 'CellPhone', 'EmailAddress')}
        address = {k: v for k, v in location.items() if k not in contact}
        data = self._call('pickup', '/Shipping/Service_1_0.svc/json/CreatePickup', {'Pickup': {
            'PickupAddress': address, 'PickupContact': contact, 'PickupLocation': 'Reception',
            'PickupDate': wcf_date(pickup_date), 'ReadyTime': wcf_date(pickup_date),
            'LastPickupTime': wcf_date(pickup_date + timedelta(hours=4)), 'ClosingTime': wcf_date(pickup_date + timedelta(hours=5)),
            'Reference1': ','.join(tracking_numbers)[:50], 'Status': 'Ready',
            'PickupItems': [{'ProductGroup': 'DOM', 'ProductType': 'ONP', 'NumberOfShipments': len(tracking_numbers),
                             'NumberOfPieces': max(package_count, 1), 'Payment': 'P',
                             'ShipmentWeight': {'Unit': 'KG', 'Value': 1}}]}})
        return str(((data.get('ProcessedPickup') or {}).get('GUID')) or ((data.get('ProcessedPickup') or {}).get('ID')) or '')

    def track(self, tracking_numbers):
        results = {}
        for start in range(0, len(tracking_numbers), 50):
            chunk = tracking_numbers[start:start + 50]
            data = self._call('track', '/Tracking/Service_1_0.svc/json/TrackShipments', {
                'Shipments': chunk, 'GetLastTrackingUpdateOnly': False})
            for item in data.get('TrackingResults') or []:
                awb = str(item.get('Key'))
                events = [TrackingEvent(time=parse_datetime(u.get('UpdateDateTime')), raw_status=u.get('UpdateCode') or '',
                                        description=u.get('UpdateDescription') or '', location=u.get('UpdateLocation') or '')
                          for u in item.get('Value') or []]
                latest = max((e for e in events if e.time), key=lambda e: e.time, default=None)
                if latest:
                    latest.normalized = self.normalize_status(latest.raw_status, latest.description)
                results[awb] = self._result_from_events(
                    awb, events, raw_status=latest.raw_status if latest else '', description=latest.description if latest else '',
                    ndr_reason=latest.description if latest and latest.normalized == 'ndr' else '')
                if latest and results[awb].normalized is None:
                    results[awb].normalized = latest.normalized
            for awb in chunk:
                results.setdefault(awb, TrackingResult(tracking_number=awb, found=False))
        return results
