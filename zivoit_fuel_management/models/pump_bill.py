# -*- coding: utf-8 -*-
import base64
from urllib.parse import quote

from odoo import api, fields, models
from odoo.exceptions import UserError


class PumpBill(models.Model):
    _name = 'pump.bill'
    _description = 'Period Bill (credit sales invoice)'
    _inherit = ['mail.thread']
    _order = 'date desc, id desc'

    name = fields.Char('Bill No.', readonly=True, copy=False, default='/')
    partner_id = fields.Many2one('res.partner', string='Customer', required=True,
                                 domain=[('is_pump_customer', '=', True)], tracking=True)
    date = fields.Date('Bill Date', required=True, default=fields.Date.context_today)
    date_from = fields.Date('Period From', required=True)
    date_to = fields.Date('Period To', required=True)
    due_date = fields.Date(compute='_compute_due', store=True)
    sale_ids = fields.One2many('pump.credit.sale', 'bill_id', string='Filling Slips')
    currency_id = fields.Many2one('res.currency', default=lambda s: s.env.company.currency_id)
    amount_total = fields.Monetary(compute='_compute_totals', store=True, currency_field='currency_id')
    qty_total = fields.Float(compute='_compute_totals', store=True, digits=(16, 2))
    slip_count = fields.Integer(compute='_compute_totals', store=True)
    voucher_id = fields.Many2one('pump.voucher', readonly=True, copy=False)
    previous_balance = fields.Monetary(readonly=True, currency_field='currency_id',
                                       help='Customer outstanding before this bill (at posting).')
    state = fields.Selection([('draft', 'Draft'), ('posted', 'Posted'), ('cancel', 'Cancelled')],
                             default='draft', tracking=True, copy=False)
    amount_paid = fields.Monetary(compute='_compute_payment', currency_field='currency_id')
    amount_due = fields.Monetary(compute='_compute_payment', currency_field='currency_id')
    payment_state = fields.Selection([('not_paid', 'Not Paid'), ('partial', 'Partially Paid'), ('paid', 'Paid'),
                                      ('overdue', 'Overdue')], compute='_compute_payment', string='Payment Status')
    days_overdue = fields.Integer(compute='_compute_payment')

    def _compute_payment(self):
        today = fields.Date.context_today(self)
        alloc_cache = {}
        for b in self:
            paid = 0.0
            if b.state == 'posted' and b.voucher_id:
                partner = b.partner_id.commercial_partner_id
                if partner.id not in alloc_cache:
                    alloc_cache[partner.id] = partner._pump_allocation()
                line = b.voucher_id.line_ids.filtered(lambda l: l.account_id.account_type == 'receivable')[:1]
                paid = alloc_cache[partner.id].get(line.id, 0.0)
            b.amount_paid = paid
            b.amount_due = max(b.amount_total - paid, 0.0) if b.state == 'posted' else 0.0
            overdue = b.state == 'posted' and b.amount_due > 0.5 and b.due_date and b.due_date < today
            b.days_overdue = (today - b.due_date).days if overdue else 0
            if b.state != 'posted' or b.amount_due <= 0.5:
                b.payment_state = 'paid' if b.state == 'posted' else 'not_paid'
            elif overdue:
                b.payment_state = 'overdue'
            else:
                b.payment_state = 'partial' if paid > 0.5 else 'not_paid'

    def vehicle_summary(self):
        self.ensure_one()
        res = {}
        for s in self.sale_ids.sorted(lambda x: (x.date, x.id)):
            key = s.vehicle_no or self.env._('Unregistered')
            r = res.setdefault(key, {'vehicle': key, 'fills': 0, 'qty': 0.0, 'amount': 0.0, 'products': set(),
                                     'first': s.date, 'last': s.date, 'sales': []})
            r['fills'] += 1
            r['qty'] += s.qty
            r['amount'] += s.amount
            r['products'].add(s.product_id.name)
            r['last'] = s.date
            r['sales'].append(s)
        rows = sorted(res.values(), key=lambda r: -r['amount'])
        for r in rows:
            r['products'] = ', '.join(sorted(r['products']))
        return rows

    def account_summary(self):
        """Previous bill → payments received → this bill → total outstanding."""
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id
        prev_bill = self.search([('partner_id', '=', partner.id), ('state', '=', 'posted'), ('id', '!=', self.id),
                                 '|', ('date', '<', self.date), '&', ('date', '=', self.date), ('id', '<', self.id)],
                                order='date desc, id desc', limit=1)
        Line = self.env['pump.voucher.line'].sudo()
        dom = [('partner_id', '=', partner.id), ('state', '=', 'posted'), ('account_type', '=', 'receivable'),
               ('voucher_id', '!=', self.voucher_id.id), ('date', '<=', self.date)]
        if prev_bill:
            dom.append(('date', '>', prev_bill.date))
        between = Line.search(dom)
        received = sum(between.mapped('credit'))
        opening = self.previous_balance + received - sum(between.mapped('debit'))
        return {'prev_bill': prev_bill, 'opening': opening, 'received': received,
                'other_debits': sum(between.mapped('debit')), 'this_bill': self.amount_total,
                'total': self.previous_balance + self.amount_total,
                'receipts': between.filtered(lambda l: l.credit > 0)}

    def upi_qr_data_uri(self, amount=None):
        """UPI payment QR (data URI) for the outstanding amount, if a UPI ID is configured."""
        self.ensure_one()
        company = self.env.company
        if not company.pump_upi_id:
            return False
        amt = amount if amount is not None else (self.previous_balance + self.amount_total)
        url = 'upi://pay?pa=%s&pn=%s&am=%.2f&cu=INR&tn=%s' % (
            quote(company.pump_upi_id), quote(company.pump_outlet_name or company.name), max(amt, 0), quote(self.name))
        png = False
        try:
            import io
            import qrcode  # part of Odoo's requirements
            buf = io.BytesIO()
            qrcode.make(url, box_size=6, border=2).save(buf, format='PNG')
            png = buf.getvalue()
        except Exception:  # noqa: BLE001 - fall back to ReportLab
            try:
                png = self.env['ir.actions.report'].barcode('QR', url, width=180, height=180)
            except Exception:  # noqa: BLE001 - no QR backend available: render without QR
                return False
        return 'data:image/png;base64,%s' % base64.b64encode(png).decode()

    @api.depends('date', 'partner_id.pump_credit_days')
    def _compute_due(self):
        for b in self:
            b.due_date = fields.Date.add(b.date, days=b.partner_id.pump_credit_days or 0) if b.date else False

    @api.depends('sale_ids.amount', 'sale_ids.qty')
    def _compute_totals(self):
        for b in self:
            b.amount_total = sum(b.sale_ids.mapped('amount'))
            b.qty_total = sum(b.sale_ids.mapped('qty'))
            b.slip_count = len(b.sale_ids)

    def product_summary(self):
        self.ensure_one()
        summary = {}
        for s in self.sale_ids:
            row = summary.setdefault(s.product_id, {'product': s.product_id, 'qty': 0.0, 'amount': 0.0})
            row['qty'] += s.qty
            row['amount'] += s.amount
        return list(summary.values())

    def action_load_slips(self):
        for b in self.filtered(lambda x: x.state == 'draft'):
            slips = self.env['pump.credit.sale'].search([
                ('partner_id', '=', b.partner_id.commercial_partner_id.id), ('state', '=', 'confirmed'),
                ('bill_id', '=', False), ('date', '>=', b.date_from), ('date', '<=', b.date_to)])
            slips.write({'bill_id': b.id})
        return True

    def action_post(self):
        clearing = self.env.ref('zivoit_fuel_management.acc_credit_clearing')
        debtors = self.env.ref('zivoit_fuel_management.acc_debtors')
        for b in self.filtered(lambda x: x.state == 'draft'):
            if not b.sale_ids:
                b.action_load_slips()
            if not b.sale_ids:
                raise UserError(self.env._('No unbilled slips for %s in the selected period.', b.partner_id.display_name))
            if b.name in (False, '/'):
                b.name = self.env['ir.sequence'].next_by_code('pump.bill') or '/'
            partner = b.partner_id.commercial_partner_id
            b.previous_balance = partner._pump_balances().get(partner.id, 0.0)
            label = self.env._('Period Bill %(n)s (%(f)s to %(t)s)', n=b.name,
                      f=b.date_from.strftime('%d-%m-%Y'), t=b.date_to.strftime('%d-%m-%Y'))
            voucher = self.env['pump.voucher']._create_posted([
                {'account_id': debtors.id, 'partner_id': partner.id, 'debit': b.amount_total, 'name': label},
                {'account_id': clearing.id, 'credit': b.amount_total, 'name': label},
            ], voucher_type='sale', date=b.date, partner_id=partner.id, ref=b.name,
                narration=self.env._('%s filling slips', b.slip_count), origin_model=b._name, origin_id=b.id)
            b.sale_ids.write({'state': 'billed'})
            b.write({'voucher_id': voucher.id, 'state': 'posted'})
        return True

    def action_cancel(self):
        for b in self:
            if b.voucher_id:
                b.voucher_id.with_context(pump_from_origin=True).action_cancel()
            b.sale_ids.with_context().write({'state': 'confirmed'})
            b.sale_ids.write({'bill_id': False})
            b.state = 'cancel'
        return True

    @api.ondelete(at_uninstall=False)
    def _unlink_except_posted(self):
        if any(b.state == 'posted' for b in self):
            raise UserError(self.env._('Posted bills cannot be deleted. Cancel them first.'))

    def unlink(self):
        self.sale_ids.write({'bill_id': False})
        return super().unlink()

    def action_print(self):
        return self.env.ref('zivoit_fuel_management.action_report_bill').report_action(self)

    @api.model
    def generate_bills(self, date_from, date_to, partners=None, bill_date=None, post=True):
        """Generate one bill per customer having unbilled slips in the period."""
        domain = [('state', '=', 'confirmed'), ('bill_id', '=', False),
                  ('date', '>=', date_from), ('date', '<=', date_to)]
        if partners:
            domain.append(('partner_id', 'in', partners.mapped('commercial_partner_id').ids))
        slips = self.env['pump.credit.sale'].search(domain)
        bills = self.browse()
        for partner in slips.mapped('partner_id'):
            bill = self.create({'partner_id': partner.id, 'date': bill_date or date_to,
                                'date_from': date_from, 'date_to': date_to})
            slips.filtered(lambda s: s.partner_id == partner).write({'bill_id': bill.id})
            bills |= bill
        if post:
            bills.action_post()
        return bills
