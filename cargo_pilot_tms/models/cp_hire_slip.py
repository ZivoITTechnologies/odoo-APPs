# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class CpHireSlip(models.Model):
    _name = 'cp.hire.slip'
    _description = 'Market Vehicle Hire Slip'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc, id desc'

    name = fields.Char(default=lambda self: _('New'), copy=False, readonly=True)
    date = fields.Date(default=fields.Date.context_today, required=True)
    trip_id = fields.Many2one('cp.trip', required=True, ondelete='restrict')
    lane_id = fields.Many2one(related='trip_id.lane_id')
    owner_id = fields.Many2one('res.partner', string='Owner / Broker', required=True,
                               domain=[('cp_is_vehicle_owner', '=', True)])
    vehicle_number = fields.Char(required=True)
    owner_pan = fields.Char(string='Owner PAN / Tax ID')
    hire_amount = fields.Monetary(required=True, tracking=True)
    detention_amount = fields.Monetary()
    extra_amount = fields.Monetary(string='Extra Charges')
    deduction_amount = fields.Monetary(string='Deductions', help='Shortage, damage, late delivery, TDS.')
    deduction_note = fields.Char()
    total_hire = fields.Monetary(compute='_compute_amounts', store=True, string='Net Hire')
    payment_ids = fields.One2many('cp.hire.slip.payment', 'hire_slip_id', string='Payments')
    paid_amount = fields.Monetary(compute='_compute_amounts', store=True)
    balance_due = fields.Monetary(compute='_compute_amounts', store=True)
    state = fields.Selection([('draft', 'Draft'), ('confirmed', 'Confirmed'), ('settled', 'Settled'),
                              ('cancel', 'Cancelled')], default='draft', tracking=True)
    move_id = fields.Many2one('account.move', string='Vendor Bill', readonly=True, copy=False)
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('cp.hire.slip') or _('New')
            if vals.get('trip_id') and not vals.get('vehicle_number'):
                vals['vehicle_number'] = self.env['cp.trip'].browse(vals['trip_id']).vehicle_number
        slips = super().create(vals_list)
        slips.mapped('trip_id').write({'is_market_vehicle': True})
        return slips

    @api.onchange('trip_id')
    def _onchange_trip(self):
        if self.trip_id and not self.vehicle_number:
            self.vehicle_number = self.trip_id.vehicle_number

    @api.depends('hire_amount', 'detention_amount', 'extra_amount', 'deduction_amount', 'payment_ids.amount')
    def _compute_amounts(self):
        for rec in self:
            rec.total_hire = rec.hire_amount + rec.detention_amount + rec.extra_amount - rec.deduction_amount
            rec.paid_amount = sum(rec.payment_ids.mapped('amount'))
            rec.balance_due = rec.total_hire - rec.paid_amount

    def action_confirm(self):
        self.write({'state': 'confirmed'})

    def action_settle(self):
        for rec in self:
            if rec.balance_due > 0.01:
                raise UserError(_('%(slip)s still has %(due)s to pay.', slip=rec.name, due=rec.balance_due))
            rec.state = 'settled'

    def action_cancel(self):
        self.write({'state': 'cancel'})

    def action_create_bill(self):
        product = self.env.ref('cargo_pilot_tms.product_cp_vehicle_hire', raise_if_not_found=False)
        for rec in self:
            if rec.move_id:
                continue
            rec.move_id = self.env['account.move'].create({
                'move_type': 'in_invoice',
                'partner_id': rec.owner_id.id,
                'invoice_date': rec.date,
                'ref': rec.name,
                'invoice_origin': rec.trip_id.name,
                'invoice_line_ids': [(0, 0, {
                    'product_id': product.id if product else False,
                    'name': _('Vehicle hire %(v)s, %(lane)s', v=rec.vehicle_number, lane=rec.lane_id.name),
                    'quantity': 1.0,
                    'price_unit': rec.total_hire,
                })],
            })
        return True


class CpHireSlipPayment(models.Model):
    _name = 'cp.hire.slip.payment'
    _description = 'Hire Slip Payment'
    _order = 'date, id'

    hire_slip_id = fields.Many2one('cp.hire.slip', required=True, ondelete='cascade')
    date = fields.Date(default=fields.Date.context_today, required=True)
    payment_type = fields.Selection([('advance', 'Advance'), ('fuel', 'Fuel Advance'), ('balance', 'Balance')],
                                    default='advance', required=True)
    amount = fields.Monetary(required=True)
    payment_mode = fields.Selection([('cash', 'Cash'), ('bank', 'Bank / UPI'), ('fuel_card', 'Fuel Card')],
                                    default='bank')
    reference = fields.Char()
    currency_id = fields.Many2one(related='hire_slip_id.currency_id')
