from odoo import fields, models


class EpcAiLog(models.Model):
    _name = "epc.ai.log"
    _description = "ZivoIT EPC AI Audit Log"
    _order = "id desc"

    feature = fields.Char(required=True, index=True, readonly=True)
    provider = fields.Char(readonly=True)
    model_name = fields.Char(string="LLM Model", readonly=True)
    res_model = fields.Char(string="Record Model", readonly=True)
    res_id = fields.Many2oneReference(string="Record ID", model_field="res_model", readonly=True)
    user_id = fields.Many2one("res.users", default=lambda self: self.env.user, readonly=True)
    success = fields.Boolean(readonly=True)
    used_fallback = fields.Boolean(string="Built-in Engine Used", readonly=True)
    error = fields.Text(readonly=True)
    prompt = fields.Text(readonly=True)
    response = fields.Text(readonly=True)
    duration_ms = fields.Integer(string="Duration (ms)", readonly=True)
