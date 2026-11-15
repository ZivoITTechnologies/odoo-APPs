# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import ValidationError


class PumpProduct(models.Model):
    _name = 'pump.product'
    _description = 'Fuel / Lubricant Product'
    _order = 'sequence, name'

    name = fields.Char(required=True)
    code = fields.Char()
    sequence = fields.Integer(default=10)
    product_type = fields.Selection([('fuel', 'Fuel (dispensed via nozzle)'), ('lube', 'Lubricant / Packed')],
                                    default='fuel', required=True)
    uom = fields.Char('Unit', default='Ltr')
    hsn_code = fields.Char('HSN Code')
    tax_note = fields.Char('Tax', help='Fuel is outside GST (VAT applicable); lubricants carry GST.')
    color = fields.Char('Colour', default='#0b5fa5', help='Hex colour used on dashboards.')
    active = fields.Boolean(default=True)
    income_account_id = fields.Many2one('pump.account', string='Sales Account', required=True,
                                        domain=[('account_type', '=', 'income')])
    purchase_account_id = fields.Many2one('pump.account', string='Purchase Account', required=True,
                                          domain=[('account_type', '=', 'expense')])
    rate_ids = fields.One2many('pump.rate', 'product_id', string='Rate History')
    currency_id = fields.Many2one('res.currency', default=lambda s: s.env.company.currency_id)
    current_rate = fields.Monetary(compute='_compute_current_rate', currency_field='currency_id',
                                   string='Current Rate')
    rate_date = fields.Date(compute='_compute_current_rate', string='Rate Effective From')
    tank_ids = fields.One2many('pump.tank', 'product_id')
    stock_qty = fields.Float(compute='_compute_stock_qty', string='Stock', digits=(16, 2))

    def _compute_current_rate(self):
        today = fields.Date.context_today(self)
        for p in self:
            rate = p._get_rate_record(today)
            p.current_rate = rate.rate if rate else 0.0
            p.rate_date = rate.date if rate else False

    def _compute_stock_qty(self):
        data = self.env['pump.stock.move']._read_group(
            [('product_id', 'in', self.ids)], ['product_id'], ['qty:sum'])
        res = {p.id: q for p, q in data}
        for p in self:
            p.stock_qty = res.get(p.id, 0.0)

    def _get_rate_record(self, date):
        self.ensure_one()
        return self.env['pump.rate'].search(
            [('product_id', '=', self.id), ('date', '<=', date)], order='date desc, id desc', limit=1)

    def get_rate(self, date=None):
        self.ensure_one()
        rate = self._get_rate_record(date or fields.Date.context_today(self))
        return rate.rate if rate else 0.0


class PumpRate(models.Model):
    _name = 'pump.rate'
    _description = 'Sale Rate (daily price update)'
    _order = 'date desc, id desc'
    _rec_name = 'product_id'

    product_id = fields.Many2one('pump.product', required=True, ondelete='cascade', index=True)
    date = fields.Date('Effective Date', required=True, default=fields.Date.context_today)
    rate = fields.Float('Rate / Unit', required=True, digits=(16, 2))
    previous_rate = fields.Float(compute='_compute_previous', digits=(16, 2))
    change = fields.Float(compute='_compute_previous', digits=(16, 2))
    note = fields.Char()

    _product_date_uniq = models.Constraint('UNIQUE(product_id, date)',
                                           'Only one rate per product per day. Edit the existing rate instead.')

    @api.depends('product_id', 'date', 'rate')
    def _compute_previous(self):
        for r in self:
            prev = self.search([('product_id', '=', r.product_id.id), ('date', '<', r.date)],
                               order='date desc', limit=1) if r.product_id and r.date else False
            r.previous_rate = prev.rate if prev else 0.0
            r.change = (r.rate - prev.rate) if prev else 0.0

    @api.constrains('rate')
    def _check_rate(self):
        for r in self:
            if r.rate <= 0:
                raise ValidationError(self.env._('Rate must be positive.'))
