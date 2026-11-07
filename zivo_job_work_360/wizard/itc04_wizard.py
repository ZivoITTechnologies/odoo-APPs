# -*- coding: utf-8 -*-
import io
from collections import defaultdict
from datetime import datetime, time

import xlsxwriter

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import format_date

from ..models.india_gst_utils import partner_state_code


class ZivoJobWorkItc04Wizard(models.TransientModel):
    _name = 'zivo.job.work.itc04.wizard'
    _description = 'ITC-04 Preparation'

    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    date_from = fields.Date('From', required=True,
                            default=lambda self: self.env['zivo.job.work']._jw_in_financial_year()[0])
    date_to = fields.Date('To', required=True, default=fields.Date.context_today)
    table4_count = fields.Integer('Lines Sent (Table 4)', compute='_compute_summary')
    table5_count = fields.Integer('Lines Received Back (Table 5A)', compute='_compute_summary')
    warning_count = fields.Integer('Validation Warnings', compute='_compute_summary')
    warning_text = fields.Text('Validation', compute='_compute_summary')

    @api.depends('company_id', 'date_from', 'date_to')
    def _compute_summary(self):
        for wiz in self:
            if not wiz.date_from or not wiz.date_to or wiz.date_from > wiz.date_to:
                wiz.update({'table4_count': 0, 'table5_count': 0, 'warning_count': 0, 'warning_text': False})
                continue
            t4, t5, warnings = wiz._jw_itc04_rows()
            wiz.table4_count = len(t4)
            wiz.table5_count = len(t5)
            wiz.warning_count = len(warnings)
            wiz.warning_text = '\n'.join(warnings[:50]) or _('No issue found.')

    def _period(self):
        return (datetime.combine(self.date_from, time.min), datetime.combine(self.date_to, time.max))

    def _jw_itc04_rows(self):
        self.ensure_one()
        start, end = self._period()
        warnings = []
        challans = self.env['zivo.job.work.challan'].search([
            ('company_id', '=', self.company_id.id), ('state', '=', 'confirmed'),
            ('date', '>=', self.date_from), ('date', '<=', self.date_to)], order='date, id')
        table4 = []
        for ch in challans:
            for line in ch.line_ids:
                rate = line.gst_rate
                mat = line.move_id.jw_material_id
                table4.append({
                    'gstin': ch.job_worker_gstin or '', 'state': ch.to_state_code or '',
                    'worker': ch.partner_id.commercial_partner_id.name, 'challan': ch.name, 'date': ch.date,
                    'goods': _('Capital Goods') if mat.is_capital_goods else _('Inputs'),
                    'description': line.name, 'hsn': line.hsn_code or '', 'uqc': line.uqc or '',
                    'qty': line.qty, 'value': line.value,
                    'igst': rate if ch.is_interstate else 0.0,
                    'cgst': 0.0 if ch.is_interstate else rate / 2.0,
                    'sgst': 0.0 if ch.is_interstate else rate / 2.0,
                    'cess': 0.0,
                })
                if not line.hsn_code:
                    warnings.append(_("%(ch)s: HSN missing for %(p)s", ch=ch.name, p=line.product_id.display_name))
                if line.uqc in ('', 'OTH'):
                    warnings.append(_("%(ch)s: set a GST UQC on unit %(u)s", ch=ch.name, u=line.product_uom_id.name))
            if not ch.job_worker_gstin:
                warnings.append(_("%(ch)s: job worker %(w)s has no GSTIN (unregistered)",
                                  ch=ch.name, w=ch.partner_id.display_name))
            if not ch.to_state_code:
                warnings.append(_("%s: job worker state is missing", ch.name))

        moves = self.env['stock.move'].search([
            ('company_id', '=', self.company_id.id), ('state', '=', 'done'),
            ('jw_material_id', '!=', False), ('jw_move_type', 'in', ('consume', 'return', 'scrap', 'loss')),
            ('date', '>=', start), ('date', '<=', end)], order='date, id')
        # group per (material, document)
        groups = defaultdict(lambda: {'qty': 0.0, 'loss': 0.0, 'date': None, 'ref': '', 'ref_date': None})
        for move in moves:
            mat = move.jw_material_id
            qty = move.product_uom._compute_quantity(move.quantity, mat.product_uom_id, round=False)
            if move.jw_production_id:
                key = (mat.id, 'prod', move.jw_production_id.id)
                doc = move.jw_production_id
                ref, ref_date = doc.vendor_challan_ref or doc.name, doc.vendor_challan_date
            elif move.jw_move_type == 'return':
                key = (mat.id, 'ret', move.picking_id.id or move.id)
                ref, ref_date = move.picking_id.name or move.reference, None
            else:
                key = (mat.id, 'loss', move.id)
                ref, ref_date = move.reference or '', None
            entry = groups[key]
            entry['mat'] = mat
            entry['ref'], entry['ref_date'] = ref, ref_date
            entry['date'] = move.date
            if move.jw_move_type in ('consume', 'return'):
                entry['qty'] += qty
            else:
                entry['loss'] += qty
        table5 = []
        for entry in groups.values():
            mat = entry['mat']
            job = mat.job_id
            worker = job.partner_id.commercial_partner_id
            orig = job.challan_ids.filtered(lambda c: c.state == 'confirmed').sorted('date')
            if not orig:
                warnings.append(_("%s: goods received back without a confirmed outward challan", job.name))
            table5.append({
                'gstin': worker.vat or '', 'state': partner_state_code(worker) or '',
                'worker': worker.name, 'orig_challan': ', '.join(orig.mapped('name')),
                'orig_date': orig[:1].date, 'jw_challan': entry['ref'], 'jw_date': entry['ref_date'],
                'nature': job.job_type_id.name, 'description': mat.product_id.display_name,
                'uqc': mat.product_uom_id._jw_get_uqc(), 'qty': entry['qty'], 'loss': entry['loss'],
                'date': entry['date'], 'job': job.name,
            })
        table5.sort(key=lambda r: (r['date'], r['job']))
        return table4, table5, warnings

    def action_export_xlsx(self):
        self.ensure_one()
        if self.date_from > self.date_to:
            raise UserError(_("The start date must be before the end date."))
        table4, table5, warnings = self._jw_itc04_rows()
        output = io.BytesIO()
        wb = xlsxwriter.Workbook(output, {'in_memory': True})
        bold = wb.add_format({'bold': True, 'bg_color': '#1F4E79', 'font_color': '#FFFFFF', 'border': 1,
                              'text_wrap': True, 'valign': 'top'})
        datef = wb.add_format({'num_format': 'dd-mm-yyyy'})
        num = wb.add_format({'num_format': '#,##0.000'})
        money = wb.add_format({'num_format': '#,##0.00'})

        def sheet(name, headers, rows, formats):
            ws = wb.add_worksheet(name)
            for col, header in enumerate(headers):
                ws.write(0, col, header, bold)
                ws.set_column(col, col, max(12, min(len(header) + 4, 40)))
            for r, row in enumerate(rows, start=1):
                for c, value in enumerate(row):
                    fmt = formats.get(c)
                    if value is None or value is False:
                        ws.write_blank(r, c, None)
                    elif fmt is datef:
                        ws.write_datetime(r, c, datetime.combine(value, time.min) if not isinstance(value, datetime)
                                          else value, datef)
                    elif fmt:
                        ws.write_number(r, c, value, fmt)
                    else:
                        ws.write(r, c, value)
            ws.freeze_panes(1, 0)
            return ws

        sheet(_('4 - Sent to Job Worker'), [
            _('GSTIN of Job Worker'), _('State Code'), _('Job Worker'), _('Challan No.'), _('Challan Date'),
            _('Types of Goods'), _('Description of Goods'), _('HSN'), _('UQC'), _('Quantity'),
            _('Taxable Value'), _('IGST Rate'), _('CGST Rate'), _('SGST/UTGST Rate'), _('Cess Rate'),
        ], [[r['gstin'], r['state'], r['worker'], r['challan'], r['date'], r['goods'], r['description'],
             r['hsn'], r['uqc'], r['qty'], r['value'], r['igst'], r['cgst'], r['sgst'], r['cess']]
            for r in table4], {4: datef, 9: num, 10: money, 11: money, 12: money, 13: money, 14: money})
        sheet(_('5A - Received Back'), [
            _('GSTIN of Job Worker'), _('State Code'), _('Job Worker'), _('Original Challan No.'),
            _('Original Challan Date'), _('Job Worker Challan No.'), _('Job Worker Challan Date'),
            _('Nature of Job Work'), _('Description of Goods'), _('UQC'), _('Quantity Received Back'),
            _('Losses & Wastes'), _('Posting Date'), _('Job Work'),
        ], [[r['gstin'], r['state'], r['worker'], r['orig_challan'], r['orig_date'], r['jw_challan'],
             r['jw_date'], r['nature'], r['description'], r['uqc'], r['qty'], r['loss'],
             r['date'].date() if r['date'] else None, r['job']]
            for r in table5], {4: datef, 6: datef, 10: num, 11: num, 12: datef})
        ws = wb.add_worksheet(_('Validation'))
        ws.set_column(0, 0, 110)
        ws.write(0, 0, _('Validation warnings'), bold)
        for i, warning in enumerate(warnings or [_('No issue found.')], start=1):
            ws.write(i, 0, warning)
        ws.write(len(warnings) + 3, 0, _(
            "Prepared by Job Work 360 for %(company)s, period %(start)s to %(end)s. This file prepares data for "
            "ITC-04; validate the format and values against the current GSTN offline tool before filing.",
            company=self.company_id.name, start=format_date(self.env, self.date_from),
            end=format_date(self.env, self.date_to)))
        wb.close()
        name = 'ITC-04_%s_%s_%s.xlsx' % (self.company_id.vat or self.company_id.name.replace(' ', '_'),
                                         self.date_from.strftime('%Y%m%d'), self.date_to.strftime('%Y%m%d'))
        attachment = self.env['ir.attachment'].create({
            'name': name,
            'raw': output.getvalue(),
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'res_model': self._name,
            'res_id': self.id,
        })
        return {'type': 'ir.actions.act_url', 'url': '/web/content/%s?download=true' % attachment.id,
                'target': 'self'}
