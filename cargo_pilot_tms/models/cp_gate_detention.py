# -*- coding: utf-8 -*-
"""FR-05 gate & loading log and FR-06 detention / halting engine."""
import math

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

DELAY_REASONS = [
    ('goods_not_ready', 'Goods not ready (company)'),
    ('doc_pending', 'Documentation / e-way bill / billing delay (company)'),
    ('dock_busy', 'Loading dock bottleneck (company)'),
    ('destination', 'Destination-side delay (company / customer)'),
    ('transporter_fault', 'Transporter delay or breakdown (vendor - no detention)'),
]


class CpGateEntry(models.Model):
    _inherit = 'cp.gate.entry'

    dispatch_plan_id = fields.Many2one('cp.dispatch.plan', string='Dispatch Plan', index=True, ondelete='set null')
    driver_phone = fields.Char()
    driver_license = fields.Char(string='Driver Licence No.')
    eway_bill_no = fields.Char(string='E-way Bill No.')
    eway_required = fields.Boolean(related='dispatch_plan_id.eway_required')
    check_license = fields.Boolean(string='Driver licence checked')
    check_fitness = fields.Boolean(string='Vehicle fitness checked')
    check_clean = fields.Boolean(string='Clean and odour-free')
    check_seal = fields.Boolean(string='Container seal verified')
    seal_number = fields.Char()
    inspection_note = fields.Char()
    inspection_ok = fields.Boolean(compute='_compute_inspection_ok', store=True)
    reporting_cutoff = fields.Datetime(related='dispatch_plan_id.reporting_cutoff')
    reported_late = fields.Boolean(compute='_compute_reported_late', store=True)
    detention_id = fields.Many2one('cp.detention.record', readonly=True, copy=False)

    @api.depends('check_license', 'check_fitness', 'check_clean', 'check_seal')
    def _compute_inspection_ok(self):
        for rec in self:
            rec.inspection_ok = rec.check_license and rec.check_fitness and rec.check_clean and rec.check_seal

    @api.depends('gate_in_time', 'dispatch_plan_id.reporting_cutoff')
    def _compute_reported_late(self):
        for rec in self:
            cutoff = rec.dispatch_plan_id.reporting_cutoff
            rec.reported_late = bool(rec.gate_in_time and cutoff and rec.gate_in_time > cutoff)

    def _cp_gate_env(self):
        """Gate security may run the gate of a dispatch without rights on shipments or plans."""
        if self.filtered('dispatch_plan_id') and not self.env.su and \
                self.env.user.has_group('cargo_pilot_tms.group_dispatch_gate'):
            return self.sudo()
        return self

    def action_gate_in(self):
        self = self._cp_gate_env()
        for rec in self.filtered('dispatch_plan_id'):
            plan = rec.dispatch_plan_id
            if plan.state not in ('assigned', 'loading'):
                raise UserError(_('%s is not waiting for a vehicle.', plan.name))
            number = (rec.vehicle_number or '').strip()
            if not number or number == _('TBA'):
                raise UserError(_('Enter the vehicle registration number.'))
            if not (rec.driver_name and rec.driver_phone and rec.driver_license):
                raise UserError(_('Enter the driver name, phone and licence number at gate-in.'))
            trip = plan.trip_id.with_context(cp_from_plan=True)
            trip.sudo().write({'vehicle_number': number, 'driver_name': rec.driver_name,
                               'driver_phone': rec.driver_phone})
            if trip.state == 'draft':
                trip.sudo().action_assign()
            rec.trip_id = trip
        res = super().action_gate_in()
        for rec in self.filtered('dispatch_plan_id'):
            rec.dispatch_plan_id.message_post(body=_('Vehicle %(v)s gated in%(late)s.', v=rec.vehicle_number,
                                                     late=_(' after the reporting cut-off') if rec.reported_late
                                                     else ''))
        return res

    def action_to_dock(self):
        self = self._cp_gate_env()
        for rec in self.filtered('dispatch_plan_id'):
            if not rec.inspection_ok:
                raise UserError(_('Complete the vehicle inspection checklist before loading.'))
        undocked = self.filtered(lambda g: g.dispatch_plan_id and not g.dock_id)
        for rec in undocked:
            if rec.state != 'gate_in':
                raise UserError(_('Gate the vehicle in before loading.'))
            rec.write({'state': 'at_dock', 'dock_in_time': fields.Datetime.now()})
            if rec.trip_id.state in ('assigned', 'reported'):
                rec.trip_id.sudo().with_context(cp_from_plan=True).action_start_loading()
        res = super(CpGateEntry, self - undocked).action_to_dock() if self - undocked else True
        for rec in self.filtered('dispatch_plan_id'):
            if rec.dispatch_plan_id.state == 'assigned':
                rec.dispatch_plan_id.state = 'loading'
        return res

    def action_loading_done(self):
        self = self._cp_gate_env()
        """Loading completed (T2); weighbridge optional."""
        for rec in self:
            if rec.state != 'at_dock':
                raise UserError(_('%s is not at a dock.', rec.vehicle_number))
            rec.write({'state': 'weighed', 'dock_out_time': fields.Datetime.now()})
        return True

    def action_gate_out(self):
        self = self._cp_gate_env()
        for rec in self.filtered('dispatch_plan_id'):
            plan = rec.dispatch_plan_id
            if rec.state not in ('at_dock', 'weighed'):
                raise UserError(_('Load %s before gate-out.', rec.vehicle_number))
            if not rec.inspection_ok:
                raise UserError(_('Complete the vehicle inspection checklist before gate-out.'))
            eway = rec.eway_bill_no or plan.eway_bill_no
            if plan.eway_required and not eway:
                raise UserError(_('Attach the e-way bill number before releasing %s.', rec.vehicle_number))
            if eway:
                plan.eway_bill_no = eway
                rec.eway_bill_no = eway
                plan.trip_id.sudo().eway_bill_no = eway
        res = super().action_gate_out()
        for rec in self.filtered('dispatch_plan_id'):
            plan = rec.dispatch_plan_id
            plan.with_context(cp_from_plan=True).cp_mark_in_transit()
            rec._cp_make_detention()
        return res

    def _cp_make_detention(self):
        Detention = self.env['cp.detention.record'].sudo()
        for rec in self:
            plan = rec.dispatch_plan_id
            if not (plan and rec.gate_in_time and rec.gate_out_time) or rec.detention_id:
                continue
            draft = Detention.new({'plan_id': plan.id, 'gate_entry_id': rec.id})
            draft._cp_apply_terms()
            if draft.gross_waiting_hours <= 0:
                continue
            record = Detention.create({'plan_id': plan.id, 'gate_entry_id': rec.id})
            record._cp_apply_terms()
            rec.detention_id = record
            plan.message_post(body=_('Truck waited %(h).1f h beyond the %(f).1f free hours: detention claim %(d)s '
                                     'needs a delay reason.', h=record.gross_waiting_hours, f=record.free_hours,
                                     d=record.name))


class CpDetentionRecord(models.Model):
    _name = 'cp.detention.record'
    _description = 'Detention / Halting Claim'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(default=lambda self: _('New'), readonly=True, copy=False)
    plan_id = fields.Many2one('cp.dispatch.plan', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='plan_id.company_id', store=True)
    currency_id = fields.Many2one(related='plan_id.currency_id')
    vendor_id = fields.Many2one(related='plan_id.vendor_id', store=True, string='Vendor')
    vehicle_type_id = fields.Many2one(related='plan_id.vehicle_type_id', store=True)
    gate_entry_id = fields.Many2one('cp.gate.entry', string='Gate Entry')
    gate_in_time = fields.Datetime(string='Gate-In (T0)')
    gate_out_time = fields.Datetime(string='Gate-Out (T3)')
    tat_hours = fields.Float(compute='_compute_amounts', store=True, string='Turnaround (h)', digits=(8, 2))
    term_id = fields.Many2one('cp.detention.term', string='Vendor Terms')
    free_hours = fields.Float(string='Free Hours', digits=(8, 2))
    method = fields.Selection([('hourly', 'Hourly rate'), ('daily', 'Daily halting slab')], default='hourly')
    hourly_rate = fields.Monetary()
    daily_rate = fields.Monetary()
    partial_day = fields.Selection([('full', 'Full day'), ('prorata', 'Pro rata')], default='full')
    gross_waiting_hours = fields.Float(compute='_compute_amounts', store=True, digits=(8, 2),
                                       string='Waiting beyond Free Time (h)')
    delay_reason = fields.Selection(DELAY_REASONS, tracking=True)
    delay_note = fields.Char(string='Delay Details')
    transporter_fault = fields.Boolean(compute='_compute_amounts', store=True)
    chargeable_hours = fields.Float(compute='_compute_amounts', store=True, digits=(8, 2))
    chargeable_days = fields.Float(compute='_compute_amounts', store=True, digits=(8, 2))
    calculated_amount = fields.Monetary(compute='_compute_amounts', store=True)
    approved_amount = fields.Monetary(tracking=True, copy=False)
    adjust_reason = fields.Char(string='Adjustment Reason')
    evidence_ids = fields.Many2many('ir.attachment', 'cp_detention_attachment_rel', 'detention_id', 'attachment_id',
                                    string='Evidence')
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Waiting Logistics Manager'),
                              ('logistics_approved', 'Waiting Plant Operations Head'), ('approved', 'Approved'),
                              ('rejected', 'Rejected')], default='draft', tracking=True)
    logistics_approved_by = fields.Many2one('res.users', readonly=True)
    logistics_approved_at = fields.Datetime(readonly=True)
    ops_approved_by = fields.Many2one('res.users', string='Operations Approved By', readonly=True)
    ops_approved_at = fields.Datetime(readonly=True)
    move_id = fields.Many2one('account.move', string='Vendor Bill', readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('cp.detention.record') or _('New')
        return super().create(vals_list)

    def _cp_apply_terms(self):
        """Copy gate times and the vendor's terms (per truck type, then vendor default, then company)."""
        for rec in self:
            entry = rec.gate_entry_id
            term = self.env['cp.detention.term'].cp_for(rec.plan_id.vendor_id, rec.plan_id.vehicle_type_id) \
                if rec.plan_id.vendor_id else self.env['cp.detention.term']
            company = rec.plan_id.company_id
            rec.update({
                'gate_in_time': entry.gate_in_time or rec.gate_in_time,
                'gate_out_time': entry.gate_out_time or rec.gate_out_time,
                'term_id': term.id,
                'free_hours': term.free_hours if term else (company.cp_detention_free_hours or 2.0),
                'method': term.method if term else 'hourly',
                'hourly_rate': term.hourly_rate if term else company.cp_detention_hourly_rate,
                'daily_rate': term.daily_rate if term else company.cp_detention_daily_rate,
                'partial_day': term.partial_day if term else 'full',
            })

    @api.depends('gate_in_time', 'gate_out_time', 'free_hours', 'delay_reason', 'method', 'hourly_rate',
                 'daily_rate', 'partial_day')
    def _compute_amounts(self):
        for rec in self:
            tat = (rec.gate_out_time - rec.gate_in_time).total_seconds() / 3600.0 \
                if rec.gate_in_time and rec.gate_out_time else 0.0
            rec.tat_hours = round(max(tat, 0.0), 2)
            rec.gross_waiting_hours = round(max(tat - (rec.free_hours or 0.0), 0.0), 2)
            rec.transporter_fault = rec.delay_reason == 'transporter_fault'
            hours = 0.0 if rec.transporter_fault else rec.gross_waiting_hours
            rec.chargeable_hours = hours
            if rec.method == 'daily':
                days = math.ceil(hours / 24.0) if rec.partial_day == 'full' else hours / 24.0
                rec.chargeable_days = days
                rec.calculated_amount = round(days * (rec.daily_rate or 0.0), 2)
            else:
                rec.chargeable_days = hours / 24.0
                rec.calculated_amount = round(hours * (rec.hourly_rate or 0.0), 2)

    @api.constrains('approved_amount', 'calculated_amount', 'adjust_reason', 'state')
    def _check_adjustment(self):
        for rec in self:
            if rec.approved_amount < 0:
                raise ValidationError(_('The approved detention cannot be negative.'))
            if rec.state in ('logistics_approved', 'approved') and \
                    abs(rec.approved_amount - rec.calculated_amount) > 0.01 and not rec.adjust_reason:
                raise ValidationError(_('Give a reason when the approved amount differs from the calculated %s.',
                                        rec.calculated_amount))

    def _cp_unbilled(self):
        """Records not on a live vendor bill (no bill yet, or the bill was cancelled)."""
        return self.filtered(lambda d: not d.move_id or d.move_id.state == 'cancel')

    def _cp_need_group(self, xmlid, label):
        if not self.env.su and not self.env.user.has_group(xmlid):
            raise UserError(_('Only the %s can do this.', label))

    def action_recalculate(self):
        self.filtered(lambda r: r.state == 'draft')._cp_apply_terms()
        return True

    def action_submit(self):
        for rec in self:
            if rec.state != 'draft':
                continue
            if not rec.delay_reason:
                raise UserError(_('Choose the reason for the delay of %s.', rec.name))
            if rec.transporter_fault or not rec.calculated_amount:
                rec.write({'state': 'approved', 'approved_amount': 0.0})
                rec.message_post(body=_('No detention payable (%s).', dict(DELAY_REASONS)[rec.delay_reason]))
                continue
            rec.write({'state': 'submitted', 'approved_amount': rec.approved_amount or rec.calculated_amount})
        return True

    def action_approve_logistics(self):
        self._cp_need_group('cargo_pilot_tms.group_cp_manager', _('Logistics Manager'))
        for rec in self:
            if rec.state != 'submitted':
                raise UserError(_('%s is not waiting for the Logistics Manager.', rec.name))
            rec.write({'state': 'logistics_approved', 'logistics_approved_by': self.env.uid,
                       'logistics_approved_at': fields.Datetime.now()})
        return True

    def action_approve_ops(self):
        self._cp_need_group('cargo_pilot_tms.group_dispatch_ops_head', _('Plant Operations Head'))
        for rec in self:
            if rec.state != 'logistics_approved':
                raise UserError(_('%s needs the Logistics Manager approval first.', rec.name))
            if rec.logistics_approved_by == self.env.user:
                raise UserError(_('A different person must give the second approval.'))
            rec.write({'state': 'approved', 'ops_approved_by': self.env.uid, 'ops_approved_at': fields.Datetime.now()})
            rec.message_post(body=_('Detention of %s approved for billing.', rec.plan_id._cp_money(rec.approved_amount)))
            billed = rec.plan_id.sudo().move_ids.filtered(lambda m: m.move_type == 'in_invoice' and m.state != 'cancel')
            if billed and rec.approved_amount > 0:
                rec.plan_id.sudo().message_post(body=_('Detention %(d)s (%(a)s) was approved after the vendor bill. Use '
                                                '"Bill Detention" to bill it.', d=rec.name,
                                                a=rec.plan_id._cp_money(rec.approved_amount)))
        return True

    def action_reject(self):
        self._cp_need_group('cargo_pilot_tms.group_cp_manager', _('Logistics Manager'))
        for rec in self:
            if rec.move_id and rec.move_id.state != 'cancel':
                raise UserError(_('%s is already billed.', rec.name))
            rec.write({'state': 'rejected', 'approved_amount': 0.0})
        return True

    def action_reset(self):
        for rec in self:
            if rec.move_id and rec.move_id.state != 'cancel':
                raise UserError(_('%s is already billed.', rec.name))
        self.write({'state': 'draft', 'logistics_approved_by': False, 'ops_approved_by': False,
                    'logistics_approved_at': False, 'ops_approved_at': False})
        return True
