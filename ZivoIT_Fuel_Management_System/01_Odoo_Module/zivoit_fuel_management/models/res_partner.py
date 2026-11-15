# -*- coding: utf-8 -*-
import calendar
from datetime import date as dt_date, timedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError

VTYPE_LABEL = {
    'receipt': 'Receipt', 'payment': 'Payment', 'journal': 'Journal', 'contra': 'Contra',
    'sale': 'Period Bill', 'purchase': 'Purchase', 'shift': 'Shift Sale', 'opening': 'Opening',
}


class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_pump_customer = fields.Boolean('Fuel Credit Customer', index=True)
    is_pump_supplier = fields.Boolean('Fuel Supplier')
    pump_code = fields.Char('Customer Code', index=True, copy=False)
    pump_credit_limit = fields.Monetary('Credit Limit', currency_field='pump_currency_id', tracking=True)
    pump_credit_days = fields.Integer('Credit Days', default=15)
    pump_billing_cycle = fields.Selection([
        ('weekly', 'Weekly'), ('fortnightly', 'Fortnightly (15 days)'), ('monthly', 'Monthly')],
        default='fortnightly', string='Billing Cycle')
    pump_block_on_limit = fields.Boolean('Stop Credit Above Limit', default=True,
                                         help='Block new filling slips when the limit is exceeded.')
    pump_vehicle_ids = fields.One2many('pump.vehicle', 'partner_id', string='Vehicles')
    pump_currency_id = fields.Many2one('res.currency', compute='_compute_pump_currency')
    pump_balance = fields.Monetary('Outstanding', compute='_compute_pump_balance',
                                   currency_field='pump_currency_id')
    pump_unbilled = fields.Monetary('Unbilled Slips', compute='_compute_pump_balance',
                                    currency_field='pump_currency_id')
    pump_exposure = fields.Monetary('Total Exposure', compute='_compute_pump_balance',
                                    currency_field='pump_currency_id')
    pump_available_credit = fields.Monetary('Available Credit', compute='_compute_pump_balance',
                                            currency_field='pump_currency_id')
    pump_over_limit = fields.Boolean(compute='_compute_pump_balance', search='_search_over_limit')
    pump_risk_score = fields.Integer('AI Risk Score', compute='_compute_pump_risk')
    pump_risk_grade = fields.Selection([('A', 'A - Low'), ('B', 'B - Moderate'), ('C', 'C - High'), ('D', 'D - Critical')],
                                       compute='_compute_pump_risk', string='AI Risk Grade')
    pump_suggested_limit = fields.Float('AI Suggested Limit', compute='_compute_pump_risk', digits=(16, 0))
    pump_overdue_amount = fields.Float('Overdue Amount', compute='_compute_pump_risk', digits=(16, 2))
    pump_slip_count = fields.Integer(compute='_compute_pump_counts')
    pump_bill_count = fields.Integer(compute='_compute_pump_counts')

    _pump_code_uniq = models.Constraint('UNIQUE(pump_code)', 'Customer code must be unique.')

    def _compute_pump_currency(self):
        for p in self:
            p.pump_currency_id = self.env.company.currency_id

    def _pump_balances(self, date_to=None):
        """Return {commercial_partner_id: receivable balance} (Dr positive)."""
        partners = self.mapped('commercial_partner_id')
        domain = [('partner_id', 'in', partners.ids), ('state', '=', 'posted'),
                  ('account_type', '=', 'receivable')]
        if date_to:
            domain.append(('date', '<=', date_to))
        data = self.env['pump.voucher.line'].sudo()._read_group(domain, ['partner_id'], ['balance:sum'])
        return {p.id: b for p, b in data}

    def _compute_pump_balance(self):
        bal = self._pump_balances()
        unbilled = dict((p.id, a) for p, a in self.env['pump.credit.sale'].sudo()._read_group(
            [('partner_id', 'in', self.mapped('commercial_partner_id').ids), ('state', '=', 'confirmed')],
            ['partner_id'], ['amount:sum']))
        for p in self:
            cp = p.commercial_partner_id.id
            p.pump_balance = bal.get(cp, 0.0)
            p.pump_unbilled = unbilled.get(cp, 0.0)
            p.pump_exposure = p.pump_balance + p.pump_unbilled
            p.pump_available_credit = p.pump_credit_limit - p.pump_exposure
            p.pump_over_limit = bool(p.pump_credit_limit) and p.pump_exposure > p.pump_credit_limit

    def _compute_pump_risk(self):
        Ai = self.env['pump.ai']
        for p in self:
            if p.is_pump_customer and p.id:
                r = Ai.customer_risk(p)
                p.pump_risk_score, p.pump_risk_grade = int(r['score']), r['grade']
                p.pump_suggested_limit, p.pump_overdue_amount = r['suggested_limit'], r['overdue_amount']
            else:
                p.pump_risk_score, p.pump_risk_grade, p.pump_suggested_limit, p.pump_overdue_amount = 0, False, 0.0, 0.0

    def _search_over_limit(self, operator, value):
        customers = self.search([('is_pump_customer', '=', True)])
        ids = customers.filtered('pump_over_limit').ids
        positive = (operator in ('=', '==') and value) or (operator == '!=' and not value)
        return [('id', 'in' if positive else 'not in', ids)]

    def _compute_pump_counts(self):
        Sale = self.env['pump.credit.sale']
        Bill = self.env['pump.bill']
        for p in self:
            p.pump_slip_count = Sale.search_count([('partner_id', '=', p.commercial_partner_id.id)])
            p.pump_bill_count = Bill.search_count([('partner_id', '=', p.commercial_partner_id.id)])

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('is_pump_customer') and not vals.get('pump_code'):
                vals['pump_code'] = self.env['ir.sequence'].next_by_code('pump.customer.code')
        return super().create(vals_list)

    def write(self, vals):
        if 'pump_credit_limit' in vals and not self.env.su and \
                not self.env.user.has_group('zivoit_fuel_management.group_pump_manager'):
            raise UserError(_('Only a Petrol Pump Manager can change credit limits.'))
        res = super().write(vals)
        if vals.get('is_pump_customer'):
            for p in self.filtered(lambda x: not x.pump_code):
                p.pump_code = self.env['ir.sequence'].next_by_code('pump.customer.code')
        return res

    # ------------------------------------------------------------------
    # Ledger services (used by back-office reports, Excel and the portal)
    # ------------------------------------------------------------------
    def _pump_check_ledger_access(self):
        if self.env.su or self.env.user.has_group('zivoit_fuel_management.group_pump_user'):
            return
        if self.commercial_partner_id != self.env.user.partner_id.commercial_partner_id:
            raise AccessError(_('You can only view your own account.'))

    def pump_ledger(self, date_from, date_to):
        """Customer ledger with opening balance and running balance."""
        self.ensure_one()
        self._pump_check_ledger_access()
        partner = self.commercial_partner_id
        date_from = fields.Date.to_date(date_from)
        date_to = fields.Date.to_date(date_to)
        Line = self.env['pump.voucher.line'].sudo()
        base = [('partner_id', '=', partner.id), ('state', '=', 'posted'), ('account_type', '=', 'receivable')]
        opening = sum(Line.search(base + [('date', '<', date_from)]).mapped('balance'))
        lines = Line.search(base + [('date', '>=', date_from), ('date', '<=', date_to)],
                            order='date, voucher_id, id')
        running = opening
        rows = []
        for l in lines:
            running += l.balance
            v = l.voucher_id
            particulars = l.name or v.ref or VTYPE_LABEL.get(v.voucher_type)
            rows.append({
                'date': l.date, 'particulars': particulars,
                'vtype': VTYPE_LABEL.get(v.voucher_type, v.voucher_type), 'vno': v.name,
                'debit': l.debit, 'credit': l.credit, 'balance': running,
                'narration': v.narration or '', 'cheque': v.cheque_no or '',
                'voucher_id': v.id, 'origin_model': v.origin_model, 'origin_id': v.origin_id,
            })
        return {
            'partner': partner, 'date_from': date_from, 'date_to': date_to,
            'opening': opening, 'lines': rows,
            'total_debit': sum(r['debit'] for r in rows), 'total_credit': sum(r['credit'] for r in rows),
            'closing': running, 'credit_limit': partner.pump_credit_limit,
            'unbilled': partner.pump_unbilled,
        }

    def _pump_allocation(self):
        """FIFO-allocate customer credits (receipts, credit journals) to the oldest
        debits (opening balance, bills). Returns {voucher_line_id: paid_amount}."""
        self.ensure_one()
        partner = self.commercial_partner_id
        lines = self.env['pump.voucher.line'].sudo().search(
            [('partner_id', '=', partner.id), ('state', '=', 'posted'), ('account_type', '=', 'receivable')],
            order='date, id')
        pool = sum(lines.mapped('credit'))
        res = {}
        for l in lines.filtered(lambda x: x.debit > 0):
            paid = min(pool, l.debit)
            res[l.id] = paid
            pool -= paid
        return res

    @api.model
    def pump_fy_bounds(self, fy_start_year):
        m = self.env.company.pump_fy_start_month or 4
        start = dt_date(fy_start_year, m, 1)
        end = (dt_date(fy_start_year + 1, m, 1) - timedelta(days=1))
        return start, end

    @api.model
    def pump_current_fy(self, on_date=None):
        on_date = on_date or fields.Date.context_today(self)
        m = self.env.company.pump_fy_start_month or 4
        return on_date.year if on_date.month >= m else on_date.year - 1

    def pump_monthly(self, fy_start_year):
        """Month-wise opening / debit / credit / closing for a financial year."""
        self.ensure_one()
        self._pump_check_ledger_access()
        partner = self.commercial_partner_id
        start, end = self.pump_fy_bounds(fy_start_year)
        Line = self.env['pump.voucher.line'].sudo()
        base = [('partner_id', '=', partner.id), ('state', '=', 'posted'), ('account_type', '=', 'receivable')]
        running = sum(Line.search(base + [('date', '<', start)]).mapped('balance'))
        rows = []
        cur = start
        while cur <= end:
            last = dt_date(cur.year, cur.month, calendar.monthrange(cur.year, cur.month)[1])
            lines = Line.search(base + [('date', '>=', cur), ('date', '<=', last)])
            amt_dr, amt_cr = sum(lines.mapped('debit')), sum(lines.mapped('credit'))
            rows.append({'month': cur.strftime('%b %Y'), 'date_from': cur, 'date_to': last,
                         'opening': running, 'debit': amt_dr, 'credit': amt_cr, 'closing': running + amt_dr - amt_cr,
                         'count': len(lines)})
            running += amt_dr - amt_cr
            cur = last + timedelta(days=1)
        return {'partner': partner, 'fy': '%s-%s' % (fy_start_year, str(fy_start_year + 1)[-2:]),
                'rows': rows, 'total_debit': sum(r['debit'] for r in rows),
                'total_credit': sum(r['credit'] for r in rows)}

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_pump_slips(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Filling Slips'), 'res_model': 'pump.credit.sale',
                'view_mode': 'list,form', 'domain': [('partner_id', '=', self.commercial_partner_id.id)],
                'context': {'default_partner_id': self.commercial_partner_id.id}}

    def action_pump_bills(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Period Bills'), 'res_model': 'pump.bill',
                'view_mode': 'list,form', 'domain': [('partner_id', '=', self.commercial_partner_id.id)],
                'context': {'default_partner_id': self.commercial_partner_id.id}}

    def action_pump_ledger(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Customer Ledger'),
                'res_model': 'pump.ledger.wizard', 'view_mode': 'form', 'target': 'new',
                'context': {'default_partner_id': self.commercial_partner_id.id}}

    def action_pump_receipt(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Quick Receipt'),
                'res_model': 'pump.quick.voucher.wizard', 'view_mode': 'form', 'target': 'new',
                'context': {'default_partner_id': self.commercial_partner_id.id, 'default_voucher_type': 'receipt'}}

    def action_pump_portal_access(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Customer Portal Login'),
                'res_model': 'pump.portal.access.wizard', 'view_mode': 'form', 'target': 'new',
                'context': {'default_partner_id': self.id}}

    def action_pump_opening_balance(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Opening Balance'),
                'res_model': 'pump.opening.wizard', 'view_mode': 'form', 'target': 'new',
                'context': {'default_partner_id': self.commercial_partner_id.id}}

    def _pump_check_credit(self, extra_amount):
        """Raise if adding extra_amount would breach the credit limit."""
        self.env.flush_all()
        self.env['res.partner'].invalidate_model(['pump_balance', 'pump_unbilled', 'pump_exposure',
                                                  'pump_available_credit', 'pump_over_limit'])
        for p in self.mapped('commercial_partner_id'):
            if p.pump_credit_limit and p.pump_block_on_limit and \
                    p.pump_exposure + extra_amount > p.pump_credit_limit + 0.005:
                if not self.env.user.has_group('zivoit_fuel_management.group_pump_manager') or \
                        not self.env.context.get('pump_override_limit'):
                    raise UserError(_(
                        'Credit limit exceeded for %(p)s.\nLimit: %(l).2f | Outstanding + unbilled: %(e).2f | '
                        'This slip: %(a).2f.\nRecord a receipt or ask a manager to increase the limit.',
                        p=p.display_name, l=p.pump_credit_limit, e=p.pump_exposure, a=extra_amount))


class PumpVehicle(models.Model):
    _name = 'pump.vehicle'
    _description = 'Customer Vehicle'
    _order = 'partner_id, name'

    name = fields.Char('Vehicle No.', required=True)
    partner_id = fields.Many2one('res.partner', required=True, ondelete='cascade',
                                 domain=[('is_pump_customer', '=', True)])
    vehicle_type = fields.Selection([('truck', 'Truck / Trailer'), ('bus', 'Bus'), ('car', 'Car / Jeep'),
                                     ('tractor', 'Tractor'), ('genset', 'Generator / Machine'),
                                     ('other', 'Other')], default='truck')
    product_id = fields.Many2one('pump.product', string='Usual Fuel')
    tank_capacity = fields.Float('Vehicle Tank (Ltr)')
    driver_name = fields.Char()
    active = fields.Boolean(default=True)

    sale_ids = fields.One2many('pump.credit.sale', 'vehicle_id')
    last_fill_date = fields.Date(compute='_compute_analytics')
    month_qty = fields.Float('Litres this month', compute='_compute_analytics', digits=(16, 2))
    month_amount = fields.Float('Spend this month', compute='_compute_analytics', digits=(16, 2))
    avg_kmpl = fields.Float('Avg km/L', compute='_compute_analytics', digits=(16, 2))
    fill_count = fields.Integer(compute='_compute_analytics')

    _name_partner_uniq = models.Constraint('UNIQUE(name, partner_id)', 'Vehicle already registered for this customer.')

    def _compute_analytics(self):
        month_start = fields.Date.context_today(self).replace(day=1)
        for v in self:
            sales = v.sale_ids.filtered(lambda s: s.state in ('confirmed', 'billed'))
            v.last_fill_date = max(sales.mapped('date')) if sales else False
            month = sales.filtered(lambda s: s.date >= month_start)
            v.month_qty = sum(month.mapped('qty'))
            v.month_amount = sum(month.mapped('amount'))
            k = sales.filtered(lambda s: s.kmpl > 0).mapped('kmpl')
            v.avg_kmpl = sum(k) / len(k) if k else 0.0
            v.fill_count = len(sales)

    @api.onchange('name')
    def _onchange_name(self):
        if self.name:
            self.name = self.name.upper().replace(' ', '')
