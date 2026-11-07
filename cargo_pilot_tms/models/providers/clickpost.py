# -*- coding: utf-8 -*-
from odoo import _, fields

from .base import CourierError, CourierProvider, guess_status, parse_dt

# ClickPost unified status codes -> normalised status
CP_STATUS = {
    1: 'booked', 2: 'booked', 3: 'booked', 4: 'picked_up', 5: 'in_transit', 6: 'out_for_delivery',
    7: 'booked', 8: 'delivered', 9: 'ndr', 10: 'cancelled', 11: 'rto', 12: 'rto', 13: 'rto', 14: 'returned',
    15: 'rto', 16: 'lost', 17: 'lost', 18: 'in_transit', 19: 'ndr', 20: 'in_transit', 21: 'rto', 22: 'in_transit',
    23: 'in_transit', 24: 'in_transit', 25: 'booked', 26: 'rto', 27: 'rto', 28: 'booked',
}


class ClickPostProvider(CourierProvider):
    """ClickPost aggregator. One integration for couriers that have no open API of their own,
    such as Shree Maruti (ClickPost partner id 69), Professional, Trackon, Gati or Ekart."""
    code = 'clickpost'
    label = 'ClickPost'
    capabilities = {'rate': False, 'serviceability': False, 'book': True, 'label': True, 'track': True,
                    'cancel': True, 'pickup': False, 'ndr': False, 'webhook': True}
    base_urls = {'test': 'https://www.clickpost.in/api', 'production': 'https://www.clickpost.in/api'}
    tracking_url = ''

    def _creds(self):
        acc = self.account.sudo()
        self._require('username', 'api_key')
        return {'username': acc.username, 'key': acc.api_key}

    def test_connection(self):
        self._creds()
        self._require('courier_partner_id')
        return _('ClickPost details saved for courier partner %s. The first booking will confirm them.',
                 self.account.courier_partner_id)

    def rates(self, parcel):
        return [{'courier_name': self.account.courier_display_name or 'ClickPost', 'service': '',
                 'price': self.account._cp_rate_card_price(parcel), 'days': self.account.rate_card_days or 0,
                 'cod': self.account.supports_cod, 'price_from_card': True, 'note': _('Price from your rate card')}]

    def create_shipment(self, parcel, option=None):
        acc = self.account.sudo()
        self._require('courier_partner_id')
        to = self.address(parcel.recipient_partner_id)
        frm = self.address(parcel.sender_partner_id)
        length, width, height = parcel._cp_dims()
        cod = parcel.payment_mode == 'cod'
        body = {
            'pickup_info': {'name': frm['company'] or frm['name'], 'phone': frm['phone'], 'email': frm['email'],
                            'address': frm['full_street'], 'city': frm['city'], 'state': frm['state'],
                            'pincode': frm['zip'], 'country_code': frm['country_code'],
                            'time': fields.Datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ'),
                            'tin': frm['vat']},
            'drop_info': {'drop_name': to['name'], 'drop_phone': to['phone'], 'drop_email': to['email'],
                          'drop_address': to['full_street'], 'drop_city': to['city'], 'drop_state': to['state'],
                          'drop_pincode': to['zip'], 'drop_country': to['country_code']},
            'shipment_details': {
                'order_type': 'COD' if cod else 'PREPAID', 'cod_value': parcel.cod_amount if cod else 0,
                'invoice_value': parcel.declared_value, 'invoice_number': parcel.invoice_number or parcel.name,
                'invoice_date': (parcel.invoice_date or fields.Date.context_today(parcel)).strftime('%Y-%m-%d'),
                'reference_number': parcel.name, 'length': length, 'breadth': width, 'height': height,
                'weight': int(round(parcel.total_weight_kg * 1000)),
                'items': [{'description': i['name'], 'quantity': i['units'], 'price': i['selling_price'],
                           'sku': i['sku']} for i in parcel._cp_items()],
                'courier_partner': int(acc.courier_partner_id), 'delivery_type': 'FORWARD',
            },
            'gst_info': {'seller_gstin': frm['vat'], 'invoice_reference': parcel.invoice_number or parcel.name,
                         'ewaybill_serial_number': parcel.eway_bill_no or ''},
            'additional': {'label': True, 'async': False, 'account_code': acc.account_code or '',
                           'return_info': {'name': frm['name'], 'phone': frm['phone'], 'address': frm['full_street'],
                                           'city': frm['city'], 'state': frm['state'], 'pincode': frm['zip'],
                                           'country': frm['country_code']}},
        }
        data = self._request('book', 'POST', self.base_url + '/v3/create-order/', params=self._creds(),
                             json_body=body)
        meta = data.get('meta') or {}
        result = data.get('result') or {}
        if not meta.get('success') or not result.get('waybill'):
            raise CourierError(_('ClickPost did not book the shipment: %s', meta.get('message') or data))
        return {'awb': str(result['waybill']),
                'courier_name': result.get('courier_name') or acc.courier_display_name or 'ClickPost',
                'label_url': result.get('label') or '', 'provider_ref': str(result.get('reference_number') or ''),
                'data': {'security_key': result.get('security_key'), 'cp_id': result.get('courier_partner_id')}}

    def fetch_label(self, parcel):
        url = parcel._cp_data().get('label_url')
        return self._download(url) if url else None

    def cancel(self, parcel):
        acc = self.account.sudo()
        params = dict(self._creds(), waybill=parcel.awb, cp_id=acc.courier_partner_id,
                      account_code=acc.account_code or '')
        data = self._request('cancel', 'GET', self.base_url + '/v1/cancel-order/', params=params)
        if (data.get('meta') or {}).get('success'):
            return True
        raise CourierError(_('ClickPost did not cancel %(awb)s: %(msg)s', awb=parcel.awb,
                             msg=(data.get('meta') or {}).get('message') or data))

    @staticmethod
    def _status(code, text):
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = None
        return CP_STATUS.get(code) or guess_status(text)

    def _parse(self, item):
        events = []
        for s in item.get('scans') or []:
            text = s.get('clickpost_status_description') or s.get('status') or ''
            events.append({'time': parse_dt(s.get('timestamp')), 'code': str(s.get('clickpost_status_code') or ''),
                           'description': ' - '.join(x for x in (s.get('status') or text, s.get('remark')) if x),
                           'location': s.get('location') or '', 'status': self._status(s.get('clickpost_status_code'),
                                                                                     text)})
        latest = item.get('latest_status') or (item.get('additional') or {}).get('latest_status') or {}
        text = latest.get('clickpost_status_description') or latest.get('status') or ''
        edd = (item.get('additional') or {}).get('courier_partner_edd')
        return {'status': self._status(latest.get('clickpost_status_code'), text), 'status_text': text,
                'events': events, 'expected_delivery': parse_dt(edd)}

    def track(self, parcels):
        acc = self.account.sudo()
        result = {}
        for parcel in parcels.filtered('awb'):
            data = self._request('track', 'GET', self.base_url + '/v2/track-order/',
                                 params=dict(self._creds(), waybill=parcel.awb, cp_id=acc.courier_partner_id))
            item = (data.get('result') or {}).get(parcel.awb)
            if item:
                result[parcel.awb] = self._parse(item)
        return result

    def parse_webhook(self, payload, headers=None):
        awb = str(payload.get('waybill') or '')
        if not awb:
            return {}
        text = payload.get('clickpost_status_description') or payload.get('status') or ''
        status = self._status(payload.get('clickpost_status_code'), text)
        return {awb: {'status': status, 'status_text': text, 'expected_delivery': None, 'events': [{
            'time': parse_dt(payload.get('timestamp')), 'code': str(payload.get('clickpost_status_code') or ''),
            'description': ' - '.join(x for x in (payload.get('status') or text, payload.get('remark')) if x),
            'location': payload.get('location') or '', 'status': status}]}}
