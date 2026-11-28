# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .zivo_track_device import expense_currency


class ZivoExpenseCategory(models.Model):
    _name = 'zivo.expense.category'
    _description = 'Field Expense Category'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    code = fields.Char(help='Technical code. TA and DA are reserved for the automatic allowances.')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', help='Leave empty to share the category between companies.')
    currency_id = fields.Many2one('res.currency', compute='_compute_currency_id')
    line_type = fields.Selection([
        ('expense', 'Expense with bill'),
        ('travel', 'Travel allowance (mileage)'),
        ('daily', 'Daily allowance'),
    ], required=True, default='expense',
        help='Travel and daily allowance categories are filled automatically from the GPS day logs.')
    product_id = fields.Many2one('product.product', string='Expense Product',
                                 domain=[('can_be_expensed', '=', True)],
                                 help='Product used when the approved claim is sent to Odoo Expenses '
                                      '(defines the expense account).')
    icon = fields.Char(default='💰', help='Emoji shown on the phone app.')
    receipt_policy = fields.Selection([
        ('always', 'Always required'),
        ('above', 'Required above an amount'),
        ('never', 'Not required'),
    ], default='above', required=True)
    receipt_threshold = fields.Monetary(string='Receipt Required Above', currency_field='currency_id', default=100)
    daily_limit = fields.Monetary(string='Daily Limit', currency_field='currency_id',
                                  help='Total per salesperson and day above which the expenses are flagged '
                                       '(0 = no limit).')
    conflicts_with_mileage = fields.Boolean(
        string='Flag on Mileage Days',
        help='Flag this expense when the salesperson is also paid a mileage allowance for the same day '
             '(e.g. fuel bills for an own vehicle).')

    @api.depends('company_id')
    def _compute_currency_id(self):
        for category in self:
            category.currency_id = expense_currency(self.env, category.company_id or self.env.company)

    @api.constrains('receipt_threshold', 'daily_limit')
    def _check_amounts(self):
        for category in self:
            if category.receipt_threshold < 0 or category.daily_limit < 0:
                raise ValidationError(_('Limits cannot be negative.'))

    def _receipt_required(self, amount):
        self.ensure_one()
        if self.line_type != 'expense' or self.receipt_policy == 'never':
            return False
        if self.receipt_policy == 'always':
            return True
        return amount > self.receipt_threshold

    @api.model
    def _system_category(self, line_type):
        return self.with_context(active_test=False).search([('line_type', '=', line_type)], limit=1)
