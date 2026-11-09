# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
from odoo.tests import tagged

from odoo.addons.zivoit_mcp_connector.services import core_tools, finance_tools as F
from odoo.addons.zivoit_mcp_connector.services import utils
from odoo.addons.zivoit_mcp_connector.services.context import ToolError

from .common import ZivoMcpAccountingCommon


@tagged('post_install', '-at_install', 'zivo_mcp')
class TestFinanceTools(ZivoMcpAccountingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.inv1 = cls._post_invoice('out_invoice', 1000.0, '2026-02-10')
        cls.inv2 = cls._post_invoice('out_invoice', 500.0, '2026-03-05', cls.partner_b)
        cls.refund = cls._post_invoice('out_refund', 100.0, '2026-03-20')
        cls.bill = cls._post_invoice('in_invoice', 300.0, '2026-02-15', cls.partner_b)
        cls.prev = cls._post_invoice('out_invoice', 400.0, '2025-02-10')

    def _company_args(self, **kw):
        return dict(company_ids=[str(self.env.company.id)], **kw)

    def test_profit_and_loss(self):
        res = F.profit_and_loss(self._ctx(), self._company_args(date_from='2026-01-01', date_to='2026-12-31',
                                                                compare='previous_year', granularity='month'))
        ent = res['entities'][0]
        self.assertAlmostEqual(ent['sections']['revenue']['total'], 1400.0)
        self.assertAlmostEqual(ent['comparison']['revenue'], 400.0)
        self.assertAlmostEqual(ent['variance']['revenue']['amount'], 1000.0)
        expenses = sum(ent['sections'][k]['total'] for k in ('cost_of_revenue', 'operating_expenses', 'depreciation', 'other_expenses'))
        self.assertAlmostEqual(ent['net_profit'], 1400.0 - expenses + ent['sections']['other_income']['total'])
        months = {t['period']: t['revenue'] for t in ent['trend']}
        self.assertAlmostEqual(months['2026-02'], 1000.0)
        self.assertAlmostEqual(months['2026-03'], 400.0)

    def test_balance_sheet_balances(self):
        res = F.balance_sheet(self._ctx(), self._company_args(date='2026-12-31'))
        ent = res['entities'][0]
        self.assertTrue(ent['balanced'], ent)
        self.assertAlmostEqual(ent['assets']['total'], ent['total_liabilities_and_equity'])

    def test_trial_balance(self):
        ent = F.trial_balance(self._ctx(), self._company_args(date_from='2026-01-01', date_to='2026-12-31'))['entities'][0]
        self.assertAlmostEqual(ent['totals']['debit'], ent['totals']['credit'])
        self.assertTrue(ent['accounts'])

    def test_aged_receivables_and_payables(self):
        ar = F.aged_receivables(self._ctx(), self._company_args(date='2026-12-31', include_documents=True))['entities'][0]
        self.assertAlmostEqual(ar['totals']['total'], 1000.0 + 500.0 - 100.0 + 400.0)
        self.assertTrue(ar['documents'])
        ap = F.aged_payables(self._ctx(), self._company_args(date='2026-12-31', min_days_overdue=30))['entities'][0]
        self.assertAlmostEqual(ap['totals']['total'], 300.0)
        self.assertTrue(all(d['days_overdue'] >= 30 for d in ap['documents']))

    def test_sales_and_purchase_analysis(self):
        sales = F.sales_analysis(self._ctx(), self._company_args(source='invoices', date_from='2026-01-01',
                                                                 date_to='2026-12-31', group_by='customer',
                                                                 compare='previous_year'))['entities'][0]
        self.assertAlmostEqual(sales['total'], 1400.0)
        self.assertAlmostEqual(sales['previous_total'], 400.0)
        self.assertAlmostEqual(sum(r['share_pct'] for r in sales['rows']), 100.0, places=0)
        bills = F.purchase_analysis(self._ctx(), self._company_args(source='bills', date_from='2026-01-01',
                                                                    date_to='2026-12-31'))['entities'][0]
        self.assertAlmostEqual(bills['total'], 300.0)

    def test_kpi_snapshot(self):
        kpis = F.kpi_snapshot(self._ctx(), self._company_args(date='2026-12-31'))['entities'][0]['kpis']
        self.assertAlmostEqual(kpis['receivables_open'], 1800.0)
        self.assertAlmostEqual(kpis['payables_open'], 300.0)

    def test_company_scope(self):
        other = self.env['res.company'].create({'name': 'Not In Scope'})
        with self.assertRaises(ToolError):
            self._ctx().resolve_companies([str(other.id)])
        self.assertEqual(self._ctx().resolve_companies(None), self.env.company)

    def test_core_search_and_security(self):
        ctx = self._ctx()
        res = core_tools.search(ctx, {'model_name': 'account.move', 'domain': '[["move_type", "=", "out_invoice"]]',
                                      'fields': ['name', 'amount_total']})
        self.assertEqual(res['total_count'], 3)
        with self.assertRaises(ToolError):
            core_tools.search(ctx, {'model_name': 'res.users.apikeys', 'domain': '[]'})
        with self.assertRaises(ToolError):
            core_tools.search(ctx, {'model_name': 'ir.config_parameter', 'domain': '[]'})
        # sensitive fields can neither be read nor used in a domain
        self.connector.restrict_models = False
        with self.assertRaises(ToolError):
            core_tools.search(ctx, {'model_name': 'res.partner', 'domain': '[]', 'fields': ['signup_token']})
        fields_list = core_tools.get_fields(ctx, {'model_name': 'res.partner'})['fields']
        self.assertNotIn('image_1920', fields_list)
        group = core_tools.read_group(ctx, {'model_name': 'account.move', 'domain': '[["move_type","=","out_invoice"]]',
                                            'groupby': ['partner_id'], 'aggregates': ['amount_untaxed:sum']})
        self.assertEqual(sum(g['amount_untaxed:sum'] for g in group['groups']), 1900.0)

    def test_write_requires_mode_and_permission(self):
        ctx = self._ctx()
        with self.assertRaises(ToolError):
            core_tools.create_records(ctx, {'model_name': 'res.partner', 'values': [{'name': 'AI Partner'}]})
        self.connector.access_mode = 'read_write'
        with self.assertRaises(ToolError):  # model not allowed for create yet
            core_tools.create_records(self._ctx(), {'model_name': 'res.partner', 'values': [{'name': 'AI Partner'}]})
        line = self.connector.model_access_ids.filtered(lambda l: l.model_name == 'res.partner')
        line.perm_create = line.perm_write = True
        res = core_tools.create_records(self._ctx(), {'model_name': 'res.partner', 'values': [{'name': 'AI Partner'}]})
        partner = self.env['res.partner'].browse(res['created'][0]['id'])
        self.assertEqual(partner.name, 'AI Partner')
        core_tools.update_records(self._ctx(), {'updates': [{'model_name': 'res.partner', 'ids': [partner.id],
                                                             'values': {'phone': '+1 555 0100'}}]})
        self.assertEqual(partner.phone, '+1 555 0100')
        with self.assertRaises(ToolError):  # no unrestricted mass update
            core_tools.update_records(self._ctx(), {'updates': [{'model_name': 'res.partner', 'domain': '[]',
                                                                 'values': {'phone': 'x'}}]})

    def test_parse_domain(self):
        self.assertEqual(utils.parse_domain("[('a', '=', 1)]"), [('a', '=', 1)])
        self.assertEqual(utils.parse_domain('[["a", "in", [1, 2]], "|"]'), [('a', 'in', [1, 2]), '|'])
        self.assertEqual(utils.parse_domain(''), [])
        with self.assertRaises(ValueError):
            utils.parse_domain("__import__('os')")
        with self.assertRaises(ValueError):
            utils.parse_domain('[["a", "="]]')
