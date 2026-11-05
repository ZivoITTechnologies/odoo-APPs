# -*- coding: utf-8 -*-
"""Godowns, batches and stock moves against the TallyPrime simulator."""
import unittest
from datetime import date

from odoo.tests import tagged

from .common import TallyCommon


@tagged('post_install', '-at_install', 'zivoit_tally')
class TestTallyStock(TallyCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref('stock.group_production_lot') | \
            cls.env.ref('stock.group_stock_multi_locations') | \
            cls.env.ref('stock.group_stock_manager')
        sale_mgr = cls.env.ref('sales_team.group_sale_manager', raise_if_not_found=False)
        if sale_mgr:
            cls.env.user.group_ids |= sale_mgr
        cls.wh = cls.env['stock.warehouse'].search([('company_id', '=', cls.env.company.id)],
                                                   limit=1)
        cls.stock = cls.wh.lot_stock_id
        cls.shelf = cls.env['stock.location'].create({
            'name': 'Hyderabad WH', 'usage': 'internal', 'location_id': cls.wh.lot_stock_id.id})
        cls.widget.is_storable = True
        cls.paint = cls.env['product.product'].create({
            'name': 'Paint Drum 20L', 'type': 'consu', 'is_storable': True, 'tracking': 'lot',
            'list_price': 1000.0, 'taxes_id': [(6, 0, cls.gst18_s.ids)],
            'supplier_taxes_id': [(6, 0, cls.gst18_p.ids)],
            'property_account_income_id': cls.company_data['default_account_revenue'].id})
        cls.Quant = cls.env['stock.quant']

    def setUp(self):
        super().setUp()
        self.conn.warehouse_id = self.wh
        self.seed_tally_masters()
        self.sim.add_item('Paint Drum 20L', unit='Nos', hsn='3208', gst_rate=18, price=1000.0)
        self.sim.company.items['Paint Drum 20L']['batchwise'] = True
        self.sim.add_ledger('Purchase GST 18%', 'Purchase Accounts')
        self.sim.add_ledger('Input CGST 9%', 'Duties & Taxes', tax_type='GST', duty_head='Central Tax', rate='9')
        self.sim.add_ledger('Input SGST 9%', 'Duties & Taxes', tax_type='GST', duty_head='State Tax', rate='9')
        self.sim.add_ledger('Steel Suppliers', 'Sundry Creditors', gstin=self.gstin_local,
                            state='Telangana')

    # ------------------------------------------------------------------ helpers
    def _pull(self):
        self.conn.action_pull_now()

    def _map_core(self):
        Map = self.env['tally.mapping']
        rev = self.company_data['default_account_revenue']
        exp = self.company_data['default_account_expense']
        Map._find(self.conn, 'ledger', tally_name='Sales GST 18%').write(
            {'account_id': rev.id, 'state': 'confirmed'})
        Map._find(self.conn, 'ledger', tally_name='Purchase GST 18%').write(
            {'account_id': exp.id, 'state': 'confirmed'})
        Map._find(self.conn, 'tax', tally_name='Input CGST 9%').write(
            {'tax_id': self.cgst_p.id, 'state': 'confirmed'})
        Map._find(self.conn, 'tax', tally_name='Input SGST 9%').write(
            {'tax_id': self.sgst_p.id, 'state': 'confirmed'})

    def _qty(self, product, location, lot=None):
        return self.Quant._get_available_quantity(product, location, lot_id=lot, strict=True,
                                                  allow_negative=True)

    def _sale(self, number, batches, vtype='Sales', party='M/s. Sri Lakshmi Traders',
              item='Paint Drum 20L', rate=1000.0):
        qty = sum(b['qty'] for b in batches)
        amount = qty * rate
        tax = round(amount * 0.09, 2)
        sign = 1 if vtype == 'Sales' else -1
        for b in batches:
            b['amount'] = sign * b['qty'] * rate
        return self.sim.add_voucher(vtype, date(2026, 10, 12), number, party=party, ledger_entries=[
            {'ledger': party, 'amount': -sign * (amount + 2 * tax)},
            {'ledger': 'Output CGST 9%', 'amount': sign * tax},
            {'ledger': 'Output SGST 9%', 'amount': sign * tax}], inventory_entries=[
            {'item': item, 'qty': qty, 'rate': rate, 'unit': 'Nos', 'amount': sign * amount,
             'ledger': 'Sales GST 18%', 'batches': batches}])

    def _stage(self, number):
        return self.env['tally.voucher'].search([('number', '=', number)])

    # ------------------------------------------------------------------ tests
    def test_01_godowns_pulled_and_mapped(self):
        self.sim.add_godown('Hyderabad WH')
        self.sim.add_godown('Vizag Depot')
        self._pull()
        Map = self.env['tally.mapping']
        self.assertEqual(Map._find(self.conn, 'godown', tally_name='Main Location').location_id,
                         self.stock)
        self.assertEqual(Map._find(self.conn, 'godown', tally_name='Hyderabad WH').location_id,
                         self.shelf)
        vizag = Map._find(self.conn, 'godown', tally_name='Vizag Depot').location_id
        self.assertEqual(vizag.usage, 'internal')
        self.assertEqual(vizag.warehouse_id, self.wh)

    def test_02_sale_import_creates_delivery_with_godowns_and_lots(self):
        self.sim.add_godown('Hyderabad WH')
        self._pull()
        self._map_core()
        self._sale('GS/STK1', [{'godown': 'Main Location', 'batch': 'B-101', 'qty': 3},
                               {'godown': 'Hyderabad WH', 'batch': 'B-102', 'qty': 2}])
        self._pull()
        st = self._stage('GS/STK1')
        self.assertEqual(st.state, 'done', st.message)
        self.assertFalse(st.stock_message)
        pick = st.picking_ids
        self.assertEqual(pick.state, 'done')
        self.assertEqual(pick.picking_type_id, self.wh.out_type_id)
        self.assertEqual(st.move_id.tally_picking_ids, pick)
        mls = pick.move_line_ids.sorted('quantity')
        self.assertEqual(mls.mapped('lot_id.name'), ['B-102', 'B-101'])
        self.assertEqual(mls.mapped('location_id'), self.shelf | self.stock)
        lot101 = mls.filtered(lambda l: l.lot_id.name == 'B-101').lot_id
        self.assertEqual(self._qty(self.paint, self.stock, lot101), -3.0)

    def test_03_purchase_import_creates_receipt_and_lots(self):
        self._pull()
        self._map_core()
        self.sim.add_voucher('Purchase', date(2026, 10, 3), 'PB/9', party='Steel Suppliers',
                             ledger_entries=[
                                 {'ledger': 'Steel Suppliers', 'amount': 11800.0},
                                 {'ledger': 'Input CGST 9%', 'amount': -900.0},
                                 {'ledger': 'Input SGST 9%', 'amount': -900.0}],
                             inventory_entries=[{
                                 'item': 'Paint Drum 20L', 'qty': 10, 'rate': 1000.0, 'unit': 'Nos',
                                 'amount': -10000.0, 'ledger': 'Purchase GST 18%',
                                 'batches': [{'godown': 'Main Location', 'batch': 'P-77',
                                              'qty': 10, 'amount': -10000.0,
                                              'expiry': date(2028, 3, 31)}]}])
        self._pull()
        st = self._stage('PB/9')
        self.assertEqual(st.state, 'done', st.message)
        self.assertEqual(st.move_id.move_type, 'in_invoice')
        self.assertEqual(st.picking_ids.picking_type_id, self.wh.in_type_id)
        lot = self.env['stock.lot'].search([('name', '=', 'P-77'), ('product_id', '=', self.paint.id)])
        self.assertTrue(lot)
        self.assertEqual(self._qty(self.paint, self.stock, lot), 10.0)
        if 'expiration_date' in lot._fields:
            self.assertEqual(lot.expiration_date.date(), date(2028, 3, 31))

    def test_04_credit_note_returns_stock(self):
        self._pull()
        self._map_core()
        self._sale('CN/1', [{'godown': 'Main Location', 'batch': 'B-200', 'qty': 2}],
                   vtype='Credit Note')
        self._pull()
        st = self._stage('CN/1')
        self.assertEqual(st.state, 'done', st.message)
        self.assertEqual(st.move_id.move_type, 'out_refund')
        lot = st.picking_ids.move_line_ids.lot_id
        self.assertEqual(self._qty(self.paint, self.stock, lot), 2.0)
        self.assertEqual(st.picking_ids.location_id,
                         self.env.ref('stock.stock_location_customers'))

    def test_05_altered_voucher_reverses_and_rebuilds_stock(self):
        self.conn.conflict_policy = 'tally'
        self._pull()
        self._map_core()
        v = self._sale('GS/ALT', [{'godown': 'Main Location', 'batch': 'B-1', 'qty': 4}])
        self._pull()
        lot = self._stage('GS/ALT').picking_ids.move_line_ids.lot_id
        self.assertEqual(self._qty(self.paint, self.stock, lot), -4.0)
        nv = self._sale('TMP', [{'godown': 'Main Location', 'batch': 'B-1', 'qty': 1}])
        self.sim.delete_voucher(nv['guid'])
        self.sim.alter_voucher(v['guid'], ledger_entries=nv['ledger_entries'],
                               inventory_entries=nv['inventory_entries'])
        self._pull()
        st = self._stage('GS/ALT')
        self.assertEqual(st.state, 'done', st.message)
        self.assertEqual(self._qty(self.paint, self.stock, lot), -1.0)
        self.assertEqual(len(st.picking_ids), 3, 'original + reversal + new delivery')

    def test_06_deleted_voucher_reverses_stock(self):
        self.conn.conflict_policy = 'tally'
        self._pull()
        self._map_core()
        v = self._sale('GS/DEL', [{'godown': 'Main Location', 'batch': 'B-9', 'qty': 2}])
        self._pull()
        lot = self._stage('GS/DEL').picking_ids.move_line_ids.lot_id
        self.sim.delete_voucher(v['guid'])
        self.conn.action_detect_deletions()
        st = self._stage('GS/DEL')
        self.assertEqual(st.state, 'deleted')
        self.assertEqual(st.move_id.state, 'cancel')
        self.assertEqual(self._qty(self.paint, self.stock, lot), 0.0)

    def test_07_tracked_item_without_batch_left_for_review(self):
        self._pull()
        self._map_core()
        self.sim.company.items['Paint Drum 20L']['batchwise'] = False
        self._sale('GS/NOB', [{'godown': 'Main Location', 'batch': '', 'qty': 1}])
        self._pull()
        st = self._stage('GS/NOB')
        self.assertEqual(st.state, 'done', st.message)
        self.assertNotEqual(st.picking_ids.state, 'done')
        self.assertIn('no batch', st.stock_message)

    def test_08_untracked_without_batches_uses_main_location(self):
        self._pull()
        self._map_core()
        self.sim.add_voucher('Sales', date(2026, 10, 12), 'GS/W1', party='M/s. Sri Lakshmi Traders',
                             ledger_entries=[
                                 {'ledger': 'M/s. Sri Lakshmi Traders', 'amount': -1180.0},
                                 {'ledger': 'Output CGST 9%', 'amount': 90.0},
                                 {'ledger': 'Output SGST 9%', 'amount': 90.0}],
                             inventory_entries=[{'item': 'Steel Bracket', 'qty': 4, 'rate': 250.0,
                                                 'unit': 'Nos', 'amount': 1000.0,
                                                 'ledger': 'Sales GST 18%', 'batches': []}])
        self._pull()
        st = self._stage('GS/W1')
        self.assertEqual(st.state, 'done', st.message)
        self.assertEqual(st.picking_ids.state, 'done')
        self.assertEqual(st.picking_ids.move_line_ids.location_id, self.stock)
        self.assertEqual(self._qty(self.widget, self.stock), -4.0)

    def test_09_push_invoice_without_delivery_main_location(self):
        inv = self.make_invoice()
        self.run_queue()
        self.assertEqual(inv.tally_state, 'synced', self.dump())
        v = [x for x in self.sim.company.vouchers.values() if x['number'] == inv.name][0]
        self.assertEqual(v['inventory_entries'][0]['batches'][0]['godown'], 'Main Location')

    def test_10_push_tracked_item_is_batchwise_in_tally(self):
        self.conn.voucher_mode = 'item'
        inv = self.make_invoice(product=self.paint, qty=1, price=1000.0)
        self.run_queue()
        self.assertTrue(self.sim.company.items['Paint Drum 20L']['batchwise'])
        # without a delivery there is no lot -> Tally rejects, error doctor explains
        self.assertEqual(inv.tally_state, 'error')
        req = self.requests('push_move', 'failed')
        self.assertEqual(req.error_code, 'batch_required', req.error)

    def test_11_push_sale_delivery_batches_from_two_godowns(self):
        if 'sale_line_ids' not in self.env['account.move.line']._fields:
            raise unittest.SkipTest('sale_stock not installed')
        Lot = self.env['stock.lot']
        l1 = Lot.create({'name': 'L-HYD-1', 'product_id': self.paint.id,
                         'company_id': self.env.company.id})
        l2 = Lot.create({'name': 'L-HYD-2', 'product_id': self.paint.id,
                         'company_id': self.env.company.id})
        self.Quant._update_available_quantity(self.paint, self.stock, 3, lot_id=l1)
        self.Quant._update_available_quantity(self.paint, self.shelf, 2, lot_id=l2)
        so = self.env['sale.order'].create({
            'partner_id': self.cust.id,
            'order_line': [(0, 0, {'product_id': self.paint.id, 'product_uom_qty': 5,
                                   'price_unit': 1000.0, 'tax_ids': [(6, 0, self.gst18_s.ids)]})]})
        so.action_confirm()
        pick = so.picking_ids
        pick.action_assign()
        self.assertEqual(sorted(pick.move_line_ids.mapped('lot_id.name')), ['L-HYD-1', 'L-HYD-2'])
        pick.move_ids.picked = True
        pick._action_done()
        self.assertEqual(pick.state, 'done')
        inv = so._create_invoices()
        inv.invoice_date = date(2026, 10, 25)
        inv.action_post()
        self.run_queue()
        self.assertEqual(inv.tally_state, 'synced', self.dump())
        v = [x for x in self.sim.company.vouchers.values() if x['number'] == inv.name][0]
        batches = {(b['godown'], b['batch']): (b['qty'], b['amount'])
                   for b in v['inventory_entries'][0]['batches']}
        self.assertEqual(batches, {('Main Location', 'L-HYD-1'): (3.0, 3000.0),
                                   ('Hyderabad WH', 'L-HYD-2'): (2.0, 2000.0)})
        self.assertIn('Hyderabad WH', self.sim.company.godowns, 'godown master created first')

    def test_12_auto_heal_missing_godown(self):
        if 'sale_line_ids' not in self.env['account.move.line']._fields:
            raise unittest.SkipTest('sale_stock not installed')
        self.Quant._update_available_quantity(self.widget, self.shelf, 10)
        so = self.env['sale.order'].create({
            'partner_id': self.cust.id,
            'order_line': [(0, 0, {'product_id': self.widget.id, 'product_uom_qty': 2,
                                   'price_unit': 250.0, 'tax_ids': [(6, 0, self.gst18_s.ids)]})]})
        so.action_confirm()
        so.picking_ids.action_assign()
        so.picking_ids.move_ids.picked = True
        so.picking_ids._action_done()
        inv = so._create_invoices()
        inv.action_post()
        self.run_queue()
        self.assertEqual(inv.tally_state, 'synced', self.dump())
        del self.sim.company.godowns['Hyderabad WH']
        so2 = so.copy()
        so2.action_confirm()
        so2.picking_ids.action_assign()
        so2.picking_ids.move_ids.picked = True
        so2.picking_ids._action_done()
        inv2 = so2._create_invoices()
        inv2.action_post()
        self.run_queue()
        self.assertEqual(inv2.tally_state, 'synced', self.dump())
        self.assertIn('Hyderabad WH', self.sim.company.godowns)
