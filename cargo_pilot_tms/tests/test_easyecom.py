# -*- coding: utf-8 -*-
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.cargo_pilot_tms.tests.common import CpCommon

REQ = 'odoo.addons.cargo_pilot_tms.models.cp_easyecom.requests.request'


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def ee_order(oid, ref, pincode='500001', marketplace='Amazon.in', serials='', weight=120, value=45000, wh='WH-RPR'):
    return {'order_id': oid, 'reference_code': ref, 'invoice_number': 'INV-%s' % oid, 'invoice_id': 9000 + oid,
            'marketplace': marketplace, 'order_date': '2026-10-20 10:15:00', 'customer_name': 'Buyer %s' % oid,
            'contact_num': '9876501234', 'address_line_1': 'Road 1', 'city': 'Hyderabad', 'state': 'Telangana',
            'pin_code': pincode, 'location_key': wh, 'total_amount': value, 'total_weight': weight,
            'package_count': 3, 'eway_bill_number': '39100000%04d' % oid,
            'suborders': [{'sku': 'PUMP-1HP', 'productName': 'Pump 1HP', 'item_quantity': 2, 'selling_price': 15000,
                           'serial_no': serials}, {'sku': 'PANEL', 'item_quantity': 1, 'selling_price': 15000}]}


class FakeEasyEcom:
    def __init__(self):
        self.calls = []
        self.token_calls = 0
        self.expire_once = False

    def __call__(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append({'method': method, 'url': url, 'headers': headers or {}, 'params': params, 'json': json})
        if url.endswith('/access/token'):
            self.token_calls += 1
            return FakeResponse({'code': 200, 'data': {'token': {'jwt_token': 'jwt-%s' % self.token_calls}}})
        if self.expire_once:
            self.expire_once = False
            return FakeResponse({'code': 401, 'message': 'Token expired'}, 401)
        if '/orders/V2/getAllOrders?cursor=abc' in url or 'cursor=abc' in url:
            return FakeResponse({'code': 200, 'data': {'orders': [ee_order(3, 'SHP-3003', marketplace='Shopify',
                                                                           pincode='400001')]}})
        if '/orders/V2/getAllOrders' in url:
            return FakeResponse({'code': 200, 'data': {'orders': [ee_order(1, 'AMZ-1001', serials='SN-1,SN-2'),
                                                                  ee_order(2, 'RRL-2002', marketplace='Reliance Retail')],
                                                       'nextUrl': '/orders/V2/getAllOrders?cursor=abc'}})
        if '/getInventoryDetailsV3' in url:
            return FakeResponse({'code': 200, 'data': {'inventoryData': [
                {'sku': 'PUMP-1HP', 'productName': 'Pump 1HP', 'availableInventory': 40, 'reservedInventory': 5,
                 'location_key': 'WH-RPR'},
                {'sku': 'PANEL', 'locations': [{'location_key': 'WH-RPR', 'availableInventory': 12},
                                               {'location_key': 'FBA-HYD', 'availableInventory': 30}]}]}})
        if '/Carrier/updateTrackingStatus' in url:
            return FakeResponse({'code': 200, 'message': 'Updated'})
        return FakeResponse({'code': 404, 'message': 'no route'}, 404)

    def find(self, part):
        return [c for c in self.calls if part in c['url']]


@tagged('post_install', '-at_install', 'cargo_pilot')
class TestEasyEcom(CpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        india = env.ref('base.in')
        State = env['res.country.state']
        cls.st_tg = State.search([('country_id', '=', india.id), ('code', '=', 'TS')], limit=1) or \
            State.create({'name': 'Telangana', 'code': 'TS', 'country_id': india.id})
        cls.account = env['cp.easyecom.account'].create({
            'name': 'EE', 'api_key': 'ee-api-key-123', 'email': 'ops@example.com', 'password': 'ee-pass-1',
            'location_key': 'WH-RPR',
            'warehouse_ids': [(0, 0, {'name': 'Raipur WH', 'location_key': 'WH-RPR', 'location_id': cls.origin.id})]})
        ltl = env['cp.rate.contract'].create({
            'transporter_id': cls.tr_b.id, 'date_start': fields.Date.today() - timedelta(days=5),
            'date_end': fields.Date.today() + timedelta(days=300),
            'line_ids': [(0, 0, {'dest_state_id': cls.st_tg.id, 'charge_basis': 'kg', 'rate': 9.0,
                                 'min_weight_kg': 50, 'min_charge': 800, 'docket_charge': 100})]})
        ltl.action_activate()

    def _pull(self, fake):
        with patch(REQ, fake):
            return self.account._pull_orders()

    def test_pull_orders_with_paging_and_channels(self):
        fake = FakeEasyEcom()
        self.assertEqual(self._pull(fake), 3)
        Order = self.env['cp.channel.order']
        amz = Order.search([('reference', '=', 'AMZ-1001')])
        self.assertEqual(amz.channel, 'marketplace')
        self.assertEqual(Order.search([('reference', '=', 'RRL-2002')]).channel, 'modern_retail')
        self.assertEqual(Order.search([('reference', '=', 'SHP-3003')]).channel, 'd2c')
        self.assertEqual(amz.origin_id, self.origin)
        self.assertEqual(amz.state_id, self.st_tg)
        self.assertEqual(amz.serial_numbers, 'SN-1\nSN-2')
        self.assertEqual(amz.weight_kg, 120)
        self.assertEqual(len(amz.line_ids), 2)
        self.assertEqual(amz.quantity, 3)
        token_call = fake.find('/access/token')[0]
        self.assertEqual(token_call['json']['location_key'], 'WH-RPR')
        order_call = fake.find('getAllOrders')[0]
        self.assertEqual(order_call['headers']['x-api-key'], 'ee-api-key-123')
        self.assertEqual(order_call['headers']['Authorization'], 'Bearer jwt-1')
        # pulling again updates instead of duplicating, and the token is reused
        self._pull(fake)
        self.assertEqual(Order.search_count([('account_id', '=', self.account.id)]), 3)
        self.assertEqual(fake.token_calls, 1)
        log = self.env['cp.easyecom.log'].search([('account_id', '=', self.account.id)], limit=1)
        self.assertTrue(log)
        self.assertNotIn('ee-pass-1', ''.join(self.env['cp.easyecom.log'].search([]).mapped('request_body')))

    def test_expired_token_is_renewed(self):
        fake = FakeEasyEcom()
        self._pull(fake)
        fake.expire_once = True
        self._pull(fake)
        self.assertEqual(fake.token_calls, 2)

    def test_plan_ltl_and_send_status(self):
        self.account.write({'plan_grouping': 'pincode', 'plan_split_channel': True})
        fake = FakeEasyEcom()
        self._pull(fake)
        Order = self.env['cp.channel.order']
        orders = Order.search([('reference', 'in', ('AMZ-1001', 'RRL-2002', 'SHP-3003'))])
        wizard = self.env['cp.channel.plan.wizard'].with_context(active_ids=orders.ids).create({'shipment_mode': 'ltl'})
        self.assertEqual(wizard.order_count, 3)
        wizard.action_plan()
        indents = orders.indent_id
        # Hyderabad marketplace, Hyderabad retail and Mumbai D2C are three loads
        self.assertEqual(len(indents), 3)
        amz = orders.filtered(lambda o: o.reference == 'AMZ-1001')
        indent = amz.indent_id
        self.assertEqual(indent.shipment_mode, 'ltl')
        self.assertEqual(indent.channel, 'marketplace')
        self.assertEqual(indent.line_ids.order_ref, 'AMZ-1001')
        self.assertEqual(indent.line_ids.serial_numbers, 'SN-1\nSN-2')
        indent.action_confirm()
        indent.action_auto_allocate()
        trip = indent.trip_ids
        self.assertEqual(trip.transporter_id, self.tr_b)
        self.assertEqual(amz.trip_id, trip)
        self.assertEqual(amz.state, 'planned')
        with patch(REQ, fake):
            trip.write({'docket_number': 'DKT-55'})
            trip.action_assign()
            self.assertEqual(amz.state, 'shipped')
            self.env['cp.easyecom.account']._cron_sync()
            push = fake.find('updateTrackingStatus')[-1]['json']
            self.assertEqual(push['awb'], 'DKT-55')
            self.assertEqual(push['current_shipment_status_id'], 2)
            self.assertEqual(push['order_id'], '1')
            trip.action_dispatch()
            trip.action_mark_delivered()
            self.assertEqual(amz.state, 'delivered')
            self.env['cp.easyecom.account']._cron_sync()
        self.assertEqual(fake.find('updateTrackingStatus')[-1]['json']['current_shipment_status_id'], 5)
        self.assertFalse(amz.tracking_dirty)
        self.assertEqual(amz.last_push_status, 'delivered')
        self.assertGreater(amz.freight_cost, 0)

    def test_unmapped_warehouse_blocks_planning(self):
        order = self.env['cp.channel.order'].cp_upsert(self.account, ee_order(7, 'X-7', wh='UNKNOWN'))
        with self.assertRaises(UserError):
            order.action_plan_transport()

    def test_inventory_pull_and_cancel_from_webhook(self):
        fake = FakeEasyEcom()
        with patch(REQ, fake):
            self.account._pull_inventory()
        Stock = self.env['cp.channel.stock']
        self.assertEqual(Stock.search_count([('account_id', '=', self.account.id)]), 3)
        fba = Stock.search([('sku', '=', 'PANEL'), ('location_key', '=', 'FBA-HYD')])
        self.assertEqual(fba.available_qty, 30)
        Stock.cp_apply(self.account, [{'sku': 'PANEL', 'location_key': 'FBA-HYD', 'inventory': 18}])
        self.assertEqual(fba.available_qty, 18)
        order = self.env['cp.channel.order'].cp_upsert(self.account, ee_order(8, 'C-8'))
        self.assertEqual(order.state, 'new')
        self.env['cp.channel.order'].cp_upsert(self.account, dict(ee_order(8, 'C-8'), order_status='Cancelled'))
        self.assertEqual(order.state, 'cancel')

    def test_same_city_orders_share_one_load(self):
        """Live test bug 12: three Hyderabad orders with different PIN codes become one multi-drop load."""
        Order = self.env['cp.channel.order']
        self.assertEqual(self.account.plan_grouping, 'city')
        rows = [ee_order(11, 'AMZ-11', pincode='500081'), ee_order(12, 'FK-12', pincode='500032', marketplace='Flipkart'),
                ee_order(13, 'SHOP-13', pincode='500034', marketplace='Shopify')]
        chennai = ee_order(14, 'AMZ-14', pincode='600040')
        chennai.update({'city': 'Chennai', 'state': 'Tamil Nadu'})
        orders = Order.browse()
        for row in rows + [chennai]:
            orders |= Order.cp_upsert(self.account, row)
        orders.action_plan_transport()
        hyd = orders.filtered(lambda o: o.city == 'Hyderabad')
        self.assertEqual(len(hyd.indent_id), 1)
        indent = hyd.indent_id
        self.assertEqual(len(indent.line_ids), 3)
        self.assertIn('multi-drop', indent.lane_id.destination_id.name)
        self.assertEqual(sorted(indent.line_ids.mapped('drop_point')),
                         ['Hyderabad 500032', 'Hyderabad 500034', 'Hyderabad 500081'])
        self.assertFalse(indent.channel, 'marketplace and D2C orders share the load')
        self.assertEqual(sorted(indent.origin.split(', ')), ['AMZ-11', 'FK-12', 'SHOP-13'])
        self.assertNotEqual(orders.filtered(lambda o: o.city == 'Chennai').indent_id, indent)
        self.assertEqual(len(orders.indent_id), 2)
        # the same city drop is reused next time
        again = Order.cp_upsert(self.account, ee_order(15, 'AMZ-15', pincode='500001')) | \
            Order.cp_upsert(self.account, ee_order(16, 'AMZ-16', pincode='500002'))
        again.action_plan_transport()
        self.assertEqual(again.indent_id.lane_id, indent.lane_id)

    def test_split_by_channel_option(self):
        Order = self.env['cp.channel.order']
        self.account.plan_split_channel = True
        orders = Order.cp_upsert(self.account, ee_order(21, 'AMZ-21', pincode='500081')) | \
            Order.cp_upsert(self.account, ee_order(22, 'FK-22', pincode='500032', marketplace='Flipkart')) | \
            Order.cp_upsert(self.account, ee_order(23, 'SHOP-23', pincode='500034', marketplace='Shopify'))
        orders.action_plan_transport()
        self.assertEqual(len(orders.indent_id), 2)
        market = orders.filtered(lambda o: o.reference in ('AMZ-21', 'FK-22')).indent_id
        self.assertEqual(len(market), 1)
        self.assertEqual(market.channel, 'marketplace')
        self.account.write({'plan_grouping': 'order', 'plan_split_channel': False})
        single = Order.cp_upsert(self.account, ee_order(24, 'AMZ-24')) | Order.cp_upsert(self.account, ee_order(25, 'AMZ-25'))
        single.action_plan_transport()
        self.assertEqual(len(single.indent_id), 2)
