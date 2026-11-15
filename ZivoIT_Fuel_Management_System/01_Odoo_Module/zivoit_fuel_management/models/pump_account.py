# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, float_is_zero

ACCOUNT_TYPES = [
    ('cash', 'Cash'),
    ('bank', 'Bank'),
    ('card', 'Card / Wallet Settlement'),
    ('receivable', 'Sundry Debtors (Customers)'),
    ('payable', 'Sundry Creditors (Suppliers)'),
    ('clearing', 'Clearing / Suspense'),
    ('asset', 'Other Asset'),
    ('liability', 'Other Liability'),
    ('equity', 'Capital & Reserves'),
    ('income', 'Income'),
    ('expense', 'Expense'),
]
# Natural debit-balance types (used for statement grouping)
DEBIT_TYPES = ('cash', 'bank', 'card', 'receivable', 'clearing', 'asset', 'expense')

VOUCHER_TYPES = [
    ('receipt', 'Receipt'),
    ('payment', 'Payment'),
    ('journal', 'Journal'),
    ('contra', 'Contra (Bank/Cash)'),
    ('sale', 'Period Bill'),
    ('purchase', 'Purchase'),
    ('shift', 'Shift Sale'),
    ('opening', 'Opening Balance'),
]


PARTNER_BALANCE_FIELDS = ['pump_balance', 'pump_unbilled', 'pump_exposure', 'pump_available_credit',
                          'pump_over_limit']


class PumpAccount(models.Model):
    _name = 'pump.account'
    _description = 'Ledger Account'
    _order = 'code, name'

    name = fields.Char(required=True)
    code = fields.Char(required=True)
    account_type = fields.Selection(ACCOUNT_TYPES, required=True, default='expense')
    active = fields.Boolean(default=True)
    note = fields.Text()
    currency_id = fields.Many2one('res.currency', default=lambda s: s.env.company.currency_id)
    balance = fields.Monetary(compute='_compute_balance', currency_field='currency_id',
                              help='Debit minus credit of all posted entries.')

    _code_uniq = models.Constraint('UNIQUE(code)', 'Account code must be unique.')

    @api.depends('code', 'name')
    def _compute_display_name(self):
        for acc in self:
            acc.display_name = f'{acc.code} {acc.name}' if acc.code else acc.name

    def _compute_balance(self):
        data = self.env['pump.voucher.line']._read_group(
            [('account_id', 'in', self.ids), ('state', '=', 'posted')],
            ['account_id'], ['debit:sum', 'credit:sum'])
        res = {acc.id: amt_dr - amt_cr for acc, amt_dr, amt_cr in data}
        for acc in self:
            acc.balance = res.get(acc.id, 0.0)

    def action_view_entries(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Entries: %s', self.display_name),
            'res_model': 'pump.voucher.line',
            'view_mode': 'list,pivot',
            'domain': [('account_id', '=', self.id), ('state', '=', 'posted')],
        }

    @api.model
    def _get(self, xmlid):
        return self.env.ref('zivoit_fuel_management.%s' % xmlid)


class PumpVoucher(models.Model):
    _name = 'pump.voucher'
    _description = 'Accounting Voucher'
    _inherit = ['mail.thread']
    _order = 'date desc, id desc'

    name = fields.Char('Voucher No.', readonly=True, copy=False, default='/')
    voucher_type = fields.Selection(VOUCHER_TYPES, required=True, default='journal', tracking=True)
    date = fields.Date(required=True, default=fields.Date.context_today, tracking=True)
    shift_id = fields.Many2one('pump.shift')
    partner_id = fields.Many2one('res.partner', string='Party',
                                 help='Main party of the voucher (customer, supplier or employee).')
    narration = fields.Text()
    ref = fields.Char('Reference')
    payment_mode = fields.Selection([
        ('cash', 'Cash'), ('cheque', 'Cheque'), ('neft', 'NEFT / RTGS / IMPS'),
        ('upi', 'UPI'), ('card', 'Card'), ('other', 'Other')], string='Mode')
    cheque_no = fields.Char('Cheque / UTR No.')
    line_ids = fields.One2many('pump.voucher.line', 'voucher_id', string='Lines', copy=True)
    state = fields.Selection([('draft', 'Draft'), ('posted', 'Posted'), ('cancel', 'Cancelled')],
                             default='draft', required=True, tracking=True, copy=False)
    currency_id = fields.Many2one('res.currency', default=lambda s: s.env.company.currency_id)
    amount = fields.Monetary(compute='_compute_amount', store=True, currency_field='currency_id')
    origin_model = fields.Char(readonly=True, copy=False)
    origin_id = fields.Integer(readonly=True, copy=False)

    @api.depends('line_ids.debit')
    def _compute_amount(self):
        for v in self:
            v.amount = sum(v.line_ids.mapped('debit'))

    def _check_balanced(self):
        for v in self:
            if not v.line_ids:
                raise UserError(_('Voucher %s has no lines.', v.name))
            amt_dr = sum(v.line_ids.mapped('debit'))
            amt_cr = sum(v.line_ids.mapped('credit'))
            if float_compare(amt_dr, amt_cr, precision_digits=2) != 0:
                raise UserError(_('Voucher is not balanced: Debit %(dr).2f vs Credit %(cr).2f.', dr=amt_dr, cr=amt_cr))
            if float_is_zero(amt_dr, precision_digits=2):
                raise UserError(_('Voucher amount cannot be zero.'))
            for line in v.line_ids:
                if line.account_id.account_type in ('receivable', 'payable') and not line.partner_id:
                    raise UserError(_('A party is required on line for account %s.', line.account_id.display_name))

    def action_post(self):
        for v in self.filtered(lambda x: x.state == 'draft'):
            v._check_balanced()
            if v.name in (False, '/'):
                v.name = self.env['ir.sequence'].next_by_code('pump.voucher.%s' % v.voucher_type) or '/'
            v.state = 'posted'
        self._invalidate_balances()
        return True

    def _invalidate_balances(self):
        self.env.flush_all()
        self.env['pump.account'].invalidate_model(['balance'])
        self.env['res.partner'].invalidate_model(PARTNER_BALANCE_FIELDS)

    def action_cancel(self):
        for v in self:
            if v.origin_model and not self.env.context.get('pump_from_origin'):
                raise UserError(_('Voucher %s was generated automatically; cancel its source document instead.', v.name))
        self.write({'state': 'cancel'})
        self._invalidate_balances()
        return True

    def action_draft(self):
        for v in self:
            if v.origin_model:
                raise UserError(_('Automatic vouchers cannot be reset to draft.'))
        self.write({'state': 'draft'})

    def unlink(self):
        if any(v.state == 'posted' for v in self):
            raise UserError(_('Posted vouchers cannot be deleted. Cancel them instead.'))
        return super().unlink()

    @api.model
    def _create_posted(self, vals_list_lines, **vals):
        """Helper: create and post a voucher from line dicts. Lines with zero
        amount are skipped; debits/credits rounded to 2 decimals."""
        lines = []
        for lv in vals_list_lines:
            amt_dr = round(lv.get('debit', 0.0), 2)
            amt_cr = round(lv.get('credit', 0.0), 2)
            if amt_dr < 0:
                amt_dr, amt_cr = 0.0, amt_cr - amt_dr
            if amt_cr < 0:
                amt_cr, amt_dr = 0.0, amt_dr - amt_cr
            if float_is_zero(amt_dr, precision_digits=2) and float_is_zero(amt_cr, precision_digits=2):
                continue
            lv = dict(lv, debit=amt_dr, credit=amt_cr)
            lines.append((0, 0, lv))
        voucher = self.create(dict(vals, line_ids=lines))
        voucher.action_post()
        return voucher


class PumpVoucherLine(models.Model):
    _name = 'pump.voucher.line'
    _description = 'Voucher Line / Ledger Entry'
    _order = 'date, voucher_id, id'

    voucher_id = fields.Many2one('pump.voucher', required=True, ondelete='cascade', index=True)
    account_id = fields.Many2one('pump.account', required=True, index=True)
    account_type = fields.Selection(related='account_id.account_type', store=True)
    partner_id = fields.Many2one('res.partner', string='Party', index=True)
    name = fields.Char('Narration')
    debit = fields.Monetary(currency_field='currency_id')
    credit = fields.Monetary(currency_field='currency_id')
    balance = fields.Monetary(compute='_compute_balance', store=True, currency_field='currency_id')
    currency_id = fields.Many2one(related='voucher_id.currency_id')
    date = fields.Date(related='voucher_id.date', store=True, index=True)
    state = fields.Selection(related='voucher_id.state', store=True, index=True)
    voucher_type = fields.Selection(related='voucher_id.voucher_type', store=True)

    _debit_credit = models.Constraint(
        'CHECK(debit >= 0 AND credit >= 0 AND NOT (debit > 0 AND credit > 0))',
        'A line is either a debit or a credit, never negative.')

    @api.depends('debit', 'credit')
    def _compute_balance(self):
        for line in self:
            line.balance = line.debit - line.credit

    @api.constrains('account_id', 'partner_id')
    def _check_partner(self):
        for line in self:
            if line.account_id.account_type == 'receivable' and line.partner_id \
                    and line.partner_id.commercial_partner_id != line.partner_id:
                raise ValidationError(_('Use the company (commercial) contact on receivable lines.'))
