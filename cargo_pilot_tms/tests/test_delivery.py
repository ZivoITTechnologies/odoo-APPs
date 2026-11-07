# -*- coding: utf-8 -*-
from odoo.tests import Form, tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install')
class TestCourierDelivery(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        india = cls.env.ref('base.in')
        cls.env.company.partner_id.write({'street': 'Plot 1', 'city': 'Raipur', 'zip': '492003',
                                          'country_id': india.id, 'phone': '9826000001'})
        cls.customer = cls.env['res.partner'].create({
            'name': 'Asha Retail', 'street': '22 FC Road', 'city': 'Pune', 'zip': '411005', 'country_id': india.id,
            'phone': '+91 98220 45671'})
        cls.account = cls.env['cp.courier.account'].create({
            'name': 'Sim', 'provider': 'demo', 'pickup_partner_id': cls.env.company.partner_id.id,
            'notify_customer': False})
        cls.carrier = cls.env['delivery.carrier'].create({
            'name': 'Courier best', 'delivery_type': 'cp_courier', 'cp_account_id': cls.account.id,
            'product_id': cls.env.ref('cargo_pilot_tms.product_cp_courier_shipping').id,
            'cp_choice': 'cheapest'})
        cls.product = cls.env['product.product'].create({'name': 'Kettle', 'is_storable': True, 'weight': 1.5,
                                                         'list_price': 1200})
        cls.env['stock.quant']._update_available_quantity(
            cls.product, cls.env.ref('stock.warehouse0').lot_stock_id, 20)

    def _order(self):
        order = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'order_line': [(0, 0, {'product_id': self.product.id, 'product_uom_qty': 2, 'price_unit': 1200})]})
        return order

    def test_rate_on_order(self):
        order = self._order()
        res = self.carrier.rate_shipment(order)
        self.assertTrue(res['success'], res.get('error_message'))
        self.assertGreater(res['price'], 0)
        # 3 kg on the rate card, then the simulator's cheapest service
        self.assertIn('day', res['warning_message'])
        self.assertFalse(self.env['cp.parcel'].search([('reference', '=', order.name)]),
                         'quoting must not create parcels')

    def test_validate_delivery_books_awb(self):
        order = self._order()
        wizard = Form(self.env['choose.delivery.carrier'].with_context(
            default_order_id=order.id, default_carrier_id=self.carrier.id))
        wizard.save().button_confirm()
        order.action_confirm()
        picking = order.picking_ids
        self.assertEqual(picking.carrier_id, self.carrier)
        picking.move_ids.quantity = 2
        picking.move_ids.picked = True
        picking.button_validate()
        self.assertEqual(picking.state, 'done')
        parcel = picking.cp_parcel_ids
        self.assertEqual(len(parcel), 1)
        self.assertEqual(parcel.state, 'booked')
        self.assertEqual(picking.carrier_tracking_ref, parcel.awb)
        self.assertEqual(parcel.sale_order_id, order)
        self.assertAlmostEqual(parcel.total_weight_kg, 3.0)
        self.assertIn('/cp/parcel/', picking.carrier_tracking_url)
        self.assertTrue(self.env['ir.attachment'].search_count([('res_model', '=', 'stock.picking'),
                                                                ('res_id', '=', picking.id)]))
        picking.cancel_shipment()
        self.assertEqual(parcel.state, 'cancelled')
        self.assertFalse(picking.carrier_tracking_ref)
