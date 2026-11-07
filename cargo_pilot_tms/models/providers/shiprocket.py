# -*- coding: utf-8 -*-
from odoo import _, fields

from .base import CourierError, CourierProvider, guess_status, parse_dt

# Shiprocket shipment_status ids -> normalised status
SR_STATUS = {
    6: 'in_transit', 7: 'delivered', 8: 'cancelled', 9: 'rto', 10: 'returned', 12: 'lost', 13: 'booked',
    14: 'rto', 15: 'booked', 16: 'cancelled', 17: 'out_for_delivery', 18: 'in_transit', 19: 'booked',
    20: 'booked', 21: 'ndr', 22: 'in_transit', 38: 'in_transit', 39: 'in_transit', 40: 'rto', 41: 'rto',
    42: 'picked_up', 43: 'lost', 44: 'lost', 45: 'cancelled', 46: 'rto', 47: 'in_transit', 48: 'in_transit',
    49: 'in_transit', 50: 'in_transit', 51: 'picked_up', 52: 'booked', 54: 'in_transit', 55: 'in_transit',
    56: 'in_transit', 57: 'in_transit', 59: 'booked', 60: 'in_transit', 61: 'in_transit', 62: 'in_transit',
    63: 'in_transit', 68: 'in_transit', 71: 'in_transit', 72: 'in_transit', 75: 'rto', 76: 'returned',
    77: 'in_transit',
}


class ShiprocketProvider(CourierProvider):
    """Shiprocket aggregator: one account, 25+ couriers (Delhivery, Blue Dart, DTDC, Xpressbees, Ekart...)."""
    code = 'shiprocket'
    label = 'Shiprocket'
    capabilities = {'rate': True, 'serviceability': True, 'book': True, 'label': True, 'track': True,
                    'cancel': True, 'pickup': True, 'ndr': True, 'webhook': True}
    base_urls = {'test': 'https://apiv2.shiprocket.in/v1/external',
                 'production': 'https://apiv2.shiprocket.in/v1/external'}
    tracking_url = 'https://shiprocket.co/tracking/{awb}'
    token_hours = 216  # tokens live 10 days; refresh after 9

    def _fetch_token(self):
        acc = self.account.sudo()
        self._require('username', 'password')
        data = self._request('token', 'POST', self.base_url + '/auth/login', auth=False,
                             json_body={'email': acc.username, 'password': acc.password})
        return data.get('token')

    def _auth_headers(self):
        return {'Authorization': 'Bearer %s' % self._cached_token(self._fetch_token),
                'Content-Type': 'application/json'}

    def test_connection(self):
        self.account.sudo().write({'token': False, 'token_expiry': False})
        self._auth_headers()
        return _('Connected to Shiprocket. API user logged in.')

    # ------------------------------------------------------------------
    def rates(self, parcel):
        length, width, height = parcel._cp_dims()
        data = self._request('rate', 'GET', self.base_url + '/courier/serviceability/', params={
            'pickup_postcode': parcel.origin_pin, 'delivery_postcode': parcel.dest_pin,
            'weight': round(parcel.total_weight_kg, 3), 'cod': 1 if parcel.payment_mode == 'cod' else 0,
            'declared_value': parcel.declared_value, 'length': length, 'breadth': width, 'height': height,
        })
        body = data.get('data') or {}
        couriers = body.get('available_courier_companies') or []
        if not couriers:
            return [{'courier_name': 'Shiprocket', 'service': '', 'price': 0.0, 'days': 0, 'cod': False,
                     'error': data.get('message') or _('No courier serves %(a)s to %(b)s.', a=parcel.origin_pin,
                                                                                         b=parcel.dest_pin)}]
        recommended = body.get('recommended_courier_company_id') or body.get('shiprocket_recommended_courier_id')
        options = []
        for c in couriers:
            days = c.get('estimated_delivery_days')
            try:
                days = int(days)
            except (TypeError, ValueError):
                days = 0
            options.append({
                'courier_name': c.get('courier_name') or 'Courier', 'service': 'Surface' if c.get('is_surface')
                else 'Air', 'price': float(c.get('rate') or c.get('freight_charge') or 0.0), 'days': days,
                'cod': bool(c.get('cod')), 'ref': str(c.get('courier_company_id') or ''),
                'rating': float(c.get('rating') or 0.0),
                'note': _('Shiprocket pick') if recommended and c.get('courier_company_id') == recommended else '',
            })
        return options

    def create_shipment(self, parcel, option=None):
        acc = self.account.sudo()
        self._require('pickup_location_name')
        to = self.address(parcel.recipient_partner_id)
        length, width, height = parcel._cp_dims()
        first, _sp, last = to['name'].partition(' ')
        order = {
            'order_id': parcel.name, 'order_date': fields.Datetime.now().strftime('%Y-%m-%d %H:%M'),
            'pickup_location': acc.pickup_location_name, 'channel_id': acc.channel_id or '',
            'comment': parcel.note or '',
            'billing_customer_name': first, 'billing_last_name': last, 'billing_address': to['street'] or to['full_street'],
            'billing_address_2': to['street2'], 'billing_city': to['city'], 'billing_pincode': to['zip'],
            'billing_state': to['state'], 'billing_country': to['country'], 'billing_email': to['email'],
            'billing_phone': to['phone'], 'shipping_is_billing': True,
            'order_items': parcel._cp_items(),
            'payment_method': 'COD' if parcel.payment_mode == 'cod' else 'Prepaid',
            'sub_total': parcel.cod_amount if parcel.payment_mode == 'cod' else parcel.declared_value,
            'length': length, 'breadth': width, 'height': height, 'weight': round(parcel.total_weight_kg, 3),
        }
        if parcel.eway_bill_no:
            order['ewaybill_no'] = parcel.eway_bill_no
        created = self._request('book', 'POST', self.base_url + '/orders/create/adhoc', json_body=order)
        shipment_id = created.get('shipment_id')
        if not shipment_id:
            raise CourierError(_('Shiprocket did not create the order: %s',
                                 created.get('message') or created.get('errors') or created))
        assign_body = {'shipment_id': shipment_id}
        if option and option.get('ref'):
            assign_body['courier_id'] = option['ref']
        assigned = self._request('book', 'POST', self.base_url + '/courier/assign/awb', json_body=assign_body)
        info = ((assigned.get('response') or {}).get('data') or {})
        if not assigned.get('awb_assign_status') or not info.get('awb_code'):
            raise CourierError(_('Shiprocket created order %(order)s but could not assign a courier: %(msg)s',
                                 order=created.get('order_id'),
                                 msg=info.get('awb_assign_error') or assigned.get('message') or assigned))
        booking = {'awb': str(info['awb_code']), 'courier_name': info.get('courier_name') or 'Shiprocket',
                   'provider_ref': str(shipment_id),
                   'data': {'order_id': created.get('order_id'), 'shipment_id': shipment_id,
                            'courier_company_id': info.get('courier_company_id')}}
        if acc.auto_pickup:
            try:
                booking['pickup_ref'] = self._pickup([shipment_id])
            except CourierError:
                booking['pickup_ref'] = False
        return booking

    def fetch_label(self, parcel):
        data = self._request('label', 'POST', self.base_url + '/courier/generate/label',
                             json_body={'shipment_id': [int(parcel.provider_ref)]})
        url = data.get('label_url')
        if not url:
            raise CourierError(_('Shiprocket has no label yet: %s', data.get('response') or data.get('message') or data))
        return self._download(url)

    def _pickup(self, shipment_ids):
        data = self._request('pickup', 'POST', self.base_url + '/courier/generate/pickup',
                             json_body={'shipment_id': [int(s) for s in shipment_ids]})
        resp = data.get('response') or {}
        if not data.get('pickup_status'):
            raise CourierError(_('Shiprocket did not schedule the pickup: %s',
                                 resp.get('data') or data.get('message') or data))
        return str(resp.get('pickup_token_number') or resp.get('pickup_scheduled_date') or 'scheduled')

    def request_pickup(self, parcels, when):
        return self._pickup([p.provider_ref for p in parcels if p.provider_ref])

    def cancel(self, parcel):
        data = self._request('cancel', 'POST', self.base_url + '/orders/cancel/shipment/awbs',
                             json_body={'awbs': [parcel.awb]})
        if data.get('status_code') in (200, 201, None) and 'error' not in str(data.get('message', '')).lower():
            return True
        raise CourierError(_('Shiprocket did not cancel %(awb)s: %(msg)s', awb=parcel.awb, msg=data.get('message')))

    def ndr_action(self, parcel, action, note=''):
        self._request('ndr', 'POST', self.base_url + '/ndr/%s/action' % parcel.awb, json_body={
            'action': 're-attempt' if action == 'reattempt' else 'return', 'comments': note or 'Action from Odoo'})
        return True

    # ------------------------------------------------------------------
    @staticmethod
    def _status(code, text):
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = None
        return SR_STATUS.get(code) or guess_status(text)

    def _parse(self, tracking):
        events = []
        for a in tracking.get('shipment_track_activities') or []:
            label = a.get('sr-status-label') or ''
            events.append({'time': parse_dt(a.get('date')), 'code': str(a.get('sr-status') or ''),
                           'description': a.get('activity') or label, 'location': a.get('location') or '',
                           'status': self._status(a.get('sr-status'), '%s %s' % (label, a.get('activity') or ''))})
        track = (tracking.get('shipment_track') or [{}])[0]
        text = track.get('current_status') or ''
        return {'status': self._status(tracking.get('shipment_status'), text), 'status_text': text,
                'events': events, 'courier_name': track.get('courier_name'),
                'expected_delivery': parse_dt(tracking.get('etd') or track.get('edd'))}

    def track(self, parcels):
        result = {}
        for parcel in parcels.filtered('awb'):
            data = self._request('track', 'GET', self.base_url + '/courier/track/awb/%s' % parcel.awb)
            tracking = data.get('tracking_data') or (data.get(parcel.awb) or {}).get('tracking_data') or {}
            if tracking.get('error'):
                continue
            result[parcel.awb] = self._parse(tracking)
        return result

    def parse_webhook(self, payload, headers=None):
        awb = str(payload.get('awb') or '')
        if not awb:
            return {}
        events = [{'time': parse_dt(s.get('date')), 'code': str(s.get('sr-status') or s.get('status') or ''),
                   'description': s.get('activity') or s.get('sr-status-label') or '',
                   'location': s.get('location') or '',
                   'status': self._status(s.get('sr-status'), s.get('sr-status-label') or s.get('activity'))}
                  for s in payload.get('scans') or []]
        text = payload.get('current_status') or payload.get('shipment_status') or ''
        return {awb: {'status': self._status(payload.get('current_status_id') or payload.get('shipment_status_id'),
                                             text),
                      'status_text': text, 'events': events, 'courier_name': payload.get('courier_name'),
                      'expected_delivery': parse_dt(payload.get('etd'))}}
