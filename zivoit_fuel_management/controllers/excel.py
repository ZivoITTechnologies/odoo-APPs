# -*- coding: utf-8 -*-
import io

import xlsxwriter

from odoo import fields, http
from odoo.exceptions import AccessError
from odoo.http import content_disposition, request

NAVY = '#0a2f5c'
YELLOW = '#ffd400'


def _formats(wb):
    return {
        'title': wb.add_format({'bold': True, 'font_size': 15, 'font_color': NAVY}),
        'sub': wb.add_format({'font_size': 10, 'font_color': '#555555'}),
        'head': wb.add_format({'bold': True, 'bg_color': NAVY, 'font_color': 'white', 'border': 1,
                               'align': 'center', 'valign': 'vcenter', 'text_wrap': True}),
        'cell': wb.add_format({'border': 1}),
        'date': wb.add_format({'border': 1, 'num_format': 'dd-mm-yyyy', 'align': 'center'}),
        'num': wb.add_format({'border': 1, 'num_format': '#,##0.00'}),
        'tot': wb.add_format({'bold': True, 'border': 1, 'bg_color': YELLOW, 'num_format': '#,##0.00'}),
        'totl': wb.add_format({'bold': True, 'border': 1, 'bg_color': YELLOW}),
        'bold': wb.add_format({'bold': True}),
        'boldnum': wb.add_format({'bold': True, 'num_format': '#,##0.00'}),
    }


def _drcr(v):
    return '%s %s' % ('{:,.2f}'.format(abs(v)), 'Dr' if v >= 0 else 'Cr')


def ledger_xlsx(env, partner, date_from, date_to):
    data = partner.pump_ledger(date_from, date_to)
    company = env.company
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {'in_memory': True})
    f = _formats(wb)
    ws = wb.add_worksheet('Ledger')
    ws.set_column(0, 0, 5)
    ws.set_column(1, 1, 12)
    ws.set_column(2, 2, 42)
    ws.set_column(3, 4, 16)
    ws.set_column(5, 7, 16)
    ws.set_column(8, 9, 22)
    ws.write(0, 0, company.pump_outlet_name or company.name, f['title'])
    ws.write(1, 0, 'Customer Ledger - %s (%s)' % (partner.name, partner.pump_code or ''), f['bold'])
    ws.write(2, 0, 'Period: %s to %s' % (data['date_from'].strftime('%d-%m-%Y'),
                                         data['date_to'].strftime('%d-%m-%Y')), f['sub'])
    heads = ['#', 'Date', 'Particulars', 'V. Type', 'V. No.', 'Debit', 'Credit', 'Balance', 'Narration', 'Cheque/UTR']
    row = 4
    for c, h in enumerate(heads):
        ws.write(row, c, h, f['head'])
    row += 1
    ws.write(row, 0, 1, f['cell'])
    ws.write(row, 1, '', f['cell'])
    ws.write(row, 2, 'Opening Balance', f['cell'])
    for c in (3, 4):
        ws.write(row, c, '', f['cell'])
    ws.write(row, 5, data['opening'] if data['opening'] > 0 else 0, f['num'])
    ws.write(row, 6, -data['opening'] if data['opening'] < 0 else 0, f['num'])
    ws.write(row, 7, _drcr(data['opening']), f['cell'])
    ws.write(row, 8, '', f['cell'])
    ws.write(row, 9, '', f['cell'])
    for i, l in enumerate(data['lines'], start=2):
        row += 1
        ws.write(row, 0, i, f['cell'])
        ws.write_datetime(row, 1, fields.Datetime.to_datetime(l['date']), f['date'])
        ws.write(row, 2, l['particulars'], f['cell'])
        ws.write(row, 3, l['vtype'], f['cell'])
        ws.write(row, 4, l['vno'], f['cell'])
        ws.write(row, 5, l['debit'], f['num'])
        ws.write(row, 6, l['credit'], f['num'])
        ws.write(row, 7, _drcr(l['balance']), f['cell'])
        ws.write(row, 8, l['narration'], f['cell'])
        ws.write(row, 9, l['cheque'], f['cell'])
    row += 1
    ws.merge_range(row, 0, row, 4, 'Total', f['totl'])
    ws.write(row, 5, data['total_debit'], f['tot'])
    ws.write(row, 6, data['total_credit'], f['tot'])
    ws.write(row, 7, _drcr(data['closing']), f['totl'])
    row += 2
    ws.write(row, 2, 'Closing Balance', f['bold'])
    ws.write(row, 3, _drcr(data['closing']), f['bold'])
    ws.write(row + 1, 2, 'Credit Limit', f['bold'])
    ws.write(row + 1, 3, data['credit_limit'], f['boldnum'])
    ws.write(row + 2, 2, 'Unbilled Slips', f['bold'])
    ws.write(row + 2, 3, data['unbilled'], f['boldnum'])
    ws.freeze_panes(5, 0)
    wb.close()
    return buf.getvalue()


def slips_xlsx(env, partner, slips, date_from, date_to):
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {'in_memory': True})
    f = _formats(wb)
    ws = wb.add_worksheet('Filling Slips')
    widths = [5, 12, 18, 12, 14, 26, 12, 12, 14, 14, 14]
    for i, w in enumerate(widths):
        ws.set_column(i, i, w)
    ws.write(0, 0, env.company.pump_outlet_name or env.company.name, f['title'])
    ws.write(1, 0, 'Filling Slips - %s' % partner.name, f['bold'])
    ws.write(2, 0, 'Period: %s to %s' % (date_from.strftime('%d-%m-%Y'), date_to.strftime('%d-%m-%Y')), f['sub'])
    heads = ['#', 'Date', 'Slip Ref', 'Book Slip', 'Vehicle', 'Product', 'Qty', 'Rate', 'Amount', 'Status', 'Bill No.']
    for c, h in enumerate(heads):
        ws.write(4, c, h, f['head'])
    row = 4
    for i, s in enumerate(slips, start=1):
        row += 1
        ws.write(row, 0, i, f['cell'])
        ws.write_datetime(row, 1, fields.Datetime.to_datetime(s.date), f['date'])
        ws.write(row, 2, s.name, f['cell'])
        ws.write(row, 3, s.slip_no or '', f['cell'])
        ws.write(row, 4, s.vehicle_no or '', f['cell'])
        ws.write(row, 5, s.product_id.name, f['cell'])
        ws.write(row, 6, s.qty, f['num'])
        ws.write(row, 7, s.rate, f['num'])
        ws.write(row, 8, s.amount, f['num'])
        ws.write(row, 9, 'Billed' if s.state == 'billed' else 'Unbilled', f['cell'])
        ws.write(row, 10, s.bill_id.name or '', f['cell'])
    row += 1
    ws.merge_range(row, 0, row, 5, 'Total', f['totl'])
    ws.write(row, 6, sum(slips.mapped('qty')), f['tot'])
    ws.write(row, 7, '', f['totl'])
    ws.write(row, 8, sum(slips.mapped('amount')), f['tot'])
    ws.freeze_panes(5, 0)
    wb.close()
    return buf.getvalue()


def xlsx_response(content, filename):
    return request.make_response(content, headers=[
        ('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
        ('Content-Disposition', content_disposition(filename)),
    ])


class PumpExcelController(http.Controller):

    @http.route('/fuel/ledger/xlsx/<int:partner_id>', type='http', auth='user')
    def admin_ledger_xlsx(self, partner_id, date_from=None, date_to=None, **kw):
        if not request.env.user.has_group('zivoit_fuel_management.group_pump_user'):
            raise AccessError(request.env._('Not allowed'))
        partner = request.env['res.partner'].browse(partner_id).exists()
        if not partner:
            raise request.not_found()
        d_from = fields.Date.to_date(date_from) if date_from else fields.Date.today().replace(day=1)
        d_to = fields.Date.to_date(date_to) if date_to else fields.Date.today()
        content = ledger_xlsx(request.env, partner, d_from, d_to)
        return xlsx_response(content, 'Ledger_%s_%s_%s.xlsx' % (partner.pump_code or partner.id, d_from, d_to))
