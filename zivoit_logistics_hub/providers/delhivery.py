# -*- coding: utf-8 -*-
"""Delhivery B2C (Express) API.

Docs: Delhivery One developer portal. Production ``https://track.delhivery.com``,
staging ``https://staging-express.delhivery.com``. Header ``Authorization: Token <api token>``.
"""
import json
from datetime import timedelta

from .base import (BaseProvider, LabelResult, ProviderError, RateOption, ServiceabilityResult,
                   ShipmentResult, TrackingEvent, TrackingResult, parse_datetime, register, to_bool)


@register
class DelhiveryProvider(BaseProvider):
    code = 'delhivery'
    name = 'Delhivery'
    production_url = 'https://track.delhivery.com'
    sandbox_url = 'https://staging-express.delhivery.com'
    tracking_url = 'https://www.delhivery.com/track/package/{awb}'
    capabilities = {'serviceability', 'rate', 'create', 'label', 'track', 'cancel', 'pickup', 'ndr', 'webhook'}
    credential_fields = ('api_key', 'pickup_location_code')

    # Delhivery "Status" + "StatusType" (UD forward, DL delivered, RT return, PP pickup pending, CN cancelled)
    STATUS_MAP = {
        'manifested': 'booked', 'not picked': 'pickup_scheduled', 'pending': 'pickup_scheduled',
        'open': 'booked', 'scheduled': 'pickup_scheduled', 'pp': 'pickup_scheduled',
        'picked up': 'picked_up', 'pu': 'picked_up',
        'in transit': 'in_transit', 'ud': 'in_transit', 'dispatched': 'out_for_delivery',
        'out for delivery': 'out_for_delivery', 'delivered': 'delivered', 'dl': 'delivered',
        'rto': 'rto_initiated', 'rt': 'rto_in_transit', 'returned': 'rto_delivered', 'rto delivered': 'rto_delivered',
        'canceled': 'cancelled', 'cancelled': 'cancelled', 'cn': 'cancelled',
        'lost': 'lost', 'ls': 'lost', 'undelivered': 'ndr', 'not delivered': 'ndr',
    }

    def _headers(self, content='application/json'):
        self.require_fields('api_key')
        return {'Authorization': 'Token %s' % self.config.api_key, 'Content-Type': content}

    def normalize_status(self, raw_status, description=''):
        key = str(raw_status or '').strip().lower()
        if '|' in key:  # we encode "Status|StatusType"
            status, stype = (p.strip() for p in key.split('|', 1))
            if stype == 'dl':
                return 'rto_delivered' if status in ('rto', 'dto', 'returned') else 'delivered'
            if stype == 'rt':
                return 'rto_in_transit'
            if stype == 'cn':
                return 'cancelled'
            if stype == 'ls':
                return 'lost'
            return self.STATUS_MAP.get(status) or self.STATUS_MAP.get(stype)
        return self.STATUS_MAP.get(key)

    def test_connection(self):
        self.check_serviceability('', '110001')
        return 'Delhivery: API token accepted.'

    def check_serviceability(self, pickup_zip, delivery_zip, weight_kg=0.5, cod=False):
        data = self.http.request('GET', self.url('/c/api/pin-codes/json/'), operation='serviceability',
                                 params={'filter_codes': delivery_zip}, headers=self._headers())
        codes = (data or {}).get('delivery_codes') or []
        if not codes:
            return ServiceabilityResult(zip=delivery_zip, serviceable=False, message='Pincode not serviceable')
        pc = codes[0].get('postal_code') or {}
        cod_ok, prepaid_ok = to_bool(pc.get('cod')), to_bool(pc.get('pre_paid'))
        remark = (pc.get('remarks') or '').strip()
        serviceable = (cod_ok or prepaid_ok) and 'embargo' not in remark.lower()
        return ServiceabilityResult(zip=delivery_zip, serviceable=serviceable, cod=cod_ok, prepaid=prepaid_ok,
                                    pickup=to_bool(pc.get('pickup')), message=remark)

    def get_rates(self, request):
        rates = []
        for mode, label, tat in (('S', 'Surface', 5.0), ('E', 'Express', 3.0)):
            params = {'md': mode, 'ss': 'Delivered', 'o_pin': request.pickup.zip, 'd_pin': request.delivery.zip,
                      'cgm': int(round(request.chargeable_weight * 1000)),
                      'pt': 'COD' if request.is_cod else 'Pre-paid'}
            if request.is_cod:
                params['cod'] = round(request.cod_amount, 2)
            try:
                data = self.http.request('GET', self.url('/api/kinko/v1/invoice/charges/.json'), operation='rate',
                                         params=params, headers=self._headers())
            except ProviderError:
                continue
            row = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
            if row.get('total_amount') is None:
                continue
            rates.append(RateOption(service_code=mode, service_name='Delhivery %s' % label, courier_name='Delhivery',
                                    price=float(row['total_amount']), tat_days=tat, cod_available=True))
        return rates

    def create_shipment(self, request):
        d, length, width, height = request.delivery, *request.max_dimensions
        r = request.return_address or request.pickup
        shipment = {
            'name': d.name, 'add': d.full_street, 'pin': d.zip, 'city': d.city, 'state': d.state,
            'country': 'India', 'phone': d.clean_phone, 'order': request.reference,
            'payment_mode': 'COD' if request.is_cod else ('Pickup' if request.is_return else 'Prepaid'),
            'cod_amount': round(request.cod_amount, 2) if request.is_cod else 0,
            'total_amount': round(request.declared_value, 2),
            'products_desc': ', '.join(i.name for i in request.items)[:200] or 'Goods',
            'quantity': int(sum(i.qty for i in request.items) or 1),
            'weight': int(round(request.total_weight * 1000)),
            'shipment_length': length, 'shipment_width': width, 'shipment_height': height,
            'shipping_mode': 'Express' if request.service_code == 'E' else 'Surface',
            'seller_inv': request.invoice_number or '', 'waybill': '',
            'return_name': r.name, 'return_add': r.full_street, 'return_pin': r.zip, 'return_city': r.city,
            'return_state': r.state, 'return_country': 'India', 'return_phone': r.clean_phone,
            'order_date': request.order_date.strftime('%Y-%m-%d %H:%M:%S'),
        }
        if request.ewaybill:
            shipment['ewbn'] = request.ewaybill
        body = {'shipments': [shipment],
                'pickup_location': {'name': request.pickup_location_code or self.config.pickup_location_code}}
        data = self.http.request('POST', self.url('/api/cmu/create.json'), operation='create',
                                 data='format=json&data=%s' % json.dumps(body),
                                 headers=self._headers('application/x-www-form-urlencoded'))
        packages = (data or {}).get('packages') or []
        pkg = packages[0] if packages else {}
        if not pkg.get('waybill') or str(pkg.get('status', '')).lower() not in ('success', ''):
            remarks = pkg.get('remarks') or (data or {}).get('rmk') or data
            raise ProviderError('Delhivery booking failed: %s' % (remarks if isinstance(remarks, str) else json.dumps(remarks)),
                                payload=data)
        label = LabelResult()
        try:
            label = self.get_label(pkg['waybill'])
        except ProviderError:
            pass
        return ShipmentResult(tracking_number=pkg['waybill'], carrier_reference=pkg.get('refnum') or request.reference,
                              service_code=request.service_code or 'S', courier_name='Delhivery',
                              label_url=label.url, raw=data)

    def get_label(self, tracking_number, carrier_reference=''):
        data = self.http.request('GET', self.url('/api/p/packing_slip'), operation='label',
                                 params={'wbns': tracking_number, 'pdf': 'true'}, headers=self._headers())
        pkgs = (data or {}).get('packages') or []
        url = pkgs and (pkgs[0].get('pdf_download_link') or pkgs[0].get('pdf_url'))
        if not url:
            raise ProviderError('Delhivery label not available yet.')
        return LabelResult(url=url, filename='Label-%s.pdf' % tracking_number)

    def track(self, tracking_numbers):
        data = self.http.request('GET', self.url('/api/v1/packages/json/'), operation='track',
                                 params={'waybill': ','.join(tracking_numbers)}, headers=self._headers())
        results = {awb: TrackingResult(tracking_number=awb, found=False) for awb in tracking_numbers}
        for row in (data or {}).get('ShipmentData') or []:
            ship = row.get('Shipment') or {}
            results[str(ship.get('AWB'))] = self._parse_shipment(ship)
        return results

    def _parse_shipment(self, ship):
        events = []
        for scan in ship.get('Scans') or []:
            sd = scan.get('ScanDetail') or {}
            events.append(TrackingEvent(
                time=parse_datetime(sd.get('ScanDateTime') or sd.get('StatusDateTime')),
                raw_status='%s|%s' % (sd.get('Scan') or '', sd.get('ScanType') or ''),
                description=sd.get('Instructions') or sd.get('Scan') or '', location=sd.get('ScannedLocation') or ''))
        status = ship.get('Status') or {}
        raw = '%s|%s' % (status.get('Status') or '', status.get('StatusType') or '')
        instructions = status.get('Instructions') or ''
        result = self._result_from_events(
            str(ship.get('AWB')), events, raw_status=raw, description=instructions,
            edd=parse_datetime(ship.get('ExpectedDeliveryDate') or ship.get('PromisedDeliveryDate')),
            charged_weight=float(ship.get('ChargedWeight') or 0.0) / (1000.0 if float(ship.get('ChargedWeight') or 0) > 50 else 1.0))
        if result.normalized == 'ndr' or (result.normalized == 'in_transit' and 'not delivered' in instructions.lower()):
            result.normalized = 'ndr'
            result.ndr_reason = instructions
        return result

    def cancel(self, tracking_number, carrier_reference=''):
        data = self.http.request('POST', self.url('/api/p/edit'), operation='cancel',
                                 json_body={'waybill': tracking_number, 'cancellation': 'true'}, headers=self._headers())
        if isinstance(data, dict) and data.get('status') is False:
            raise ProviderError('Delhivery cancellation refused: %s' % self.http.extract_error(data))
        return True

    def schedule_pickup(self, tracking_numbers, pickup_date, carrier_references=None, package_count=1):
        data = self.http.request('POST', self.url('/fm/request/new/'), operation='pickup', json_body={
            'pickup_location': self.config.pickup_location_code, 'pickup_date': pickup_date.strftime('%Y-%m-%d'),
            'pickup_time': (pickup_date + timedelta(hours=0)).strftime('%H:%M:%S') if pickup_date.hour else '14:00:00',
            'expected_package_count': max(package_count, len(tracking_numbers)),
        }, headers=self._headers())
        return str((data or {}).get('pickup_id') or 'scheduled')

    def ndr_action(self, tracking_number, action, data=None):
        act = {'reattempt': 'RE-ATTEMPT', 'reschedule': 'PICKUP_RESCHEDULE', 'rto': 'RTO'}.get(action, action.upper())
        self.http.request('POST', self.url('/api/p/update'), operation='ndr',
                          json_body={'data': [{'waybill': tracking_number, 'act': act}]}, headers=self._headers())
        return True

    def parse_webhook(self, payload, headers=None):
        ship = payload.get('Shipment') or payload
        if not ship.get('AWB'):
            return []
        return [self._parse_shipment(ship)]
