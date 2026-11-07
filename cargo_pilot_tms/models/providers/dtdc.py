# -*- coding: utf-8 -*-
from odoo import _

from .base import CourierError, CourierProvider, guess_status, parse_dt

TRACK_BASE = 'https://blktracksvc.dtdc.com/dtdc-api'


class DtdcProvider(CourierProvider):
    """DTDC booking API (Shipsy platform, api-key) plus the DTDC tracking service (username / password)."""
    code = 'dtdc'
    label = 'DTDC'
    capabilities = {'rate': False, 'serviceability': False, 'book': True, 'label': True, 'track': True,
                    'cancel': True, 'pickup': False, 'ndr': False, 'webhook': True}
    base_urls = {'test': 'https://demodashboardapi.shipsy.in', 'production': 'https://dtdcapi.shipsy.io'}
    tracking_url = 'https://www.dtdc.in/trace.asp?strCnno={awb}'
    token_hours = 12

    def _auth_headers(self):
        self._require('api_key')
        return {'api-key': self.account.sudo().api_key, 'Content-Type': 'application/json'}

    def _track_token(self):
        acc = self.account.sudo()
        self._require('username', 'password')

        def fetch():
            text = self._request('token', 'GET', TRACK_BASE + '/api/dtdc/authenticate', auth=False, expect='text',
                                 params={'username': acc.username, 'password': acc.password})
            return (text or '').strip().strip('"')
        return self._cached_token(fetch)

    def test_connection(self):
        self._auth_headers()
        msg = [_('Booking key saved.')]
        if self.account.username:
            self.account.sudo().write({'token': False, 'token_expiry': False})
            self._track_token()
            msg.append(_('Tracking login works.'))
        return ' '.join(msg)

    def rates(self, parcel):
        return [{'courier_name': 'DTDC', 'service': self.account.service_code or 'B2C PRIORITY',
                 'price': self.account._cp_rate_card_price(parcel), 'days': self.account.rate_card_days or 0,
                 'cod': self.account.supports_cod, 'price_from_card': True, 'note': _('Price from your rate card')}]

    def create_shipment(self, parcel, option=None):
        acc = self.account.sudo()
        self._require('customer_code')
        to = self.address(parcel.recipient_partner_id)
        frm = self.address(parcel.sender_partner_id)
        length, width, height = parcel._cp_dims()
        cod = parcel.payment_mode == 'cod'

        def details(a):
            return {'name': a['name'], 'phone': a['phone'], 'alternate_phone': a['phone'],
                    'address_line_1': a['street'] or a['full_street'], 'address_line_2': a['street2'],
                    'pincode': a['zip'], 'city': a['city'], 'state': a['state']}

        consignment = {
            'customer_code': acc.customer_code, 'service_type_id': acc.service_code or 'B2C PRIORITY',
            'load_type': 'NON-DOCUMENT', 'consignment_type': 'Forward',
            'description': parcel.contents or 'Goods', 'dimension_unit': 'cm',
            'length': length, 'width': width, 'height': height, 'weight_unit': 'kg',
            'weight': round(parcel.total_weight_kg, 3), 'declared_value': parcel.declared_value,
            'num_pieces': parcel.piece_count, 'origin_details': details(frm), 'destination_details': details(to),
            'customer_reference_number': parcel.reference or parcel.name,
            'cod_collection_mode': 'cash' if cod else '', 'cod_amount': parcel.cod_amount if cod else '',
            'commodity_id': acc.commodity_code or '', 'invoice_number': parcel.invoice_number or '',
            'invoice_date': parcel.invoice_date.strftime('%d %b %Y') if parcel.invoice_date else '',
            'eway_bill': parcel.eway_bill_no or '', 'is_risk_surcharge_applicable': False,
            'pieces_detail': [{'description': parcel.contents or 'Goods',
                               'declared_value': round(parcel.declared_value / max(parcel.piece_count, 1), 2),
                               'weight': p.weight_kg, 'height': p.height_cm, 'length': p.length_cm,
                               'width': p.width_cm}
                              for p in parcel.package_ids for _i in range(max(p.quantity, 1))],
        }
        data = self._request('book', 'POST', self.base_url + '/api/customer/integration/consignment/softdata',
                             json_body={'consignments': [consignment]})
        rows = data.get('data') or []
        row = rows[0] if rows else {}
        if not row.get('success') or not row.get('reference_number'):
            raise CourierError(_('DTDC did not book the consignment: %s',
                                 row.get('message') or data.get('message') or data))
        return {'awb': str(row['reference_number']), 'courier_name': 'DTDC',
                'service': acc.service_code or 'B2C PRIORITY',
                'provider_ref': row.get('customer_reference_number') or ''}

    def fetch_label(self, parcel):
        response = self._request('label', 'GET',
                                 self.base_url + '/api/customer/integration/consignment/shippinglabel/stream',
                                 expect='bytes', params={'reference_number': parcel.awb,
                                                         'label_code': 'SHIP_LABEL_4X6', 'label_format': 'pdf'})
        if response.content[:4] == b'%PDF':
            return response.content
        try:
            payload = response.json()
        except ValueError:
            return None
        rows = payload.get('data') or []
        if isinstance(rows, list) and rows:
            return self._pdf_from(rows[0].get('label'))
        return None

    def cancel(self, parcel):
        acc = self.account.sudo()
        data = self._request('cancel', 'POST', self.base_url + '/api/customer/integration/consignment/cancel',
                             json_body={'AWBNo': [parcel.awb], 'customerCode': acc.customer_code})
        if data.get('success') or str(data.get('status')).upper() == 'OK':
            return True
        raise CourierError(_('DTDC did not cancel %(awb)s: %(msg)s', awb=parcel.awb,
                             msg=data.get('message') or data))

    # ------------------------------------------------------------------
    def _parse(self, data):
        header = data.get('trackHeader') or {}
        events = []
        for d in data.get('trackDetails') or []:
            when = parse_dt('%s %s' % (d.get('strActionDate', ''), d.get('strActionTime', '')),
                            formats=('%d%m%Y %H%M', '%d%m%Y %H:%M'))
            desc = d.get('strAction') or ''
            events.append({'time': when, 'code': d.get('strCode') or '',
                           'description': ' - '.join(x for x in (desc, d.get('sTrRemarks')) if x),
                           'location': d.get('strOrigin') or d.get('strDestination') or '',
                           'status': guess_status(desc, d.get('sTrRemarks'))})
        text = header.get('strStatus') or ''
        return {'status': guess_status(text) or (events[-1]['status'] if events else ''),
                'status_text': text, 'events': events,
                'expected_delivery': parse_dt(header.get('strExpectedDeliveryDate'), formats=('%d%m%Y',))}

    def track(self, parcels):
        token = self._track_token()
        result = {}
        for parcel in parcels.filtered('awb'):
            data = self._request('track', 'POST', TRACK_BASE + '/rest/JSONCnTrk/getTrackDetails', auth=False,
                                 headers={'X-Access-Token': token, 'Content-Type': 'application/json'},
                                 json_body={'trkType': 'cnno', 'strcnno': parcel.awb, 'addtnlDtl': 'Y'})
            if data.get('statusFlag') is False:
                continue
            result[parcel.awb] = self._parse(data)
        return result

    def parse_webhook(self, payload, headers=None):
        """DTDC / Shipsy push: {"shipment": {"strShipmentNo": ...}, "shipmentStatus": [...]} or tracking JSON."""
        result = {}
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            if 'trackHeader' in item:
                awb = (item['trackHeader'] or {}).get('strShipmentNo')
                if awb:
                    result[str(awb)] = self._parse(item)
                continue
            awb = item.get('reference_number') or item.get('awb') or item.get('AWBNo')
            text = item.get('status') or item.get('type') or item.get('event') or ''
            if awb:
                result[str(awb)] = {'status': guess_status(text), 'status_text': text, 'events': [{
                    'time': parse_dt(item.get('event_time') or item.get('timestamp')), 'code': item.get('type', ''),
                    'description': text, 'location': item.get('hub_name') or item.get('location') or '',
                    'status': guess_status(text)}], 'expected_delivery': None}
        return result
