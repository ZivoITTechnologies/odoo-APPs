# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


def _first_of_month(d):
    return d.replace(day=1)


class PumpLedgerWizard(models.TransientModel):
    _name = 'pump.ledger.wizard'
    _description = 'Customer Ledger / Statement'

    partner_id = fields.Many2one('res.partner', string='Customer', required=True,
                                 domain=[('is_pump_customer', '=', True)])
    date_from = fields.Date(required=True, default=lambda s: _first_of_month(fields.Date.context_today(s)))
    date_to = fields.Date(required=True, default=fields.Date.context_today)

    def _check(self):
        if self.date_from > self.date_to:
            raise ValidationError(_('From date must be before To date.'))

    def action_pdf(self):
        self._check()
        return self.env.ref('zivoit_fuel_management.action_report_statement').report_action(
            self.partner_id, data={'date_from': str(self.date_from), 'date_to': str(self.date_to)})

    def action_excel(self):
        self._check()
        return {'type': 'ir.actions.act_url', 'target': 'self',
                'url': '/fuel/ledger/xlsx/%s?date_from=%s&date_to=%s' % (
                    self.partner_id.commercial_partner_id.id, self.date_from, self.date_to)}

    def action_view(self):
        self._check()
        return {
            'type': 'ir.actions.act_window', 'name': _('Ledger: %s', self.partner_id.display_name),
            'res_model': 'pump.voucher.line', 'view_mode': 'list,pivot',
            'views': [(self.env.ref('zivoit_fuel_management.view_voucher_line_ledger_list').id, 'list'), (False, 'pivot')],
            'domain': [('partner_id', '=', self.partner_id.commercial_partner_id.id), ('state', '=', 'posted'),
                       ('account_type', '=', 'receivable'), ('date', '>=', self.date_from), ('date', '<=', self.date_to)],
        }


class PumpQuickVoucherWizard(models.TransientModel):
    _name = 'pump.quick.voucher.wizard'
    _description = 'Quick Receipt / Payment'

    voucher_type = fields.Selection([('receipt', 'Receipt from Customer'), ('payment', 'Payment / Expense'),
                                     ('contra', 'Bank Deposit / Withdrawal')], required=True, default='receipt')
    date = fields.Date(required=True, default=fields.Date.context_today)
    partner_id = fields.Many2one('res.partner', string='Party')
    amount = fields.Float(required=True, digits=(16, 2))
    via = fields.Selection([('cash', 'Cash'), ('bank', 'Bank')], required=True, default='bank', string='Cash / Bank')
    payment_mode = fields.Selection([('cash', 'Cash'), ('cheque', 'Cheque'), ('neft', 'NEFT / RTGS / IMPS'),
                                     ('upi', 'UPI'), ('card', 'Card'), ('other', 'Other')], default='neft')
    cheque_no = fields.Char('Cheque / UTR No.')
    account_id = fields.Many2one('pump.account', string='Expense / Ledger Account',
                                 help='For payments: the expense or ledger account being paid.')
    contra_direction = fields.Selection([('deposit', 'Cash deposited into bank'),
                                         ('withdraw', 'Cash withdrawn from bank'),
                                         ('settle', 'Card / UPI / Fleet settlement credited to bank')],
                                        default='deposit')
    settle_account_id = fields.Many2one('pump.account', string='Settlement Account',
                                        domain=[('account_type', '=', 'card')])
    charges = fields.Float('Bank / MDR Charges', digits=(16, 2))
    shift_id = fields.Many2one('pump.shift')
    narration = fields.Char()
    partner_currency_id = fields.Many2one(related='partner_id.pump_currency_id')
    partner_balance = fields.Monetary(related='partner_id.pump_balance', string='Current Outstanding',
                                      currency_field='partner_currency_id')

    @api.onchange('via')
    def _onchange_via(self):
        if self.via == 'cash':
            self.payment_mode = 'cash'

    def action_confirm(self):
        self.ensure_one()
        if self.amount <= 0:
            raise UserError(_('Amount must be positive.'))
        ref = lambda x: self.env.ref('zivoit_fuel_management.%s' % x)
        money = ref('acc_cash' if self.via == 'cash' else 'acc_bank')
        partner = self.partner_id.commercial_partner_id if self.partner_id else False
        label = self.narration or ''
        if self.voucher_type == 'receipt':
            if not partner:
                raise UserError(_('Select the customer.'))
            receivable = ref('acc_debtors') if partner.is_pump_customer else (self.account_id or ref('acc_commission'))
            label = label or _('Receipt %(m)s %(c)s', m=dict(self._fields['payment_mode'].selection).get(self.payment_mode), c=self.cheque_no or '')
            lines = [{'account_id': money.id, 'debit': self.amount, 'name': label},
                     {'account_id': receivable.id, 'credit': self.amount, 'name': label,
                      'partner_id': partner.id if receivable.account_type in ('receivable', 'payable') else False}]
        elif self.voucher_type == 'payment':
            target = self.account_id or (ref('acc_creditors') if partner else False)
            if not target:
                raise UserError(_('Select the expense / ledger account.'))
            label = label or _('Payment - %s', target.name)
            lines = [{'account_id': target.id, 'debit': self.amount, 'name': label,
                      'partner_id': partner.id if partner and target.account_type in ('receivable', 'payable') else False},
                     {'account_id': money.id, 'credit': self.amount, 'name': label}]
        elif self.contra_direction == 'settle':
            if not self.settle_account_id:
                raise UserError(_('Select the card / UPI settlement account.'))
            if self.charges < 0 or self.charges >= self.amount:
                raise UserError(_('Charges must be between 0 and the settled amount.'))
            label = label or _('Settlement - %s', self.settle_account_id.name)
            lines = [{'account_id': ref('acc_bank').id, 'debit': self.amount - self.charges, 'name': label},
                     {'account_id': ref('acc_bank_charges').id, 'debit': self.charges, 'name': _('MDR / bank charges')},
                     {'account_id': self.settle_account_id.id, 'credit': self.amount, 'name': label}]
        else:
            cash, bank = ref('acc_cash'), ref('acc_bank')
            amt_dr, amt_cr = (bank, cash) if self.contra_direction == 'deposit' else (cash, bank)
            label = label or dict(self._fields['contra_direction'].selection)[self.contra_direction]
            lines = [{'account_id': amt_dr.id, 'debit': self.amount, 'name': label},
                     {'account_id': amt_cr.id, 'credit': self.amount, 'name': label}]
        voucher = self.env['pump.voucher']._create_posted(
            lines, voucher_type=self.voucher_type, date=self.date, partner_id=partner.id if partner else False,
            payment_mode=self.payment_mode, cheque_no=self.cheque_no, narration=self.narration,
            shift_id=self.shift_id.id)
        return {'type': 'ir.actions.act_window', 'res_model': 'pump.voucher', 'res_id': voucher.id,
                'view_mode': 'form', 'target': 'current'}


class PumpBillGenerateWizard(models.TransientModel):
    _name = 'pump.bill.generate.wizard'
    _description = 'Generate Period Bills'

    date_from = fields.Date(required=True, default=lambda s: _first_of_month(fields.Date.context_today(s)))
    date_to = fields.Date(required=True, default=fields.Date.context_today)
    bill_date = fields.Date(default=fields.Date.context_today)
    partner_ids = fields.Many2many('res.partner', string='Customers', domain=[('is_pump_customer', '=', True)],
                                   help='Leave empty to bill every customer with unbilled slips.')
    post = fields.Boolean('Post Immediately', default=True)
    preview_count = fields.Integer(compute='_compute_preview', string='Unbilled Slips')
    preview_amount = fields.Float(compute='_compute_preview', string='Unbilled Amount')

    @api.depends('date_from', 'date_to', 'partner_ids')
    def _compute_preview(self):
        for w in self:
            dom = [('state', '=', 'confirmed'), ('bill_id', '=', False),
                   ('date', '>=', w.date_from), ('date', '<=', w.date_to)]
            if w.partner_ids:
                dom.append(('partner_id', 'in', w.partner_ids.ids))
            slips = self.env['pump.credit.sale'].search(dom) if w.date_from and w.date_to else self.env['pump.credit.sale']
            w.preview_count = len(slips)
            w.preview_amount = sum(slips.mapped('amount'))

    def action_generate(self):
        self.ensure_one()
        if self.date_from > self.date_to:
            raise ValidationError(_('From date must be before To date.'))
        bills = self.env['pump.bill'].generate_bills(self.date_from, self.date_to, self.partner_ids or None,
                                                     self.bill_date, post=self.post)
        if not bills:
            raise UserError(_('No unbilled filling slips found for the selected period.'))
        return {'type': 'ir.actions.act_window', 'name': _('Generated Bills'), 'res_model': 'pump.bill',
                'view_mode': 'list,form', 'domain': [('id', 'in', bills.ids)]}


class PumpTrialBalanceWizard(models.TransientModel):
    _name = 'pump.trial.balance.wizard'
    _description = 'Trial Balance'

    date_from = fields.Date(required=True, default=lambda s: fields.Date.context_today(s).replace(
        month=4, day=1) if fields.Date.context_today(s).month >= 4 else fields.Date.context_today(s).replace(
        year=fields.Date.context_today(s).year - 1, month=4, day=1))
    date_to = fields.Date(required=True, default=fields.Date.context_today)

    @api.model
    def compute_rows(self, date_from, date_to):
        Line = self.env['pump.voucher.line']
        rows = []
        for acc in self.env['pump.account'].search([]):
            base = [('account_id', '=', acc.id), ('state', '=', 'posted')]
            opening = sum(Line.search(base + [('date', '<', date_from)]).mapped('balance'))
            period = Line.search(base + [('date', '>=', date_from), ('date', '<=', date_to)])
            amt_dr, amt_cr = sum(period.mapped('debit')), sum(period.mapped('credit'))
            closing = opening + amt_dr - amt_cr
            if any(abs(x) > 0.004 for x in (opening, amt_dr, amt_cr)):
                rows.append({'code': acc.code, 'name': acc.name,
                             'type': dict(acc._fields['account_type'].selection)[acc.account_type],
                             'opening': opening, 'debit': amt_dr, 'credit': amt_cr, 'closing': closing})
        return rows

    def action_pdf(self):
        return self.env.ref('zivoit_fuel_management.action_report_trial_balance').report_action(
            self, data={'date_from': str(self.date_from), 'date_to': str(self.date_to)})


class PumpPortalAccessWizard(models.TransientModel):
    _name = 'pump.portal.access.wizard'
    _description = 'Grant Customer Portal Login'

    partner_id = fields.Many2one('res.partner', required=True)
    login = fields.Char('Login (email / mobile / code)', required=True)
    password = fields.Char(required=True)
    existing_user_id = fields.Many2one('res.users', compute='_compute_existing')

    @api.depends('partner_id')
    def _compute_existing(self):
        for w in self:
            w.existing_user_id = self.env['res.users'].with_context(active_test=False).search(
                [('partner_id', '=', w.partner_id.id)], limit=1)

    @api.onchange('partner_id')
    def _onchange_partner(self):
        if self.partner_id and not self.login:
            self.login = self.partner_id.email or (self.partner_id.pump_code or '').lower()

    def action_grant(self):
        self.ensure_one()
        if len(self.password) < 6:
            raise UserError(_('Use a password of at least 6 characters.'))
        portal = self.env.ref('base.group_portal')
        user = self.existing_user_id
        if user:
            if not user._is_portal() and not user._is_internal():
                user.group_ids = [(4, portal.id)]
            user.sudo().write({'login': self.login, 'password': self.password, 'active': True})
        else:
            user = self.env['res.users'].with_context(no_reset_password=True).sudo().create({
                'name': self.partner_id.name, 'login': self.login, 'password': self.password,
                'partner_id': self.partner_id.id, 'group_ids': [(6, 0, [portal.id])],
            })
        self.partner_id.message_post(body=_('Customer portal login granted: %s', self.login))
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': _('Portal access ready'), 'type': 'success', 'sticky': True,
                           'message': _('Login: %(l)s — share the password securely with the customer. '
                                        'Portal URL: /my/fuel', l=user.login),
                           'next': {'type': 'ir.actions.act_window_close'}}}


class PumpOpeningWizard(models.TransientModel):
    _name = 'pump.opening.wizard'
    _description = 'Customer Opening Balance'

    partner_id = fields.Many2one('res.partner', required=True)
    date = fields.Date(required=True, default=fields.Date.context_today)
    amount = fields.Float('Opening Balance (Dr +, Cr -)', digits=(16, 2), required=True)

    def action_confirm(self):
        self.ensure_one()
        ref = lambda x: self.env.ref('zivoit_fuel_management.%s' % x)
        amt = self.amount
        partner = self.partner_id.commercial_partner_id
        self.env['pump.voucher']._create_posted([
            {'account_id': ref('acc_debtors').id, 'partner_id': partner.id,
             'debit': amt if amt > 0 else 0.0, 'credit': -amt if amt < 0 else 0.0, 'name': _('Opening Balance')},
            {'account_id': ref('acc_opening').id, 'credit': amt if amt > 0 else 0.0,
             'debit': -amt if amt < 0 else 0.0, 'name': _('Opening Balance')},
        ], voucher_type='opening', date=self.date, partner_id=partner.id, narration=_('Opening balance'))
        return {'type': 'ir.actions.act_window_close'}


class PumpRateUpdateWizard(models.TransientModel):
    _name = 'pump.rate.update.wizard'
    _description = 'Daily Rate Update'

    date = fields.Date('Effective Date', required=True, default=fields.Date.context_today)
    line_ids = fields.One2many('pump.rate.update.line', 'wizard_id')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        res['line_ids'] = [(0, 0, {'product_id': p.id, 'old_rate': p.current_rate, 'new_rate': p.current_rate})
                           for p in self.env['pump.product'].search([])]
        return res

    def action_apply(self):
        Rate = self.env['pump.rate']
        for l in self.line_ids.filtered(lambda x: x.new_rate > 0):
            existing = Rate.search([('product_id', '=', l.product_id.id), ('date', '=', self.date)])
            if existing:
                existing.rate = l.new_rate
            elif round(l.new_rate - l.old_rate, 2) or not l.old_rate:
                Rate.create({'product_id': l.product_id.id, 'date': self.date, 'rate': l.new_rate})
        return {'type': 'ir.actions.act_window_close'}


class PumpRateUpdateLine(models.TransientModel):
    _name = 'pump.rate.update.line'
    _description = 'Daily Rate Update Line'

    wizard_id = fields.Many2one('pump.rate.update.wizard', ondelete='cascade')
    product_id = fields.Many2one('pump.product', required=True)
    old_rate = fields.Float('Current Rate', digits=(16, 2), readonly=True)
    new_rate = fields.Float('New Rate', digits=(16, 2))
