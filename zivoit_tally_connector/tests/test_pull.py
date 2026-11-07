# -*- coding: utf-8 -*-
"""Tally → Odoo against the TallyPrime simulator."""
from datetime import date

from odoo.tests import tagged

from .common import TallyCommon, make_gstin


@tagged('post_install', '-at_install', 'zivoit_tally')
class TestTallyPull(TallyCommon):

    def setUp(self):
        super().setUp()
        self.seed_tally_masters()

    def _pull(self):
        self.conn.action_pull_now()

    def _map(self, kind, name):
        return self.env['tally.mapping']._find(self.conn, kind, tally_name=name)

    def _confirm_core_maps(self):
        rev = self.company_data['default_account_revenue']
        self._map('ledger', 'Sales GST 18%').write({'account_id': rev.id, 'state': 'confirmed'})
        self._map('ledger', 'Round Off').write({'account_id': rev.id, 'state': 'confirmed'})
        self._map('bank', 'HDFC Bank').write({'journal_id': self.bank_journal.id,
                                             'state': 'confirmed'})

    def _sale_voucher(self, number='GS/1001', qty=4, vdate=date(2026, 10, 12),
                      party='M/s. Sri Lakshmi Traders', vtype='Sales'):
        amount = 250.0 * qty
        tax = round(amount * 0.09, 2)
        return self.sim.add_voucher(vtype, vdate, number, party=party, ledger_entries=[
            {'ledger': party, 'amount': -(amount + 2 * tax),
             'bills': [{'name': number, 'type': 'New Ref', 'amount': -(amount + 2 * tax)}]},
            {'ledger': 'Output CGST 9%', 'amount': tax},
            {'ledger': 'Output SGST 9%', 'amount': tax},
        ], inventory_entries=[{'item': 'Steel Bracket', 'qty': qty, 'rate': 250.0, 'unit': 'Nos',
                               'amount': amount, 'ledger': 'Sales GST 18%'}])

    # ------------------------------------------------------------------
    def test_01_masters_ai_matching_and_creation(self):
        self._pull()
        party = self._map('party', 'M/s. Sri Lakshmi Traders')
        self.assertEqual(party.partner_id, self.cust, self.dump())
        self.assertEqual(party.state, 'confirmed')
        self.assertEqual(party.match_reason, 'GSTIN match')
        new = self._map('party', 'Deccan Pumps & Valves')
        self.assertTrue(new.partner_id, 'unknown Tally party must be created in Odoo')
        self.assertEqual(new.partner_id.state_id, self.telangana)
        self.assertEqual(new.partner_id.email, 'ap@deccanpumps.in')
        self.assertEqual(new.partner_id.street, '5-9-22 Abids')
        self.assertEqual(new.partner_id.city, 'Hyderabad')
        cgst = self._map('tax', 'Output CGST 9%')
        self.assertEqual(cgst.tax_id, self.cgst_s)
        self.assertEqual(self._map('bank', 'HDFC Bank').tally_role, 'bank')
        item = self._map('item', 'Steel Bracket')
        self.assertEqual(item.product_id, self.widget, 'part number must match SB-100')
        bolt = self._map('item', 'Hex Bolt M12')
        self.assertTrue(bolt.product_id)
        self.assertEqual(bolt.product_id.list_price, 12.5)
        self.assertEqual(bolt.product_id.taxes_id, self.gst18_s)
        self.assertEqual(self.conn.last_alter_ledger,
                         max(l['alter_id'] for l in self.sim.company.ledgers.values()))

    def test_02_incremental_pull_only_changes(self):
        self._pull()
        n_before = len(self.sim.requests)
        self._pull()
        ledger_req = self.requests('pull_ledgers').sorted('id')[-1]
        self.assertEqual(ledger_req.records_in, 0, 'nothing changed -> nothing re-read')
        self.sim.company.ledgers['Deccan Pumps & Valves']['email'] = 'finance@deccanpumps.in'
        self.sim.company.touch(self.sim.company.ledgers['Deccan Pumps & Valves'])
        self._pull()
        self.assertEqual(self.requests('pull_ledgers').sorted('id')[-1].records_in, 1)
        self.assertEqual(self._map('party', 'Deccan Pumps & Valves').partner_id.email,
                         'finance@deccanpumps.in')
        self.assertGreater(len(self.sim.requests), n_before)

    def test_03_sales_voucher_becomes_posted_invoice(self):
        self._pull()
        self._confirm_core_maps()
        self._sale_voucher()
        self._pull()
        stage = self.env['tally.voucher'].search([('number', '=', 'GS/1001')])
        self.assertEqual(stage.state, 'done', stage.message)
        move = stage.move_id
        self.assertEqual(move.move_type, 'out_invoice')
        self.assertEqual(move.state, 'posted')
        self.assertEqual(move.partner_id, self.cust)
        self.assertAlmostEqual(move.amount_total, 1180.0)
        self.assertEqual(move.invoice_line_ids.product_id, self.widget)
        self.assertEqual(move.invoice_line_ids.tax_ids, self.gst18_s)
        self.assertEqual(move.tally_state, 'imported')
        self.assertEqual(stage.import_mode, 'invoice')
        # never echoed back to Tally
        self.assertFalse(self.requests('push_move'))

    def test_04_custom_voucher_type_resolves_to_sales(self):
        self.sim.company.vchtypes['GST Sales'] = 'Sales'
        self.sim.company.vchtype_meta['GST Sales'] = self.sim.company._meta()
        self._pull()
        self._confirm_core_maps()
        self._sale_voucher(number='GS/77', vtype='GST Sales')
        self._pull()
        stage = self.env['tally.voucher'].search([('number', '=', 'GS/77')])
        self.assertEqual(stage.kind, 'sale')
        self.assertEqual(stage.state, 'done', stage.message)

    def test_05_receipt_reconciles_invoice_via_bill_allocation(self):
        self._pull()
        self._confirm_core_maps()
        self._sale_voucher()
        self._pull()
        inv = self.env['tally.voucher'].search([('number', '=', 'GS/1001')]).move_id
        self.sim.add_voucher('Receipt', date(2026, 10, 20), 'RC/9', party='M/s. Sri Lakshmi Traders',
                             ledger_entries=[
                                 {'ledger': 'HDFC Bank', 'amount': -1180.0},
                                 {'ledger': 'M/s. Sri Lakshmi Traders', 'amount': 1180.0,
                                  'bills': [{'name': 'GS/1001', 'type': 'Agst Ref',
                                             'amount': 1180.0}]}])
        self._pull()
        rc = self.env['tally.voucher'].search([('number', '=', 'RC/9')])
        self.assertEqual(rc.state, 'done', rc.message)
        self.assertEqual(rc.move_id.journal_id, self.bank_journal)
        self.assertIn(inv.payment_state, ('paid', 'in_payment'))

    def test_06_unmapped_ledger_needs_attention_then_processes(self):
        self._pull()
        self._sale_voucher()   # Sales GST 18% not yet mapped (only suggested)
        self._map('ledger', 'Sales GST 18%').write({'account_id': False, 'state': 'unmapped'})
        self._pull()
        stage = self.env['tally.voucher'].search([('number', '=', 'GS/1001')])
        # Item invoice: the sales ledger is only an allocation; invoice uses product account
        if stage.state == 'error':
            self.assertIn('Unmapped', stage.message)
            self._confirm_core_maps()
            stage.action_process()
        self.assertEqual(stage.state, 'done', stage.message)

    def test_07_journal_voucher_imported_as_entry(self):
        self.sim.add_ledger('Rent', 'Indirect Expenses')
        self._pull()
        self._confirm_core_maps()
        self._map('ledger', 'Rent').write({
            'account_id': self.company_data['default_account_expense'].id, 'state': 'confirmed'})
        self.sim.add_voucher('Payment', date(2026, 10, 5), 'PY/3', ledger_entries=[
            {'ledger': 'Rent', 'amount': -45000.0}, {'ledger': 'HDFC Bank', 'amount': 45000.0}])
        self._pull()
        stage = self.env['tally.voucher'].search([('number', '=', 'PY/3')])
        self.assertEqual(stage.state, 'done', stage.message)
        self.assertEqual(stage.import_mode, 'entry')
        self.assertAlmostEqual(sum(stage.move_id.line_ids.mapped('debit')), 45000.0)

    def test_08_conflict_manual_then_apply_tally_version(self):
        self._pull()
        self._confirm_core_maps()
        v = self._sale_voucher()
        self._pull()
        stage = self.env['tally.voucher'].search([('number', '=', 'GS/1001')])
        first_move = stage.move_id
        # accountant edits quantity in Tally after import
        self.sim.alter_voucher(v['guid'], ledger_entries=[
            {'ledger': 'M/s. Sri Lakshmi Traders', 'amount': -1770.0, 'bills': []},
            {'ledger': 'Output CGST 9%', 'amount': 135.0},
            {'ledger': 'Output SGST 9%', 'amount': 135.0}],
            inventory_entries=[{'item': 'Steel Bracket', 'qty': 6, 'rate': 250.0, 'unit': 'Nos',
                                'amount': 1500.0, 'ledger': 'Sales GST 18%'}])
        self._pull()
        self.assertEqual(stage.state, 'conflict')
        stage.action_apply_tally_version()
        self.assertEqual(stage.state, 'done', stage.message)
        self.assertEqual(first_move.state, 'cancel')
        self.assertAlmostEqual(stage.move_id.amount_total, 1770.0)

    def test_09_conflict_policy_tally_wins_auto(self):
        self.conn.conflict_policy = 'tally'
        self._pull()
        self._confirm_core_maps()
        v = self._sale_voucher()
        self._pull()
        self.sim.alter_voucher(v['guid'], cancelled=True)
        self._pull()
        stage = self.env['tally.voucher'].search([('number', '=', 'GS/1001')])
        self.assertEqual(stage.state, 'cancelled')
        self.assertEqual(stage.move_id.state, 'cancel')

    def test_10_echo_prevention(self):
        inv = self.make_invoice()
        self.run_queue()
        self.assertEqual(inv.tally_state, 'synced', inv.tally_error)
        self._pull()
        self.assertFalse(self.env['tally.voucher'].search([('number', '=', inv.name)]))

    def test_11_anomaly_guard_holds_duplicate_and_wrong_gst(self):
        self._pull()
        self._confirm_core_maps()
        # Karnataka party billed with CGST/SGST -> place-of-supply error (blocking)
        self.sim.add_ledger('Bengaluru Electricals', 'Sundry Debtors', gstin=self.gstin_ka,
                            state='Karnataka')
        self._pull()
        self._sale_voucher(number='GS/2001', party='Bengaluru Electricals')
        self._pull()
        stage = self.env['tally.voucher'].search([('number', '=', 'GS/2001')])
        self.assertEqual(stage.state, 'anomaly')
        self.assertIn('IGST expected', stage.findings)
        self.assertEqual(stage.move_id.state, 'draft')
        stage.action_process()   # accountant overrides
        self.assertEqual(stage.move_id.state, 'posted')

    def test_12_reconciliation(self):
        self._pull()
        self._confirm_core_maps()
        self._sale_voucher()
        self._pull()
        self.sim.company.ledgers['Deccan Pumps & Valves']['opening'] = -5000.0
        self.conn.action_reconcile()
        lines = self.env['tally.recon.line'].search([('connection_id', '=', self.conn.id)])
        slt = lines.filtered(lambda l: l.ledger == 'M/s. Sri Lakshmi Traders')
        self.assertEqual(slt.status, 'match')
        self.assertAlmostEqual(slt.tally_balance, 1180.0)
        deccan = lines.filtered(lambda l: l.ledger == 'Deccan Pumps & Valves')
        self.assertEqual(deccan.status, 'mismatch')
        self.assertAlmostEqual(deccan.difference, 5000.0)

    def test_13_file_import_wizard(self):
        import base64
        from ..lib import tally_xml as TX
        self.conn.transport = 'file'
        xml = self.sim.handle(TX.build_collection_request('Ledger', company='Zivo Demo Pvt Ltd'))
        wiz = self.env['tally.file.import'].create({
            'connection_id': self.conn.id, 'filename': 'masters.xml',
            'file': base64.b64encode(xml.encode())})
        wiz.action_import()
        self.assertIn('Pull ledgers', wiz.summary)
        self.assertTrue(self._map('party', 'Deccan Pumps & Valves').partner_id)

    def test_14_unknown_company_fails_gracefully(self):
        self.conn.tally_company = 'Closed Co'
        self._pull()
        failed = self.requests(state='pending') | self.requests(state='failed')
        self.assertTrue(failed)
        self.assertTrue(all('Closed Co' in (r.error or '') for r in failed))

    def test_15_migration_pull(self):
        self._pull()
        self._confirm_core_maps()
        self._sale_voucher(number='OLD/1', vdate=date(2025, 6, 1))
        wiz = self.env['tally.migration'].create({
            'connection_id': self.conn.id, 'direction': 'pull', 'date_from': date(2025, 4, 1),
            'date_to': date(2026, 3, 31), 'dry_run': False})
        wiz.action_run()
        stage = self.env['tally.voucher'].search([('number', '=', 'OLD/1')])
        self.assertEqual(stage.state, 'done', stage.message)
        self.assertIn('1 of 1', wiz.summary)

    def test_17_tax_match_prefers_regular_over_reverse_charge(self):
        Tax = self.env['account.tax']
        acc = self.company_data['default_account_tax_sale'][:1] or \
            self.company_data['default_account_revenue']
        rep = [(0, 0, {'repartition_type': 'base'}),
               (0, 0, {'repartition_type': 'tax', 'account_id': acc.id})]
        rc_c = Tax.create({'name': '9% CGST RC', 'amount': 9, 'type_tax_use': 'none', 'sequence': 1,
                           'invoice_repartition_line_ids': rep, 'refund_repartition_line_ids': rep})
        rc_s = Tax.create({'name': '9% SGST RC', 'amount': 9, 'type_tax_use': 'none', 'sequence': 1,
                           'invoice_repartition_line_ids': rep, 'refund_repartition_line_ids': rep})
        Tax.create({'name': '18% GST RC', 'amount_type': 'group', 'type_tax_use': 'sale',
                    'sequence': 1, 'children_tax_ids': [(6, 0, (rc_c | rc_s).ids)]})
        self.sim.add_ledger('Output CGST RC 9%', 'Duties & Taxes', tax_type='GST',
                            duty_head='Central Tax', rate='9')
        self._pull()
        self.assertEqual(self._map('tax', 'Output CGST 9%').tax_id, self.cgst_s)
        self.assertEqual(self._map('tax', 'Output CGST RC 9%').tax_id, rc_c)
        self.assertEqual(self._map('item', 'Hex Bolt M12').product_id.taxes_id, self.gst18_s)

    def test_18_no_false_conflict_after_odoo_activity(self):
        """Odoo-created party re-read from Tally after more postings must not conflict."""
        partner = self.env['res.partner'].create({
            'name': 'Apex Infra Projects', 'is_company': True, 'state_id': self.telangana.id,
            'country_id': self.india.id, 'vat': make_gstin('36', 'AAKCA7531R')})
        self.make_invoice(partner=partner)
        self.run_queue()
        self.make_invoice(partner=partner, qty=1)   # bumps customer_rank / write_date
        self.run_queue()
        self._pull()
        m = self.env['tally.mapping']._find(self.conn, 'party', record=partner)
        self.assertEqual(m.state, 'confirmed', m.conflict_data)
        # a real change on both sides is still a conflict
        partner.email = 'odoo@apex.in'
        led = self.sim.company.ledgers[m.tally_name]
        led['email'] = 'tally@apex.in'
        self.sim.company.touch(led)
        self._pull()
        self.assertEqual(m.state, 'conflict')
        m.action_resolve_keep_tally()
        self.assertEqual(partner.email, 'tally@apex.in')

    def test_16_same_pan_other_state_suggested(self):
        self.sim.add_ledger('Sri Lakshmi Traders (AP)', 'Sundry Debtors',
                            gstin=make_gstin('37', 'AAACZ1234F'), state='Andhra Pradesh')
        self._pull()
        m = self._map('party', 'Sri Lakshmi Traders (AP)')
        self.assertIn('Same PAN', m.match_reason or '')
