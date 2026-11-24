# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    eduagent_default_provider_id = fields.Many2one(
        related="company_id.eduagent_default_provider_id", readonly=False)
    eduagent_monthly_token_budget = fields.Integer(
        related="company_id.eduagent_monthly_token_budget", readonly=False)
    eduagent_learner_daily_messages = fields.Integer(
        related="company_id.eduagent_learner_daily_messages", readonly=False)
    eduagent_pii_masking = fields.Boolean(related="company_id.eduagent_pii_masking", readonly=False)
    eduagent_eu_mode = fields.Boolean(related="company_id.eduagent_eu_mode", readonly=False)
    eduagent_auto_nudge = fields.Boolean(related="company_id.eduagent_auto_nudge", readonly=False)
    eduagent_expected_course_days = fields.Integer(
        related="company_id.eduagent_expected_course_days", readonly=False)
    eduagent_require_oral_check = fields.Boolean(
        related="company_id.eduagent_require_oral_check", readonly=False)
    eduagent_admissions_team_id = fields.Many2one(
        related="company_id.eduagent_admissions_team_id", readonly=False)
    eduagent_disclosure_text = fields.Char(
        related="company_id.eduagent_disclosure_text", readonly=False)
