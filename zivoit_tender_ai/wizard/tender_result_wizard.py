# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class TenderResultWizard(models.TransientModel):
    _name = 'zivoit.tender.result.wizard'
    _description = 'Record Tender Result'

    tender_id = fields.Many2one('zivoit.tender', required=True)
    currency_id = fields.Many2one(related='tender_id.currency_id')
    outcome = fields.Selection([('won', 'Won / Awarded'), ('lost', 'Lost'), ('dropped', 'No-Bid / Dropped')],
                               required=True, default='won')
    lost_reason_id = fields.Many2one('zivoit.tender.lost.reason')
    our_rank = fields.Integer('Our Rank (L-n)')
    l1_bidder = fields.Char('L1 Bidder')
    l1_amount = fields.Monetary('L1 Amount')
    awarded_value = fields.Monetary('Awarded / Contract Value')
    result_date = fields.Date(default=fields.Date.context_today)
    notes = fields.Text()
    create_project = fields.Boolean('Create Execution Project', default=True)

    @api.onchange('outcome')
    def _onchange_outcome(self):
        if self.outcome == 'won':
            self.our_rank = 1
            self.awarded_value = self.tender_id.bid_amount or self.tender_id.estimated_value

    def action_confirm(self):
        self.ensure_one()
        tender = self.tender_id
        if self.outcome in ('lost', 'dropped') and not self.lost_reason_id:
            raise UserError(_('Please select a reason.'))
        vals = {
            'result_date': self.result_date,
            'result_notes': self.notes,
            'lost_reason_id': self.lost_reason_id.id if self.outcome != 'won' else False,
        }
        if self.outcome != 'dropped':
            vals.update({'our_rank': self.our_rank, 'l1_bidder': self.l1_bidder,
                         'l1_amount': self.l1_amount or (tender.bid_amount if self.outcome == 'won' else 0.0)})
        if self.outcome == 'won':
            vals['awarded_value'] = self.awarded_value or tender.bid_amount or tender.estimated_value
            vals['l1_bidder'] = self.l1_bidder or self.env.company.name
        tender.with_context(skip_auto_project=not self.create_project).write(
            dict(vals, stage_id=tender._stage(self.outcome).id))
        if self.outcome == 'won' and self.create_project and not tender.project_id:
            tender.action_create_project()
        if self.outcome == 'won' and tender.project_id:
            return tender.action_view_project()
        return {'type': 'ir.actions.act_window_close'}
