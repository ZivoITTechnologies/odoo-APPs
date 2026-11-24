# -*- coding: utf-8 -*-
import uuid

from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    eduagent_default_provider_id = fields.Many2one("eduagent.provider", string="Default AI Provider")
    eduagent_monthly_token_budget = fields.Integer(
        string="Monthly Token Budget", default=2000000,
        help="Total LLM tokens (input + output) this company may use per calendar month. 0 = unlimited.")
    eduagent_learner_daily_messages = fields.Integer(
        string="Learner Daily Message Limit", default=60,
        help="Maximum AI messages per learner per day across all agents. 0 = unlimited.")
    eduagent_pii_masking = fields.Boolean(string="Mask Personal Data Before LLM Calls", default=True)
    eduagent_eu_mode = fields.Boolean(
        string="EU AI Act Mode",
        help="Adds AI disclosures to every agent, forces human review on all grading and "
             "credential decisions and records the reviewer in the audit trail.")
    eduagent_auto_nudge = fields.Boolean(string="Send Retention Nudges Automatically", default=True)
    eduagent_expected_course_days = fields.Integer(string="Expected Course Duration (days)", default=60)
    eduagent_require_oral_check = fields.Boolean(
        string="Require Oral Check Before Credential",
        help="A learner must pass a human-reviewed integrity (viva) check before a credential is issued.")
    eduagent_credential_secret = fields.Char(
        string="Credential Signing Secret", copy=False, groups="base.group_system",
        default=lambda self: uuid.uuid4().hex)
    eduagent_admissions_team_id = fields.Many2one("crm.team", string="Admissions Sales Team")
    eduagent_disclosure_text = fields.Char(
        string="AI Disclosure", translate=True,
        default="You are chatting with an AI assistant. Answers may contain mistakes; "
                "a human can take over at any time.")
