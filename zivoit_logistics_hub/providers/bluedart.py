# -*- coding: utf-8 -*-
"""Blue Dart (DHL eCommerce India) API gateway.

Docs: https://developer.dhl.com (Blue Dart APIs). Base ``https://apigateway.bluedart.com/in/transportation``.
JWT via ``GET /token/v1/login`` with headers ``ClientID`` / ``clientSecret`` (consumer key / secret),
then header ``JWTToken`` on every call. Business calls also carry a ``Profile`` (LoginID + LicenceKey).
Cancellation is only possible before in-scan. No public rate API: rates come from the rate card.
"""
from datetime import datetime, timedelta

from .base import (BaseProvider, ProviderError, ServiceabilityResult, ShipmentResult, TrackingEvent,
                   TrackingResult, b64_to_bytes, parse_datetime, register)


@register
class BlueDartProvider(BaseProvider):
    code = 'bluedart'
    name = 'Blue Dart'
    production_url = 'https://apigateway.bluedart.com/in/transportation'
    sandbox_url = 'https://apigateway-sandbox.bluedart.com/in/transportation'
    tracking_url = 'https://www.bluedart.com/web/guest/trackdartresultthirdparty?trackFor=0&trackNo={awb}'
    capabilities = {'auth', 'serviceability', 'create', 'label', 'track', 'cancel', 'pickup'}
    credential_fields = ('client_id', 'client_secret', 'username', 'license_key', 'customer_code', 'account_number')
    token_lifetime = timedelta(hours=11)

    # ScanType: UD (undelivered/forward), DL delivered, RT returned, PU picked up
    STATUS_MAP = {
        'shipment booked': 'booked', 'booked': 'booked', 'pickup scheduled': 'pickup_scheduled',
        'shipment picked up': 'picked_up', 'pu': 'picked_up', 'picked up': 'picked_up',
        'in transit': 'in_transit', 'shipment in transit': 'in_transit', 'ud': 'in_transit', 'network': 'in_transit',
        'shipment out for delivery': 'out_for_delivery', 'out for delivery': 'out_for_delivery',
        'shipment delivered': 'delivered', 'delivered': 'delivered', 'dl': 'delivered',
        'shipment returned': 'rto_delivered', 'rt': 'rto_in_transit', 'return to origin': 'rto_initiated',
        'consignee not available': 'ndr', 'shipment undelivered': 'ndr', 'undelivered': 'ndr',
        'shipment cancelled': 'cancelled', 'cancelled': 'cancelled', 'lost': 'lost',
    }

    def _profile(self):
        self.require_fields('username', 'license_key')
        return {'LoginID': self.config.username, 'LicenceKey': self.config.license_key, 'Api_type': 'S'}

    def _headers(self):
        return {'JWTToken': self.get_token(), 'Content-Type': 'application/json'}

    def authenticate(self):
        self.require_fields('client_id', 'client_secret')
        data = self.http.request('GET', self.url('/token/v1/login'), operation='auth',
                                 headers={'ClientID': self.config.client_id, 'clientSecret': self.config.client_secret})
        token = isinstance(data, dict) and data.get('JWTToken')
        if not token:
            raise ProviderError('Blue Dart JWT generation failed: %s' % self.http.extract_error(data))
        return token, self.token_lifetime

    @staticmethod
    def _check(result, what):
        if not isinstance(result, dict):
            raise ProviderError('Blue Dart %s: unexpected response' % what)
        if result.get('IsError') or result.get('IsErrorInPU'):
            msgs = [s.get('StatusInformation') for s in result.get('Status') or [] if s.get('StatusInformation')]
            raise ProviderError('Blue Dart %s failed: %s' % (what, '; '.join(msgs) or 'unknown error'), payload=result)
        return result

    def check_serviceability(self, pickup_zip, delivery_zip, weight_kg=0.5, cod=False):
        data = self.http.request('POST', self.url('/finder/v1/GetServicesforPincode'), operation='serviceability',
                                 json_body={'pinCode': delivery_zip, 'profile': self._profile()}, headers=self._headers())
        res = (data or {}).get('GetServicesforPincodeResult') or {}
        if res.get('IsError'):
            return ServiceabilityResult(zip=delivery_zip, serviceable=False, message=res.get('ErrorMessage') or '')
        cod_ok = any(str(res.get(k, '')).lower() in ('yes', 'y', 'true') for k in ('eTailCODAirInbound', 'eTailCODGroundInbound'))
        prepaid_ok = any(str(res.get(k, '')).lower() in ('yes', 'y', 'true') for k in ('eTailPrePaidAirInbound', 'eTailPrePaidGroundInbound', 'ApexInbound'))
        return ServiceabilityResult(zip=delivery_zip, serviceable=cod_ok or prepaid_ok, cod=cod_ok, prepaid=prepaid_ok,
                                    pickup=str(res.get('ApexOutbound', '')).lower() in ('yes', 'y', 'true'),
                                    message=res.get('AreaCode') or '')

    def create_shipment(self, request):
        p, d = request.pickup, request.delivery
        pickup_dt = datetime.now() + timedelta(hours=2)
        body = {
            'Request': {
                'Consignee': {
                    'ConsigneeName': d.name[:30], 'ConsigneeAddress1': (d.street or '')[:30],
                    'ConsigneeAddress2': (d.street2 or '')[:30], 'ConsigneeAddress3': d.city[:30],
                    'ConsigneePincode': d.zip, 'ConsigneeMobile': d.clean_phone, 'ConsigneeEmailID': d.email or '',
                },
                'Shipper': {
                    'CustomerCode': self.config.customer_code, 'CustomerName': p.company or p.name,
                    'CustomerAddress1': (p.street or '')[:30], 'CustomerAddress2': (p.street2 or '')[:30],
                    'CustomerAddress3': p.city[:30], 'CustomerPincode': p.zip, 'CustomerMobile': p.clean_phone,
                    'OriginArea': (self.config.extra or {}).get('origin_area') or self.config.account_number,
                    'Sender': p.name, 'IsToPayCustomer': False,
                },
                'Services': {
                    'ProductCode': (request.service_code or self.config.default_service_code or 'A')[:1],
                    'SubProductCode': 'C' if request.is_cod else 'P',
                    'ActualWeight': request.total_weight, 'PieceCount': len(request.parcels),
                    'DeclaredValue': round(request.declared_value, 2),
                    'CollectableAmount': round(request.cod_amount, 2) if request.is_cod else 0,
                    'CreditReferenceNo': request.reference[:20],
                    'InvoiceNo': request.invoice_number or '',
                    'PickupDate': '/Date(%d)/' % int(pickup_dt.timestamp() * 1000),
                    'PickupTime': pickup_dt.strftime('%H%M'),
                    'RegisterPickup': False, 'PDFOutputNotRequired': False,
                    'Dimensions': [{'Length': par.length_cm, 'Breadth': par.width_cm, 'Height': par.height_cm, 'Count': 1}
                                   for par in request.parcels],
                    'itemdtl': [{'ItemName': i.name[:50], 'Itemquantity': int(i.qty or 1), 'ItemValue': round(i.unit_price * (i.qty or 1), 2),
                                 'HSCode': i.hsn or ''} for i in request.items[:10]],
                },
            },
            'Profile': self._profile(),
        }
        data = self.http.request('POST', self.url('/waybill/v1/GenerateWayBill'), operation='create',
                                 json_body=body, headers=self._headers())
        res = self._check((data or {}).get('GenerateWayBillResult') or data, 'waybill generation')
        awb = res.get('AWBNo')
        if not awb:
            raise ProviderError('Blue Dart did not return an AWB number.', payload=data)
        return ShipmentResult(tracking_number=str(awb), carrier_reference=str(res.get('TokenNumber') or request.reference),
                              service_code=body['Request']['Services']['ProductCode'], courier_name='Blue Dart',
                              label_pdf=b64_to_bytes(res.get('AWBPrintContent')),
                              edd=parse_datetime(res.get('ShipmentPickupDate')), raw={'AWBNo': awb, 'Status': res.get('Status')})

    def cancel(self, tracking_number, carrier_reference=''):
        data = self.http.request('POST', self.url('/waybill/v1/CancelWaybill'), operation='cancel',
                                 json_body={'Request': {'AWBNo': tracking_number}, 'Profile': self._profile()},
                                 headers=self._headers())
        self._check((data or {}).get('CancelWaybillResult') or data, 'cancellation')
        return True

    def schedule_pickup(self, tracking_numbers, pickup_date, carrier_references=None, package_count=1):
        cfg = self.config
        data = self.http.request('POST', self.url('/pickup/v1/GetPickup'), operation='pickup', json_body={
            'request': {
                'AWBNo': list(tracking_numbers), 'CustomerCode': cfg.customer_code,
                'AreaCode': (cfg.extra or {}).get('origin_area') or cfg.account_number,
                'ShipmentPickupDate': '/Date(%d)/' % int(pickup_date.timestamp() * 1000),
                'ShipmentPickupTime': pickup_date.strftime('%H%M') if pickup_date.hour else '1400',
                'NumberofPieces': max(package_count, len(tracking_numbers)), 'ProductCode': 'A',
            },
            'profile': self._profile(),
        }, headers=self._headers())
        res = self._check((data or {}).get('GetPickupResult') or data, 'pickup registration')
        return str(res.get('TokenNumber') or 'scheduled')

    def track(self, tracking_numbers):
        results = {awb: TrackingResult(tracking_number=awb, found=False) for awb in tracking_numbers}
        data = self.http.request('GET', self.url('/tracking/v1/shipment'), operation='track', params={
            'handler': 'tnt', 'action': 'custawbquery', 'loginid': self.config.username,
            'awb': 'awb', 'numbers': ','.join(tracking_numbers), 'format': 'json',
            'lickey': self.config.license_key, 'verno': 1, 'scan': 1,
        }, headers=self._headers())
        shipments = ((data or {}).get('ShipmentData') or {}).get('Shipment') or []
        if isinstance(shipments, dict):
            shipments = [shipments]
        for ship in shipments:
            awb = str(ship.get('WaybillNo'))
            events = []
            for scan in ship.get('Scans') or []:
                sd = scan.get('ScanDetail') or scan
                when = parse_datetime('%s %s' % (sd.get('ScanDate') or '', sd.get('ScanTime') or '')) or parse_datetime(sd.get('ScanDate'))
                events.append(TrackingEvent(time=when, raw_status=sd.get('Scan') or sd.get('ScanCode') or '',
                                            description=sd.get('Scan') or '', location=sd.get('ScannedLocation') or ''))
            raw = ship.get('Status') or ''
            res = self._result_from_events(awb, events, raw_status=raw, description=raw,
                                           edd=parse_datetime(ship.get('ExpectedDeliveryDate')))
            if res.normalized is None and ship.get('StatusType'):
                res.normalized = self.normalize_status(ship['StatusType'])
            results[awb] = res
        return results
