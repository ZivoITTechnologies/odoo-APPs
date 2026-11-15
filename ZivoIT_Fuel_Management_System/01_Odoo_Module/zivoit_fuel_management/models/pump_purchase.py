# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class PumpPurchase(models.Model):
    _name = 'pump.purchase'
    _description = 'Tanker Receipt / Purchase Invoice'
    _inherit = ['mail.thread']
    _order = 'date desc, id desc'

    name = fields.Char(readonly=True, default='/', copy=False)
    date = fields.Date(required=True, default=fields.Date.context_today)
    partner_id = fields.Many2one('res.partner', string='Supplier', required=True,
                                 default=lambda s: s.env.ref('zivoit_fuel_management.partner_bpcl_supplier',
                                                             raise_if_not_found=False))
    invoice_no = fields.Char('Supplier Invoice No.')
    truck_no = fields.Char('Tanker Lorry No.')
    tank_id = fields.Many2one('pump.tank', required=True)
    product_id = fields.Many2one(related='tank_id.product_id', store=True)
    qty = fields.Float('Invoice Qty (Ltr)', digits=(16, 2), required=True)
    received_qty = fields.Float('Received Qty (Ltr)', digits=(16, 2),
                                help='Quantity measured by DIP after decantation; defaults to invoice qty.')
    rate = fields.Float('Rate / Ltr (incl. tax)', digits=(16, 4), required=True)
    currency_id = fields.Many2one('res.currency', default=lambda s: s.env.company.currency_id)
    amount = fields.Monetary(compute='_compute_amount', store=True, string='Invoice Amount')
    paid_from = fields.Selection([('credit', 'On Credit (pay later)'), ('bank', 'Paid from Bank')],
                                 default='bank', required=True)
    voucher_id = fields.Many2one('pump.voucher', readonly=True, copy=False)
    state = fields.Selection([('draft', 'Draft'), ('posted', 'Posted'), ('cancel', 'Cancelled')],
                             default='draft', tracking=True, copy=False)

    @api.depends('qty', 'rate')
    def _compute_amount(self):
        for p in self:
            p.amount = round(p.qty * p.rate, 2)

    @api.constrains('qty', 'rate')
    def _check(self):
        for p in self:
            if p.qty <= 0 or p.rate <= 0:
                raise ValidationError(_('Quantity and rate must be positive.'))

    def action_post(self):
        for p in self.filtered(lambda x: x.state == 'draft'):
            recv = p.received_qty or p.qty
            if p.tank_id.capacity and p.tank_id.stock_qty + recv > p.tank_id.capacity * 1.02:
                raise UserError(_('Tank %(t)s has only %(u).0f Ltr ullage.', t=p.tank_id.name, u=p.tank_id.ullage))
            if p.name in (False, '/'):
                p.name = self.env['ir.sequence'].next_by_code('pump.purchase') or '/'
            self.env['pump.stock.move'].create({
                'date': p.date, 'tank_id': p.tank_id.id, 'qty': recv, 'move_type': 'purchase',
                'reference': '%s %s' % (p.name, p.invoice_no or ''), 'res_model': p._name, 'res_id': p.id})
            label = _('%(prod)s %(q).0f Ltr - Inv %(inv)s', prod=p.product_id.name, q=p.qty, inv=p.invoice_no or '')
            credit_acc = self.env.ref('zivoit_fuel_management.acc_bank') if p.paid_from == 'bank' \
                else self.env.ref('zivoit_fuel_management.acc_creditors')
            voucher = self.env['pump.voucher']._create_posted([
                {'account_id': p.product_id.purchase_account_id.id, 'debit': p.amount, 'name': label},
                {'account_id': credit_acc.id, 'credit': p.amount, 'name': label,
                 'partner_id': p.partner_id.id if p.paid_from == 'credit' else False},
            ], voucher_type='purchase', date=p.date, partner_id=p.partner_id.id, ref=p.invoice_no,
                narration=label, origin_model=p._name, origin_id=p.id)
            p.write({'voucher_id': voucher.id, 'state': 'posted'})
        return True

    def action_cancel(self):
        for p in self:
            if p.voucher_id:
                p.voucher_id.with_context(pump_from_origin=True).action_cancel()
            self.env['pump.stock.move'].search([('res_model', '=', p._name), ('res_id', '=', p.id)]).unlink()
            p.state = 'cancel'
        return True
