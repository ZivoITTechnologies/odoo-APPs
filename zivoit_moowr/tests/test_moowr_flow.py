from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import Form, tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install')
class TestMoowrFlow(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        india = cls.env.ref('base.in')
        cls.company.country_id = india
        cls.inr = cls.env.ref('base.INR')
        cls.inr.active = True
        cls.company.currency_id = cls.inr
        cls.warehouse = cls.env['stock.warehouse'].search([('company_id', '=', cls.company.id)], limit=1)
        cls.stock = cls.warehouse.lot_stock_id
        cls.licence = cls.env['moowr.licence'].create({
            'name': 'TEST/MOOWR/001', 'permission_number': 'S65/001', 'warehouse_id': cls.warehouse.id,
            'customs_station': 'INMAA1', 'date_start': fields.Date.today() - timedelta(days=30),
        })
        cls.vendor = cls.env['res.partner'].create({'name': 'Overseas Cell Supplier', 'country_id': cls.env.ref('base.cn').id})
        cls.local_customer = cls.env['res.partner'].create({'name': 'Domestic Dealer', 'country_id': india.id})
        cls.foreign_customer = cls.env['res.partner'].create({'name': 'Export Buyer', 'country_id': cls.env.ref('base.us').id})

        def product(name, goods_type, bcd=10.0, price=0.0):
            return cls.env['product.product'].create({
                'name': name, 'type': 'consu', 'is_storable': True, 'moowr_goods_type': goods_type,
                'moowr_bcd_rate': bcd, 'moowr_sws_rate': 10.0, 'moowr_igst_rate': 18.0, 'standard_price': price,
            })
        cls.cell = product('LFP Cell 3.2V 100Ah', 'input', price=1000.0)
        cls.bms = product('BMS 16S 100A', 'input', bcd=7.5, price=4000.0)
        cls.pack = product('Battery Pack 51.2V 100Ah', 'finished')
        cls.bom = cls.env['mrp.bom'].create({
            'product_tmpl_id': cls.pack.product_tmpl_id.id, 'product_qty': 1.0,
            'bom_line_ids': [(0, 0, {'product_id': cls.cell.id, 'product_qty': 16.0}),
                             (0, 0, {'product_id': cls.bms.id, 'product_qty': 1.0})],
        })

    # ------------------------------------------------------------------
    def _purchase(self, lines):
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id, 'currency_id': self.inr.id,
            'picking_type_id': self.warehouse.in_type_id.id,
            'order_line': [(0, 0, {'product_id': p.id, 'product_qty': q, 'price_unit': price}) for p, q, price in lines],
        })
        po.button_confirm()
        return po

    def _receive(self, po):
        picking = po.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        picking.button_validate()
        return picking

    def _bill_of_entry(self, po, name):
        action = po.action_create_moowr_boe()
        boe = self.env['moowr.boe'].browse(action['res_id'])
        boe.name = name
        boe.action_confirm()
        return boe

    def _produce(self, qty):
        mo_form = Form(self.env['mrp.production'])
        mo_form.product_id = self.pack
        mo_form.bom_id = self.bom
        mo_form.product_qty = qty
        mo = mo_form.save()
        mo.action_confirm()
        mo.action_assign()
        mo_form = Form(mo)
        mo_form.qty_producing = qty
        mo = mo_form.save()
        mo.move_raw_ids.picked = True
        mo.button_mark_done()
        self.assertEqual(mo.state, 'done')
        return mo

    def _deliver(self, partner, product, qty, removal='auto'):
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.warehouse.out_type_id.id, 'partner_id': partner.id,
            'location_id': self.stock.id, 'location_dest_id': self.env.ref('stock.stock_location_customers').id,
            'moowr_clearance_type': removal, 'moowr_document_number': 'DOC-%s' % qty,
            'move_ids': [(0, 0, {'product_id': product.id, 'product_uom_qty': qty, 'product_uom': product.uom_id.id,
                                 'location_id': self.stock.id,
                                 'location_dest_id': self.env.ref('stock.stock_location_customers').id})],
        })
        picking.action_confirm()
        picking.action_assign()
        for move in picking.move_ids:
            move.quantity = qty
            move.picked = True
        picking.button_validate()
        self.assertEqual(picking.state, 'done')
        return picking

    # ------------------------------------------------------------------
    def test_01_licence_and_locations(self):
        self.assertEqual(self.stock.moowr_licence_id, self.licence)
        self.assertTrue(self.stock.moowr_bonded)
        child = self.env['stock.location'].create({'name': 'Rack A', 'location_id': self.stock.id})
        self.assertEqual(child._moowr_licence(), self.licence)
        customers = self.env.ref('stock.stock_location_customers')
        self.assertFalse(customers._moowr_licence())
        with self.assertRaises(ValidationError):
            self.licence.date_end = self.licence.date_start - timedelta(days=1)

    def test_02_duty_calculation(self):
        po = self._purchase([(self.cell, 100, 1000.0)])
        self.assertTrue(po.order_line.moowr_bonded)
        boe = self._bill_of_entry(po, 'BOE-1001')
        line = boe.line_ids
        self.assertEqual(line.assessable_value, 100000.0)
        self.assertEqual(line.bcd_amount, 10000.0)
        self.assertEqual(line.sws_amount, 1000.0)
        self.assertEqual(line.igst_amount, 19980.0)  # 18% of (100000 + 10000 + 1000)
        self.assertEqual(line.duty_amount, 30980.0)
        self.assertAlmostEqual(line.unit_duty, 309.8)

    def test_03_full_bonded_cycle(self):
        po = self._purchase([(self.cell, 100, 1000.0), (self.bms, 10, 4000.0)])
        boe = self._bill_of_entry(po, 'BOE-2001')
        self._receive(po)
        cell_line = boe.line_ids.filtered(lambda l: l.product_id == self.cell)
        bms_line = boe.line_ids.filtered(lambda l: l.product_id == self.bms)
        self.assertEqual(cell_line.qty_received, 100)
        self.assertEqual(bms_line.qty_balance, 10)
        self.assertEqual(len(boe.line_ids.ledger_ids.filtered(lambda l: l.move_type == 'receipt')), 2)

        # manufacture 5 packs: 80 cells + 5 BMS consumed
        mo = self._produce(5)
        self.assertEqual(cell_line.qty_consumed, 80)
        self.assertEqual(bms_line.qty_consumed, 5)
        batch = self.env['moowr.batch'].search([('production_id', '=', mo.id)])
        self.assertEqual(len(batch), 1)
        expected_duty = round(80 * cell_line.unit_duty, 2) + round(5 * bms_line.unit_duty, 2)
        self.assertAlmostEqual(batch.duty_content, expected_duty, places=2)
        self.assertEqual(batch.qty_balance, 5)

        # 2 packs sold in India: duty payable on the imported content
        dom = self._deliver(self.local_customer, self.pack, 2)
        clearance = dom.moowr_clearance_ids
        self.assertEqual(clearance.clearance_type, 'home')
        self.assertAlmostEqual(clearance.amount_duty, round(batch.duty_content * 2 / 5, 2), places=1)
        self.assertEqual(clearance.amount_payable, clearance.amount_duty)
        self.assertEqual(clearance.document_number, 'DOC-2')

        # 1 pack exported: no duty payable
        exp = self._deliver(self.foreign_customer, self.pack, 1)
        self.assertEqual(exp.moowr_clearance_ids.clearance_type, 'export')
        self.assertEqual(exp.moowr_clearance_ids.amount_payable, 0.0)
        self.assertGreater(exp.moowr_clearance_ids.duty_forgone, 0.0)
        self.assertEqual(batch.qty_balance, 2)

        # 5 cells cleared as such in India
        self._deliver(self.local_customer, self.cell, 5)
        self.assertEqual(cell_line.qty_cleared, 5)
        self.assertEqual(cell_line.qty_balance, 15)

        # clearance workflow
        with self.assertRaises(UserError):
            clearance.write({'document_number': False})
            clearance.action_file()
        clearance.document_number = 'HOME-BOE-77'
        clearance.action_file()
        self.assertEqual(clearance.state, 'filed')
        with self.assertRaises(UserError):
            clearance.action_mark_paid()
        clearance.payment_reference = 'CHALLAN-1'
        clearance.action_mark_paid()
        self.assertEqual(clearance.state, 'paid')

        # licence KPIs
        lic = self.licence
        self.assertGreater(lic.duty_in_bond, 0)
        self.assertEqual(lic.open_boe_count, 1)
        self.assertAlmostEqual(lic.duty_in_bond, round(15 * cell_line.unit_duty, 2) + round(5 * bms_line.unit_duty, 2)
                               + batch.duty_balance, places=1)

    def test_04_receipt_without_bill_of_entry_is_blocked(self):
        po = self._purchase([(self.cell, 10, 1000.0)])
        self.assertTrue(po.order_line.moowr_bonded)
        with self.assertRaises(UserError):
            self._receive(po)

    def test_05_receipt_over_declared_quantity_is_blocked(self):
        po = self._purchase([(self.cell, 10, 1000.0)])
        boe = po.action_create_moowr_boe()
        boe = self.env['moowr.boe'].browse(boe['res_id'])
        boe.line_ids.quantity = 6
        boe.name = 'BOE-SHORT'
        boe.action_confirm()
        with self.assertRaises(UserError):
            self._receive(po)

    def test_06_scrap_return_and_destruction(self):
        po = self._purchase([(self.cell, 20, 1000.0)])
        boe = self._bill_of_entry(po, 'BOE-3001')
        self._receive(po)
        line = boe.line_ids
        scrap = self.env['stock.scrap'].create({
            'product_id': self.cell.id, 'scrap_qty': 2, 'location_id': self.stock.id,
        })
        scrap.action_validate()
        self.assertEqual(line.qty_scrapped, 2)
        clearance = self.env['moowr.clearance'].search([('clearance_type', '=', 'scrap')], limit=1, order='id desc')
        self.assertAlmostEqual(clearance.amount_payable, round(2 * line.unit_duty, 2), places=2)
        clearance.action_mark_destroyed()
        self.assertEqual(clearance.amount_payable, 0.0)
        self.assertEqual(clearance.state, 'done')

        ret_picking = self.env['stock.picking'].create({
            'picking_type_id': self.warehouse.out_type_id.id, 'partner_id': self.vendor.id,
            'location_id': self.stock.id, 'location_dest_id': self.env.ref('stock.stock_location_suppliers').id,
            'move_ids': [(0, 0, {'product_id': self.cell.id, 'product_uom_qty': 3, 'product_uom': self.cell.uom_id.id,
                                 'location_id': self.stock.id,
                                 'location_dest_id': self.env.ref('stock.stock_location_suppliers').id})],
        })
        ret_picking.action_confirm()
        ret_picking.action_assign()
        ret_picking.move_ids.write({'quantity': 3, 'picked': True})
        ret_picking.button_validate()
        self.assertEqual(line.qty_returned, 3)
        self.assertEqual(line.qty_balance, 15)
        self.assertEqual(ret_picking.moowr_clearance_ids.amount_payable, 0.0)

    def test_07_interest_after_free_period(self):
        po = self._purchase([(self.cell, 10, 1000.0)])
        boe = self._bill_of_entry(po, 'BOE-4001')
        self._receive(po)
        line = boe.line_ids
        received = line.date_first_receipt
        interest, days = line._interest_for(1000.0, received + timedelta(days=90))
        self.assertEqual(interest, 0.0)
        interest, days = line._interest_for(1000.0, received + timedelta(days=455))
        self.assertEqual(days, 455)
        self.assertAlmostEqual(interest, 150.0, places=2)  # 15% for 365 days beyond the free period

    def test_08_fifo_across_bills_of_entry(self):
        po1 = self._purchase([(self.cell, 10, 1000.0)])
        boe1 = self._bill_of_entry(po1, 'BOE-5001')
        self._receive(po1)
        po2 = self._purchase([(self.cell, 10, 1200.0)])
        boe2 = self._bill_of_entry(po2, 'BOE-5002')
        boe2.boe_date = boe1.boe_date + timedelta(days=1)
        self._receive(po2)
        self._deliver(self.local_customer, self.cell, 14)
        self.assertEqual(boe1.line_ids.qty_cleared, 10)
        self.assertEqual(boe2.line_ids.qty_cleared, 4)
        self.assertEqual(boe1.state, 'closed', "A fully used bill of entry closes automatically")
        self.assertEqual(boe2.state, 'confirmed')
        boe1.action_reopen()
        self.assertEqual(boe1.state, 'confirmed')
        boe1.action_close()
        self.assertEqual(boe1.state, 'closed')
        with self.assertRaises(UserError):
            boe2.action_close()

    def test_09_igst_not_deferred(self):
        self.licence.defer_igst = False
        po = self._purchase([(self.cell, 10, 1000.0)])
        boe = self._bill_of_entry(po, 'BOE-6001')
        self.assertEqual(boe.line_ids.duty_amount, boe.line_ids.bcd_amount + boe.line_ids.sws_amount)

    def test_10_register_is_protected(self):
        po = self._purchase([(self.cell, 10, 1000.0)])
        boe = self._bill_of_entry(po, 'BOE-7001')
        self._receive(po)
        entry = boe.line_ids.ledger_ids[:1]
        with self.assertRaises(UserError):
            entry.quantity = 1
        with self.assertRaises(UserError):
            entry.unlink()
        with self.assertRaises(UserError):
            boe.action_draft()
        with self.assertRaises(UserError):
            boe.unlink()

    def test_11_monthly_return(self):
        po = self._purchase([(self.cell, 40, 1000.0), (self.bms, 4, 4000.0)])
        self._bill_of_entry(po, 'BOE-8001')
        self._receive(po)
        self._produce(2)
        self._deliver(self.local_customer, self.pack, 1)
        self._deliver(self.foreign_customer, self.pack, 1)
        ret = self.licence._ensure_return(fields.Date.today())
        self.assertEqual(ret.date_from, fields.Date.today().replace(day=1))
        self.assertEqual(ret.due_date.day, 10)
        data = ret._get_return_data()
        self.assertEqual(len(data['receipts']), 2)
        self.assertEqual(len(data['operations']), 1)
        self.assertEqual(len(data['removals']), 2)
        self.assertTrue(data['balances'])
        self.assertGreater(ret.receipt_duty, 0)
        self.assertGreater(ret.home_duty, 0)
        self.assertGreater(ret.closing_duty, 0)
        content = ret._generate_xlsx()
        self.assertTrue(content.startswith(b'PK'))
        html = self.env['ir.actions.report']._render_qweb_html('zivoit_moowr.report_moowr_return', ret.ids)[0]
        self.assertIn(b'BOE-8001', html)
        clearance = self.env['moowr.clearance'].search([('licence_id', '=', self.licence.id)], limit=1)
        html = self.env['ir.actions.report']._render_qweb_html('zivoit_moowr.report_moowr_clearance', clearance.ids)[0]
        self.assertIn(b'Removal from Bonded Warehouse', html)
        with self.assertRaises(UserError):
            ret.action_submit()
        ret.acknowledgement = 'ACK-123'
        ret.action_submit()
        self.assertEqual(ret.state, 'submitted')

    def test_12_cron_and_dashboard(self):
        self.env['moowr.licence']._cron_moowr_reminders()
        self.assertTrue(self.env['moowr.return'].search([('licence_id', '=', self.licence.id)]))
        self.licence.invalidate_recordset()
        self.assertTrue(self.licence.next_return_due)
        for method in ('action_open_boes', 'action_open_stock', 'action_open_aged', 'action_open_ledger',
                       'action_open_clearances', 'action_open_returns', 'action_open_batches',
                       'action_open_pending_clearances', 'action_open_open_boes'):
            self.assertEqual(getattr(self.licence, method)()['type'], 'ir.actions.act_window')

    def test_13_domestic_inputs_need_no_bill_of_entry(self):
        local_vendor = self.env['res.partner'].create({'name': 'Local Vendor', 'country_id': self.env.ref('base.in').id})
        po = self.env['purchase.order'].create({
            'partner_id': local_vendor.id, 'currency_id': self.inr.id, 'picking_type_id': self.warehouse.in_type_id.id,
            'order_line': [(0, 0, {'product_id': self.cell.id, 'product_qty': 5, 'price_unit': 900.0})],
        })
        po.button_confirm()
        self.assertFalse(po.order_line.moowr_bonded)
        self._receive(po)
        self.assertFalse(self.env['moowr.ledger'].search([('picking_id', 'in', po.picking_ids.ids)]))
        # duty-paid stock leaves without any duty entry
        picking = self._deliver(self.local_customer, self.cell, 5)
        self.assertFalse(picking.moowr_clearance_ids)

    def test_14_finished_goods_stored_outside_bond(self):
        po = self._purchase([(self.cell, 16, 1000.0), (self.bms, 1, 4000.0)])
        self._bill_of_entry(po, 'BOE-9001')
        self._receive(po)
        domestic_loc = self.env['stock.location'].create({'name': 'Duty Paid FG', 'usage': 'internal',
                                                          'location_id': self.warehouse.view_location_id.id})
        mo_form = Form(self.env['mrp.production'])
        mo_form.product_id = self.pack
        mo_form.bom_id = self.bom
        mo_form.product_qty = 1
        mo = mo_form.save()
        mo.location_dest_id = domestic_loc
        mo.move_finished_ids.location_dest_id = domestic_loc
        mo.action_confirm()
        mo.action_assign()
        mo_form = Form(mo)
        mo_form.qty_producing = 1
        mo = mo_form.save()
        mo.move_raw_ids.picked = True
        mo.button_mark_done()
        self.assertEqual(mo.state, 'done')
        clearance = self.env['moowr.clearance'].search([('production_id', '=', mo.id)])
        self.assertEqual(clearance.clearance_type, 'home')
        self.assertGreater(clearance.amount_payable, 0)

    def test_15_inventory_user_without_moowr_rights(self):
        po = self._purchase([(self.cell, 10, 1000.0)])
        boe = self._bill_of_entry(po, 'BOE-9101')
        user = self.env['res.users'].create({
            'name': 'Store Keeper', 'login': 'storekeeper_moowr',
            'group_ids': [(6, 0, [self.env.ref('stock.group_stock_user').id])],
        })
        picking = po.picking_ids.with_user(user)
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        picking.button_validate()
        self.assertEqual(boe.line_ids.qty_received, 10)

    def test_16_unit_of_measure_conversion(self):
        dozen = self.env.ref('uom.product_uom_dozen')
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id, 'currency_id': self.inr.id, 'picking_type_id': self.warehouse.in_type_id.id,
            'order_line': [(0, 0, {'product_id': self.cell.id, 'product_qty': 2, 'product_uom_id': dozen.id,
                                   'price_unit': 12000.0})],
        })
        po.button_confirm()
        boe = self._bill_of_entry(po, 'BOE-9201')
        self.assertEqual(boe.line_ids.quantity, 24)
        self._receive(po)
        self.assertEqual(boe.line_ids.qty_received, 24)
        self.assertEqual(boe.line_ids.qty_balance, 24)

    def test_17_manual_export_override(self):
        po = self._purchase([(self.cell, 10, 1000.0)])
        self._bill_of_entry(po, 'BOE-9301')
        self._receive(po)
        picking = self._deliver(self.local_customer, self.cell, 4, removal='export')
        self.assertEqual(picking.moowr_clearance_ids.clearance_type, 'export')
        self.assertEqual(picking.moowr_clearance_ids.amount_payable, 0.0)
        picking.moowr_clearance_ids.action_file()
        self.assertEqual(picking.moowr_clearance_ids.state, 'done')

    def test_18_sub_assembly_carries_duty(self):
        module = self.env['product.product'].create({'name': 'Cell Module 4S', 'is_storable': True,
                                                     'moowr_goods_type': 'finished'})
        self.env['mrp.bom'].create({'product_tmpl_id': module.product_tmpl_id.id, 'product_qty': 1.0,
                                    'bom_line_ids': [(0, 0, {'product_id': self.cell.id, 'product_qty': 4.0})]})
        pack_bom = self.env['mrp.bom'].create({
            'product_tmpl_id': self.pack.product_tmpl_id.id, 'product_qty': 1.0, 'sequence': 1,
            'bom_line_ids': [(0, 0, {'product_id': module.id, 'product_qty': 4.0}),
                             (0, 0, {'product_id': self.bms.id, 'product_qty': 1.0})],
        })
        po = self._purchase([(self.cell, 16, 1000.0), (self.bms, 1, 4000.0)])
        boe = self._bill_of_entry(po, 'BOE-9401')
        self._receive(po)

        def make(product, bom, qty):
            mo_form = Form(self.env['mrp.production'])
            mo_form.product_id = product
            mo_form.bom_id = bom
            mo_form.product_qty = qty
            mo = mo_form.save()
            mo.action_confirm()
            mo.action_assign()
            mo_form = Form(mo)
            mo_form.qty_producing = qty
            mo = mo_form.save()
            mo.move_raw_ids.picked = True
            mo.button_mark_done()
            return mo
        mo_module = make(module, module.bom_ids, 4)
        module_batch = self.env['moowr.batch'].search([('production_id', '=', mo_module.id)])
        mo_pack = make(self.pack, pack_bom, 1)
        pack_batch = self.env['moowr.batch'].search([('production_id', '=', mo_pack.id)])
        self.assertEqual(module_batch.qty_balance, 0)
        cell_duty = boe.line_ids.filtered(lambda l: l.product_id == self.cell).duty_amount
        bms_duty = boe.line_ids.filtered(lambda l: l.product_id == self.bms).duty_amount
        self.assertAlmostEqual(pack_batch.duty_content, cell_duty + bms_duty, places=1)
