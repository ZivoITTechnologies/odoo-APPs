# -*- coding: utf-8 -*-
"""Cost centres to analytic accounts, and deletion detection (census)."""
from datetime import date

from odoo.tests import tagged

from .common import TallyCommon


@tagged('post_install', '-at_install', 'zivoit_tally')
class TestTallyCostCentres(TallyCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.plan = cls.env['account.analytic.plan'].create({'name': 'Projects'})
        cls.plan_region = cls.env['account.analytic.plan'].create({'name': 'Region'})
        cls.aa_hyd = cls.env['account.analytic.account'].create(
            {'name': 'Hyderabad Metro', 'plan_id': cls.plan.id})
        cls.aa_blr = cls.env['account.analytic.account'].create(
            {'name': 'Bengaluru Airport', 'plan_id': cls.plan.id})
        cls.aa_south = cls.env['account.analytic.account'].create(
            {'name': 'South', 'plan_id': cls.plan_region.id})

    def setUp(self):
        super().setUp()
        self.conn.primary_plan_id = self.plan

    def _voucher(self, number):
        return [v for v in self.sim.company.vouchers.values() if v['number'] == number][0]

    def test_01_push_item_invoice_with_distribution(self):
        inv = self.make_invoice(post=False)
        inv.invoice_line_ids.analytic_distribution = {
            str(self.aa_hyd.id): 60, str(self.aa_blr.id): 40, str(self.aa_south.id): 100}
        inv.action_post()
        self.run_queue()
        self.assertEqual(inv.tally_state, 'synced', self.dump())
        comp = self.sim.company
        self.assertEqual(comp.costcentres['Hyderabad Metro']['category'], 'Primary Cost Category')
        self.assertEqual(comp.costcentres['South']['category'], 'Region')
        self.assertIn('Region', comp.costcats)
        ie = self._voucher(inv.name)['inventory_entries'][0]
        cc = {(a['category'], a['name']): a['amount'] for a in ie['cost_centres']}
        self.assertEqual(cc[('Primary Cost Category', 'Hyderabad Metro')], 600.0)
        self.assertEqual(cc[('Primary Cost Category', 'Bengaluru Airport')], 400.0)
        self.assertEqual(cc[('Region', 'South')], 1000.0)

    def test_02_push_accounting_mode_ledger_allocations_and_ledger_flag(self):
        self.conn.voucher_mode = 'accounting'
        inv = self.make_invoice(product=self.service, qty=1, price=1000, post=False)
        inv.invoice_line_ids.analytic_distribution = {str(self.aa_hyd.id): 100}
        inv.action_post()
        self.run_queue()
        self.assertEqual(inv.tally_state, 'synced', self.dump())
        v = self._voucher(inv.name)
        income = [le for le in v['ledger_entries'] if le.get('cost_centres')][0]
        self.assertEqual(income['cost_centres'][0]['name'], 'Hyderabad Metro')
        self.assertEqual(income['cost_centres'][0]['amount'], 1000.0)
        self.assertTrue(self.sim.company.ledgers[income['ledger']]['cost_centres_on'])

    def test_03_pull_cost_centres_match_and_create(self):
        self.sim.add_cost_centre('Hyderabad Metro')
        self.sim.add_cost_centre('Chennai Port')
        self.sim.add_cost_centre('West', category='Zones')
        self.conn.action_pull_now()
        Map = self.env['tally.mapping']
        m = Map._find(self.conn, 'costcentre', tally_name='Hyderabad Metro')
        self.assertEqual(m.analytic_account_id, self.aa_hyd)
        self.assertEqual(m.state, 'confirmed')
        new = Map._find(self.conn, 'costcentre', tally_name='Chennai Port')
        self.assertEqual(new.analytic_account_id.plan_id, self.plan)
        west = Map._find(self.conn, 'costcentre', tally_name='West')
        self.assertEqual(west.analytic_account_id.plan_id.name, 'Zones')

    def test_04_pull_voucher_sets_analytic_distribution(self):
        self.seed_tally_masters()
        self.sim.add_ledger('Site Expenses', 'Indirect Expenses')
        self.sim.add_cost_centre('Hyderabad Metro')
        self.sim.add_cost_centre('Bengaluru Airport')
        self.conn.action_pull_now()
        Map = self.env['tally.mapping']
        Map._find(self.conn, 'ledger', tally_name='Site Expenses').write({
            'account_id': self.company_data['default_account_expense'].id, 'state': 'confirmed'})
        Map._find(self.conn, 'bank', tally_name='HDFC Bank').write({
            'journal_id': self.bank_journal.id, 'state': 'confirmed'})
        self.sim.add_voucher('Payment', date(2026, 10, 6), 'PY/CC1', ledger_entries=[
            {'ledger': 'Site Expenses', 'amount': -10000.0, 'cost_centres': [
                {'category': 'Primary Cost Category', 'name': 'Hyderabad Metro', 'amount': -7500.0},
                {'category': 'Primary Cost Category', 'name': 'Bengaluru Airport',
                 'amount': -2500.0}]},
            {'ledger': 'HDFC Bank', 'amount': 10000.0}])
        self.conn.action_pull_now()
        stage = self.env['tally.voucher'].search([('number', '=', 'PY/CC1')])
        self.assertEqual(stage.state, 'done', stage.message)
        line = stage.move_id.line_ids.filtered(lambda l: l.debit)
        self.assertEqual(line.analytic_distribution,
                         {str(self.aa_hyd.id): 75.0, str(self.aa_blr.id): 25.0})

    def test_05_unmapped_cost_centre_is_reported(self):
        self.seed_tally_masters()
        self.sim.add_ledger('Site Expenses', 'Indirect Expenses')
        self.conn.sync_cost_centres = 'push'   # no auto-creation from Tally
        self.sim.add_cost_centre('Unknown Site')
        self.conn.action_pull_now()
        Map = self.env['tally.mapping']
        Map._find(self.conn, 'ledger', tally_name='Site Expenses').write({
            'account_id': self.company_data['default_account_expense'].id, 'state': 'confirmed'})
        Map._find(self.conn, 'bank', tally_name='HDFC Bank').write({
            'journal_id': self.bank_journal.id, 'state': 'confirmed'})
        self.sim.add_voucher('Payment', date(2026, 10, 6), 'PY/CC2', ledger_entries=[
            {'ledger': 'Site Expenses', 'amount': -500.0, 'cost_centres': [
                {'category': 'Primary Cost Category', 'name': 'Unknown Site', 'amount': -500.0}]},
            {'ledger': 'HDFC Bank', 'amount': 500.0}])
        self.conn.action_pull_now()
        stage = self.env['tally.voucher'].search([('number', '=', 'PY/CC2')])
        self.assertEqual(stage.state, 'error')
        self.assertIn('Unknown Site', stage.message)

    def test_06_auto_heal_deleted_cost_centre(self):
        inv = self.make_invoice(post=False)
        inv.invoice_line_ids.analytic_distribution = {str(self.aa_hyd.id): 100}
        inv.action_post()
        self.run_queue()
        del self.sim.company.costcentres['Hyderabad Metro']
        inv2 = self.make_invoice(qty=1, post=False)
        inv2.invoice_line_ids.analytic_distribution = {str(self.aa_hyd.id): 100}
        inv2.action_post()
        self.run_queue()
        self.assertEqual(inv2.tally_state, 'synced', self.dump())
        self.assertIn('Hyderabad Metro', self.sim.company.costcentres)


@tagged('post_install', '-at_install', 'zivoit_tally')
class TestTallyDeletions(TallyCommon):

    def setUp(self):
        super().setUp()
        self.seed_tally_masters()
        self.conn.action_pull_now()
        rev = self.company_data['default_account_revenue']
        Map = self.env['tally.mapping']
        Map._find(self.conn, 'ledger', tally_name='Sales GST 18%').write(
            {'account_id': rev.id, 'state': 'confirmed'})

    def _tally_invoice(self, number):
        return self.sim.add_voucher('Sales', date(2026, 10, 12), number,
                                    party='M/s. Sri Lakshmi Traders', ledger_entries=[
            {'ledger': 'M/s. Sri Lakshmi Traders', 'amount': -1180.0},
            {'ledger': 'Output CGST 9%', 'amount': 90.0},
            {'ledger': 'Output SGST 9%', 'amount': 90.0}], inventory_entries=[
            {'item': 'Steel Bracket', 'qty': 4, 'rate': 250.0, 'unit': 'Nos', 'amount': 1000.0,
             'ledger': 'Sales GST 18%'}])

    def _stage(self, number):
        return self.env['tally.voucher'].search([('number', '=', number)])

    def test_01_tally_voucher_deleted_manual_policy(self):
        v = self._tally_invoice('DEL/1')
        self.conn.action_pull_now()
        stage = self._stage('DEL/1')
        self.assertEqual(stage.state, 'done', stage.message)
        self.sim.delete_voucher(v['guid'])
        self.conn.action_detect_deletions()
        self.assertEqual(stage.state, 'deleted')
        self.assertEqual(stage.move_id.state, 'posted', 'manual policy must not touch Odoo')
        stage.action_apply_deletion()
        self.assertEqual(stage.move_id.state, 'cancel')
        census = self.requests('census_vouchers')
        self.assertEqual(census.state, 'done')
        self.assertEqual(census.altered, 1)

    def test_02_tally_voucher_deleted_tally_wins(self):
        self.conn.conflict_policy = 'tally'
        v = self._tally_invoice('DEL/2')
        self.conn.action_pull_now()
        self.sim.delete_voucher(v['guid'])
        self.conn.action_detect_deletions()
        stage = self._stage('DEL/2')
        self.assertEqual(stage.state, 'deleted')
        self.assertEqual(stage.move_id.state, 'cancel')
        # no cancellation is echoed back to Tally
        self.assertFalse(self.requests('cancel_move'))

    def test_03_keep_in_odoo(self):
        v = self._tally_invoice('DEL/3')
        self.conn.action_pull_now()
        self.sim.delete_voucher(v['guid'])
        self.conn.action_detect_deletions()
        stage = self._stage('DEL/3')
        stage.action_keep_after_deletion()
        self.assertEqual(stage.state, 'done')
        self.assertEqual(stage.move_id.state, 'posted')

    def test_04_odoo_invoice_deleted_in_tally_odoo_wins_repushes(self):
        self.conn.conflict_policy = 'odoo'
        inv = self.make_invoice()
        self.run_queue()
        self.sim.delete_voucher(inv.tally_guid)
        self.conn.action_detect_deletions()
        self.assertEqual(inv.tally_state, 'synced', self.dump())
        self.assertTrue([x for x in self.sim.company.vouchers.values() if x['number'] == inv.name])

    def test_05_odoo_invoice_deleted_in_tally_manual_then_repush(self):
        inv = self.make_invoice()
        self.run_queue()
        self.sim.delete_voucher(inv.tally_guid)
        self.conn.action_detect_deletions()
        self.assertEqual(inv.tally_state, 'deleted')
        self.assertIn('Deleted in Tally', inv.tally_error)
        inv.action_tally_push()
        self.assertEqual(inv.tally_state, 'synced', self.dump())

    def test_06_mass_deletion_guard(self):
        vs = [self._tally_invoice('G/%s' % i) for i in range(6)]
        self.conn.action_pull_now()
        for v in vs[:4]:
            self.sim.delete_voucher(v['guid'])
        self.conn.action_detect_deletions()
        census = self.requests('census_vouchers')
        self.assertEqual(census.state, 'failed')
        self.assertIn('Mass-deletion guard', census.error)
        self.assertEqual(census.attempts, 1, 'guard trips must not be retried automatically')
        self.assertFalse(self.env['tally.voucher'].search([('state', '=', 'deleted')]))

    def test_07_ledger_deleted_in_tally(self):
        Map = self.env['tally.mapping']
        m = Map._find(self.conn, 'party', tally_name='Deccan Pumps & Valves')
        self.assertTrue(m.partner_id)
        self.sim.delete_ledger('Deccan Pumps & Valves')
        self.conn.action_detect_deletions()
        self.assertEqual(m.state, 'deleted')
        self.assertTrue(m.partner_id.active, 'Odoo contacts are never deleted')

    def test_08_ledger_deleted_odoo_wins_recreated(self):
        self.conn.conflict_policy = 'odoo'
        m = self.env['tally.mapping']._find(self.conn, 'party', tally_name='Deccan Pumps & Valves')
        self.sim.delete_ledger('Deccan Pumps & Valves')
        self.conn.action_detect_deletions()
        self.run_queue()
        self.assertIn('Deccan Pumps & Valves', self.sim.company.ledgers)
        self.assertTrue(m.synced_to_tally)

    def test_09_census_respects_interval(self):
        self.conn._queue_census(only_if_due=True)
        n = len(self.requests('census_vouchers'))
        self.conn._queue_census(only_if_due=True)
        self.assertEqual(len(self.requests('census_vouchers')), n)
