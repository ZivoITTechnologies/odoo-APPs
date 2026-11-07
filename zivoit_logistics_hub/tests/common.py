# -*- coding: utf-8 -*-
import json
import re
from contextlib import contextmanager
from unittest.mock import patch

import requests

from odoo.tests import TransactionCase


def fake_response(status=200, payload=None, content=None, content_type='application/json'):
    resp = requests.Response()
    resp.status_code = status
    if content is not None:
        resp._content = content
    elif isinstance(payload, (dict, list)):
        resp._content = json.dumps(payload).encode()
    else:
        resp._content = (payload or '').encode()
    resp.headers['Content-Type'] = content_type
    resp.encoding = 'utf-8'
    return resp


class FakeCarrierServer:
    """Routes ``requests.Session.request`` calls to canned responses: [(method, url_regex, response|callable)]."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, session, method, url, **kwargs):
        self.calls.append({'method': method, 'url': url, **kwargs})
        for r_method, pattern, response in self.routes:
            if r_method == method and re.search(pattern, url):
                return response(method, url, kwargs) if callable(response) else response
        return fake_response(404, {'message': 'no route for %s %s' % (method, url)})

    def called(self, pattern, method=None):
        return [c for c in self.calls if re.search(pattern, c['url']) and (not method or c['method'] == method)]


@contextmanager
def carrier_server(routes):
    server = FakeCarrierServer(routes)

    def _request(session, method, url, **kwargs):
        return server(session, method, url, **kwargs)
    with patch.object(requests.Session, 'request', autospec=True, side_effect=_request):
        yield server


class LogisticsCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.company = env.company
        cls.state_ts = env.ref('base.state_in_ts')
        cls.state_ka = env.ref('base.state_in_ka')
        cls.state_mh = env.ref('base.state_in_mh')
        india = env.ref('base.in')
        cls.company.partner_id.write({'street': 'Plot 12, IDA Cherlapally, near Railway Station', 'city': 'Hyderabad',
                                      'zip': '500051', 'state_id': cls.state_ts.id, 'country_id': india.id,
                                      'phone': '+91 9849012345', 'email': 'ops@example.com'})
        cls.warehouse = env['stock.warehouse'].search([('company_id', '=', cls.company.id)], limit=1)
        cls.warehouse.partner_id = cls.company.partner_id
        cls.warehouse.out_type_id.print_label = True
        cls.customer = env['res.partner'].create({
            'name': 'Ravi Kumar', 'street': 'Flat 402, Sai Residency, 4th Cross', 'street2': 'Near Metro Station, Indiranagar',
            'city': 'Bengaluru', 'zip': '560038', 'state_id': cls.state_ka.id, 'country_id': india.id,
            'phone': '+91 98480 22338', 'email': 'ravi@example.com',
        })
        cls.bad_customer = env['res.partner'].create({
            'name': 'xx', 'street': 'asdf', 'city': '', 'zip': '12345', 'state_id': cls.state_ts.id, 'country_id': india.id,
            'phone': '12345',
        })
        cls.ndr_customer = env['res.partner'].create({
            'name': 'Meera Shah', 'street': 'B-12, Shanti Nagar, near Bus Depot', 'city': 'Mumbai', 'zip': '400013',
            'state_id': cls.state_mh.id, 'country_id': india.id, 'phone': '9820012345', 'email': 'meera@example.com',
        })
        cls.product = env['product.product'].create({'name': 'Kurta Set', 'type': 'consu', 'weight': 0.8,
                                                     'list_price': 1499.0, 'default_code': 'KS-01'})
        cls.delivery_product = env['product.product'].create({'name': 'Shipping', 'type': 'service', 'list_price': 0.0})
        Account = env['zivo.logistics.account']
        cls.acc_api = Account.create({'name': 'Sandbox API', 'provider': 'demo', 'rate_source': 'api',
                                      'extra_config': '{"step_minutes": 0}'})
        cls.acc_card = Account.create({
            'name': 'Sandbox Card', 'provider': 'demo', 'rate_source': 'rate_card', 'gst_pct': 18.0,
            'extra_config': '{"step_minutes": 0}',
            'rate_card_ids': [(0, 0, {'service_code': 'SURF', 'service_name': 'Surface', 'zone': 'any', 'base_price': 20.0,
                                      'additional_price': 18.0, 'cod_fixed': 25.0, 'cod_percent': 1.5, 'tat_days': 6})],
        })
        cls.carrier = env['delivery.carrier'].create({
            'name': 'Smart Shipping', 'delivery_type': 'zivo_hub', 'product_id': cls.delivery_product.id,
            'zivo_strategy': 'ai_best', 'zivo_account_ids': [(6, 0, (cls.acc_api | cls.acc_card).ids)],
            'integration_level': 'rate_and_ship', 'allow_cash_on_delivery': True,
        })

    def _create_order(self, partner=None, qty=1, cod=False, carrier=None):
        order = self.env['sale.order'].create({
            'partner_id': (partner or self.customer).id,
            'order_line': [(0, 0, {'product_id': self.product.id, 'product_uom_qty': qty, 'price_unit': 1499.0})],
        })
        order.zivo_payment_mode = 'cod' if cod else 'prepaid'
        carrier = carrier or self.carrier
        rate = carrier.rate_shipment(order)
        self.assertTrue(rate['success'], rate.get('error_message'))
        order.set_delivery_line(carrier, rate['price'])
        order.action_confirm()
        return order

    def _validate(self, picking):
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.move_ids.picked = True
        picking.button_validate()
        return picking
