# -*- coding: utf-8 -*-
from datetime import date

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .tally_simulator import TallySimulator, serve

GSTIN_CHARS = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'


def make_gstin(state_code, pan, entity='1'):
    """Build a checksum-valid GSTIN."""
    base = '%s%s%sZ' % (state_code, pan, entity)
    total = 0
    for i, c in enumerate(base):
        v = GSTIN_CHARS.index(c) * (1 if i % 2 == 0 else 2)
        total += v // 36 + v % 36
    return base + GSTIN_CHARS[(36 - total % 36) % 36]


class TallyCommon(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        cls.env.user.group_ids |= cls.env.ref('zivoit_tally_connector.group_tally_manager')
        cls.india = cls.env.ref('base.in')
        cls.telangana = cls.env['res.country.state'].search(
            [('country_id', '=', cls.india.id), ('name', '=', 'Telangana')], limit=1)
        cls.karnataka = cls.env['res.country.state'].search(
            [('country_id', '=', cls.india.id), ('name', '=', 'Karnataka')], limit=1)
        company.write({'country_id': cls.india.id, 'state_id': cls.telangana.id})

        tax_acc_s = cls.company_data['default_account_tax_sale'][:1] or \
            cls.company_data['default_account_revenue']
        tax_acc_p = cls.company_data['default_account_tax_purchase'][:1] or \
            cls.company_data['default_account_expense']
        Tax = cls.env['account.tax']

        def rep(acc):
            return [(0, 0, {'repartition_type': 'base'}),
                    (0, 0, {'repartition_type': 'tax', 'account_id': acc.id})]

        def child(name, amount, acc):
            return Tax.create({'name': name, 'amount': amount, 'amount_type': 'percent',
                               'type_tax_use': 'none', 'company_id': company.id,
                               'invoice_repartition_line_ids': rep(acc),
                               'refund_repartition_line_ids': rep(acc)})

        cls.cgst_s, cls.sgst_s = child('CGST 9%', 9, tax_acc_s), child('SGST 9%', 9, tax_acc_s)
        cls.cgst_p, cls.sgst_p = child('CGST 9%', 9, tax_acc_p), child('SGST 9%', 9, tax_acc_p)
        cls.gst18_s = Tax.create({'name': 'GST 18%', 'amount_type': 'group', 'type_tax_use': 'sale',
                                  'company_id': company.id,
                                  'children_tax_ids': [(6, 0, (cls.cgst_s | cls.sgst_s).ids)]})
        cls.gst18_p = Tax.create({'name': 'GST 18%', 'amount_type': 'group',
                                  'type_tax_use': 'purchase', 'company_id': company.id,
                                  'children_tax_ids': [(6, 0, (cls.cgst_p | cls.sgst_p).ids)]})
        cls.igst18_s = Tax.create({'name': 'IGST 18%', 'amount': 18, 'amount_type': 'percent',
                                   'type_tax_use': 'sale', 'company_id': company.id,
                                   'invoice_repartition_line_ids': rep(tax_acc_s),
                                   'refund_repartition_line_ids': rep(tax_acc_s)})

        cls.gstin_local = make_gstin('36', 'AAACZ1234F')
        cls.gstin_ka = make_gstin('29', 'AABCK5678L')
        cls.cust = cls.env['res.partner'].create({
            'name': 'Sri Lakshmi Traders Pvt Ltd', 'is_company': True, 'vat': cls.gstin_local,
            'state_id': cls.telangana.id, 'country_id': cls.india.id, 'street': 'Plot 12, IDA',
            'city': 'Cherlapally', 'zip': '500051', 'email': 'accounts@srilakshmi.in'})
        cls.cust_ka = cls.env['res.partner'].create({
            'name': 'Bengaluru Electricals', 'is_company': True, 'vat': cls.gstin_ka,
            'state_id': cls.karnataka.id, 'country_id': cls.india.id})
        cls.uom_unit = cls.env.ref('uom.product_uom_unit')
        cls.widget = cls.env['product.product'].create({
            'name': 'Steel Bracket', 'default_code': 'SB-100', 'type': 'consu',
            'list_price': 250.0, 'taxes_id': [(6, 0, cls.gst18_s.ids)],
            'supplier_taxes_id': [(6, 0, cls.gst18_p.ids)], 'uom_id': cls.uom_unit.id,
            'property_account_income_id': cls.company_data['default_account_revenue'].id})
        cls.service = cls.env['product.product'].create({
            'name': 'Installation Service', 'type': 'service', 'list_price': 1000.0,
            'taxes_id': [(6, 0, cls.gst18_s.ids)]})
        cls.bank_journal = cls.company_data['default_journal_bank']

    def setUp(self):
        super().setUp()
        self.sim = TallySimulator('Zivo Demo Pvt Ltd')
        self.sim.company.books_from = date(2020, 4, 1)
        self.httpd = serve(self.sim)
        self.addCleanup(self.httpd.shutdown)
        self.conn = self.env['tally.connection'].create({
            'name': 'Test Tally', 'tally_company': 'Zivo Demo Pvt Ltd', 'transport': 'direct',
            'host': '127.0.0.1', 'port': self.httpd.server_address[1], 'timeout': 10,
            'company_id': self.env.company.id, 'sale_journal_id': self.company_data[
                'default_journal_sale'].id, 'purchase_journal_id': self.company_data[
                'default_journal_purchase'].id, 'misc_journal_id': self.company_data[
                'default_journal_misc'].id, 'bank_journal_id': self.bank_journal.id,
            'pull_from_date': date(2026, 4, 1), 'push_on_post': True,
            'sync_sales': 'both', 'sync_purchases': 'both', 'sync_payments': 'both',
            'sync_journals': 'both', 'max_attempts': 3,
        })

    # ------------------------------------------------------------------
    def make_invoice(self, partner=None, product=None, qty=4, price=250.0, taxes=None,
                     move_type='out_invoice', inv_date=date(2026, 10, 15), post=True):
        taxes = self.gst18_s if taxes is None else taxes
        move = self.env['account.move'].create({
            'move_type': move_type, 'partner_id': (partner or self.cust).id,
            'invoice_date': inv_date, 'date': inv_date,
            'invoice_line_ids': [(0, 0, {'product_id': (product or self.widget).id,
                                         'quantity': qty, 'price_unit': price,
                                         'tax_ids': [(6, 0, taxes.ids)]})]})
        if post:
            move.action_post()
        return move

    def run_queue(self):
        self.env['tally.request']._process_queue(self.conn)

    def requests(self, purpose=None, state=None):
        dom = [('connection_id', '=', self.conn.id)]
        if purpose:
            dom.append(('purpose', '=', purpose))
        if state:
            dom.append(('state', '=', state))
        return self.env['tally.request'].search(dom)

    def dump(self):
        return '\n'.join('#%s %s %s %s %s' % (r.id, r.purpose, r.state, r.res_ids, (r.error or '')[:200])
                         for r in self.requests().sorted('id'))

    def seed_tally_masters(self):
        s = self.sim
        s.add_ledger('Sales GST 18%', 'Sales Accounts')
        s.add_ledger('Output CGST 9%', 'Duties & Taxes', tax_type='GST',
                     duty_head='Central Tax', rate='9')
        s.add_ledger('Output SGST 9%', 'Duties & Taxes', tax_type='GST',
                     duty_head='State Tax', rate='9')
        s.add_ledger('Round Off', 'Indirect Expenses')
        s.add_ledger('HDFC Bank', 'Bank Accounts')
        # same GSTIN as an Odoo partner but a different spelling
        s.add_ledger('M/s. Sri Lakshmi Traders', 'Sundry Debtors', gstin=self.gstin_local,
                     state='Telangana', address=['Plot 12, IDA', 'Cherlapally'])
        # unknown party -> must be created in Odoo
        s.add_ledger('Deccan Pumps & Valves', 'Sundry Debtors',
                     gstin=make_gstin('36', 'AADCD4321K'), state='Telangana',
                     address=['5-9-22 Abids', 'Opp GPO', 'Hyderabad'], email='ap@deccanpumps.in')
        s.add_item('Steel Bracket', unit='Nos', hsn='7308', gst_rate=18, price=250.0,
                   part_no='SB-100')
        s.add_item('Hex Bolt M12', unit='Nos', hsn='7318', gst_rate=18, price=12.5)
