# -*- coding: utf-8 -*-
from odoo import fields
from odoo.tests import tagged

from odoo.addons.cargo_pilot_tms.tests.common import PIXEL, CpCommon


@tagged('post_install', '-at_install', 'cargo_pilot')
class TestCargoPilotStock(CpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        india = env.ref('base.in')
        State = env['res.country.state']
        cls.st_tg = State.search([('country_id', '=', india.id), ('code', '=', 'TS')], limit=1) or \
            State.create({'name': 'Telangana', 'code': 'TS', 'country_id': india.id})
        cls.wh = env['stock.warehouse'].search([('company_id', '=', env.company.id)], limit=1)
        cls.wh.cp_location_id = cls.origin
        cls.wh2 = env['stock.warehouse'].create({'name': 'Hyderabad DC', 'code': 'HYD'})
        cls.buyer = env['res.partner'].create({'name': 'Retail DC Hyderabad', 'state_id': cls.st_tg.id,
                                               'city': 'Hyderabad', 'zip': '500001', 'cp_location_id': cls.dest.id})
        cls.pump = env['product.product'].create({'name': 'Solar Pump 3HP', 'default_code': 'PUMP-3HP',
                                                  'is_storable': True, 'tracking': 'serial', 'weight': 45.0,
                                                  'list_price': 30000, 'standard_price': 21000})
        stock_loc = cls.wh.lot_stock_id
        for i in range(1, 5):
            lot = env['stock.lot'].create({'name': 'SP3-%03d' % i, 'product_id': cls.pump.id})
            env['stock.quant']._update_available_quantity(cls.pump, stock_loc, 1, lot_id=lot)

    def _sale(self, qty=2, partner=None):
        order = self.env['sale.order'].create({
            'partner_id': (partner or self.buyer).id, 'cp_channel': 'modern_retail',
            'order_line': [(0, 0, {'product_id': self.pump.id, 'product_uom_qty': qty, 'price_unit': 30000})]})
        order.action_confirm()
        return order

    def test_plan_truck_from_delivery_and_margin_after_freight(self):
        order = self._sale()
        picking = order.picking_ids
        picking.action_assign()
        self.assertEqual(sorted(picking.cp_serials.split()), ['SP3-001', 'SP3-002'])
        self.assertEqual(picking.cp_weight_kg, 90.0)
        self.assertEqual(picking.cp_channel, 'modern_retail')
        wizard = self.env['cp.picking.plan.wizard'].with_context(active_ids=picking.ids).create(
            {'shipment_mode': 'ftl', 'vehicle_type_id': self.vtype.id})
        self.assertEqual(wizard.serial_count, 2)
        wizard.action_plan()
        indent = picking.cp_indent_id
        self.assertEqual(indent.lane_id, self.lane)
        self.assertEqual(indent.channel, 'modern_retail')
        line = indent.line_ids
        self.assertEqual(line.order_ref, order.name)
        self.assertEqual(line.picking_id, picking)
        # Order No. and Source Document carry the sales order, not the delivery number (live test bug 10)
        self.assertEqual(line.sale_order_id, order)
        self.assertEqual(indent.origin, order.name)
        self.assertNotIn(picking.name, indent.origin)
        self.assertAlmostEqual(line.order_value, 60000)
        indent.action_confirm()
        indent.action_auto_allocate()
        trip = indent.trip_ids
        self.assertEqual(trip.picking_ids, picking)
        self.assertEqual(trip.order_ids.sale_order_id, order)
        self.assertEqual(picking.cp_trip_id, trip)
        self.assertEqual(order.cp_shipment_count, 1)
        self.assertEqual(order.cp_freight_cost, 50000)
        self.assertAlmostEqual(order.cp_margin_after_freight, order.margin - 50000)
        self.assertAlmostEqual(order.cp_freight_pct, 50000 / 60000 * 100, places=2)
        # serials travel to the proof of delivery and the delivery is validated from the shipment
        trip.write({'vehicle_number': 'CG04ST0001'})
        trip.action_assign()
        trip.action_dispatch()
        epod = self.env['cp.epod'].create({
            'trip_id': trip.id, 'receiver_name': 'DC', 'received_weight_ton': trip.loaded_weight_ton,
            'latitude': self.dest.latitude, 'longitude': self.dest.longitude, 'signature': PIXEL,
            'scanned_serials': 'SP3-001\nSP3-002', 'photo_ids': [(0, 0, {'name': 'pod.png', 'datas': PIXEL})]})
        epod.action_submit()
        self.assertFalse(epod.missing_serials)
        self.assertEqual(trip.state, 'delivered')
        trip.action_cp_validate_pickings()
        self.assertEqual(picking.state, 'done')

    def test_warehouse_rules(self):
        Rule = self.env['cp.allocation.rule']
        Rule.create({'name': 'South from Hyderabad', 'sequence': 1, 'warehouse_id': self.wh2.id,
                     'state_ids': [(6, 0, self.st_tg.ids)], 'check_stock': True})
        order = self.env['sale.order'].create({
            'partner_id': self.buyer.id, 'warehouse_id': self.wh2.id,
            'order_line': [(0, 0, {'product_id': self.pump.id, 'product_uom_qty': 1})]})
        warehouse, rule = Rule.cp_choose(order)
        self.assertEqual(warehouse, self.wh, 'Hyderabad has no stock, so the warehouse with stock ships')
        self.assertFalse(rule)
        order.action_confirm()
        self.assertEqual(order.warehouse_id, self.wh)
        self.assertEqual(order.picking_ids.picking_type_id.warehouse_id, self.wh)
        Rule.search([]).check_stock = False
        order2 = self.env['sale.order'].create({
            'partner_id': self.buyer.id, 'order_line': [(0, 0, {'product_id': self.pump.id, 'product_uom_qty': 1})]})
        order2.action_cp_allocate_warehouse()
        self.assertEqual(order2.warehouse_id, self.wh2)

    def test_stock_360(self):
        order = self._sale(qty=1)
        picking = order.picking_ids
        picking.action_assign()
        supplier = self.env['res.partner'].create({'name': 'Pump OEM'})
        receipt = self.env['stock.picking'].create({
            'picking_type_id': self.wh.in_type_id.id, 'partner_id': supplier.id,
            'location_id': self.env.ref('stock.stock_location_suppliers').id,
            'location_dest_id': self.wh.lot_stock_id.id,
            'move_ids': [(0, 0, {'product_id': self.pump.id, 'product_uom_qty': 10,
                                 'product_uom': self.pump.uom_id.id, 'location_id': self.env.ref('stock.stock_location_suppliers').id,
                                 'location_dest_id': self.wh.lot_stock_id.id})]})
        receipt.action_confirm()
        picking.action_cp_plan_transport(mode='ftl', vehicle_type=self.vtype)
        indent = picking.cp_indent_id
        indent.action_confirm()
        indent.action_auto_allocate()
        trip = indent.trip_ids
        trip.write({'vehicle_number': 'CG04ST0002'})
        trip.action_assign()
        Position = self.env['cp.stock.position']
        Position.cp_refresh(self.env.company)
        rows = Position.search([('product_id', '=', self.pump.id)])
        by_bucket = {b: sum(rows.filtered(lambda r: r.bucket == b).mapped('quantity')) for b in set(rows.mapped('bucket'))}
        self.assertEqual(by_bucket['warehouse'], 3)
        self.assertEqual(by_bucket['reserved'], 1)
        self.assertEqual(by_bucket['incoming'], 10)
        self.assertEqual(by_bucket['transit'], 1)
        self.assertEqual(rows.filtered(lambda r: r.bucket == 'incoming').place, 'Pump OEM')
        self.assertAlmostEqual(rows.filtered(lambda r: r.bucket == 'warehouse').value, 3 * 21000)

    def test_order_numbers_from_undelivered_sales_orders(self):
        open_order = self._sale(qty=2)
        open_order.picking_ids.action_assign()
        second = self._sale(qty=1)
        done_order = self._sale(qty=1)
        picking = done_order.picking_ids
        picking.action_assign()
        picking.move_ids.quantity = 1
        picking.with_context(skip_backorder=True).button_validate()
        self.assertEqual(done_order.delivery_status, 'full')
        # only confirmed, not fully delivered orders can be picked
        domain = self.env['cp.indent.line']._fields['sale_order_id'].domain
        allowed = self.env['sale.order'].search(
            eval(domain, {'company_id': self.env.company.id}))
        self.assertIn(open_order, allowed)
        self.assertIn(second, allowed)
        self.assertNotIn(done_order, allowed)
        indent = self.env['cp.indent'].create({
            'lane_id': self.lane.id, 'vehicle_type_id': self.vtype.id,
            'line_ids': [(0, 0, {'sale_order_id': open_order.id}), (0, 0, {'sale_order_id': second.id})]})
        first_line = indent.line_ids.filtered(lambda l: l.sale_order_id == open_order)
        self.assertEqual(first_line.order_ref, open_order.name)
        self.assertEqual(first_line.customer_id, self.buyer)
        self.assertAlmostEqual(first_line.order_value, 60000)
        self.assertAlmostEqual(first_line.weight_kg, 90.0)
        self.assertEqual(first_line.picking_id, open_order.picking_ids)
        self.assertEqual(sorted(first_line.serial_numbers.split()), ['SP3-001', 'SP3-002'])
        # the source document lists the chosen orders and follows changes
        self.assertEqual(indent.origin, '%s, %s' % (open_order.name, second.name))
        indent.line_ids.filtered(lambda l: l.sale_order_id == second).unlink()
        self.assertEqual(indent.origin, open_order.name)
        indent.action_confirm()
        indent.action_auto_allocate()
        trip = indent.trip_ids
        self.assertEqual(trip.order_ids.sale_order_id, open_order)
        self.assertEqual(trip.customer_ref, open_order.name)

    def test_plan_truck_for_several_deliveries_lists_sales_orders(self):
        first, second = self._sale(qty=1), self._sale(qty=1)
        pickings = first.picking_ids | second.picking_ids
        wizard = self.env['cp.picking.plan.wizard'].with_context(active_ids=pickings.ids).create(
            {'shipment_mode': 'ftl', 'vehicle_type_id': self.vtype.id})
        wizard.action_plan()
        indent = first.picking_ids.cp_indent_id
        self.assertEqual(indent, second.picking_ids.cp_indent_id)
        self.assertEqual(set(indent.line_ids.mapped('sale_order_id').ids), {first.id, second.id})
        self.assertEqual(indent.origin, '%s, %s' % (first.name, second.name))
