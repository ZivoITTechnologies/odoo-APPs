# -*- coding: utf-8 -*-
import secrets

from odoo import api, fields, models

CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'


class SurveySurvey(models.Model):
    _inherit = 'survey.survey'

    zivo_allow_save_later = fields.Boolean(
        string='Save & Continue Later',
        help='Show a "Save & continue later" button. Respondents (including anonymous ones) get a '
             'personal resume link and a short resume code, and can email the link to themselves.')
    zivo_resume_validity_days = fields.Integer(string='Resume Link Validity (days)', default=30)
    zivo_event_id = fields.Many2one('event.event', string='Related Event',
                                    help='Optional: link the survey to an event (feedback, pre-event forms).')
    zivo_saved_count = fields.Integer(compute='_compute_zivo_saved_count', string='Saved for Later')

    def _compute_zivo_saved_count(self):
        data = dict(self.env['survey.user_input'].sudo()._read_group(
            [('survey_id', 'in', self.ids), ('zivo_saved_later', '=', True), ('state', '!=', 'done')],
            ['survey_id'], ['__count']))
        for survey in self:
            survey.zivo_saved_count = data.get(survey, 0)

    def action_zivo_view_saved(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Saved for Later'),
            'res_model': 'survey.user_input',
            'view_mode': 'list,form',
            'domain': [('survey_id', '=', self.id), ('zivo_saved_later', '=', True), ('state', '!=', 'done')],
        }


class SurveyUserInput(models.Model):
    _inherit = 'survey.user_input'

    zivo_resume_code = fields.Char(string='Resume Code', copy=False, index=True, readonly=True)
    zivo_saved_later = fields.Boolean(string='Saved for Later', copy=False, readonly=True)
    zivo_saved_date = fields.Datetime(string='Saved On', copy=False, readonly=True)
    zivo_save_count = fields.Integer(string='Times Saved', copy=False, readonly=True)
    zivo_resume_email = fields.Char(string='Resume Link Sent To', copy=False, readonly=True)
    zivo_resume_url = fields.Char(compute='_compute_zivo_resume_url', string='Resume Link')

    _zivo_resume_code_uniq = models.Constraint('unique(zivo_resume_code)', 'Resume code must be unique.')

    def _compute_zivo_resume_url(self):
        for ui in self:
            ui.zivo_resume_url = '%s/survey/%s/%s' % (ui.survey_id.get_base_url(), ui.survey_id.access_token,
                                                      ui.access_token) if ui.access_token else False

    @api.model
    def _zivo_new_code(self):
        while True:
            code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(8))
            code = '%s-%s' % (code[:4], code[4:])
            if not self.sudo().search_count([('zivo_resume_code', '=', code)]):
                return code

    def _zivo_mark_saved(self):
        for ui in self:
            vals = {'zivo_saved_later': True, 'zivo_saved_date': fields.Datetime.now(),
                    'zivo_save_count': ui.zivo_save_count + 1}
            if not ui.zivo_resume_code:
                vals['zivo_resume_code'] = self._zivo_new_code()
            ui.write(vals)

    @api.model
    def _zivo_find_by_code(self, code):
        code = (code or '').strip().upper().replace(' ', '')
        if len(code) == 8 and '-' not in code:
            code = '%s-%s' % (code[:4], code[4:])
        if not code:
            return self.browse()
        ui = self.sudo().search([('zivo_resume_code', '=', code), ('state', '!=', 'done')], limit=1)
        if ui and ui.survey_id.zivo_resume_validity_days and ui.zivo_saved_date:
            age = (fields.Datetime.now() - ui.zivo_saved_date).days
            if age > ui.survey_id.zivo_resume_validity_days:
                return self.browse()
        return ui
