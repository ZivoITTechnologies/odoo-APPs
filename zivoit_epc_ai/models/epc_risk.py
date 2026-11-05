from odoo import api, fields, models

from .project_project import RISK_LEVELS

RISK_CATEGORIES = [
    ("schedule", "Schedule"),
    ("cost", "Cost"),
    ("procurement", "Procurement / Supply chain"),
    ("engineering", "Engineering / Design"),
    ("site", "Site / Execution"),
    ("contract", "Contractual / Commercial"),
    ("hse", "Health, Safety & Environment"),
    ("quality", "Quality"),
    ("external", "External / Statutory"),
]


class EpcRisk(models.Model):
    _name = "epc.risk"
    _description = "EPC Risk"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "state_sequence, score desc, id desc"

    name = fields.Char(required=True, tracking=True)
    project_id = fields.Many2one("project.project", required=True, index=True, ondelete="cascade",
                                 domain=[("is_epc", "=", True)])
    company_id = fields.Many2one(related="project_id.company_id", store=True, index=True)
    activity_id = fields.Many2one("epc.activity", domain="[('project_id', '=', project_id)]", ondelete="set null")
    purchase_line_id = fields.Many2one("purchase.order.line", string="Procurement Line", ondelete="set null")
    category = fields.Selection(RISK_CATEGORIES, default="schedule", required=True)
    description = fields.Text()
    probability = fields.Selection([(str(i), str(i)) for i in range(1, 6)], default="3", required=True,
                                   help="1 = rare, 5 = almost certain")
    impact = fields.Selection([(str(i), str(i)) for i in range(1, 6)], default="3", required=True,
                              help="1 = negligible, 5 = severe")
    score = fields.Integer(compute="_compute_score", store=True)
    level = fields.Selection(RISK_LEVELS, compute="_compute_score", store=True)
    owner_id = fields.Many2one("res.users", string="Owner", default=lambda self: self.env.user, tracking=True)
    mitigation = fields.Text()
    due_date = fields.Date(string="Mitigation Due")
    state = fields.Selection(
        [("open", "Open"), ("mitigating", "Mitigating"), ("closed", "Closed")],
        default="open", required=True, tracking=True)
    state_sequence = fields.Integer(compute="_compute_state_sequence", store=True)
    source = fields.Selection(
        [("manual", "Manual"), ("engine", "Prediction engine"), ("contract", "Contract review"), ("ai", "AI assistant")],
        default="manual", required=True, readonly=True)
    engine_key = fields.Char(index=True, readonly=True, copy=False,
                             help="Technical key used by the prediction engine to update its own alerts.")
    color = fields.Integer(compute="_compute_color")

    @api.depends("probability", "impact")
    def _compute_score(self):
        for risk in self:
            score = int(risk.probability or 0) * int(risk.impact or 0)
            risk.score = score
            if score >= 20:
                level = "critical"
            elif score >= 12:
                level = "high"
            elif score >= 6:
                level = "medium"
            else:
                level = "low"
            risk.level = level

    @api.depends("level")
    def _compute_color(self):
        colors = {"low": 10, "medium": 3, "high": 2, "critical": 1}
        for risk in self:
            risk.color = colors.get(risk.level, 0)

    @api.depends("state")
    def _compute_state_sequence(self):
        order = {"open": 1, "mitigating": 2, "closed": 3}
        for risk in self:
            risk.state_sequence = order.get(risk.state, 9)

    def action_close(self):
        self.write({"state": "closed"})
        return True

    def action_mitigate(self):
        self.write({"state": "mitigating"})
        return True

    def action_reopen(self):
        self.write({"state": "open"})
        return True
