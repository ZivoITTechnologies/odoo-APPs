# -*- coding: utf-8 -*-
from odoo import api, fields, models


class EduagentUsageLog(models.Model):
    _name = "eduagent.usage.log"
    _description = "EduAgent AI Audit & Usage Log"
    _order = "create_date desc, id desc"

    create_date = fields.Datetime(string="Time", readonly=True)
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company, readonly=True)
    agent_id = fields.Many2one("eduagent.agent", readonly=True, ondelete="set null")
    provider_id = fields.Many2one("eduagent.provider", readonly=True, ondelete="set null")
    model_name = fields.Char(string="Model", readonly=True)
    purpose = fields.Char(readonly=True)
    user_id = fields.Many2one("res.users", default=lambda self: self.env.user, readonly=True)
    partner_id = fields.Many2one("res.partner", string="Learner / Contact", readonly=True)
    input_tokens = fields.Integer(readonly=True)
    output_tokens = fields.Integer(readonly=True)
    total_tokens = fields.Integer(compute="_compute_total", store=True)
    cost = fields.Float(string="Est. Cost (USD)", digits=(12, 6), readonly=True)
    latency_ms = fields.Integer(string="Latency (ms)", readonly=True)
    status = fields.Selection([("ok", "OK"), ("error", "Error"), ("blocked", "Blocked")],
                              default="ok", readonly=True)
    high_risk = fields.Boolean(
        string="High-risk Decision Support", readonly=True,
        help="Grading, credential or admission support. A human must take the final decision.")
    pii_masked = fields.Boolean(readonly=True)
    error = fields.Text(readonly=True)
    request_excerpt = fields.Text(readonly=True)
    response_excerpt = fields.Text(readonly=True)

    @api.depends("input_tokens", "output_tokens")
    def _compute_total(self):
        for rec in self:
            rec.total_tokens = (rec.input_tokens or 0) + (rec.output_tokens or 0)
