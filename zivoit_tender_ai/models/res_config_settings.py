# -*- coding: utf-8 -*-
import pytz

from odoo import api, fields, models, _


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    zivoit_ai_provider = fields.Selection(
        [('none', 'Rules only (no AI)'), ('anthropic', 'Anthropic Claude'), ('openai', 'OpenAI')],
        string='AI Provider', default='none', config_parameter='zivoit_tender_ai.ai_provider')
    zivoit_ai_api_key = fields.Char('AI API Key', config_parameter='zivoit_tender_ai.ai_api_key')
    zivoit_ai_model = fields.Char('AI Model', config_parameter='zivoit_tender_ai.ai_model',
                                  help='Claude: claude-sonnet-5-5 / claude-haiku-5-5. OpenAI: gpt-4o-mini etc. '
                                       'Leave empty for the default model.')
    zivoit_ai_web_search = fields.Boolean('AI Web Search', config_parameter='zivoit_tender_ai.ai_web_search',
                                          default=True)
    zivoit_ai_web_tool = fields.Char('Web Search Tool Type', config_parameter='zivoit_tender_ai.ai_web_tool',
                                     help='Advanced. Claude default: web_search_20250305. OpenAI default: web_search.')
    zivoit_ai_max_searches = fields.Integer('Max Web Searches per Run', config_parameter='zivoit_tender_ai.ai_max_searches',
                                            default=5)
    zivoit_portal_tz = fields.Selection(
        '_tz_selection', string='Portal Time Zone', config_parameter='zivoit_tender_ai.portal_tz', default='Asia/Kolkata',
        help='Time zone in which the tender portals publish dates (Indian portals use IST).')
    zivoit_reminder_days = fields.Char('Deadline Reminders (days before)', config_parameter='zivoit_tender_ai.reminder_days',
                                       default='7,3,1')
    zivoit_auto_lead = fields.Boolean('Create CRM Opportunity on Shortlist',
                                      config_parameter='zivoit_tender_ai.auto_lead_on_shortlist')
    zivoit_auto_project = fields.Boolean('Create Project when Won', config_parameter='zivoit_tender_ai.auto_project_on_win',
                                         default=True)

    @api.model
    def _tz_selection(self):
        return [(tz, tz) for tz in pytz.common_timezones]

    def action_zivoit_test_ai(self):
        self.execute()
        AI = self.env['zivoit.tender.ai']
        if not AI._is_enabled():
            message, kind = _('No AI provider configured: the agent uses the built-in rule engine.'), 'warning'
        else:
            try:
                answer = AI._call_llm('Reply with the single word OK.', 'ping', max_tokens=10)
                message, kind = _('AI connection works. Answer: %s', answer[:50]), 'success'
            except Exception as error:  # noqa: BLE001
                message, kind = str(error)[:300], 'danger'
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': _('Tender AI'), 'message': message, 'type': kind, 'sticky': False}}
