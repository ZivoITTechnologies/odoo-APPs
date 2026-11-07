# -*- coding: utf-8 -*-
"""FedEx REST APIs (OAuth2 client credentials).

Production ``https://apis.fedex.com``, sandbox ``https://apis-sandbox.fedex.com``.
Rate: /rate/v1/rates/quotes · Ship: /ship/v1/shipments · Cancel: /ship/v1/shipments/cancel ·
Track: /track/v1/trackingnumbers.
"""
from datetime import date, timedelta

from .base import (BaseProvider, ProviderError, RateOption, ShipmentResult, TrackingEvent, TrackingResult,
                   b64_to_bytes, parse_datetime, register)


@register
class FedExProvider(BaseProvider):
    code = 'fedex'
    name = 'FedEx'
    country = 'INTL'
    production_url = 'https://apis.fedex.com'
    sandbox_url = 'https://apis-sandbox.fedex.com'
    tracking_url = 'https://www.fedex.com/fedextrack/?trknbr={awb}'
    capabilities = {'auth', 'rate', 'create', 'label', 'track', 'cancel'}
    credential_fields = ('client_id', 'client_secret', 'account_number')

    # latestStatusDetail.code / scanEvents.eventType
    STATUS_MAP = {
        'oc': 'booked', 'label created': 'booked', 'pu': 'picked_up', 'pickup': 'picked_up', 'picked up': 'picked_up',
        'it': 'in_transit', 'ar': 'in_transit', 'dp': 'in_transit', 'in transit': 'in_transit', 'at': 'in_transit',
        'od': 'out_for_delivery', 'on fedex vehicle for delivery': 'out_for_delivery', 'out for delivery': 'out_for_delivery',
        'dl': 'delivered', 'delivered': 'delivered',
        'de': 'ndr', 'delivery exception': 'ndr', 'du': 'ndr', 'se': 'exception', 'shipment exception': 'exception',
        'rs': 'rto_in_transit', 'returned to shipper': 'rto_delivered', 'ca': 'cancelled', 'cancelled': 'cancelled',
        'hl': 'out_for_delivery', 'hp': 'out_for_delivery',
    }

    def authenticate(self):
        self.require_fields('client_id', 'client_secret')
        data = self.http.request('POST', self.url('/oauth/token'), operation='auth', data={
            'grant_type': 'client_credentials', 'client_id': self.config.client_id,
            'client_secret': self.config.client_secret,
        }, headers={'Content-Type': 'application/x-www-form-urlencoded'})
        token = isinstance(data, dict) and data.get('access_token')
        if not token:
            raise ProviderError('FedEx OAuth failed: %s' % self.http.extract_error(data))
        return token, timedelta(seconds=int(data.get('expires_in') or 3600) - 120)

    def _headers(self):
        return {'Authorization': 'Bearer %s' % self.get_token(), 'Content-Type': 'application/json',
                'X-locale': 'en_US'}

    @staticmethod
    def _addr(a):
        return {'streetLines': [s for s in (a.street, a.street2) if s][:2] or ['-'], 'city': a.city,
                'stateOrProvinceCode': (a.state_code or '')[:2], 'postalCode': a.zip, 'countryCode': a.country_code}

    def _packages(self, req):
        return [{'weight': {'units': 'KG', 'value': round(max(p.weight_kg, 0.1), 2)},
                 'dimensions': {'length': int(p.length_cm), 'width': int(p.width_cm), 'height': int(p.height_cm),
                                'units': 'CM'},
                 **({'declaredValue': {'amount': round(p.declared_value, 2), 'currency': req.currency}} if p.declared_value else {})}
                for p in req.parcels]

    def get_rates(self, request):
        self.require_fields('account_number')
        body = {
            'accountNumber': {'value': self.config.account_number},
            'rateRequestControlParameters': {'returnTransitTimes': True},
            'requestedShipment': {
                'shipper': {'address': self._addr(request.pickup)},
                'recipient': {'address': self._addr(request.delivery)},
                'pickupType': 'DROPOFF_AT_FEDEX_LOCATION',
                'rateRequestType': ['ACCOUNT', 'LIST'],
                'preferredCurrency': request.currency,
                'requestedPackageLineItems': self._packages(request),
            },
        }
        if request.pickup.country_code != request.delivery.country_code:
            body['requestedShipment']['customsClearanceDetail'] = self._customs(request)
        data = self.http.request('POST', self.url('/rate/v1/rates/quotes'), operation='rate', json_body=body,
                                 headers=self._headers())
        rates = []
        for det in ((data or {}).get('output') or {}).get('rateReplyDetails') or []:
            rated = det.get('ratedShipmentDetails') or [{}]
            best = next((r for r in rated if r.get('rateType') == 'ACCOUNT'), rated[0])
            transit = (det.get('operationalDetail') or {}).get('transitTime') or ''
            days = {'ONE_DAY': 1, 'TWO_DAYS': 2, 'THREE_DAYS': 3, 'FOUR_DAYS': 4, 'FIVE_DAYS': 5,
                    'SIX_DAYS': 6, 'SEVEN_DAYS': 7}.get(transit, 0)
            rates.append(RateOption(service_code=det.get('serviceType'), service_name=det.get('serviceName') or det.get('serviceType'),
                                    courier_name='FedEx', price=float(best.get('totalNetCharge') or 0.0),
                                    currency=best.get('currency') or request.currency, tat_days=days,
                                    cod_available=False))
        return rates

    @staticmethod
    def _customs(req):
        return {
            'dutiesPayment': {'paymentType': 'SENDER'},
            'commodities': [{'description': i.name[:35], 'quantity': int(i.qty or 1), 'quantityUnits': 'PCS',
                             'countryOfManufacture': req.pickup.country_code, 'harmonizedCode': i.hsn or '',
                             'weight': {'units': 'KG', 'value': round(max(i.weight_kg, 0.1), 2)},
                             'customsValue': {'amount': round(i.unit_price * (i.qty or 1), 2), 'currency': req.currency}}
                            for i in req.items] or [{'description': 'Goods', 'quantity': 1, 'quantityUnits': 'PCS',
                                                      'countryOfManufacture': req.pickup.country_code,
                                                      'weight': {'units': 'KG', 'value': req.total_weight},
                                                      'customsValue': {'amount': req.declared_value, 'currency': req.currency}}],
        }

    def create_shipment(self, request):
        self.require_fields('account_number')

        def party(a):
            return {'contact': {'personName': a.name, 'companyName': a.company or '', 'phoneNumber': a.clean_phone or '0000000000',
                                'emailAddress': a.email or ''}, 'address': self._addr(a)}
        shipment = {
            'shipper': party(request.pickup),
            'recipients': [party(request.delivery)],
            'shipDatestamp': date.today().isoformat(),
            'serviceType': request.service_code or self.config.default_service_code or 'STANDARD_OVERNIGHT',
            'packagingType': 'YOUR_PACKAGING',
            'pickupType': 'USE_SCHEDULED_PICKUP',
            'blockInsightVisibility': False,
            'shippingChargesPayment': {'paymentType': 'SENDER'},
            'labelSpecification': {'imageType': 'PDF', 'labelStockType': 'PAPER_4X6'},
            'requestedPackageLineItems': self._packages(request),
        }
        if request.pickup.country_code != request.delivery.country_code:
            shipment['customsClearanceDetail'] = self._customs(request)
        if request.reference:
            for pkg in shipment['requestedPackageLineItems']:
                pkg['customerReferences'] = [{'customerReferenceType': 'CUSTOMER_REFERENCE', 'value': request.reference[:30]}]
        data = self.http.request('POST', self.url('/ship/v1/shipments'), operation='create', json_body={
            'labelResponseOptions': 'LABEL', 'accountNumber': {'value': self.config.account_number},
            'requestedShipment': shipment,
        }, headers=self._headers())
        txs = ((data or {}).get('output') or {}).get('transactionShipments') or []
        if not txs:
            raise ProviderError('FedEx shipment failed: %s' % self.http.extract_error(data), payload=data)
        tx = txs[0]
        label = None
        for piece in tx.get('pieceResponses') or []:
            for doc in piece.get('packageDocuments') or []:
                if doc.get('encodedLabel'):
                    label = b64_to_bytes(doc['encodedLabel'])
                    break
            if label:
                break
        rating = ((tx.get('completedShipmentDetail') or {}).get('shipmentRating') or {}).get('shipmentRateDetails') or [{}]
        return ShipmentResult(tracking_number=tx.get('masterTrackingNumber'), carrier_reference=tx.get('masterTrackingNumber'),
                              price=float(rating[0].get('totalNetCharge') or 0.0), currency=rating[0].get('currency') or request.currency,
                              service_code=tx.get('serviceType') or shipment['serviceType'], courier_name='FedEx',
                              label_pdf=label, raw={'serviceType': tx.get('serviceType')})

    def cancel(self, tracking_number, carrier_reference=''):
        data = self.http.request('PUT', self.url('/ship/v1/shipments/cancel'), operation='cancel', json_body={
            'accountNumber': {'value': self.config.account_number}, 'trackingNumber': tracking_number,
            'deletionControl': 'DELETE_ALL_PACKAGES',
        }, headers=self._headers())
        if not ((data or {}).get('output') or {}).get('cancelledShipment'):
            raise ProviderError('FedEx cancellation refused: %s' % self.http.extract_error(data))
        return True

    def track(self, tracking_numbers):
        results = {}
        for chunk_start in range(0, len(tracking_numbers), 30):  # FedEx accepts 30 numbers per call
            chunk = tracking_numbers[chunk_start:chunk_start + 30]
            data = self.http.request('POST', self.url('/track/v1/trackingnumbers'), operation='track', json_body={
                'includeDetailedScans': True,
                'trackingInfo': [{'trackingNumberInfo': {'trackingNumber': n}} for n in chunk],
            }, headers=self._headers())
            for complete in ((data or {}).get('output') or {}).get('completeTrackResults') or []:
                awb = complete.get('trackingNumber')
                tr = (complete.get('trackResults') or [{}])[0]
                if tr.get('error'):
                    results[awb] = TrackingResult(tracking_number=awb, found=False,
                                                  description=(tr['error'] or {}).get('message', ''))
                    continue
                events = [TrackingEvent(time=parse_datetime(ev.get('date')), raw_status=ev.get('eventType') or '',
                                        description=ev.get('eventDescription') or '',
                                        location=', '.join(x for x in ((ev.get('scanLocation') or {}).get('city'),
                                                                       (ev.get('scanLocation') or {}).get('countryCode')) if x))
                          for ev in tr.get('scanEvents') or []]
                latest = tr.get('latestStatusDetail') or {}
                edd = None
                for dt in tr.get('dateAndTimes') or []:
                    if dt.get('type') in ('ESTIMATED_DELIVERY', 'ACTUAL_DELIVERY'):
                        edd = parse_datetime(dt.get('dateTime'))
                results[awb] = self._result_from_events(
                    awb, events, raw_status=latest.get('code') or '', description=latest.get('description') or '', edd=edd,
                    ndr_reason=(latest.get('ancillaryDetails') or [{}])[0].get('reasonDescription', '') if latest.get('code') in ('DE', 'DU') else '')
            for n in chunk:
                results.setdefault(n, TrackingResult(tracking_number=n, found=False))
        return results
