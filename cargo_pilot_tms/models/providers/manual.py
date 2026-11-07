# -*- coding: utf-8 -*-
import random
from datetime import timedelta

from odoo import _, fields

from .base import CourierProvider


class ManualProvider(CourierProvider):
    """Couriers you book at the counter or by phone (Shree Maruti, Professional, Trackon, India Post...).

    The AWB is typed in, or taken from a number series you were given. Cargo Pilot prints the label,
    keeps the tracking page and records status updates you add by hand or receive by e-mail.
    """
    code = 'manual'
    label = 'Counter / phone booking'
    capabilities = {'rate': False, 'serviceability': False, 'book': True, 'label': False, 'track': False,
                    'cancel': False, 'pickup': False, 'ndr': False, 'webhook': False}

    def test_connection(self):
        return _('Nothing to connect. Bookings use the AWB you type in or your AWB number series.')

    def rates(self, parcel):
        return [{'courier_name': self.account.courier_display_name or self.account.name, 'service': '',
                 'price': self.account._cp_rate_card_price(parcel), 'days': self.account.rate_card_days or 0,
                 'cod': self.account.supports_cod, 'price_from_card': True, 'note': _('Price from your rate card')}]

    def create_shipment(self, parcel, option=None):
        awb = parcel.awb or self.account._cp_next_awb()
        if not awb:
            from odoo.exceptions import UserError
            raise UserError(_('Type the AWB / docket number from the courier receipt, or set an AWB number '
                              'series on the courier account.'))
        return {'awb': awb, 'courier_name': self.account.courier_display_name or self.account.name}

    def cancel(self, parcel):
        return True


class DemoProvider(CourierProvider):
    """A built-in courier simulator so you can try every screen before you have API keys.

    Each tracking sync moves the parcel one step forward. A receiver phone number ending in 0
    simulates a failed delivery attempt.
    """
    code = 'demo'
    label = 'Demo courier (simulator)'
    capabilities = {'rate': True, 'serviceability': True, 'book': True, 'label': False, 'track': True,
                    'cancel': True, 'pickup': True, 'ndr': True, 'webhook': True}
    tracking_url = ''

    SERVICES = [('Swift Air', 1.45, 2), ('Swift Surface', 1.0, 4), ('Swift Economy', 0.82, 6)]

    def test_connection(self):
        return _('Simulator ready. No real parcels are booked.')

    def serviceable(self, parcel):
        ok = bool(parcel.dest_pin and len(parcel.dest_pin) == 6 and not parcel.dest_pin.startswith('99'))
        return {'ok': ok, 'cod': ok, 'note': '' if ok else _('Pincode %s is not served.', parcel.dest_pin)}

    def rates(self, parcel):
        service = self.serviceable(parcel)
        if not service['ok']:
            return [{'courier_name': 'Swift', 'service': '', 'price': 0.0, 'days': 0, 'cod': False,
                     'error': service['note']}]
        base = self.account._cp_rate_card_price(parcel)
        far = parcel.origin_pin[:1] != parcel.dest_pin[:1] if parcel.origin_pin and parcel.dest_pin else True
        return [{'courier_name': 'Swift', 'service': name, 'price': round(base * factor * (1.25 if far else 1.0), 2),
                 'days': days + (1 if far else 0), 'cod': True, 'ref': name} for name, factor, days in self.SERVICES]

    def create_shipment(self, parcel, option=None):
        awb = 'SW%010d' % random.randint(10 ** 9, 10 ** 10 - 1)
        days = dict((n, d) for n, _f, d in self.SERVICES).get((option or {}).get('ref'), 4)
        return {'awb': awb, 'courier_name': 'Swift', 'service': (option or {}).get('ref') or 'Swift Surface',
                'expected_delivery': fields.Datetime.now() + timedelta(days=days),
                'provider_ref': 'SIM-%s' % parcel.id}

    def cancel(self, parcel):
        return True

    def request_pickup(self, parcels, when):
        return 'PU%06d' % random.randint(1, 999999)

    def ndr_action(self, parcel, action, note=''):
        return True

    # Every sync moves the parcel one step on.
    STEPS = [
        ('picked_up', 'Shipment picked up', 'origin'),
        ('in_transit', 'Departed origin hub', 'origin'),
        ('in_transit', 'Arrived at destination hub', 'destination'),
        ('out_for_delivery', 'Out for delivery', 'destination'),
        ('delivered', 'Delivered to receiver', 'destination'),
    ]

    def track(self, parcels):
        result = {}
        now = fields.Datetime.now()
        for parcel in parcels.filtered('awb'):
            state = parcel.state
            if state in ('delivered', 'returned', 'cancelled', 'lost'):
                continue
            if state == 'ndr':
                if parcel.ndr_action == 'rto':
                    step = ('rto', 'Return to origin started', 'destination')
                elif parcel.ndr_action == 'reattempt':
                    step = ('out_for_delivery', 'Out for delivery (second attempt)', 'destination')
                else:
                    continue
            elif state == 'rto':
                step = ('returned', 'Returned to shipper', 'origin')
            elif state == 'booked' or state == 'draft':
                step = self.STEPS[0]
            elif state == 'picked_up':
                step = self.STEPS[1]
            elif state == 'in_transit':
                last = parcel.event_ids.filtered(lambda e: e.source == 'courier').sorted(lambda e: (e.event_time, e.id))[-1:]
                step = self.STEPS[2] if last.description == self.STEPS[1][1] else self.STEPS[3]
            else:  # out_for_delivery
                phone = (parcel.recipient_partner_id.phone or '').strip()
                if phone.endswith('0') and not parcel.ndr_attempts:
                    step = ('ndr', 'Delivery attempted - receiver not available', 'destination')
                else:
                    step = self.STEPS[4]
            city = (parcel.sender_partner_id.city if step[2] == 'origin' else parcel.recipient_partner_id.city) or ''
            result[parcel.awb] = {
                'status': step[0], 'status_text': step[1],
                'events': [{'time': now, 'code': step[0].upper(), 'description': step[1], 'location': city,
                            'status': step[0]}],
                'expected_delivery': None,
            }
        return result

    def parse_webhook(self, payload, headers=None):
        awb = str(payload.get('awb') or '')
        text = payload.get('status') or ''
        from .base import guess_status, parse_dt
        status = guess_status(text)
        return {awb: {'status': status, 'status_text': text, 'expected_delivery': None, 'events': [{
            'time': parse_dt(payload.get('time')) or fields.Datetime.now(), 'code': payload.get('code', ''),
            'description': text, 'location': payload.get('location', ''), 'status': status}]}} if awb else {}
