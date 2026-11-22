# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class TenderRaBill(models.Model):
    _name = 'zivoit.tender.ra.bill'
    _description = 'Running Account (RA) Bill'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'tender_id, bill_no desc'

    name = fields.Char(readonly=True, default='/', copy=False)
    tender_id = fields.Many2one('zivoit.tender', required=True, index=True, tracking=True,
                                domain="[('stage_type', '=', 'won')]")
    project_id = fields.Many2one(related='tender_id.project_id', store=True)
    partner_id = fields.Many2one(related='tender_id.partner_id', store=True)
    company_id = fields.Many2one(related='tender_id.company_id', store=True)
    currency_id = fields.Many2one(related='tender_id.currency_id')
    bill_no = fields.Integer('RA Bill No.', readonly=True, copy=False)
    is_final = fields.Boolean('Final Bill')
    date = fields.Date(default=fields.Date.context_today, required=True, tracking=True)
    period_from = fields.Date()
    period_to = fields.Date()
    state = fields.Selection([('draft', 'Draft'), ('submitted', 'Submitted'), ('certified', 'Certified'),
                              ('paid', 'Paid'), ('cancel', 'Cancelled')], default='draft', tracking=True, required=True)
    line_ids = fields.One2many('zivoit.tender.ra.bill.line', 'bill_id', copy=True)

    gross_amount = fields.Monetary('Work Done (This Bill)', compute='_compute_amounts', store=True)
    cumulative_amount = fields.Monetary('Work Done (Up to Date)', compute='_compute_amounts', store=True)
    retention_pct = fields.Float('Retention / Security Deposit %', default=5.0)
    retention_amount = fields.Monetary(compute='_compute_amounts', store=True)
    mobilisation_recovery = fields.Monetary('Mobilisation Advance Recovery')
    material_advance_recovery = fields.Monetary('Secured Advance Recovery')
    other_deductions = fields.Monetary('Other Deductions (LD, penalties)')
    gst_pct = fields.Float('GST %', default=18.0)
    gst_amount = fields.Monetary(compute='_compute_amounts', store=True)
    tds_pct = fields.Float('TDS % (u/s 194C)', default=2.0)
    tds_amount = fields.Monetary('TDS', compute='_compute_amounts', store=True)
    gst_tds_pct = fields.Float('GST TDS %', default=2.0)
    gst_tds_amount = fields.Monetary('GST TDS', compute='_compute_amounts', store=True)
    labour_cess_pct = fields.Float('Labour Cess %', default=1.0)
    labour_cess_amount = fields.Monetary(compute='_compute_amounts', store=True)
    net_payable = fields.Monetary(compute='_compute_amounts', store=True, tracking=True)
    certified_amount = fields.Monetary(tracking=True)
    paid_amount = fields.Monetary(tracking=True)
    payment_date = fields.Date()
    notes = fields.Text()

    @api.depends('line_ids.current_amount', 'line_ids.cumulative_amount', 'retention_pct', 'mobilisation_recovery',
                 'material_advance_recovery', 'other_deductions', 'gst_pct', 'tds_pct', 'gst_tds_pct', 'labour_cess_pct')
    def _compute_amounts(self):
        for bill in self:
            gross = sum(bill.line_ids.mapped('current_amount'))
            bill.gross_amount = gross
            bill.cumulative_amount = sum(bill.line_ids.mapped('cumulative_amount'))
            bill.retention_amount = gross * bill.retention_pct / 100.0
            bill.gst_amount = gross * bill.gst_pct / 100.0
            bill.tds_amount = gross * bill.tds_pct / 100.0
            bill.gst_tds_amount = gross * bill.gst_tds_pct / 100.0
            bill.labour_cess_amount = gross * bill.labour_cess_pct / 100.0
            bill.net_payable = (gross + bill.gst_amount - bill.retention_amount - bill.tds_amount - bill.gst_tds_amount
                                - bill.labour_cess_amount - bill.mobilisation_recovery
                                - bill.material_advance_recovery - bill.other_deductions)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            tender = self.env['zivoit.tender'].browse(vals.get('tender_id'))
            last = self.search([('tender_id', '=', tender.id)], order='bill_no desc', limit=1)
            vals['bill_no'] = (last.bill_no or 0) + 1
            vals['name'] = '%s/RA-%02d' % (tender.code or 'TND', vals['bill_no'])
        bills = super().create(vals_list)
        for bill in bills.filtered(lambda b: not b.line_ids):
            bill.action_load_boq()
        return bills

    def action_load_boq(self):
        for bill in self:
            if bill.state != 'draft':
                raise UserError(_('Only draft bills can be reloaded.'))
            existing = bill.line_ids.mapped('boq_line_id')
            bill.line_ids = [(0, 0, {'boq_line_id': boq.id, 'current_qty': 0.0})
                             for boq in bill.tender_id.boq_line_ids.filtered(lambda l: not l.display_type) - existing]
        return True

    def action_submit(self):
        for bill in self:
            if not bill.line_ids.filtered('current_qty'):
                raise UserError(_('Enter the measured quantities for this bill period first.'))
        self.write({'state': 'submitted'})

    def action_certify(self):
        for bill in self:
            bill.write({'state': 'certified', 'certified_amount': bill.certified_amount or bill.net_payable})

    def action_mark_paid(self):
        for bill in self:
            bill.write({'state': 'paid', 'paid_amount': bill.paid_amount or bill.certified_amount or bill.net_payable,
                        'payment_date': bill.payment_date or fields.Date.context_today(bill)})

    def action_cancel(self):
        self.write({'state': 'cancel'})

    def write(self, vals):
        res = super().write(vals)
        if 'state' in vals:
            self.line_ids._refresh_following_bills()
        return res

    def action_draft(self):
        self.write({'state': 'draft'})


class TenderRaBillLine(models.Model):
    _name = 'zivoit.tender.ra.bill.line'
    _description = 'RA Bill Measurement Line'
    _order = 'bill_id, sequence, id'

    bill_id = fields.Many2one('zivoit.tender.ra.bill', required=True, ondelete='cascade', index=True)
    currency_id = fields.Many2one(related='bill_id.currency_id')
    sequence = fields.Integer(related='boq_line_id.sequence', store=True)
    boq_line_id = fields.Many2one('zivoit.tender.boq.line', 'BOQ Item', required=True, ondelete='restrict',
                                  domain="[('tender_id', '=', parent.tender_id), ('display_type', '=', False)]")
    item_code = fields.Char(related='boq_line_id.item_code')
    name = fields.Text(related='boq_line_id.name')
    uom = fields.Char(related='boq_line_id.uom')
    contract_qty = fields.Float(related='boq_line_id.quantity', digits='Product Unit')
    rate = fields.Monetary(compute='_compute_rate', store=True, readonly=False)
    previous_qty = fields.Float('Up to Previous Bill', compute='_compute_previous', store=True, digits='Product Unit')
    current_qty = fields.Float('This Bill Qty', digits='Product Unit')
    cumulative_qty = fields.Float('Up to Date Qty', compute='_compute_line_amounts', store=True, digits='Product Unit')
    current_amount = fields.Monetary('This Bill Amount', compute='_compute_line_amounts', store=True)
    cumulative_amount = fields.Monetary('Up to Date Amount', compute='_compute_line_amounts', store=True)
    excess_qty = fields.Boolean('Exceeds Contract Qty', compute='_compute_line_amounts', store=True)
    measurement_ref = fields.Char('MB Page / Ref.')

    @api.depends('boq_line_id')
    def _compute_rate(self):
        for line in self:
            line.rate = line.boq_line_id.quoted_rate

    @api.depends('boq_line_id', 'bill_id.bill_no', 'bill_id.tender_id')
    def _compute_previous(self):
        for line in self:
            if not line.boq_line_id or not line.bill_id.bill_no:
                line.previous_qty = 0.0
                continue
            previous = self.search([
                ('boq_line_id', '=', line.boq_line_id.id),
                ('bill_id.tender_id', '=', line.bill_id.tender_id.id),
                ('bill_id.bill_no', '<', line.bill_id.bill_no),
                ('bill_id.state', '!=', 'cancel'),
            ])
            line.previous_qty = sum(previous.mapped('current_qty'))

    @api.depends('previous_qty', 'current_qty', 'rate', 'contract_qty')
    def _compute_line_amounts(self):
        for line in self:
            line.cumulative_qty = line.previous_qty + line.current_qty
            line.current_amount = line.current_qty * line.rate
            line.cumulative_amount = line.cumulative_qty * line.rate
            line.excess_qty = bool(line.contract_qty) and line.cumulative_qty > line.contract_qty * 1.0001

    def write(self, vals):
        res = super().write(vals)
        if 'current_qty' in vals:
            self._refresh_following_bills()
        return res

    def _refresh_following_bills(self):
        """Keep 'up to previous bill' quantities of later bills in sync when an earlier bill changes."""
        for line in self:
            later = self.search([
                ('boq_line_id', '=', line.boq_line_id.id),
                ('bill_id.tender_id', '=', line.bill_id.tender_id.id),
                ('bill_id.bill_no', '>', line.bill_id.bill_no),
            ])
            if later:
                later._compute_previous()

    @api.constrains('current_qty')
    def _check_qty(self):
        for line in self:
            if line.current_qty < 0:
                raise ValidationError(_('Measured quantity cannot be negative.'))
