# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class CpDock(models.Model):
    _name = 'cp.dock'
    _description = 'Loading Dock / Bay'
    _order = 'location_id, sequence, name'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    location_id = fields.Many2one('cp.location', required=True, ondelete='cascade')
    dock_type = fields.Selection([('loading', 'Loading'), ('unloading', 'Unloading'), ('both', 'Both')],
                                 default='both', required=True)
    active = fields.Boolean(default=True)
    occupied = fields.Boolean(compute='_compute_occupied')

    def _compute_occupied(self):
        busy = self.env['cp.gate.entry'].search([('dock_id', 'in', self.ids), ('state', 'in', ('at_dock',))])
        for dock in self:
            dock.occupied = dock in busy.mapped('dock_id')


class CpGateEntry(models.Model):
    _name = 'cp.gate.entry'
    _description = 'Gate & Yard Entry'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(default=lambda self: _('New'), copy=False, readonly=True)
    location_id = fields.Many2one('cp.location', required=True, string='Plant / Site')
    trip_id = fields.Many2one('cp.trip')
    vehicle_number = fields.Char(required=True)
    driver_name = fields.Char()
    rfid_tag = fields.Char(string='RFID / FASTag ID')
    direction = fields.Selection([('outbound', 'Outbound (Loading)'), ('inbound', 'Inbound (Unloading)')],
                                 default='outbound', required=True)
    state = fields.Selection([
        ('expected', 'Expected'), ('gate_in', 'Gate In'), ('at_dock', 'At Dock'),
        ('weighed', 'Weighed'), ('gate_out', 'Gate Out'), ('cancel', 'Cancelled'),
    ], default='expected', tracking=True)
    slot_start = fields.Datetime(string='Dock Slot Start')
    slot_end = fields.Datetime(string='Dock Slot End')
    dock_id = fields.Many2one('cp.dock', domain="[('location_id', '=', location_id)]")
    gate_in_time = fields.Datetime(readonly=True)
    dock_in_time = fields.Datetime(readonly=True)
    dock_out_time = fields.Datetime(readonly=True)
    gate_out_time = fields.Datetime(readonly=True)
    tare_weight_kg = fields.Float(string='Tare (kg)')
    gross_weight_kg = fields.Float(string='Gross (kg)')
    net_weight_kg = fields.Float(compute='_compute_net', store=True, string='Net (kg)')
    turnaround_min = fields.Integer(compute='_compute_tat', store=True, string='Turnaround (min)')
    dock_min = fields.Integer(compute='_compute_tat', store=True, string='At Dock (min)')
    checklist_ok = fields.Boolean(string='Safety / Document Check Passed')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('cp.gate.entry') or _('New')
            if vals.get('trip_id') and not vals.get('vehicle_number'):
                vals['vehicle_number'] = self.env['cp.trip'].browse(vals['trip_id']).vehicle_number
        return super().create(vals_list)

    @api.onchange('trip_id')
    def _onchange_trip(self):
        if self.trip_id:
            self.vehicle_number = self.trip_id.vehicle_number
            self.driver_name = self.trip_id.driver_name
            self.location_id = self.trip_id.origin_id if self.direction == 'outbound' else self.trip_id.destination_id

    @api.depends('tare_weight_kg', 'gross_weight_kg')
    def _compute_net(self):
        for rec in self:
            rec.net_weight_kg = max(rec.gross_weight_kg - rec.tare_weight_kg, 0.0) if rec.gross_weight_kg else 0.0

    @api.depends('gate_in_time', 'gate_out_time', 'dock_in_time', 'dock_out_time')
    def _compute_tat(self):
        for rec in self:
            rec.turnaround_min = int((rec.gate_out_time - rec.gate_in_time).total_seconds() // 60) \
                if rec.gate_in_time and rec.gate_out_time else 0
            rec.dock_min = int((rec.dock_out_time - rec.dock_in_time).total_seconds() // 60) \
                if rec.dock_in_time and rec.dock_out_time else 0

    @api.constrains('dock_id', 'slot_start', 'slot_end')
    def _check_slot(self):
        for rec in self:
            if not (rec.dock_id and rec.slot_start and rec.slot_end):
                continue
            if rec.slot_end <= rec.slot_start:
                raise ValidationError(_('The dock slot must end after it starts.'))
            clash = self.search([
                ('id', '!=', rec.id), ('dock_id', '=', rec.dock_id.id), ('state', '!=', 'cancel'),
                ('slot_start', '<', rec.slot_end), ('slot_end', '>', rec.slot_start),
            ], limit=1)
            if clash:
                raise ValidationError(_('%(dock)s is already booked by %(other)s in that slot.',
                                        dock=rec.dock_id.name, other=clash.vehicle_number))

    def action_gate_in(self):
        for rec in self:
            if rec.state != 'expected':
                raise UserError(_('%s is already inside.', rec.vehicle_number))
            rec.write({'state': 'gate_in', 'gate_in_time': fields.Datetime.now()})
            if rec.trip_id.state == 'assigned':
                rec.trip_id.action_report()

    def action_to_dock(self):
        for rec in self:
            if not rec.dock_id:
                raise UserError(_('Choose a dock for %s.', rec.vehicle_number))
            if rec.dock_id.occupied:
                raise UserError(_('%s is occupied.', rec.dock_id.name))
            rec.write({'state': 'at_dock', 'dock_in_time': fields.Datetime.now()})
            if rec.direction == 'outbound' and rec.trip_id.state in ('assigned', 'reported'):
                rec.trip_id.action_start_loading()

    def action_weigh(self):
        for rec in self:
            if not (rec.tare_weight_kg and rec.gross_weight_kg):
                raise UserError(_('Record tare and gross weight from the weighbridge.'))
            rec.write({'state': 'weighed', 'dock_out_time': rec.dock_out_time or fields.Datetime.now()})
            if rec.trip_id and rec.direction == 'outbound':
                rec.trip_id.loaded_weight_ton = rec.net_weight_kg / 1000.0

    def action_gate_out(self):
        for rec in self:
            rec.write({'state': 'gate_out', 'gate_out_time': fields.Datetime.now(),
                       'dock_out_time': rec.dock_out_time or fields.Datetime.now()})
            if rec.trip_id and rec.direction == 'outbound' and rec.trip_id.state in ('assigned', 'reported', 'loading'):
                rec.trip_id.action_dispatch()

    def action_cancel(self):
        self.write({'state': 'cancel'})
