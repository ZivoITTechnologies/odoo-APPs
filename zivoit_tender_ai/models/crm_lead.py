# -*- coding: utf-8 -*-
from odoo import fields, models


class CrmLead(models.Model):
    _inherit = 'crm.lead'

    tender_id = fields.Many2one('zivoit.tender', 'Tender', copy=False, index='btree_not_null')

    def action_view_tender(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'zivoit.tender', 'res_id': self.tender_id.id,
                'view_mode': 'form'}
