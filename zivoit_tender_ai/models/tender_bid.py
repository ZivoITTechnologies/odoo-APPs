# -*- coding: utf-8 -*-
from markupsafe import Markup

from odoo import api, fields, models, _


class TenderChecklistLine(models.Model):
    _name = 'zivoit.tender.checklist.line'
    _description = 'Bid / No-Bid Qualification Criterion'
    _order = 'sequence, id'

    tender_id = fields.Many2one('zivoit.tender', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    name = fields.Char('Criterion', required=True)
    category = fields.Selection([
        ('eligibility', 'Eligibility / Pre-qualification'),
        ('technical', 'Technical Capability'),
        ('financial', 'Financial Capacity'),
        ('commercial', 'Commercial / Risk'),
        ('document', 'Document Required'),
        ('strategic', 'Strategic Fit'),
    ], required=True, default='eligibility')
    weight = fields.Integer(default=10)
    mandatory = fields.Boolean()
    status = fields.Selection([('pending', 'Pending'), ('yes', 'Met'), ('partial', 'Partially'), ('no', 'Not Met'),
                               ('na', 'N/A')], default='pending', required=True)
    requirement = fields.Char('Tender Requirement', help='e.g. "Similar work 40% of ECV in last 7 years"')
    our_position = fields.Char('Our Position', help='e.g. "Completed ₹18 Cr ROB for SCR in 2024"')
    responsible_id = fields.Many2one('res.users')
    attachment_ids = fields.Many2many('ir.attachment', string='Evidence')
    notes = fields.Char()


class TenderBoqLine(models.Model):
    _name = 'zivoit.tender.boq.line'
    _description = 'Tender BOQ / Schedule Item'
    _order = 'tender_id, sequence, id'

    tender_id = fields.Many2one('zivoit.tender', required=True, ondelete='cascade', index=True)
    currency_id = fields.Many2one(related='tender_id.currency_id')
    sequence = fields.Integer(default=10)
    display_type = fields.Selection([('line_section', 'Section'), ('line_note', 'Note')], default=False)
    item_code = fields.Char('Item / SSR Code')
    name = fields.Text('Description', required=True)
    uom = fields.Char('Unit', default='Cum')
    quantity = fields.Float(digits='Product Unit', default=1.0)
    estimate_rate = fields.Monetary('Department Rate', help='Rate as per the tender estimate / schedule of rates')
    material_rate = fields.Monetary('Material')
    labour_rate = fields.Monetary('Labour')
    equipment_rate = fields.Monetary('Plant & Machinery')
    subcontract_rate = fields.Monetary('Sub-contract')
    cost_rate = fields.Monetary('Direct Cost Rate', compute='_compute_cost_rate', store=True, readonly=False,
                                help='Sum of material, labour, plant and sub-contract; can be entered directly.')
    quoted_rate = fields.Monetary('Quoted Rate', compute='_compute_quoted_rate')
    estimate_amount = fields.Monetary('Estimate Amount', compute='_compute_amounts', store=True)
    cost_amount = fields.Monetary('Cost Amount', compute='_compute_amounts', store=True)
    executed_qty = fields.Float('Executed Qty', compute='_compute_executed', digits='Product Unit')
    progress = fields.Float('Progress %', compute='_compute_executed')
    task_id = fields.Many2one('project.task', 'Execution Task', copy=False)

    @api.depends('material_rate', 'labour_rate', 'equipment_rate', 'subcontract_rate')
    def _compute_cost_rate(self):
        for line in self:
            components = line.material_rate + line.labour_rate + line.equipment_rate + line.subcontract_rate
            line.cost_rate = components or line.cost_rate

    @api.depends('quantity', 'estimate_rate', 'cost_rate', 'display_type')
    def _compute_amounts(self):
        for line in self:
            if line.display_type:
                line.estimate_amount = line.cost_amount = 0.0
                continue
            line.estimate_amount = line.quantity * line.estimate_rate
            line.cost_amount = line.quantity * line.cost_rate

    @api.depends('cost_rate', 'tender_id.overhead_pct', 'tender_id.contingency_pct', 'tender_id.margin_pct',
                 'tender_id.final_quoted_amount', 'tender_id.quoted_amount')
    def _compute_quoted_rate(self):
        for line in self:
            tender = line.tender_id
            factor = (1 + (tender.overhead_pct + tender.contingency_pct) / 100.0) * (1 + tender.margin_pct / 100.0)
            rate = line.cost_rate * factor
            if tender.final_quoted_amount and tender.quoted_amount:
                rate *= tender.final_quoted_amount / tender.quoted_amount
            line.quoted_rate = rate

    def _compute_executed(self):
        BillLine = self.env['zivoit.tender.ra.bill.line']
        data = BillLine._read_group(
            [('boq_line_id', 'in', self.ids), ('bill_id.state', 'in', ('submitted', 'certified', 'paid'))],
            ['boq_line_id'], ['current_qty:sum'])
        qty = {line.id: total for line, total in data}
        for line in self:
            line.executed_qty = qty.get(line.id, 0.0)
            line.progress = (line.executed_qty / line.quantity * 100.0) if line.quantity else 0.0


class TenderCorrigendum(models.Model):
    _name = 'zivoit.tender.corrigendum'
    _description = 'Tender Corrigendum / Addendum'
    _order = 'date desc, id desc'

    tender_id = fields.Many2one('zivoit.tender', required=True, ondelete='cascade', index=True)
    name = fields.Char('Corrigendum No.', required=True)
    date = fields.Date(default=fields.Date.context_today, required=True)
    description = fields.Text(required=True)
    new_date_closing = fields.Datetime('Revised Submission End')
    new_date_opening = fields.Datetime('Revised Opening')
    new_estimated_value = fields.Float('Revised Estimated Value')
    attachment_ids = fields.Many2many('ir.attachment', string='Documents')
    applied = fields.Boolean(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._apply()
        return records

    def _apply(self):
        for corr in self.filtered(lambda c: not c.applied):
            tender = corr.tender_id
            vals = {}
            if corr.new_date_closing:
                vals['date_closing'] = corr.new_date_closing
                vals['last_reminder_days'] = 0
            if corr.new_date_opening:
                vals['date_opening'] = corr.new_date_opening
            if corr.new_estimated_value:
                vals['estimated_value'] = corr.new_estimated_value
            if vals:
                tender.write(vals)
            tender.message_post(
                body=Markup('<b>%s</b><br/>%s') % (_('Corrigendum %s issued', corr.name), corr.description),
                partner_ids=(tender.user_id | tender.team_member_ids).partner_id.ids,
                subtype_xmlid='mail.mt_comment')
            corr.applied = True


class TenderCompetitor(models.Model):
    _name = 'zivoit.tender.competitor'
    _description = 'Tender Bidder / Result Line'
    _order = 'tender_id, rank, quoted_amount'

    tender_id = fields.Many2one('zivoit.tender', required=True, ondelete='cascade', index=True)
    currency_id = fields.Many2one(related='tender_id.currency_id')
    partner_id = fields.Many2one('res.partner', 'Bidder')
    name = fields.Char('Bidder Name', compute='_compute_name', store=True, readonly=False)
    is_us = fields.Boolean('Our Company')
    technically_qualified = fields.Boolean(default=True)
    quoted_amount = fields.Monetary()
    pct_vs_estimate = fields.Float('% vs Estimate', compute='_compute_pct', store=True, digits=(16, 2))
    rank = fields.Integer('Rank (L-n)', compute='_compute_rank', store=True, readonly=False)
    notes = fields.Char()

    @api.depends('partner_id')
    def _compute_name(self):
        for line in self:
            if line.partner_id:
                line.name = line.partner_id.name

    @api.depends('quoted_amount', 'tender_id.estimated_value')
    def _compute_pct(self):
        for line in self:
            est = line.tender_id.estimated_value
            line.pct_vs_estimate = ((line.quoted_amount - est) / est * 100.0) if est and line.quoted_amount else 0.0

    @api.depends('quoted_amount', 'technically_qualified', 'tender_id.competitor_ids.quoted_amount',
                 'tender_id.competitor_ids.technically_qualified')
    def _compute_rank(self):
        for tender in self.mapped('tender_id'):
            lines = tender.competitor_ids.filtered(lambda l: l.technically_qualified and l.quoted_amount)
            for idx, line in enumerate(lines.sorted('quoted_amount'), start=1):
                line.rank = idx
            for line in tender.competitor_ids - lines:
                line.rank = 0
