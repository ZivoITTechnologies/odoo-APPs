# -*- coding: utf-8 -*-
from odoo import fields, models


class ProjectProject(models.Model):
    _inherit = 'project.project'

    tender_id = fields.Many2one('zivoit.tender', 'Source Tender', copy=False, index='btree_not_null')
    contract_value = fields.Monetary('Contract Value', currency_field='currency_id')
    ra_billed_amount = fields.Monetary('Billed (RA)', compute='_compute_ra_totals', currency_field='currency_id')
    ra_bill_count = fields.Integer(compute='_compute_ra_totals')
    physical_progress = fields.Float('Physical Progress %', compute='_compute_ra_totals')

    def _compute_ra_totals(self):
        for project in self:
            bills = project.tender_id.ra_bill_ids.filtered(lambda b: b.state in ('submitted', 'certified', 'paid'))
            project.ra_bill_count = len(project.tender_id.ra_bill_ids)
            project.ra_billed_amount = sum(bills.mapped('gross_amount'))
            value = project.contract_value or sum(project.tender_id.boq_line_ids.mapped('cost_amount'))
            project.physical_progress = (project.ra_billed_amount / value * 100.0) if value else 0.0

    def action_view_tender(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'zivoit.tender', 'res_id': self.tender_id.id,
                'view_mode': 'form'}

    def action_view_ra_bills(self):
        self.ensure_one()
        return self.tender_id.action_view_ra_bills()
