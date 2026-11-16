# -*- coding: utf-8 -*-
from odoo import _, fields, models

from .ai_service import PROVIDERS, RETAIL_SECTORS


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    zlms_ai_provider = fields.Selection(PROVIDERS, string='AI Provider', default='none',
                                        config_parameter='zivoit_retail_lms.ai_provider')
    zlms_ai_api_key = fields.Char(string='AI API Key', config_parameter='zivoit_retail_lms.ai_api_key')
    zlms_ai_model = fields.Char(string='AI Model', config_parameter='zivoit_retail_lms.ai_model',
                                help='Leave empty to use the provider default.')
    zlms_ai_base_url = fields.Char(string='AI Base URL', config_parameter='zivoit_retail_lms.ai_base_url',
                                   help='Only for OpenAI-compatible endpoints or proxies, e.g. https://api.groq.com/openai/v1')
    zlms_ai_temperature = fields.Float(string='AI Creativity', default=0.4,
                                       config_parameter='zivoit_retail_lms.ai_temperature')
    zlms_brand_name = fields.Char(string='Brand Name', config_parameter='zivoit_retail_lms.brand_name',
                                  help='Used by the AI engines. Defaults to the company name.')
    zlms_retail_sector = fields.Selection(RETAIL_SECTORS, string='Retail Sector', default='general',
                                          config_parameter='zivoit_retail_lms.retail_sector')
    zlms_market = fields.Char(string='Market / Country', config_parameter='zivoit_retail_lms.market',
                              help='e.g. India, UAE, United Kingdom - helps the AI use local context.')
    zlms_boost_daily_questions = fields.Integer(string='Daily Boost Questions', default=5,
                                                config_parameter='zivoit_retail_lms.boost_daily_questions')

    def action_zlms_test_ai(self):
        self.set_values()
        answer = self.env['zlms.ai.service'].test_connection()
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _('AI connected'), 'message': _('Provider answered: %s', (answer or '')[:80]),
                       'type': 'success', 'sticky': False},
        }
