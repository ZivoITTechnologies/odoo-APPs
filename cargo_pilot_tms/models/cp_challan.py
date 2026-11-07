# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class CpChallan(models.Model):
    _name = 'cp.challan'
    _description = 'Loading Challan / Manifest'
    _inherit = ['mail.thread']
    _order = 'date desc, id desc'

    name = fields.Char(default=lambda self: _('New'), copy=False, readonly=True)
    date = fields.Datetime(default=fields.Datetime.now, required=True)
    trip_id = fields.Many2one('cp.trip', string='Trip')
    vehicle_number = fields.Char(related='trip_id.vehicle_number')
    from_hub_id = fields.Many2one('cp.location', string='From Hub', required=True)
    to_hub_id = fields.Many2one('cp.location', string='To Hub', required=True)
    consignment_ids = fields.Many2many('cp.consignment', 'cp_challan_consignment_rel', 'challan_id',
                                       'consignment_id', string='Consignments',
                                       domain="[('state', 'in', ('booked', 'at_hub')), ('current_hub_id', '=', from_hub_id)]")
    total_packages = fields.Integer(compute='_compute_totals')
    total_weight_kg = fields.Float(compute='_compute_totals', string='Total Weight (kg)')
    total_freight = fields.Monetary(compute='_compute_totals')
    is_last_mile = fields.Boolean(string='Last-Mile Delivery Run',
                                  help='Consignments are delivered to consignees instead of a hub.')
    state = fields.Selection([('draft', 'Draft'), ('dispatched', 'Dispatched'), ('received', 'Received'),
                              ('cancel', 'Cancelled')], default='draft', tracking=True)
    received_time = fields.Datetime(readonly=True)
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('cp.challan') or _('New')
        return super().create(vals_list)

    @api.depends('consignment_ids.total_packages', 'consignment_ids.actual_weight_kg',
                 'consignment_ids.total_amount')
    def _compute_totals(self):
        for rec in self:
            rec.total_packages = sum(rec.consignment_ids.mapped('total_packages'))
            rec.total_weight_kg = sum(rec.consignment_ids.mapped('actual_weight_kg'))
            rec.total_freight = sum(rec.consignment_ids.mapped('total_amount'))

    def action_load_pending(self):
        """Pull every consignment waiting at the from-hub for the to-hub destination."""
        for rec in self:
            pending = self.env['cp.consignment'].search([
                ('state', 'in', ('booked', 'at_hub')), ('current_hub_id', '=', rec.from_hub_id.id),
                ('id', 'not in', rec.consignment_ids.ids),
            ])
            if not rec.is_last_mile:
                pending = pending.filtered(lambda c: c.to_location_id == rec.to_hub_id) or pending
            rec.consignment_ids = [(4, c.id) for c in pending]

    def action_dispatch(self):
        for rec in self:
            if not rec.consignment_ids:
                raise UserError(_('Add consignments to %s.', rec.name))
            rec.consignment_ids.write({'state': 'in_transit', 'current_hub_id': False})
            if rec.trip_id:
                rec.consignment_ids.filtered(lambda c: not c.trip_id).write({'trip_id': rec.trip_id.id})
            rec.state = 'dispatched'

    def action_receive(self):
        for rec in self:
            if rec.state != 'dispatched':
                raise UserError(_('Only dispatched challans can be received.'))
            if rec.is_last_mile:
                rec.consignment_ids.action_mark_delivered()
            else:
                arriving = rec.consignment_ids
                final = arriving.filtered(lambda c: c.to_location_id == rec.to_hub_id)
                (arriving - final).write({'state': 'at_hub', 'current_hub_id': rec.to_hub_id.id})
                final.write({'state': 'at_hub', 'current_hub_id': rec.to_hub_id.id})
            rec.write({'state': 'received', 'received_time': fields.Datetime.now()})

    def action_cancel(self):
        for rec in self:
            if rec.state == 'received':
                raise UserError(_('Received challans cannot be cancelled.'))
            if rec.state == 'dispatched':
                rec.consignment_ids.write({'state': 'at_hub', 'current_hub_id': rec.from_hub_id.id})
        self.write({'state': 'cancel'})
