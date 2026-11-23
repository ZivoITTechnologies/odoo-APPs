# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    zivo_api_enabled = fields.Boolean(string='Enable Headless Events API',
                                      config_parameter='zivoit_event_connect.api_enabled', default=True)
    zivo_api_page_size = fields.Integer(string='Default API Page Size',
                                        config_parameter='zivoit_event_connect.api_page_size', default=20)
    zivo_api_max_page_size = fields.Integer(string='Maximum API Page Size',
                                            config_parameter='zivoit_event_connect.api_max_page_size', default=100)
    zivo_api_cache_seconds = fields.Integer(string='API Cache-Control (seconds)',
                                            config_parameter='zivoit_event_connect.api_cache_seconds', default=60)

    # ---- AI -------------------------------------------------------------
    zivo_ai_enabled = fields.Boolean(string='Enable AI', config_parameter='zivoit_event_connect.ai_enabled')
    zivo_ai_provider = fields.Selection([
        ('openai', 'OpenAI'),
        ('anthropic', 'Anthropic Claude'),
        ('gemini', 'Google Gemini'),
        ('openai_compatible', 'OpenAI-compatible (Azure, Ollama, LM Studio, vLLM, OpenRouter…)'),
    ], string='AI Provider', config_parameter='zivoit_event_connect.ai_provider', default='openai')
    zivo_ai_api_key = fields.Char(string='API Key', config_parameter='zivoit_event_connect.ai_api_key')
    zivo_ai_model = fields.Char(string='Model', config_parameter='zivoit_event_connect.ai_model',
                                help='Leave empty for the provider default (gpt-4o-mini, claude-sonnet-4-5, '
                                     'gemini-2.0-flash, llama3.1).')
    zivo_ai_base_url = fields.Char(string='Base URL', config_parameter='zivoit_event_connect.ai_base_url',
                                   help='Only for OpenAI-compatible endpoints, e.g. http://localhost:11434/v1')
    zivo_ai_feature_writer = fields.Boolean(string='AI Event Writer', config_parameter='zivoit_event_connect.ai_feature_writer')
    zivo_ai_feature_search = fields.Boolean(string='AI Website Search', config_parameter='zivoit_event_connect.ai_feature_search')
    zivo_ai_feature_booking = fields.Boolean(string='AI Booking Assistant', config_parameter='zivoit_event_connect.ai_feature_booking')
    zivo_ai_booking_auto = fields.Boolean(string='Assess Bookings on Submit', config_parameter='zivoit_event_connect.ai_booking_auto')
    zivo_ai_feature_survey = fields.Boolean(string='AI Survey Insights', config_parameter='zivoit_event_connect.ai_feature_survey')
    zivo_ai_survey_exclude_anonymous = fields.Boolean(
        string='Exclude Anonymous Answers', config_parameter='zivoit_event_connect.ai_survey_exclude_anonymous')
    zivo_ai_feature_document = fields.Boolean(string='AI Document Assistant', config_parameter='zivoit_event_connect.ai_feature_document')
    zivo_ai_document_auto = fields.Boolean(string='Analyse on Upload', config_parameter='zivoit_event_connect.ai_document_auto')
    zivo_ai_feature_anomaly = fields.Boolean(string='AI Anomaly Explanations', config_parameter='zivoit_event_connect.ai_feature_anomaly')

    def action_zivo_ai_test(self):
        self.execute()
        return self.env['zivo.ai.service'].action_test_connection()

    def action_zivo_open_ai_log(self):
        return self.env['ir.actions.act_window']._for_xml_id('zivoit_event_connect.action_zivo_ai_log')

    def action_zivo_open_api_clients(self):
        return self.env['ir.actions.act_window']._for_xml_id('zivoit_event_connect.action_zivo_api_client')

    def action_zivo_open_webhooks(self):
        return self.env['ir.actions.act_window']._for_xml_id('zivoit_event_connect.action_zivo_webhook')
