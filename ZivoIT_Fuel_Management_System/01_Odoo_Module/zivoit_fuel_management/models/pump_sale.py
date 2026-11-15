# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

from .pump_account import PARTNER_BALANCE_FIELDS


class PumpCreditSale(models.Model):
    _name = 'pump.credit.sale'
    _description = 'Credit Filling Slip'
    _inherit = ['mail.thread']
    _order = 'date desc, id desc'

    name = fields.Char('Slip Ref', readonly=True, copy=False, default='/')
    slip_no = fields.Char('Book Slip No.', help='Number printed on the physical slip book.')
    date = fields.Date(required=True, default=fields.Date.context_today, tracking=True)
    shift_id = fields.Many2one('pump.shift')
    partner_id = fields.Many2one('res.partner', string='Customer', required=True, tracking=True,
                                 domain=[('is_pump_customer', '=', True)])
    vehicle_id = fields.Many2one('pump.vehicle', domain="[('partner_id', '=', partner_id)]")
    vehicle_no = fields.Char('Vehicle No.', help='Free text if the vehicle is not registered.')
    product_id = fields.Many2one('pump.product', required=True)
    qty = fields.Float('Qty', digits=(16, 2), required=True)
    rate = fields.Float(digits=(16, 2), required=True)
    discount = fields.Float('Discount / Unit', digits=(16, 2))
    amount = fields.Monetary(compute='_compute_amount', store=True, currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', default=lambda s: s.env.company.currency_id)
    employee_id = fields.Many2one('hr.employee', string='Filled By')
    odometer = fields.Float('Odometer (km)', help='Vehicle odometer at filling — enables km/L tracking.')
    prev_odometer = fields.Float(compute='_compute_kmpl', store=True)
    kmpl = fields.Float('km / Ltr', compute='_compute_kmpl', store=True, digits=(16, 2))
    source = fields.Selection([('nozzle', 'At Station (nozzle)'), ('delivery', 'Doorstep Delivery')],
                              default='nozzle', required=True)
    delivery_trip_id = fields.Many2one('pump.tanker.trip', readonly=True, copy=False)
    ai_flag = fields.Char('AI Flag', compute='_compute_ai_flag', help='Anomaly detected by the AI checks.')
    indent_id = fields.Many2one('pump.indent', readonly=True)
    bill_id = fields.Many2one('pump.bill', readonly=True, copy=False, index=True)
    note = fields.Char('Comment')
    state = fields.Selection([('draft', 'Draft'), ('confirmed', 'Confirmed (Unbilled)'),
                              ('billed', 'Billed'), ('cancel', 'Cancelled')],
                             default='draft', required=True, tracking=True, copy=False)

    @api.depends('qty', 'rate', 'discount')
    def _compute_amount(self):
        for s in self:
            s.amount = round(s.qty * (s.rate - s.discount), 2)

    @api.depends('odometer', 'vehicle_id', 'date', 'qty')
    def _compute_kmpl(self):
        for s in self:
            prev = False
            if s.vehicle_id and s.odometer and s.date:
                prev = self.search([('vehicle_id', '=', s.vehicle_id.id), ('odometer', '>', 0),
                                    ('odometer', '<', s.odometer), ('state', 'in', ('confirmed', 'billed')),
                                    ('id', '!=', s._origin.id or 0)], order='odometer desc', limit=1)
            s.prev_odometer = prev.odometer if prev else 0.0
            s.kmpl = round((s.odometer - prev.odometer) / prev.qty, 2) if prev and prev.qty else 0.0

    def _compute_ai_flag(self):
        flags = self.env['pump.ai']._slip_flags(self)
        for s in self:
            s.ai_flag = flags.get(s.id, '')

    @api.onchange('product_id', 'date')
    def _onchange_product(self):
        if self.product_id:
            self.rate = self.product_id.get_rate(self.date)

    @api.onchange('vehicle_id')
    def _onchange_vehicle(self):
        if self.vehicle_id:
            self.vehicle_no = self.vehicle_id.name
            if self.vehicle_id.product_id:
                self.product_id = self.vehicle_id.product_id

    @api.onchange('partner_id')
    def _onchange_partner(self):
        if self.vehicle_id and self.vehicle_id.partner_id != self.partner_id:
            self.vehicle_id = False

    @api.constrains('qty', 'rate')
    def _check_values(self):
        for s in self:
            if s.qty <= 0 or s.rate <= 0:
                raise ValidationError(_('Quantity and rate must be positive.'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('product_id') and not vals.get('rate'):
                vals['rate'] = self.env['pump.product'].browse(vals['product_id']).get_rate(
                    vals.get('date') or fields.Date.context_today(self))
            if vals.get('vehicle_id') and not vals.get('vehicle_no'):
                vals['vehicle_no'] = self.env['pump.vehicle'].browse(vals['vehicle_id']).name
        return super().create(vals_list)

    def action_confirm(self):
        for s in self.filtered(lambda x: x.state == 'draft'):
            if s.vehicle_id and s.vehicle_id.tank_capacity and s.product_id.product_type == 'fuel' \
                    and s.qty > s.vehicle_id.tank_capacity * 1.05 and not self.env.context.get('pump_override_limit'):
                raise UserError(_('%(q).2f L is more than the tank capacity of %(v)s (%(c).0f L). Check the slip.',
                                  q=s.qty, v=s.vehicle_id.name, c=s.vehicle_id.tank_capacity))
            s.partner_id._pump_check_credit(s.amount)
            if s.name in (False, '/'):
                s.name = self.env['ir.sequence'].next_by_code('pump.credit.sale') or '/'
            s.state = 'confirmed'
            if s.indent_id and s.indent_id.state == 'approved':
                s.indent_id.state = 'delivered'
        return True

    def action_cancel(self):
        if any(s.state == 'billed' for s in self):
            raise UserError(_('Billed slips cannot be cancelled. Cancel the bill first.'))
        self.write({'state': 'cancel'})

    def action_draft(self):
        self.filtered(lambda s: s.state == 'cancel').write({'state': 'draft'})

    def unlink(self):
        if any(s.state in ('confirmed', 'billed') for s in self):
            raise UserError(_('Confirmed slips cannot be deleted; cancel them instead.'))
        return super().unlink()

    def write(self, vals):
        locked = {'partner_id', 'product_id', 'qty', 'rate', 'discount', 'date'}
        if locked & set(vals) and any(s.state == 'billed' for s in self):
            raise UserError(_('Billed slips cannot be modified.'))
        res = super().write(vals)
        if {'state', 'amount', 'qty', 'rate', 'partner_id', 'discount'} & set(vals):
            self.env.flush_all()
            self.env['res.partner'].invalidate_model(PARTNER_BALANCE_FIELDS)
        return res

    def action_print(self):
        return self.env.ref('zivoit_fuel_management.action_report_slip').report_action(self)


class PumpIndent(models.Model):
    _name = 'pump.indent'
    _description = 'Fuel Indent (customer request)'
    _inherit = ['mail.thread']
    _order = 'date desc, id desc'

    name = fields.Char(readonly=True, default='/', copy=False)
    date = fields.Date(required=True, default=fields.Date.context_today)
    partner_id = fields.Many2one('res.partner', required=True, domain=[('is_pump_customer', '=', True)])
    vehicle_id = fields.Many2one('pump.vehicle', domain="[('partner_id', '=', partner_id)]")
    product_id = fields.Many2one('pump.product', required=True)
    indent_type = fields.Selection([('qty', 'Quantity'), ('amount', 'Amount'), ('full', 'Tank Full')],
                                   default='qty', required=True)
    qty = fields.Float(digits=(16, 2))
    amount = fields.Float(digits=(16, 2))
    comment = fields.Char()
    source = fields.Selection([('portal', 'Customer Portal'), ('office', 'Office')], default='office')
    state = fields.Selection([('requested', 'Requested'), ('approved', 'Approved'),
                              ('delivered', 'Delivered'), ('rejected', 'Rejected')],
                             default='requested', tracking=True)
    sale_ids = fields.One2many('pump.credit.sale', 'indent_id')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code('pump.indent') or '/'
        return super().create(vals_list)

    @api.constrains('indent_type', 'qty', 'amount')
    def _check_qty(self):
        for r in self:
            if r.indent_type == 'qty' and r.qty <= 0:
                raise ValidationError(_('Enter a quantity for the indent.'))
            if r.indent_type == 'amount' and r.amount <= 0:
                raise ValidationError(_('Enter an amount for the indent.'))

    def action_approve(self):
        self.filtered(lambda r: r.state == 'requested').write({'state': 'approved'})

    def action_reject(self):
        self.filtered(lambda r: r.state in ('requested', 'approved')).write({'state': 'rejected'})

    def action_create_slip(self):
        self.ensure_one()
        if self.state not in ('requested', 'approved'):
            raise UserError(_('Only open indents can be converted.'))
        rate = self.product_id.get_rate(fields.Date.context_today(self))
        qty = self.qty
        if self.indent_type == 'amount' and rate:
            qty = round(self.amount / rate, 2)
        elif self.indent_type == 'full':
            qty = self.vehicle_id.tank_capacity or 0.0
        self.state = 'approved'
        return {
            'type': 'ir.actions.act_window', 'res_model': 'pump.credit.sale', 'view_mode': 'form',
            'context': {'default_partner_id': self.partner_id.id, 'default_vehicle_id': self.vehicle_id.id,
                        'default_vehicle_no': self.vehicle_id.name, 'default_product_id': self.product_id.id,
                        'default_qty': qty, 'default_rate': rate, 'default_indent_id': self.id},
        }
