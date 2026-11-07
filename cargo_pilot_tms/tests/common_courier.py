# -*- coding: utf-8 -*-
import json
from contextlib import contextmanager
from unittest.mock import patch

from odoo.tests.common import TransactionCase

PDF = b'%PDF-1.4\n% test label\n%%EOF'


class FakeResponse:
    def __init__(self, payload=None, status=200, content=None, url=''):
        self.status_code = status
        self._payload = payload
        if content is not None:
            self.content = content
            self.text = content.decode('latin-1') if isinstance(content, bytes) else content
        else:
            self.text = json.dumps(payload) if not isinstance(payload, str) else payload
            self.content = self.text.encode()
        self.reason = 'OK' if status < 400 else 'Error'
        self.headers = {}

        class _Req:
            pass
        self.request = _Req()
        self.request.url = url

    def json(self):
        if isinstance(self._payload, (dict, list)):
            return self._payload
        return json.loads(self.text)


class FakeCourier:
    """Routes requests.request calls to canned answers: {(METHOD, url fragment): payload | callable}."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers=None, params=None, json=None, data=None, timeout=None):
        self.calls.append({'method': method, 'url': url, 'headers': headers or {}, 'params': params or {},
                           'json': json, 'data': data})
        for (m, fragment), answer in self.routes.items():
            if m == method and fragment in url:
                if callable(answer):
                    answer = answer(method, url, headers, params, json, data)
                if isinstance(answer, FakeResponse):
                    return answer
                return FakeResponse(answer, url=url)
        return FakeResponse({'error': 'no route for %s %s' % (method, url)}, status=404, url=url)

    def last(self, fragment):
        return [c for c in self.calls if fragment in c['url']][-1]


@contextmanager
def fake_courier(routes):
    fake = FakeCourier(routes)
    with patch('odoo.addons.cargo_pilot_tms.models.providers.base.requests.request', new=fake):
        yield fake


class CourierCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True, mail_notrack=True))
        india = cls.env.ref('base.in')
        cls.sender = cls.env['res.partner'].create({
            'name': 'Test Warehouse', 'is_company': True, 'street': 'Plot 1, Industrial Area', 'city': 'Raipur',
            'zip': '492003', 'country_id': india.id, 'phone': '+91 98260 00001', 'email': 'wh@example.com'})
        cls.receiver = cls.env['res.partner'].create({
            'name': 'Asha Retail', 'street': '22 FC Road', 'street2': 'Shivajinagar', 'city': 'Pune', 'zip': '411005',
            'country_id': india.id, 'phone': '+91 98220 45671', 'email': 'asha@example.com'})
        cls.unlucky = cls.env['res.partner'].create({
            'name': 'Meera', 'street': '5 Besant Nagar', 'city': 'Chennai', 'zip': '600090',
            'country_id': india.id, 'phone': '+91 98400 77770'})
        Account = cls.env['cp.courier.account']
        cls.demo = Account.create({'name': 'Sim', 'provider': 'demo', 'pickup_partner_id': cls.sender.id,
                                   'notify_customer': False})

    def make_account(self, provider, **vals):
        base = {'name': provider.title(), 'provider': provider, 'pickup_partner_id': self.sender.id,
                'notify_customer': False, 'auto_book_label': True}
        base.update(vals)
        return self.env['cp.courier.account'].create(base)

    def make_parcel(self, account=None, receiver=None, **vals):
        base = {'account_id': (account or self.demo).id, 'sender_partner_id': self.sender.id,
                'recipient_partner_id': (receiver or self.receiver).id, 'reference': 'SO-TEST',
                'declared_value': 1500.0, 'contents': 'Shoes',
                'package_ids': [(0, 0, {'quantity': 1, 'weight_kg': 1.2, 'length_cm': 30, 'width_cm': 20,
                                        'height_cm': 10})]}
        base.update(vals)
        return self.env['cp.parcel'].create(base)
