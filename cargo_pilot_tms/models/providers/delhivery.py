# -*- coding: utf-8 -*-
import json

from odoo import _
from odoo.exceptions import UserError

from .base import CourierError, CourierProvider, guess_status, parse_dt


class DelhiveryProvider(CourierProvider):
    """Delhivery B2C express API (token auth).

    Endpoints follow Delhivery's client developer portal (Delhivery One > Developer Portal).
    """
    code = 'delhivery'
    label = 'Delhivery'
    capabilities = {'rate': True, 'serviceability': True, 'book': True, 'label': True, 'track': True,
                    'cancel': True, 'pickup': True, 'ndr': True, 'webhook': True}
    base_urls = {'test': 'https://staging-express.delhivery.com', 'production': 'https://track.delhivery.com'}
    tracking_url = 'https://www.delhivery.com/track-v2/package/{awb}'

    def _auth_headers(self):
        self._require('api_key')
        return {'Authorization': 'Token %s' % self.account.sudo().api_key, 'Accept': 'application/json'}

    # ------------------------------------------------------------------
    def test_connection(self):
        pin = (self.account.pickup_partner_id.zip or '110001').replace(' ', '')
        data = self._request('test', 'GET', self.base_url + '/c/api/pin-codes/json/', params={'filter_codes': pin})
        if 'delivery_codes' not in data:
            raise CourierError(_('Delhivery answered, but not with pincode data. Check the API token.'))
        return _('Connected to Delhivery. Pincode %s checked.', pin)

    def serviceable(self, parcel):
        data = self._request('serviceability', 'GET', self.base_url + '/c/api/pin-codes/json/',
                             params={'filter_codes': parcel.dest_pin})
        codes = data.get('delivery_codes') or []
        if not codes:
            return {'ok': False, 'cod': False, 'note': _('Delhivery does not deliver to %s.', parcel.dest_pin)}
        pc = codes[0].get('postal_code', {})
        cod = pc.get('cod') == 'Y'
        prepaid = pc.get('pre_paid') == 'Y'
        ok = cod if parcel.payment_mode == 'cod' else prepaid
        return {'ok': ok, 'cod': cod, 'note': '' if ok else _('Pincode %s is not open for this payment mode.',
                                                               parcel.dest_pin)}

    def rates(self, parcel):
        service = self.serviceable(parcel)
        if service and not service['ok']:
            return [{'courier_name': 'Delhivery', 'service': '', 'price': 0.0, 'days': 0, 'cod': False,
                     'error': service['note']}]
        options = []
        modes = [('E', 'Express'), ('S', 'Surface')]
        if self.account.service_code in ('Surface', 'Express'):
            modes = [m for m in modes if m[1] == self.account.service_code]
        for md, label in modes:
            params = {
                'md': md, 'ss': 'Delivered', 'o_pin': parcel.origin_pin, 'd_pin': parcel.dest_pin,
                'cgm': int(round(parcel.chargeable_weight_kg * 1000)),
                'pt': 'COD' if parcel.payment_mode == 'cod' else 'Pre-paid',
                'cod': parcel.cod_amount if parcel.payment_mode == 'cod' else 0,
            }
            try:
                data = self._request('rate', 'GET', self.base_url + '/api/kinko/v1/invoice/charges/.json',
                                     params=params)
            except CourierError as err:
                options.append({'courier_name': 'Delhivery', 'service': label, 'price': 0.0, 'days': 0,
                                'cod': False, 'error': str(err.args[0] if err.args else err)})
                continue
            row = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
            price = float(row.get('total_amount') or row.get('gross_amount') or 0.0)
            options.append({'courier_name': 'Delhivery', 'service': label, 'price': price,
                            'days': 2 if md == 'E' else 4, 'days_estimated': True,
                            'cod': bool(service and service.get('cod')), 'ref': label})
        return options

    # ------------------------------------------------------------------
    def _shipment_payload(self, parcel, option=None):
        acc = self.account.sudo()
        self._require('pickup_location_name')
        to = self.address(parcel.recipient_partner_id)
        sender = self.address(parcel.sender_partner_id)
        length, width, height = parcel._cp_dims()
        mode = (option or {}).get('ref') or (acc.service_code if acc.service_code in ('Surface', 'Express')
                                             else 'Surface')
        shipment = {
            'name': to['name'], 'add': to['full_street'], 'pin': to['zip'], 'city': to['city'],
            'state': to['state'], 'country': 'India', 'phone': to['phone'], 'order': parcel.reference or parcel.name,
            'payment_mode': 'COD' if parcel.payment_mode == 'cod' else 'Prepaid',
            'cod_amount': parcel.cod_amount if parcel.payment_mode == 'cod' else 0,
            'total_amount': parcel.declared_value, 'products_desc': parcel.contents or '',
            'hsn_code': parcel.hsn_code or '', 'quantity': str(parcel.piece_count),
            'weight': int(round(parcel.total_weight_kg * 1000)),
            'shipment_length': length, 'shipment_width': width, 'shipment_height': height,
            'seller_name': sender['company'] or sender['name'], 'seller_add': sender['full_street'],
            'seller_inv': parcel.invoice_number or '', 'waybill': parcel.awb or '',
            'shipping_mode': mode, 'return_pin': sender['zip'], 'return_city': sender['city'],
            'return_phone': sender['phone'], 'return_add': sender['full_street'], 'return_state': sender['state'],
            'return_country': 'India', 'return_name': acc.pickup_location_name,
        }
        if parcel.eway_bill_no:
            shipment['ewbn'] = parcel.eway_bill_no
        return {'shipments': [shipment], 'pickup_location': {'name': acc.pickup_location_name}}, mode

    def create_shipment(self, parcel, option=None):
        payload, mode = self._shipment_payload(parcel, option)
        body = 'format=json&data=%s' % json.dumps(payload)
        data = self._request('book', 'POST', self.base_url + '/api/cmu/create.json', data=body,
                             headers={'Content-Type': 'application/json'})
        packages = data.get('packages') or []
        pkg = packages[0] if packages else {}
        if not data.get('success') or not pkg.get('waybill') or pkg.get('status') not in (None, 'Success'):
            remarks = pkg.get('remarks') or data.get('rmk') or data.get('error') or data
            if isinstance(remarks, list):
                remarks = '; '.join(str(r) for r in remarks)
            raise CourierError(_('Delhivery did not book the shipment: %s', remarks))
        return {'awb': pkg['waybill'], 'courier_name': 'Delhivery', 'service': mode,
                'provider_ref': str(pkg.get('refnum') or data.get('upload_wbn') or ''),
                'data': {'upload_wbn': data.get('upload_wbn')}}

    def fetch_label(self, parcel):
        data = self._request('label', 'GET', self.base_url + '/api/p/packing_slip',
                             params={'wbns': parcel.awb, 'pdf': 'true', 'pdf_size': '4R'})
        packages = data.get('packages') or []
        link = packages and (packages[0].get('pdf_download_link') or packages[0].get('pdf_link'))
        if link:
            return self._download(link)
        return None

    # ------------------------------------------------------------------
    @staticmethod
    def _status_of(status_type, status, instructions=''):
        st = (status_type or '').upper()
        text = (status or '').lower()
        if st == 'DL':
            return 'returned' if 'rto' in text or 'return' in text else 'delivered'
        if st == 'RT':
            return 'rto'
        if st == 'CN':
            return 'cancelled'
        if st == 'LT':
            return 'lost'
        if st == 'PP':
            return 'booked'
        if st == 'PU':
            return 'picked_up'
        guessed = guess_status(status, instructions)
        if text in ('manifested', 'not picked', 'open'):
            return 'booked'
        if text == 'dispatched':
            return 'out_for_delivery'
        if text in ('in transit', 'pending'):
            return 'ndr' if guessed == 'ndr' else 'in_transit'
        return guessed or 'in_transit'

    def _parse_shipment(self, shp):
        events = []
        for scan in shp.get('Scans') or []:
            d = scan.get('ScanDetail', scan)
            events.append({
                'time': parse_dt(d.get('StatusDateTime') or d.get('ScanDateTime')),
                'code': d.get('StatusCode') or d.get('ScanType') or '',
                'description': ' - '.join(x for x in (d.get('Scan'), d.get('Instructions')) if x),
                'location': d.get('ScannedLocation') or '',
                'status': self._status_of(d.get('ScanType'), d.get('Scan'), d.get('Instructions')),
            })
        status = shp.get('Status') or {}
        return {
            'status': self._status_of(status.get('StatusType'), status.get('Status'), status.get('Instructions')),
            'status_text': ' - '.join(x for x in (status.get('Status'), status.get('Instructions')) if x),
            'location': status.get('StatusLocation') or '',
            'events': events,
            'expected_delivery': parse_dt(shp.get('ExpectedDeliveryDate') or shp.get('PromisedDeliveryDate')),
        }

    def track(self, parcels):
        result = {}
        awbs = [p.awb for p in parcels if p.awb]
        for start in range(0, len(awbs), 50):
            chunk = awbs[start:start + 50]
            data = self._request('track', 'GET', self.base_url + '/api/v1/packages/json/',
                                 params={'waybill': ','.join(chunk)})
            for item in data.get('ShipmentData') or []:
                shp = item.get('Shipment', item)
                awb = str(shp.get('AWB') or '')
                if awb:
                    result[awb] = self._parse_shipment(shp)
        return result

    def parse_webhook(self, payload, headers=None):
        items = payload if isinstance(payload, list) else [payload]
        result = {}
        for item in items:
            shp = item.get('Shipment', item) if isinstance(item, dict) else {}
            awb = str(shp.get('AWB') or shp.get('awb') or '')
            if not awb:
                continue
            parsed = self._parse_shipment(shp)
            status = shp.get('Status') or {}
            if not parsed['events'] and status:
                parsed['events'] = [{'time': parse_dt(status.get('StatusDateTime')),
                                     'code': status.get('StatusCode') or status.get('StatusType') or '',
                                     'description': parsed['status_text'], 'location': parsed['location'],
                                     'status': parsed['status']}]
            result[awb] = parsed
        return result

    def cancel(self, parcel):
        data = self._request('cancel', 'POST', self.base_url + '/api/p/edit',
                             json_body={'waybill': parcel.awb, 'cancellation': 'true'})
        if data.get('status') in (True, 'true', 'True') or 'cancel' in str(data.get('remark', '')).lower():
            return True
        raise CourierError(_('Delhivery did not cancel %(awb)s: %(msg)s', awb=parcel.awb,
                             msg=data.get('remark') or data.get('error') or data))

    def request_pickup(self, parcels, when):
        acc = self.account.sudo()
        self._require('pickup_location_name')
        data = self._request('pickup', 'POST', self.base_url + '/fm/request/new/', json_body={
            'pickup_time': when.strftime('%H:%M:%S'), 'pickup_date': when.strftime('%Y-%m-%d'),
            'pickup_location': acc.pickup_location_name, 'expected_package_count': len(parcels),
        })
        ref = data.get('pickup_id') or data.get('request_id') or data.get('id')
        if not ref:
            raise CourierError(_('Delhivery did not confirm the pickup: %s', data.get('error') or data))
        return str(ref)

    def ndr_action(self, parcel, action, note=''):
        if action != 'reattempt':
            raise UserError(_('Delhivery returns a parcel to you automatically after the failed attempts. '
                              'Raise "Return to sender" with your Delhivery account manager if you need it sooner.'))
        data = self._request('ndr', 'POST', self.base_url + '/api/p/update',
                             json_body={'data': [{'waybill': parcel.awb, 'act': 'RE-ATTEMPT'}]})
        return bool(data.get('request_id') or data.get('success') or data.get('status'))
