from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    epc_ai_provider = fields.Selection(
        [
            ("none", "Built-in engine only (no LLM)"),
            ("openai", "OpenAI"),
            ("anthropic", "Anthropic Claude"),
            ("openai_compatible", "OpenAI-compatible endpoint (self-hosted / other)"),
        ],
        string="AI Provider",
        default="none",
        config_parameter="zivoit_epc_ai.provider",
    )
    epc_ai_api_key = fields.Char(string="AI API Key", config_parameter="zivoit_epc_ai.api_key")
    epc_ai_model = fields.Char(string="AI Model", config_parameter="zivoit_epc_ai.model",
                               help="Leave empty to use the provider default.")
    epc_ai_base_url = fields.Char(string="AI Base URL", config_parameter="zivoit_epc_ai.base_url",
                                  help="Only needed for OpenAI-compatible endpoints, e.g. http://localhost:11434/v1")
    epc_ai_timeout = fields.Integer(string="AI Timeout (s)", default=60, config_parameter="zivoit_epc_ai.timeout")
    epc_alert_delay_days = fields.Integer(
        string="Alert Threshold (days of slip)", default=7,
        config_parameter="zivoit_epc_ai.alert_delay_days",
        help="Activities predicted to slip more than this raise an early-warning risk.")
    epc_alert_probability = fields.Integer(
        string="Alert Threshold (delay probability %)", default=60,
        config_parameter="zivoit_epc_ai.alert_probability")

    def action_epc_test_ai_connection(self):
        self.ensure_one()
        self.execute()
        ok, message = self.env["epc.ai.service"].test_connection()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "AI connection",
                "message": message,
                "type": "success" if ok else "warning",
                "sticky": False,
            },
        }
