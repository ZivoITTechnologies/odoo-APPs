# -*- coding: utf-8 -*-
from odoo import _

from .base import CourierError, CourierProvider, guess_status, parse_dt

XB_STATUS = {
    'pp': 'booked', 'pending pickup': 'booked', 'booked': 'booked', 'pickup': 'picked_up', 'picked up': 'picked_up',
    'it': 'in_transit', 'in transit': 'in_transit', 'rad': 'in_transit', 'ofd': 'out_for_delivery',
    'out for delivery': 'out_for_delivery', 'dl': 'delivered', 'delivered': 'delivered', 'ex': 'ndr',
    'exception': 'ndr', 'ndr': 'ndr', 'rt': 'rto', 'rt-it': 'rto', 'rto': 'rto', 'rt-dl': 'returned',
    'rto delivered': 'returned', 'lost': 'lost', 'cancelled': 'cancelled', 'can': 'cancelled',
}


class XpressbeesProvider(CourierProvider):
    """Xpressbees shipping API (email / password login, bearer token)."""
    code = 'xpressbees'
    label = 'Xpressbees'
    capabilities = {'rate': True, 'serviceability': True, 'book': True, 'label': True, 'track': True,
                    'cancel': True, 'pickup': False, 'ndr': False, 'webhook': True}
    base_urls = {'test': 'https://shipment.xpressbees.com/api', 'production': 'https://shipment.xpressbees.com/api'}
    tracking_url = 'https://www.xpressbees.com/shipment/tracking?awbNo={awb}'
    token_hours = 12

    def _fetch_token(self):
        acc = self.account.sudo()
        self._require('username', 'password')
        data = self._request('token', 'POST', self.base_url + '/users/login', auth=False,
                             json_body={'email': acc.username, 'password': acc.password})
        if not data.get('status'):
            raise CourierError(_('Xpressbees refused the login: %s', data.get('message') or data))
        return data.get('data')

    def _auth_headers(self):
        return {'Authorization': 'Bearer %s' % self._cached_token(self._fetch_token),
                'Content-Type': 'application/json'}

    def test_connection(self):
        self.account.sudo().write({'token': False, 'token_expiry': False})
        self._auth_headers()
        return _('Connected to Xpressbees.')

    def rates(self, parcel):
        length, width, height = parcel._cp_dims()
        data = self._request('rate', 'POST', self.base_url + '/courier/serviceability', json_body={
            'origin': parcel.origin_pin, 'destination': parcel.dest_pin,
            'payment_type': 'cod' if parcel.payment_mode == 'cod' else 'prepaid',
            'order_amount': parcel.cod_amount if parcel.payment_mode == 'cod' else parcel.declared_value,
            'weight': int(round(parcel.total_weight_kg * 1000)), 'length': length, 'breadth': width, 'height': height,
        })
        rows = data.get('data') or []
        if not data.get('status') or not rows:
            return [{'courier_name': 'Xpressbees', 'service': '', 'price': 0.0, 'days': 0, 'cod': False,
                     'error': data.get('message') or _('Not serviceable')}]
        return [{'courier_name': r.get('name') or 'Xpressbees', 'service': r.get('name') or '',
                 'price': float(r.get('total_charges') or r.get('freight_charges') or 0.0),
                 'days': self.account.rate_card_days or 0, 'days_estimated': True,
                 'cod': parcel.payment_mode == 'cod', 'ref': str(r.get('id') or '')} for r in rows]

    def create_shipment(self, parcel, option=None):
        acc = self.account.sudo()
        to = self.address(parcel.recipient_partner_id)
        frm = self.address(parcel.sender_partner_id)
        length, width, height = parcel._cp_dims()
        cod = parcel.payment_mode == 'cod'
        body = {
            'order_number': parcel.name, 'payment_type': 'cod' if cod else 'prepaid',
            'order_amount': parcel.cod_amount if cod else parcel.declared_value, 'discount': 0,
            'shipping_charges': 0, 'cod_charges': 0,
            'package_weight': int(round(parcel.total_weight_kg * 1000)), 'package_length': length,
            'package_breadth': width, 'package_height': height,
            'request_auto_pickup': 'yes' if acc.auto_pickup else 'no',
            'consignee': {'name': to['name'], 'address': to['street'] or to['full_street'], 'address_2': to['street2'],
                          'city': to['city'], 'state': to['state'], 'pincode': to['zip'], 'phone': to['phone']},
            'pickup': {'warehouse_name': acc.pickup_location_name or frm['name'], 'name': frm['name'],
                       'address': frm['street'] or frm['full_street'], 'address_2': frm['street2'],
                       'city': frm['city'], 'state': frm['state'], 'pincode': frm['zip'], 'phone': frm['phone']},
            'order_items': [{'name': i['name'], 'qty': i['units'], 'price': i['selling_price'], 'sku': i['sku']}
                            for i in parcel._cp_items()],
            'collectable_amount': parcel.cod_amount if cod else 0,
        }
        if option and option.get('ref'):
            body['courier_id'] = option['ref']
        data = self._request('book', 'POST', self.base_url + '/shipments2', json_body=body)
        info = data.get('data') or {}
        if not data.get('status') or not info.get('awb_number'):
            raise CourierError(_('Xpressbees did not book the shipment: %s', data.get('message') or data))
        return {'awb': str(info['awb_number']), 'courier_name': info.get('courier_name') or 'Xpressbees',
                'provider_ref': str(info.get('shipment_id') or ''), 'label_url': info.get('label') or '',
                'data': {'order_id': info.get('order_id'), 'manifest': info.get('manifest')}}

    def fetch_label(self, parcel):
        url = parcel._cp_data().get('label_url')
        return self._download(url) if url else None

    def cancel(self, parcel):
        data = self._request('cancel', 'POST', self.base_url + '/shipments2/cancel', json_body={'awb': parcel.awb})
        if data.get('status'):
            return True
        raise CourierError(_('Xpressbees did not cancel %(awb)s: %(msg)s', awb=parcel.awb, msg=data.get('message')))

    @staticmethod
    def _status(code, text):
        return XB_STATUS.get((code or '').strip().lower()) or XB_STATUS.get((text or '').strip().lower()) \
            or guess_status(text)

    def track(self, parcels):
        result = {}
        for parcel in parcels.filtered('awb'):
            data = self._request('track', 'GET', self.base_url + '/shipments2/track/%s' % parcel.awb)
            info = data.get('data') or {}
            if not data.get('status'):
                continue
            events = [{'time': parse_dt(h.get('event_time')), 'code': h.get('status_code') or '',
                       'description': h.get('message') or '', 'location': h.get('location') or '',
                       'status': self._status(h.get('status_code'), h.get('message'))}
                      for h in info.get('history') or []]
            text = info.get('status') or ''
            result[parcel.awb] = {'status': self._status(text, text), 'status_text': text, 'events': events,
                                  'expected_delivery': parse_dt(info.get('edd'))}
        return result

    def parse_webhook(self, payload, headers=None):
        awb = str(payload.get('awb_number') or payload.get('awb') or '')
        if not awb:
            return {}
        text = payload.get('status') or payload.get('message') or ''
        return {awb: {'status': self._status(payload.get('status_code') or text, text), 'status_text': text,
                      'events': [{'time': parse_dt(payload.get('event_time')), 'code': payload.get('status_code', ''),
                                  'description': payload.get('message') or text,
                                  'location': payload.get('location') or '',
                                  'status': self._status(payload.get('status_code') or text, text)}],
                      'expected_delivery': None}}
