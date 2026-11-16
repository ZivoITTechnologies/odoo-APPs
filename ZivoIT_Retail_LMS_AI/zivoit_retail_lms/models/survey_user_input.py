# -*- coding: utf-8 -*-
from odoo import models


class SurveyUserInput(models.Model):
    _inherit = 'survey.user_input'

    def _mark_done(self):
        res = super()._mark_done()
        partners = self.partner_id
        if partners:
            assignments = self.env['zlms.assignment'].sudo().search([
                ('partner_id', 'in', partners.ids),
                ('state', 'in', ('assigned', 'in_progress')),
                ('line_ids.survey_id', 'in', self.survey_id.ids),
            ])
            assignments._sync_progress()
        return res
