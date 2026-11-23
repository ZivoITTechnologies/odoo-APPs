# -*- coding: utf-8 -*-
import json
from datetime import timedelta

from markupsafe import Markup, escape

from odoo import api, fields, models
from odoo.exceptions import UserError


class ZivoSurveyInsight(models.Model):
    _name = 'zivo.survey.insight'
    _description = 'AI Survey Insight'
    _order = 'id desc'
    _rec_name = 'survey_id'

    survey_id = fields.Many2one('survey.survey', required=True, ondelete='cascade', index=True)
    date = fields.Datetime(default=fields.Datetime.now, readonly=True)
    user_id = fields.Many2one('res.users', default=lambda self: self.env.user, readonly=True)
    responses_count = fields.Integer(string='Responses Analysed', readonly=True)
    answers_count = fields.Integer(string='Text Answers', readonly=True)
    summary = fields.Html(readonly=True, sanitize=True)
    sentiment_positive = fields.Integer(string='Positive %', readonly=True)
    sentiment_neutral = fields.Integer(string='Neutral %', readonly=True)
    sentiment_negative = fields.Integer(string='Negative %', readonly=True)
    theme_ids = fields.One2many('zivo.survey.insight.theme', 'insight_id', string='Themes', readonly=True)
    recommendations = fields.Text(readonly=True)
    anonymous_excluded = fields.Boolean(readonly=True)


class ZivoSurveyInsightTheme(models.Model):
    _name = 'zivo.survey.insight.theme'
    _description = 'AI Survey Insight Theme'
    _order = 'mentions desc, id'

    insight_id = fields.Many2one('zivo.survey.insight', required=True, ondelete='cascade')
    name = fields.Char(required=True)
    sentiment = fields.Selection([('positive', 'Positive'), ('neutral', 'Neutral'), ('negative', 'Negative')])
    mentions = fields.Integer()
    example = fields.Char(string='Example Quote')


class SurveySurvey(models.Model):
    _inherit = 'survey.survey'

    zivo_insight_ids = fields.One2many('zivo.survey.insight', 'survey_id', string='AI Insights')
    zivo_insight_count = fields.Integer(compute='_compute_zivo_insight_count')
    zivo_abandon_days = fields.Integer(string='Remind Saved Respondents After (days)', default=3)
    zivo_at_risk_count = fields.Integer(compute='_compute_zivo_at_risk', string='At Risk of Abandoning')

    def _compute_zivo_insight_count(self):
        for survey in self:
            survey.zivo_insight_count = len(survey.zivo_insight_ids)

    def _zivo_at_risk_inputs(self):
        self.ensure_one()
        limit = fields.Datetime.now() - timedelta(days=self.zivo_abandon_days or 3)
        return self.env['survey.user_input'].sudo().search([
            ('survey_id', '=', self.id), ('state', '!=', 'done'), ('zivo_saved_later', '=', True),
            ('zivo_saved_date', '<=', limit)])

    def _compute_zivo_at_risk(self):
        for survey in self:
            survey.zivo_at_risk_count = len(survey._zivo_at_risk_inputs()) if survey.id else 0

    # ------------------------------------------------------------------
    def _zivo_collect_text_answers(self, max_answers=400):
        self.ensure_one()
        icp = self.env['ir.config_parameter'].sudo()
        exclude_anon = str(icp.get_param('zivoit_event_connect.ai_survey_exclude_anonymous', 'False')).lower() in ('1', 'true')
        domain = [('survey_id', '=', self.id), ('state', 'in', ('in_progress', 'done')), ('test_entry', '=', False)]
        if exclude_anon:
            domain.append(('partner_id', '!=', False))
        inputs = self.env['survey.user_input'].sudo().search(domain)
        answers = []
        for line in inputs.user_input_line_ids.sorted('id'):
            if line.skipped:
                continue
            text = False
            if line.answer_type in ('text_box', 'char_box'):
                text = line.value_text_box if line.answer_type == 'text_box' else line.value_char_box
            elif line.answer_type == 'suggestion' and line.value_char_box:
                text = line.value_char_box  # "other" comment
            if text and len(text.strip()) > 2:
                answers.append({'question': line.question_id.title, 'answer': text.strip()[:600]})
            if len(answers) >= max_answers:
                break
        return inputs, answers, exclude_anon

    def action_zivo_ai_insights(self):
        self.ensure_one()
        service = self.env['zivo.ai.service']
        service._ensure_feature('survey')
        inputs, answers, exclude_anon = self._zivo_collect_text_answers()
        if not answers:
            raise UserError(self.env._('There are no free-text answers to analyse yet.'))
        prompt = (
            'Analyse the free-text answers of the survey "%s" (%s responses).\nAnswers (JSON):\n%s\n\n'
            'Return JSON {"summary": "4-6 sentence executive summary", '
            '"sentiment": {"positive": int, "neutral": int, "negative": int} (percentages adding to 100), '
            '"themes": [{"name": str, "sentiment": "positive"|"neutral"|"negative", "mentions": int, '
            '"example": "short quote copied from an answer"}] (max 8, most mentioned first), '
            '"recommendations": ["actionable recommendation", ...] (max 5)}. '
            'Never include names, emails or phone numbers.'
        ) % (self.title, len(inputs), json.dumps(answers, ensure_ascii=False))
        data = service._zivo_ai_complete('survey', 'You are a senior customer-research analyst.', prompt,
                                         as_json=True, record=self)
        sentiment = data.get('sentiment') or {}

        def pct(key):
            try:
                return max(0, min(int(sentiment.get(key) or 0), 100))
            except (TypeError, ValueError):
                return 0
        themes = []
        for theme in (data.get('themes') or [])[:8]:
            if not isinstance(theme, dict) or not theme.get('name'):
                continue
            try:
                mentions = int(theme.get('mentions') or 0)
            except (TypeError, ValueError):
                mentions = 0
            themes.append((0, 0, {
                'name': str(theme['name'])[:120],
                'sentiment': theme.get('sentiment') if theme.get('sentiment') in ('positive', 'neutral', 'negative') else 'neutral',
                'mentions': mentions,
                'example': str(theme.get('example') or '')[:250],
            }))
        recos = data.get('recommendations') or []
        insight = self.env['zivo.survey.insight'].create({
            'survey_id': self.id,
            'responses_count': len(inputs),
            'answers_count': len(answers),
            'summary': Markup('<p>%s</p>') % escape(str(data.get('summary') or '')),
            'sentiment_positive': pct('positive'),
            'sentiment_neutral': pct('neutral'),
            'sentiment_negative': pct('negative'),
            'theme_ids': themes,
            'recommendations': '\n'.join('• %s' % r for r in recos if r) if isinstance(recos, list) else str(recos),
            'anonymous_excluded': exclude_anon,
        })
        return {
            'type': 'ir.actions.act_window', 'res_model': 'zivo.survey.insight', 'res_id': insight.id,
            'view_mode': 'form', 'target': 'current', 'name': self.env._('AI Survey Insights'),
        }

    def action_zivo_view_insights(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'res_model': 'zivo.survey.insight', 'view_mode': 'list,form',
            'domain': [('survey_id', '=', self.id)], 'name': self.env._('AI Insights'),
        }

    def action_zivo_remind_at_risk(self):
        """Email the resume link to saved respondents that did not come back (deterministic, no AI)."""
        self.ensure_one()
        template = self.env.ref('zivoit_event_connect.mail_template_survey_resume')
        sent = 0
        for ui in self._zivo_at_risk_inputs():
            email = ui.zivo_resume_email or ui.email or ui.partner_id.email
            if not email:
                continue
            template.sudo().send_mail(ui.id, email_values={'email_to': email},
                                      email_layout_xmlid='mail.mail_notification_light')
            ui.sudo().zivo_saved_date = fields.Datetime.now()
            sent += 1
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'type': 'success', 'sticky': False,
                       'message': self.env._('%s reminder(s) sent.', sent)},
        }
