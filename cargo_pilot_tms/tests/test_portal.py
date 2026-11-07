# -*- coding: utf-8 -*-
import json
from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, tagged, new_test_user

from odoo.addons.cargo_pilot_tms.tests.common import PIXEL


@tagged('post_install', '-at_install', 'cargo_pilot')
class TestDispatchPortal(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env['cp.rate.contract'].search([('state', '=', 'active')]).write({'state': 'expired'})
        env['cp.channel.rule'].search([]).write({'active': False})
        env.company.cp_dispatch_sms = False
        cls.vendor = env['res.partner'].create({'name': 'Portal 3PL', 'is_company': True, 'cp_is_3pl_vendor': True,
                                                'email': 'portal3pl@example.com'})
        cls.user = new_test_user(env, 'portal3pl', groups='base.group_portal', partner_id=env['res.partner'].create(
            {'name': 'Portal 3PL Desk', 'parent_id': cls.vendor.id, 'email': 'desk@example.com'}).id)
        warehouse = env['stock.warehouse'].search([('company_id', '=', env.company.id)], limit=1)
        customer = env['res.partner'].create({'name': 'Portal Customer', 'city': 'Pune', 'zip': '411001'})
        product = env['product.product'].create({'name': 'Burner', 'is_storable': True, 'weight': 5.0})
        order = env['sale.order'].create({'partner_id': customer.id,
                                          'order_line': [(0, 0, {'product_id': product.id, 'product_uom_qty': 4,
                                                                 'price_unit': 3000})]})
        order.action_confirm()
        cls.plan = env['cp.dispatch.plan'].create({'picking_ids': [(6, 0, order.picking_ids.ids)],
                                                   'warehouse_id': warehouse.id, 'partner_id': customer.id})
        cls.plan.action_ready()
        for carrier in env['res.partner'].search([('cp_regular_carrier', '=', True)]):
            cls.plan.cp_log_check(carrier, 'no_vehicle')
        vt = env['cp.vehicle.type'].create({'name': 'Portal 14ft', 'capacity_ton': 4})
        cls.auction = cls.plan.cp_launch_bid(cls.vendor, 15, vehicle_type=vt)

    def test_live_bid_upload_pod_and_invoice(self):
        self.authenticate('portal3pl', 'portal3pl')
        page = self.url_open('/my/cp/auctions/%s' % self.auction.id)
        self.assertEqual(page.status_code, 200)
        self.assertIn('cp-countdown', page.text)
        live = self.url_open('/my/cp/auctions/%s/live' % self.auction.id).json()
        self.assertEqual(live['state'], 'live')
        self.assertGreater(live['seconds_left'], 800)
        csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]
        res = self.url_open('/my/cp/auctions/%s/bid' % self.auction.id,
                            data={'csrf_token': csrf, 'rate': '7700', 'vehicles': '1'})
        self.assertEqual(res.status_code, 200)
        self.assertIn('Bid placed', res.text)
        self.assertEqual(self.auction.bid_ids.rate, 7700)
        self.assertEqual(self.url_open('/my/cp/auctions/%s/live' % self.auction.id).json()['my_rank'], 1)
        # time is up: the next poll locks the bid
        self.auction.write({'date_start': fields.Datetime.now() - timedelta(minutes=30), 'date_end': fields.Datetime.now() - timedelta(seconds=1)})
        live = self.url_open('/my/cp/auctions/%s/live' % self.auction.id).json()
        self.assertEqual(live['state'], 'closed')
        self.plan.invalidate_recordset()
        self.assertEqual(self.auction.proposed_bid_id.rate, 7700)
        self.plan.cp_award(self.auction.proposed_bid_id)
        trip = self.plan.trip_id
        trip.write({'vehicle_number': 'MH12 PT 1111'})
        trip.with_context(cp_from_plan=True).action_assign()
        trip.action_dispatch()
        self.assertEqual(self.plan.state, 'in_transit', 'trip dispatch moves the plan')

        page = self.url_open('/my/cp/dispatches/%s' % self.plan.id)
        self.assertEqual(page.status_code, 200)
        self.assertIn('Upload POD', page.text)
        csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]
        import base64
        res = self.url_open('/my/cp/dispatches/%s/pod' % self.plan.id, data={'csrf_token': csrf, 'receiver_name': 'DC'},
                            files={'pod_files': ('pod.png', base64.b64decode(PIXEL), 'image/png')})
        self.assertIn('POD uploaded', res.text)
        self.plan.invalidate_recordset()
        self.assertEqual(self.plan.state, 'delivered')
        self.assertEqual(self.plan.pod_status, 'pending')
        self.assertEqual(self.plan.pod_ids.source, 'portal')
        page = self.url_open('/my/cp/dispatches/%s' % self.plan.id)
        csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]
        res = self.url_open('/my/cp/dispatches/%s/invoice' % self.plan.id,
                            data={'csrf_token': csrf, 'bill_ref': 'P3PL/55', 'freight': '7700', 'detention': '0'},
                            files={'invoice_file': ('inv.pdf', b'%PDF-1.4 test', 'application/pdf')})
        self.assertIn('Invoice submitted', res.text)
        move = self.plan.move_ids
        self.assertEqual(move.ref, 'P3PL/55')
        self.assertEqual(move.state, 'draft')
        self.assertEqual(move.cp_match_state, 'ok')
        self.assertTrue(self.env['ir.attachment'].search([('res_model', '=', 'account.move'), ('res_id', '=', move.id)]))
        # someone else's dispatch is not visible
        other = self.env['res.partner'].create({'name': 'Other 3PL', 'is_company': True, 'cp_is_3pl_vendor': True})
        self.plan.sudo().vendor_id = other
        self.assertEqual(self.url_open('/my/cp/dispatches/%s' % self.plan.id).status_code, 404)
