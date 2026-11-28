# -*- coding: utf-8 -*-
from odoo import fields, models


class ZivoExpenseRejectWizard(models.TransientModel):
    _name = 'zivo.expense.reject.wizard'
    _description = 'Reject Field Expense Claim'

    claim_ids = fields.Many2many('zivo.expense.claim', required=True)
    reason = fields.Text(required=True)

    def action_reject(self):
        self.claim_ids._do_reject(self.reason)
        return {'type': 'ir.actions.act_window_close'}
