import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .epc_activity import DISCIPLINES

NCR_DISPOSITIONS = [
    ("rework", "Rework"),
    ("repair", "Repair"),
    ("use_as_is", "Use as is (concession)"),
    ("reject", "Reject / replace"),
]
NCR_KEYWORDS = [
    ("reject", ["crack through", "wrong material", "wrong grade", "failed test", "collapsed", "not as per spec"]),
    ("repair", ["honeycomb", "crack", "chipped", "dent", "coating damage", "galvanising damage", "rust", "spall"]),
    ("rework", ["misaligned", "out of plumb", "out of level", "dimension", "wrong location", "torque", "loose bolt",
                "missing bolt", "sag"]),
    ("use_as_is", ["minor", "cosmetic", "within tolerance", "marginal"]),
]


def _seq(env, code):
    return env["ir.sequence"].next_by_code(code) or _("New")


class EpcQaInspection(models.Model):
    _name = "epc.qa.inspection"
    _description = "EPC Inspection Request (ITP)"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "planned_date desc, id desc"

    name = fields.Char(default=lambda self: _("New"), readonly=True, copy=False)
    project_id = fields.Many2one("project.project", required=True, index=True, ondelete="cascade",
                                 domain=[("is_epc", "=", True)])
    company_id = fields.Many2one(related="project_id.company_id", store=True, index=True)
    activity_id = fields.Many2one("epc.activity", domain="[('project_id', '=', project_id)]")
    description = fields.Char(required=True, help="What is inspected, e.g. 'Foundation loc 45 before concreting'")
    inspection_type = fields.Selection(
        [("hold", "Hold point"), ("witness", "Witness point"), ("review", "Document review"),
         ("surveillance", "Surveillance")], default="witness", required=True)
    discipline = fields.Selection(DISCIPLINES, default="civil", required=True)
    planned_date = fields.Date(default=fields.Date.context_today, required=True)
    inspector_id = fields.Many2one("res.users", default=lambda self: self.env.user)
    client_witness = fields.Boolean(string="Client / PMC witness")
    result = fields.Selection([("pending", "Pending"), ("pass", "Accepted"), ("fail", "Rejected")],
                              default="pending", required=True, tracking=True)
    remarks = fields.Text()
    ncr_ids = fields.One2many("epc.qa.ncr", "inspection_id", string="NCRs")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == _("New"):
                vals["name"] = _seq(self.env, "epc.qa.inspection")
        return super().create(vals_list)

    def action_pass(self):
        self.write({"result": "pass"})
        return True

    def action_fail(self):
        for inspection in self:
            inspection.result = "fail"
            if not inspection.ncr_ids:
                self.env["epc.qa.ncr"].create({
                    "project_id": inspection.project_id.id,
                    "activity_id": inspection.activity_id.id,
                    "inspection_id": inspection.id,
                    "description": _("Rejected at inspection %(ref)s: %(what)s. %(remarks)s",
                                     ref=inspection.name, what=inspection.description,
                                     remarks=inspection.remarks or ""),
                })
        return True


class EpcQaNcr(models.Model):
    _name = "epc.qa.ncr"
    _description = "EPC Non-Conformance Report"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    name = fields.Char(default=lambda self: _("New"), readonly=True, copy=False)
    project_id = fields.Many2one("project.project", required=True, index=True, ondelete="cascade",
                                 domain=[("is_epc", "=", True)])
    company_id = fields.Many2one(related="project_id.company_id", store=True, index=True)
    activity_id = fields.Many2one("epc.activity", domain="[('project_id', '=', project_id)]", index=True)
    inspection_id = fields.Many2one("epc.qa.inspection", ondelete="set null")
    raised_by = fields.Selection([("internal", "Internal QC"), ("client", "Client / PMC"),
                                  ("vendor", "Vendor inspection")], default="internal", required=True)
    description = fields.Text(required=True)
    severity = fields.Selection([("minor", "Minor"), ("major", "Major"), ("critical", "Critical")],
                                default="minor", required=True, tracking=True)
    root_cause = fields.Text()
    disposition = fields.Selection(NCR_DISPOSITIONS, tracking=True)
    corrective_action = fields.Text()
    responsible_id = fields.Many2one("res.users", default=lambda self: self.env.user)
    due_date = fields.Date()
    state = fields.Selection([("open", "Open"), ("action", "Action in progress"), ("verified", "Verified"),
                              ("closed", "Closed")], default="open", required=True, tracking=True)
    ai_source = fields.Selection([("llm", "AI model"), ("fallback", "Built-in engine")], string="AI Source", readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == _("New"):
                vals["name"] = _seq(self.env, "epc.qa.ncr")
        return super().create(vals_list)

    @api.model
    def _fallback_suggest(self, text):
        low = (text or "").lower()
        disposition = "rework"
        for candidate, words in NCR_KEYWORDS:
            if any(w in low for w in words):
                disposition = candidate
                break
        actions = {
            "rework": "Dismantle/correct to drawing tolerance; re-inspect before the next activity.",
            "repair": "Repair per approved repair procedure (e.g. epoxy grout / zinc-rich paint); QC to witness.",
            "use_as_is": "Seek client concession with technical justification; record in as-built.",
            "reject": "Remove and replace with conforming material; quarantine the rejected lot.",
        }
        return {"disposition": disposition,
                "root_cause": "To be confirmed by QC: workmanship, material or method. Check the ITP step "
                              "that should have caught it.",
                "corrective_action": actions[disposition]}

    def action_ai_suggest(self):
        for ncr in self:
            answer = self.env["epc.ai.service"].ask(
                "ncr_suggest",
                "You are a QA/QC engineer on an EPC project. For this non-conformance, return JSON only with keys "
                "disposition (one of: rework, repair, use_as_is, reject), root_cause (max 3 lines) and "
                "corrective_action (max 3 lines).",
                json.dumps({"description": ncr.description, "severity": ncr.severity,
                            "activity": ncr.activity_id.display_name or ""}),
                lambda: self._fallback_suggest(ncr.description), record=ncr, expect_json=True)
            data = answer["result"] if isinstance(answer["result"], dict) else {}
            disposition = data.get("disposition")
            ncr.write({
                "disposition": disposition if disposition in dict(NCR_DISPOSITIONS) else "rework",
                "root_cause": str(data.get("root_cause") or "")[:2000],
                "corrective_action": str(data.get("corrective_action") or "")[:2000],
                "ai_source": answer["source"],
            })
        return True

    def action_start(self):
        for ncr in self:
            if not ncr.disposition:
                raise UserError(_("Choose a disposition for %s first.", ncr.name))
            ncr.state = "action"
        return True

    def action_verify(self):
        self.filtered(lambda n: n.state == "action").write({"state": "verified"})
        return True

    def action_close(self):
        for ncr in self:
            if ncr.state != "verified":
                raise UserError(_("%s must be verified before closing.", ncr.name))
            ncr.state = "closed"
        return True


class EpcPunchItem(models.Model):
    _name = "epc.punch.item"
    _description = "EPC Punch List Item"
    _inherit = ["mail.thread"]
    _order = "category, due_date, id"

    name = fields.Char(string="Item", required=True)
    project_id = fields.Many2one("project.project", required=True, index=True, ondelete="cascade",
                                 domain=[("is_epc", "=", True)])
    company_id = fields.Many2one(related="project_id.company_id", store=True, index=True)
    activity_id = fields.Many2one("epc.activity", domain="[('project_id', '=', project_id)]")
    system = fields.Char(string="System / Area")
    category = fields.Selection(
        [("A", "A — before energisation / handover"), ("B", "B — before final acceptance"), ("C", "C — minor")],
        default="B", required=True)
    discipline = fields.Selection(DISCIPLINES, default="general", required=True)
    responsible_id = fields.Many2one("res.users")
    due_date = fields.Date()
    state = fields.Selection([("open", "Open"), ("closed", "Closed")], default="open", required=True, tracking=True)
    closed_date = fields.Date(readonly=True)

    def action_close(self):
        self.write({"state": "closed", "closed_date": fields.Date.context_today(self)})
        return True


class ProjectProjectQuality(models.Model):
    _inherit = "project.project"

    epc_open_ncr_count = fields.Integer(compute="_compute_epc_quality_kpis")
    epc_open_punch_a_count = fields.Integer(string="Open Punch A", compute="_compute_epc_quality_kpis")
    epc_open_punch_count = fields.Integer(compute="_compute_epc_quality_kpis")
    epc_inspection_pass_rate = fields.Float(string="Inspection Pass Rate (%)", compute="_compute_epc_quality_kpis",
                                            digits=(5, 1))

    def _compute_epc_quality_kpis(self):
        Ncr = self.env["epc.qa.ncr"].sudo()
        Punch = self.env["epc.punch.item"].sudo()
        Inspection = self.env["epc.qa.inspection"].sudo()
        for project in self:
            if not project.id:
                project.update({"epc_open_ncr_count": 0, "epc_open_punch_a_count": 0, "epc_open_punch_count": 0,
                                "epc_inspection_pass_rate": 0.0})
                continue
            project.epc_open_ncr_count = Ncr.search_count([("project_id", "=", project.id), ("state", "!=", "closed")])
            open_punch = Punch.search([("project_id", "=", project.id), ("state", "=", "open")])
            project.epc_open_punch_count = len(open_punch)
            project.epc_open_punch_a_count = len(open_punch.filtered(lambda p: p.category == "A"))
            done = Inspection.search([("project_id", "=", project.id), ("result", "in", ("pass", "fail"))])
            passed = len(done.filtered(lambda i: i.result == "pass"))
            project.epc_inspection_pass_rate = passed / len(done) * 100.0 if done else 0.0

    def action_epc_view_ncrs(self):
        action = self._epc_action("epc.qa.ncr", _("Non-Conformance Reports"), "list,form")
        action["context"]["search_default_open"] = 1
        return action

    def action_epc_view_punch(self):
        action = self._epc_action("epc.punch.item", _("Punch List"), "list,form")
        action["context"]["search_default_open"] = 1
        return action

    def action_epc_view_inspections(self):
        return self._epc_action("epc.qa.inspection", _("Inspections"), "list,form")


class EpcPlant(models.Model):
    _name = "epc.plant"
    _description = "EPC Plant & Equipment"
    _order = "name"

    name = fields.Char(required=True)
    code = fields.Char(string="Asset / Reg. No.")
    plant_type = fields.Selection(
        [("crane", "Crane"), ("excavator", "Excavator / JCB"), ("piling_rig", "Piling rig"),
         ("concrete", "Concrete mixer / pump / batching"), ("winch", "Winch / derrick"),
         ("tensioner", "Puller / tensioner (stringing)"), ("dg_set", "DG set"), ("vehicle", "Vehicle / trailer"),
         ("other", "Other")], default="other", required=True)
    ownership = fields.Selection([("owned", "Owned"), ("hired", "Hired")], default="owned", required=True)
    vendor_id = fields.Many2one("res.partner", string="Hire vendor")
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id)
    hire_rate_per_day = fields.Monetary(currency_field="currency_id")
    project_id = fields.Many2one("project.project", string="Current project", domain=[("is_epc", "=", True)])
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company, index=True)
    active = fields.Boolean(default=True)
    log_ids = fields.One2many("epc.dpr.plant", "plant_id", string="Usage logs")
    hours_worked = fields.Float(compute="_compute_utilisation")
    hours_idle = fields.Float(compute="_compute_utilisation")
    hours_breakdown = fields.Float(compute="_compute_utilisation")
    utilisation = fields.Float(string="Utilisation (%)", compute="_compute_utilisation", digits=(5, 1))
    availability = fields.Float(string="Availability (%)", compute="_compute_utilisation", digits=(5, 1))

    @api.depends("log_ids.hours_worked", "log_ids.hours_idle", "log_ids.hours_breakdown", "log_ids.dpr_id.state")
    def _compute_utilisation(self):
        for plant in self:
            logs = plant.log_ids.filtered(lambda l: l.dpr_id.state == "approved")
            worked = sum(logs.mapped("hours_worked"))
            idle = sum(logs.mapped("hours_idle"))
            breakdown = sum(logs.mapped("hours_breakdown"))
            total = worked + idle + breakdown
            plant.hours_worked = worked
            plant.hours_idle = idle
            plant.hours_breakdown = breakdown
            plant.utilisation = worked / total * 100.0 if total else 0.0
            plant.availability = (worked + idle) / total * 100.0 if total else 0.0


class EpcDprPlant(models.Model):
    _name = "epc.dpr.plant"
    _description = "EPC DPR Plant Log"

    dpr_id = fields.Many2one("epc.dpr", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="dpr_id.company_id", store=True)
    plant_id = fields.Many2one("epc.plant", required=True, ondelete="restrict", index=True)
    activity_id = fields.Many2one("epc.activity", domain="[('project_id', '=', parent.project_id)]")
    hours_worked = fields.Float()
    hours_idle = fields.Float()
    hours_breakdown = fields.Float()
    remarks = fields.Char()

    @api.constrains("hours_worked", "hours_idle", "hours_breakdown")
    def _check_hours(self):
        for log in self:
            values = (log.hours_worked, log.hours_idle, log.hours_breakdown)
            if any(v < 0 for v in values) or sum(values) > 24:
                raise ValidationError(_("Plant hours must be positive and total at most 24 per day."))
