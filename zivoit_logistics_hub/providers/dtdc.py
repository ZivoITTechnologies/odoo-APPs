# -*- coding: utf-8 -*-
"""DTDC (Shipsy powered customer integration API).

Booking/label/cancel live on the Shipsy gateway (``https://dtdcapi.shipsy.io``) with header ``api-key``.
Tracking uses DTDC's tracking service (``https://blktracksvc.dtdc.com``) authenticated with the
tracking username/password DTDC issues alongside the API key. DTDC has no public rate API, so
rates come from the account's contract rate card (handled by the Odoo layer).
"""
from .base import (BaseProvider, LabelResult, ProviderError, ShipmentResult, TrackingEvent, TrackingResult,
                   parse_datetime, register)


@register
class DTDCProvider(BaseProvider):
    code = 'dtdc'
    name = 'DTDC'
    production_url = 'https://dtdcapi.shipsy.io'
    sandbox_url = 'https://demodashboard.shipsy.in:3001'
    tracking_url = 'https://www.dtdc.in/tracking.asp?strCnno={awb}'
    capabilities = {'create', 'label', 'track', 'cancel'}
    credential_fields = ('api_key', 'customer_code', 'username', 'password')
    TRACK_URL = 'https://blktracksvc.dtdc.com/dtdc-api'

    STATUS_MAP = {
        'booked': 'booked', 'softdata upload': 'booked', 'bkd': 'booked',
        'pickup scheduled': 'pickup_scheduled', 'pickup awaited': 'pickup_scheduled',
        'picked up': 'picked_up', 'pck': 'picked_up',
        'in transit': 'in_transit', 'dispatched': 'in_transit', 'received': 'in_transit', 'inscan': 'in_transit',
        'out for delivery': 'out_for_delivery', 'ofd': 'out_for_delivery', 'out for delivery ': 'out_for_delivery',
        'delivered': 'delivered', 'dlv': 'delivered',
        'not delivered': 'ndr', 'nondelivery': 'ndr', 'undelivered': 'ndr', 'non delivery': 'ndr', 'ndl': 'ndr',
        'rto': 'rto_initiated', 'return to origin': 'rto_initiated', 'rto in transit': 'rto_in_transit',
        'rto delivered': 'rto_delivered', 'returned': 'rto_delivered',
        'cancelled': 'cancelled', 'canceled': 'cancelled', 'lost': 'lost',
    }

    def _headers(self):
        self.require_fields('api_key')
        return {'api-key': self.config.api_key, 'Content-Type': 'application/json'}

    def test_connection(self):
        self.require_fields('api_key', 'customer_code')
        if self.config.username and self.config.password:
            self._tracking_token()
            return 'DTDC: tracking login successful; booking API key stored.'
        return 'DTDC: booking credentials stored (tracking login not configured).'

    def create_shipment(self, request):
        self.require_fields('customer_code')
        p, d, (length, width, height) = request.pickup, request.delivery, request.max_dimensions

        def addr(a):
            return {'name': a.name, 'phone': a.clean_phone, 'alternate_phone': '',
                    'address_line_1': a.street or a.full_street, 'address_line_2': a.street2 or '',
                    'pincode': a.zip, 'city': a.city, 'state': a.state}
        consignment = {
            'customer_code': self.config.customer_code,
            'reference_number': '',
            'service_type_id': request.service_code or self.config.default_service_code or 'B2C PRIORITY',
            'load_type': 'NON-DOCUMENT',
            'description': ', '.join(i.name for i in request.items)[:100] or 'Goods',
            'dimension_unit': 'cm', 'length': length, 'width': width, 'height': height,
            'weight_unit': 'kg', 'weight': request.total_weight,
            'declared_value': round(request.declared_value, 2),
            'cod_amount': round(request.cod_amount, 2) if request.is_cod else 0,
            'cod_collection_mode': 'cash' if request.is_cod else '',
            'num_pieces': len(request.parcels),
            'customer_reference_number': request.reference,
            'commodity_id': (request.extra or {}).get('dtdc_commodity_id', ''),
            'invoice_number': request.invoice_number or '',
            'eway_bill': request.ewaybill or '',
            'origin_details': addr(p),
            'destination_details': addr(d),
            'pieces_detail': [{'description': par.description or 'Package', 'declared_value': round(par.declared_value, 2),
                               'weight': par.weight_kg, 'height': par.height_cm, 'length': par.length_cm,
                               'width': par.width_cm} for par in request.parcels],
        }
        data = self.http.request('POST', self.url('/api/customer/integration/consignment/softdata'), operation='create',
                                 json_body={'consignments': [consignment]}, headers=self._headers())
        rows = (data or {}).get('data') or []
        row = rows[0] if rows else {}
        if not row.get('success', True) or not row.get('reference_number'):
            raise ProviderError('DTDC booking failed: %s' % (row.get('message') or self.http.extract_error(data)),
                                payload=data)
        awb = row['reference_number']
        label = LabelResult()
        try:
            label = self.get_label(awb)
        except ProviderError:
            pass
        return ShipmentResult(tracking_number=awb, carrier_reference=row.get('customer_reference_number') or request.reference,
                              service_code=consignment['service_type_id'], courier_name='DTDC',
                              label_pdf=label.pdf, raw=data)

    def get_label(self, tracking_number, carrier_reference=''):
        content = self.http.request(
            'GET', self.url('/api/customer/integration/consignment/shippinglabel/stream'), operation='label',
            params={'reference_number': tracking_number, 'label_code': 'SHIP_LABEL_4X6', 'label_format': 'pdf'},
            headers=self._headers(), expect='bytes')
        if not isinstance(content, bytes) or not content.startswith(b'%PDF'):
            raise ProviderError('DTDC label not available: %s' % self.http.extract_error(content))
        return LabelResult(pdf=content, filename='Label-%s.pdf' % tracking_number)

    def cancel(self, tracking_number, carrier_reference=''):
        data = self.http.request('POST', self.url('/api/customer/integration/consignment/cancel'), operation='cancel',
                                 json_body={'AWBNo': [tracking_number], 'customerCode': self.config.customer_code},
                                 headers=self._headers())
        if isinstance(data, dict) and data.get('success') is False:
            raise ProviderError('DTDC cancellation refused: %s' % self.http.extract_error(data))
        return True

    # ------------------------------------------------------------- tracking
    def _tracking_token(self):
        self.require_fields('username', 'password')
        token = self.http.request('GET', '%s/api/dtdc/authenticate' % self._track_base(), operation='auth',
                                  params={'username': self.config.username, 'password': self.config.password},
                                  expect='text')
        if not token or not isinstance(token, str) or len(token) < 8:
            raise ProviderError('DTDC tracking authentication failed.')
        return token.strip().strip('"')

    def _track_base(self):
        return (self.config.extra or {}).get('tracking_url') or self.TRACK_URL

    def track(self, tracking_numbers):
        token = self._tracking_token()
        results = {}
        for awb in tracking_numbers:
            data = self.http.request('POST', '%s/rest/JSONCnTrk/getTrackDetails' % self._track_base(), operation='track',
                                     json_body={'trkType': 'cnno', 'strcnno': awb, 'addtnlDtl': 'Y'},
                                     headers={'X-Access-Token': token, 'Content-Type': 'application/json'})
            results[awb] = self._parse(awb, data or {})
        return results

    def _parse(self, awb, data):
        header = data.get('trackHeader') or {}
        if not header and not data.get('trackDetails'):
            return TrackingResult(tracking_number=awb, found=False, description=self.http.extract_error(data))
        events = []
        for det in data.get('trackDetails') or []:
            when = parse_datetime('%s%s' % (det.get('strActionDate') or '', (det.get('strActionTime') or '').zfill(4)))
            events.append(TrackingEvent(time=when, raw_status=det.get('strAction') or det.get('strCode') or '',
                                        description=det.get('sTrRemarks') or det.get('strAction') or '',
                                        location=det.get('strOrigin') or det.get('strDestination') or ''))
        raw = header.get('strStatus') or ''
        return self._result_from_events(awb, events, raw_status=raw, description=raw,
                                        edd=parse_datetime(header.get('strExpectedDeliveryDate')),
                                        ndr_reason=(header.get('strRemarks') or raw) if 'not delivered' in raw.lower() else '')
