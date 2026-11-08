# -*- coding: utf-8 -*-
"""Odoo to Tally against the TallyPrime simulator (direct transport)."""
from datetime import date

from odoo.tests import tagged

from .common import TallyCommon


@tagged('post_install', '-at_install', 'zivoit_tally')
class TestTallyPush(TallyCommon):

    def test_01_test_connection(self):
        self.conn.action_test_connection()
        self.assertEqual(self.conn.state, 'connected')
        self.conn.tally_company = 'Unknown Co'
        self.conn.action_test_connection()
        self.assertEqual(self.conn.state, 'error')
        self.assertIn('not open', self.conn.last_test_message)

    def test_02_invoice_push_creates_masters_then_voucher(self):
        inv = self.make_invoice()
        self.assertEqual(inv.tally_state, 'queued')
        self.run_queue()
        self.assertEqual(inv.tally_state, 'synced', self.dump())
        comp = self.sim.company
        party = 'Sri Lakshmi Traders Pvt Ltd'
        self.assertIn(party, comp.ledgers)
        self.assertEqual(comp.ledgers[party]['gstin'], self.gstin_local)
        self.assertEqual(comp.ledgers[party]['parent'], 'Sundry Debtors')
        self.assertIn('Output CGST 9%', comp.ledgers)
        self.assertEqual(comp.ledgers['Output CGST 9%']['duty_head'], 'Central Tax')
        self.assertIn('Steel Bracket', comp.items)
        self.assertIn('Nos', comp.units)
        vouchers = [v for v in comp.vouchers.values() if v['number'] == inv.name]
        self.assertEqual(len(vouchers), 1)
        v = vouchers[0]
        self.assertEqual(v['vch_type'], 'Sales')
        self.assertEqual(v['inventory_entries'][0]['qty'], 4.0)
        party_entry = [le for le in v['ledger_entries'] if le['ledger'] == party][0]
        self.assertAlmostEqual(party_entry['amount'], -1180.0)
        self.assertEqual(party_entry['bills'][0]['type'], 'New Ref')
        self.assertEqual(comp.closing(party), -1180.0)
        # every exchange is logged and successful
        self.assertFalse(self.requests(state='failed'))
        self.assertTrue(inv.tally_master_id)

    def test_03_repost_alters_not_duplicates(self):
        inv = self.make_invoice()
        self.run_queue()
        inv.button_draft()
        self.assertEqual(inv.tally_state, 'modified')
        inv.invoice_line_ids[0].quantity = 6
        inv.action_post()
        self.run_queue()
        vouchers = [v for v in self.sim.company.vouchers.values() if v['number'] == inv.name]
        self.assertEqual(len(vouchers), 1)
        self.assertEqual(vouchers[0]['inventory_entries'][0]['qty'], 6.0)
        last = self.requests('push_move', 'done').sorted('id')[-1]
        self.assertEqual(last.altered, 1)

    def test_04_accounting_mode_service_and_rounding(self):
        self.conn.voucher_mode = 'accounting'
        inv = self.make_invoice(product=self.service, qty=1, price=999.99)
        self.run_queue()
        self.assertEqual(inv.tally_state, 'synced', inv.tally_error)
        v = [v for v in self.sim.company.vouchers.values() if v['number'] == inv.name][0]
        self.assertFalse(v['inventory_entries'])
        self.assertAlmostEqual(sum(le['amount'] for le in v['ledger_entries']), 0.0, 2)

    def test_05_vendor_bill_and_payment_with_bill_allocation(self):
        bill = self.make_invoice(move_type='in_invoice', taxes=self.gst18_p, price=100, qty=10)
        pay = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=bill.ids).create(
            {'journal_id': self.bank_journal.id, 'payment_date': date(2026, 10, 20)}
        )._create_payments()
        self.run_queue()
        self.assertEqual(bill.tally_state, 'synced', bill.tally_error)
        comp = self.sim.company
        bills = [v for v in comp.vouchers.values() if v['vch_type'] == 'Purchase']
        self.assertEqual(len(bills), 1)
        if pay.move_id:  # Odoo 19 creates the entry only when an outstanding account is set
            self.assertEqual(pay.move_id.tally_state, 'synced', pay.move_id.tally_error)
            pays = [v for v in comp.vouchers.values() if v['vch_type'] == 'Payment']
            self.assertEqual(len(pays), 1)
            agst = [b for le in pays[0]['ledger_entries'] for b in le['bills']
                    if b['type'] == 'Agst Ref']
            self.assertEqual(agst[0]['name'], bill.name)
            self.assertIn(self.bank_journal.name, comp.ledgers)

    def test_06_cancel_in_odoo_cancels_in_tally(self):
        inv = self.make_invoice()
        self.run_queue()
        inv.button_draft()
        inv.button_cancel()
        self.run_queue()
        v = [v for v in self.sim.company.vouchers.values() if v['number'] == inv.name][0]
        self.assertTrue(v['cancelled'])
        self.assertEqual(inv.tally_state, 'cancelled')

    def test_07_auto_heal_recreates_deleted_ledger(self):
        self.make_invoice()
        self.run_queue()
        # someone deletes the tax ledger in Tally; the next voucher must self-heal
        del self.sim.company.ledgers['Output SGST 9%']
        inv2 = self.make_invoice(qty=2)
        self.run_queue()
        self.assertEqual(inv2.tally_state, 'synced', inv2.tally_error)
        self.assertIn('Output SGST 9%', self.sim.company.ledgers)
        healed = self.requests('push_move').filtered('auto_healed')
        self.assertTrue(healed)
        self.assertIn('Auto-heal', healed[0].ai_hint)

    def test_08_ai_guard_holds_igst_on_intrastate(self):
        inv = self.make_invoice(taxes=self.igst18_s)  # Telangana -> Telangana with IGST
        self.assertEqual(inv.tally_state, 'held')
        self.assertIn('IGST', inv.tally_error)
        self.assertGreaterEqual(inv.tally_risk_score, 60)
        # inter-state IGST is fine
        inv2 = self.make_invoice(partner=self.cust_ka, taxes=self.igst18_s)
        self.assertEqual(inv2.tally_state, 'queued')

    def test_09_retry_with_backoff_when_tally_offline(self):
        inv = self.make_invoice()
        self.sim.fail_next = 50  # Tally is down
        self.run_queue()
        req = self.requests('push_ledger')
        self.assertTrue(req)
        self.assertEqual(req[0].state, 'pending')
        self.assertEqual(req[0].error_code, 'offline')
        self.assertGreater(req[0].next_attempt, req[0].sent_at)
        self.sim.fail_next = 0
        self.requests().write({'next_attempt': '2000-01-01 00:00:00'})
        self.run_queue()
        self.assertEqual(inv.tally_state, 'synced', inv.tally_error)

    def test_10_partner_rename_alters_ledger(self):
        self.make_invoice()
        self.run_queue()
        self.cust.name = 'Sri Lakshmi Steel Traders Pvt Ltd'
        self.run_queue()
        self.assertIn('Sri Lakshmi Steel Traders Pvt Ltd', self.sim.company.ledgers)
        self.assertNotIn('Sri Lakshmi Traders Pvt Ltd', self.sim.company.ledgers)

    def test_11_file_transport_export(self):
        self.conn.transport = 'file'
        inv = self.make_invoice()
        wiz = self.env['tally.file.export'].create({'connection_id': self.conn.id})
        wiz.action_export()
        self.assertTrue(wiz.file)
        self.assertEqual(inv.tally_state, 'synced')
        # the produced files import cleanly into Tally
        import base64
        import io
        import zipfile
        zf = zipfile.ZipFile(io.BytesIO(base64.b64decode(wiz.file)))
        for name in sorted(n for n in zf.namelist() if n.endswith('.xml')):
            out = self.sim.handle(zf.read(name))
            self.assertNotIn('LINEERROR', out, name)
        self.assertTrue([v for v in self.sim.company.vouchers.values()
                         if v['number'] == inv.name])

    def test_12_migration_push_dry_run(self):
        self.conn.push_on_post = False
        self.make_invoice()
        self.make_invoice(qty=1)
        wiz = self.env['tally.migration'].create({
            'connection_id': self.conn.id, 'direction': 'push', 'date_from': date(2026, 4, 1),
            'date_to': date(2026, 12, 31), 'dry_run': True})
        wiz.action_run()
        self.assertIn('2 posted documents', wiz.summary)
        wiz.dry_run = False
        wiz.action_run()
        self.assertEqual(len([v for v in self.sim.company.vouchers.values()
                              if v['vch_type'] == 'Sales']), 2)
