import io
from collections import defaultdict

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_is_zero

TYPE_LABELS = {
    'receipt': 'Received into bond', 'consumption': 'Consumed in manufacture', 'production': 'Produced in bond',
    'home': 'Cleared for home consumption', 'export': 'Exported', 'scrap': 'Scrap / write-off',
    'return': 'Returned to supplier',
}


class MoowrReturn(models.Model):
    _name = 'moowr.return'
    _description = 'MOOWR Monthly Return'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date_from desc, id desc'
    _check_company_auto = True

    name = fields.Char(compute='_compute_name', store=True)
    licence_id = fields.Many2one('moowr.licence', required=True, index=True, check_company=True)
    company_id = fields.Many2one(related='licence_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    date_from = fields.Date('Period Start', required=True)
    date_to = fields.Date('Period End', compute='_compute_dates', store=True)
    due_date = fields.Date(compute='_compute_dates', store=True)
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Submitted')], default='draft', required=True, tracking=True)
    submission_date = fields.Date(tracking=True)
    acknowledgement = fields.Char('Acknowledgement / Reference', tracking=True)
    is_late = fields.Boolean(compute='_compute_is_late')

    receipt_qty_lines = fields.Integer('Receipt Entries', compute='_compute_summary')
    removal_lines = fields.Integer('Removal Entries', compute='_compute_summary')
    receipt_value = fields.Monetary('Value Received', compute='_compute_summary')
    receipt_duty = fields.Monetary('Duty Deferred on Receipts', compute='_compute_summary')
    home_duty = fields.Monetary('Duty on Home Clearances', compute='_compute_summary')
    interest_total = fields.Monetary('Interest', compute='_compute_summary')
    export_value = fields.Monetary('Value Exported', compute='_compute_summary')
    closing_duty = fields.Monetary('Duty in Bond at Period End', compute='_compute_summary')

    _licence_period_uniq = models.Constraint(
        'UNIQUE(licence_id, date_from)', 'A return already exists for this licence and period.')

    @api.depends('licence_id.name', 'date_from')
    def _compute_name(self):
        for ret in self:
            ret.name = f"{ret.date_from.strftime('%B %Y') if ret.date_from else ''} - {ret.licence_id.name or ''}"

    @api.depends('date_from', 'licence_id.return_due_day')
    def _compute_dates(self):
        for ret in self:
            if ret.date_from:
                start = ret.date_from.replace(day=1)
                ret.date_to = start + relativedelta(months=1, days=-1)
                ret.due_date = start + relativedelta(months=1, day=ret.licence_id.return_due_day or 10)
            else:
                ret.date_to = ret.due_date = False

    def _compute_is_late(self):
        today = fields.Date.context_today(self)
        for ret in self:
            ref = ret.submission_date or today
            ret.is_late = bool(ret.due_date and ref > ret.due_date and (ret.state == 'draft' or ret.submission_date))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('date_from'):
                vals['date_from'] = fields.Date.to_date(vals['date_from']).replace(day=1)
        return super().create(vals_list)

    def _period_bounds(self):
        self.ensure_one()
        start = fields.Datetime.to_datetime(self.date_from)
        end = fields.Datetime.to_datetime(self.date_to + relativedelta(days=1))
        return start, end

    def _period_entries(self):
        start, end = self._period_bounds()
        return self.env['moowr.ledger'].search(
            [('licence_id', '=', self.licence_id.id), ('date', '>=', start), ('date', '<', end)], order='date, id')

    def _compute_summary(self):
        for ret in self:
            if not ret.licence_id or not ret.date_from:
                ret.update(dict.fromkeys(['receipt_qty_lines', 'removal_lines'], 0))
                ret.update(dict.fromkeys(['receipt_value', 'receipt_duty', 'home_duty', 'interest_total',
                                          'export_value', 'closing_duty'], 0.0))
                continue
            leds = ret._period_entries()
            rec = leds.filtered(lambda l: l.move_type == 'receipt')
            rem = leds.filtered(lambda l: l.move_type in ('home', 'export', 'scrap', 'return'))
            ret.receipt_qty_lines = len(rec)
            ret.removal_lines = len(rem)
            ret.receipt_value = sum(rec.mapped('value'))
            ret.receipt_duty = sum(rec.mapped('duty_amount'))
            home = leds.filtered(lambda l: l.move_type == 'home')
            ret.home_duty = sum(home.mapped('duty_amount'))
            ret.interest_total = sum(home.mapped('interest_amount'))
            ret.export_value = sum(leds.filtered(lambda l: l.move_type == 'export').mapped('value'))
            ret.closing_duty = sum(r['duty_closing'] for r in ret._balance_rows())

    # ------------------------------------------------------------------
    # Return data
    # ------------------------------------------------------------------
    def _balance_rows(self):
        """Opening, movements and closing per bill of entry line and per production batch."""
        self.ensure_one()
        start, end = self._period_bounds()
        Ledger = self.env['moowr.ledger']
        before = Ledger.search([('licence_id', '=', self.licence_id.id), ('date', '<', start)])
        during = Ledger.search([('licence_id', '=', self.licence_id.id), ('date', '>=', start), ('date', '<', end)])
        acc = defaultdict(lambda: {'opening': 0.0, 'in': 0.0, 'consumed': 0.0, 'removed': 0.0})

        def key(l):
            return ('boe', l.boe_line_id.id) if l.boe_line_id else ('batch', l.batch_id.id)

        for l in before:
            acc[key(l)]['opening'] += l.signed_quantity
        for l in during:
            k = key(l)
            if l.direction == 'in':
                acc[k]['in'] += l.quantity
            elif l.move_type == 'consumption':
                acc[k]['consumed'] += l.quantity
            else:
                acc[k]['removed'] += l.quantity
        rows = []
        for (kind, rid), v in acc.items():
            src = self.env['moowr.boe.line' if kind == 'boe' else 'moowr.batch'].browse(rid)
            closing = v['opening'] + v['in'] - v['consumed'] - v['removed']
            rounding = src.product_id.uom_id.rounding or 0.0001
            if all(float_is_zero(x, precision_rounding=rounding) for x in (v['opening'], v['in'], v['consumed'], v['removed'])):
                continue
            rows.append({
                'source': src.boe_id.name if kind == 'boe' else src.name,
                'source_date': src.boe_date if kind == 'boe' else fields.Date.to_date(src.date),
                'kind': 'Imported input' if kind == 'boe' else 'Bonded product',
                'product': src.product_id.display_name, 'uom': src.uom_id.name,
                'opening': v['opening'], 'in': v['in'], 'consumed': v['consumed'], 'removed': v['removed'],
                'closing': closing,
                'duty_closing': self.currency_id.round(closing * src.unit_duty),
            })
        rows.sort(key=lambda r: (r['kind'], str(r['source_date']), r['source'], r['product']))
        return rows

    def _get_return_data(self):
        self.ensure_one()
        leds = self._period_entries()
        receipts = [{
            'date': fields.Date.to_date(l.date), 'boe': l.boe_id.name, 'boe_date': l.boe_id.boe_date,
            'supplier': l.boe_id.partner_id.name or '', 'product': l.product_id.display_name, 'uom': l.uom_id.name,
            'qty': l.quantity, 'value': l.value, 'duty': l.duty_amount, 'reference': l.reference or '',
        } for l in leds.filtered(lambda l: l.move_type == 'receipt')]
        ops = defaultdict(lambda: {'inputs': [], 'outputs': []})
        for l in leds.filtered(lambda l: l.move_type in ('consumption', 'production')):
            mo = l.production_id.name or l.reference or ''
            item = {'date': fields.Date.to_date(l.date), 'product': l.product_id.display_name, 'uom': l.uom_id.name,
                    'qty': l.quantity, 'source': l.boe_id.name or l.batch_id.name or '', 'duty': l.duty_amount}
            ops[mo]['outputs' if l.move_type == 'production' else 'inputs'].append(item)
        operations = [{'reference': k, **v} for k, v in sorted(ops.items())]
        removals = [{
            'date': fields.Date.to_date(l.date), 'type': TYPE_LABELS[l.move_type],
            'document': l.clearance_id.document_number or l.clearance_id.name or l.reference or '',
            'partner': l.partner_id.name or '', 'product': l.product_id.display_name, 'uom': l.uom_id.name,
            'qty': l.quantity, 'source': l.boe_id.name or l.batch_id.name or '', 'value': l.value,
            'duty': l.duty_amount, 'interest': l.interest_amount,
            'payable': l.duty_amount + l.interest_amount if (l.clearance_id and l.clearance_id._is_duty_payable()) else 0.0,
        } for l in leds.filtered(lambda l: l.move_type in ('home', 'export', 'scrap', 'return'))]
        return {
            'licence': self.licence_id, 'company': self.company_id, 'period': self.name,
            'date_from': self.date_from, 'date_to': self.date_to, 'due_date': self.due_date,
            'receipts': receipts, 'operations': operations, 'removals': removals, 'balances': self._balance_rows(),
        }

    def action_print_pdf(self):
        return self.env.ref('zivoit_moowr.action_report_moowr_return').report_action(self)

    def action_export_xlsx(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_url', 'url': f'/moowr/return/{self.id}/xlsx', 'target': 'self'}

    def action_submit(self):
        for ret in self:
            if not ret.acknowledgement:
                raise UserError(self.env._("Enter the acknowledgement or reference received when filing %s.", ret.name))
            ret.write({'state': 'submitted', 'submission_date': ret.submission_date or fields.Date.context_today(ret)})
            ret.activity_ids.action_done()

    def action_reset_draft(self):
        self.write({'state': 'draft'})

    def _generate_xlsx(self):
        import xlsxwriter
        self.ensure_one()
        data = self._get_return_data()
        out = io.BytesIO()
        wb = xlsxwriter.Workbook(out, {'in_memory': True})
        title = wb.add_format({'bold': True, 'font_size': 14, 'font_color': '#0B1F44'})
        head = wb.add_format({'bold': True, 'bg_color': '#0B1F44', 'font_color': '#FFFFFF', 'border': 1, 'text_wrap': True})
        cell = wb.add_format({'border': 1})
        num = wb.add_format({'border': 1, 'num_format': '#,##0.00'})
        dt = wb.add_format({'border': 1, 'num_format': 'dd-mm-yyyy'})
        lic = data['licence']

        def sheet(name, headers, rows, widths):
            ws = wb.add_worksheet(name)
            ws.write(0, 0, f"{name} - {data['period']}", title)
            ws.write(1, 0, f"Licence {lic.name}  |  Section 65 permission {lic.permission_number or '-'}  |  "
                           f"{data['company'].name}  |  Due {data['due_date']}")
            for c, h in enumerate(headers):
                ws.write(3, c, h, head)
                ws.set_column(c, c, widths[c])
            for r, row in enumerate(rows, start=4):
                for c, v in enumerate(row):
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        ws.write_number(r, c, v, num)
                    elif hasattr(v, 'year'):
                        ws.write_datetime(r, c, fields.Datetime.to_datetime(v), dt)
                    else:
                        ws.write(r, c, v or '', cell)
            ws.freeze_panes(4, 0)
            return ws

        sheet('Receipts', ['Date', 'Bill of Entry', 'BoE Date', 'Supplier', 'Product', 'Unit', 'Quantity',
                           'Assessable Value', 'Duty Deferred', 'Receipt Ref'],
              [[r['date'], r['boe'], r['boe_date'], r['supplier'], r['product'], r['uom'], r['qty'], r['value'],
                r['duty'], r['reference']] for r in data['receipts']], [12, 18, 12, 24, 32, 8, 12, 16, 16, 16])
        op_rows = []
        for op in data['operations']:
            for o in op['outputs']:
                op_rows.append([op['reference'], 'Produced', o['date'], o['product'], o['uom'], o['qty'], o['source'], o['duty']])
            for i in op['inputs']:
                op_rows.append([op['reference'], 'Consumed', i['date'], i['product'], i['uom'], i['qty'], i['source'], i['duty']])
        sheet('Operations', ['Manufacturing Order', 'Movement', 'Date', 'Product', 'Unit', 'Quantity', 'Source',
                             'Duty Content'], op_rows, [20, 12, 12, 34, 8, 12, 20, 16])
        sheet('Removals', ['Date', 'Type', 'Customs Document', 'Partner', 'Product', 'Unit', 'Quantity', 'Source',
                           'Assessable Value', 'Duty', 'Interest', 'Payable'],
              [[r['date'], r['type'], r['document'], r['partner'], r['product'], r['uom'], r['qty'], r['source'],
                r['value'], r['duty'], r['interest'], r['payable']] for r in data['removals']],
              [12, 26, 20, 22, 30, 8, 12, 18, 16, 14, 12, 14])
        sheet('Stock Balance', ['Goods', 'Source', 'Source Date', 'Product', 'Unit', 'Opening', 'Received / Produced',
                                'Consumed', 'Removed', 'Closing', 'Duty in Bond'],
              [[b['kind'], b['source'], b['source_date'], b['product'], b['uom'], b['opening'], b['in'], b['consumed'],
                b['removed'], b['closing'], b['duty_closing']] for b in data['balances']],
              [16, 20, 12, 32, 8, 12, 14, 12, 12, 12, 16])
        wb.close()
        return out.getvalue()
