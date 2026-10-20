# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ZivoJobWorkCost(models.Model):
    _name = 'zivo.job.work.cost'
    _description = 'Job Work Additional Cost'
    _order = 'job_id, sequence, id'
    _check_company_auto = True

    job_id = fields.Many2one('zivo.job.work', 'Job Work', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(related='job_id.company_id', store=True)
    company_currency_id = fields.Many2one(related='company_id.currency_id', string='Company Currency')
    cost_type = fields.Selection([
        ('freight', 'Freight'),
        ('inspection', 'Inspection'),
        ('rework', 'Rework'),
        ('other', 'Other'),
    ], 'Cost Type', required=True, default='freight')
    name = fields.Char('Description', required=True)
    partner_id = fields.Many2one('res.partner', 'Supplier')
    currency_id = fields.Many2one('res.currency', 'Currency', required=True,
                                  default=lambda self: self.env.company.currency_id)
    expected_amount = fields.Monetary('Expected', currency_field='currency_id')
    actual_amount = fields.Monetary('Actual', currency_field='currency_id')
    expected_amount_company = fields.Monetary('Expected (Company Currency)', compute='_compute_company_amounts',
                                              currency_field='company_currency_id')
    actual_amount_company = fields.Monetary('Actual (Company Currency)', compute='_compute_company_amounts',
                                            currency_field='company_currency_id')
    bill_id = fields.Many2one('account.move', 'Vendor Bill',
                              domain="[('move_type', 'in', ('in_invoice', 'in_refund'))]")
    date = fields.Date(default=fields.Date.context_today)

    @api.depends('expected_amount', 'actual_amount', 'currency_id', 'date')
    def _compute_company_amounts(self):
        for line in self:
            job = line.job_id
            if not job:
                line.expected_amount_company = line.expected_amount
                line.actual_amount_company = line.actual_amount
                continue
            line.expected_amount_company = job._jw_to_company_currency(line.expected_amount, line.currency_id,
                                                                       line.date)
            line.actual_amount_company = job._jw_to_company_currency(line.actual_amount, line.currency_id, line.date)

    @api.onchange('bill_id')
    def _onchange_bill_id(self):
        if self.bill_id:
            self.partner_id = self.bill_id.partner_id
            self.currency_id = self.bill_id.currency_id
            self.actual_amount = self.bill_id.amount_untaxed
