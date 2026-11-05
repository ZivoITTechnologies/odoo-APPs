import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

PERMIT_TYPES = [
    ("hot_work", "Hot work (welding, cutting, grinding)"),
    ("height", "Work at height"),
    ("excavation", "Excavation"),
    ("electrical", "Electrical isolation / live line"),
    ("confined_space", "Confined space"),
    ("lifting", "Critical lifting"),
    ("general", "General work"),
]

PERMIT_CHECKLISTS = {
    "hot_work": [
        "Fire extinguisher (DCP/CO2) within 10 m and fire watcher assigned",
        "Combustibles removed or covered within 11 m",
        "Gas cylinders upright, chained, with flashback arrestors",
        "Welding machine earthed; cables free of damage",
        "Face shield, leather gloves and apron worn",
        "Area checked 30 minutes after work for smouldering",
    ],
    "height": [
        "Full-body harness with double lanyard, anchored 100% of the time",
        "Scaffold tagged green / ladder inspected and secured",
        "Lifeline or safety net installed where needed",
        "Tools tethered; area below barricaded",
        "Worker medically fit for height work",
        "Wind speed checked (stop above site limit)",
    ],
    "excavation": [
        "Underground services located and marked before digging",
        "Sides sloped, benched or shored beyond 1.5 m depth",
        "Spoil kept at least 1 m from the edge",
        "Hard barricade and warning signs around the pit",
        "Safe access ladder every 7.5 m of trench",
        "Dewatering arranged; pit checked after rain",
    ],
    "electrical": [
        "Line/equipment isolated and permit from the controlling authority received",
        "Lock-out / tag-out applied and verified with a tester",
        "Earthing (discharge rods) applied on both sides of the work zone",
        "Insulated tools and rated gloves in use",
        "Minimum approach distances briefed to the crew",
        "Supervisor present throughout the work",
    ],
    "confined_space": [
        "Atmosphere tested for O2, LEL, H2S and CO before entry",
        "Continuous ventilation in place",
        "Standby attendant outside with rescue plan and tripod",
        "Entry log maintained",
        "Intrinsically safe lighting and tools",
        "Communication method agreed",
    ],
    "lifting": [
        "Lifting plan and load chart checked; load under 75% of capacity",
        "Crane and tackles have valid test certificates",
        "Ground bearing checked; outriggers fully extended on pads",
        "Exclusion zone barricaded; tag lines used",
        "Certified rigger and signaller assigned",
        "Wind speed within crane limits",
    ],
    "general": [
        "Toolbox talk held and attendance recorded",
        "Required PPE worn: helmet, shoes, high-visibility vest",
        "Work area barricaded and housekeeping in order",
        "Emergency contacts and first-aid kit available",
    ],
}

INCIDENT_TYPES = [
    ("near_miss", "Near miss"),
    ("first_aid", "First aid case"),
    ("medical", "Medical treatment case"),
    ("lti", "Lost time injury"),
    ("fatality", "Fatality"),
    ("property_damage", "Property / equipment damage"),
    ("environmental", "Environmental"),
]
ROOT_CAUSE_CATEGORIES = [
    ("people", "People / behaviour"),
    ("equipment", "Equipment / tools"),
    ("method", "Method / procedure"),
    ("material", "Material"),
    ("environment", "Environment / site conditions"),
    ("management", "Supervision / management system"),
]
ROOT_CAUSE_KEYWORDS = [
    ("equipment", ["crane", "sling", "brake", "failure", "defect", "broken", "machine", "tool", "scaffold",
                   "ladder", "cable", "wire rope", "hydraulic"]),
    ("environment", ["rain", "wind", "slippery", "mud", "heat", "dark", "lighting", "storm", "flood", "terrain"]),
    ("method", ["procedure", "method statement", "permit", "sequence", "without plan", "no plan", "shortcut"]),
    ("material", ["material", "stack", "pile of", "falling object", "load", "loose"]),
    ("management", ["supervisor", "supervision", "training", "untrained", "induction", "briefing"]),
    ("people", ["harness", "ppe", "helmet", "not wearing", "careless", "fatigue", "rushed", "horseplay"]),
]
OBSERVATION_TYPES = [
    ("unsafe_act", "Unsafe act"),
    ("unsafe_condition", "Unsafe condition"),
    ("ppe", "PPE violation"),
    ("housekeeping", "Housekeeping"),
    ("environment", "Environmental"),
    ("good_practice", "Good practice"),
]
SEVERITY = [("low", "Low"), ("medium", "Medium"), ("high", "High")]


def _seq(env, code):
    return env["ir.sequence"].next_by_code(code) or _("New")


class EpcHsePermit(models.Model):
    _name = "epc.hse.permit"
    _description = "EPC Permit to Work"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "valid_from desc, id desc"

    name = fields.Char(default=lambda self: _("New"), readonly=True, copy=False)
    project_id = fields.Many2one("project.project", required=True, index=True, ondelete="cascade",
                                 domain=[("is_epc", "=", True)])
    company_id = fields.Many2one(related="project_id.company_id", store=True, index=True)
    activity_id = fields.Many2one("epc.activity", domain="[('project_id', '=', project_id)]")
    permit_type = fields.Selection(PERMIT_TYPES, required=True, default="general", tracking=True)
    location = fields.Char(required=True)
    work_description = fields.Text(required=True)
    valid_from = fields.Datetime(required=True, default=fields.Datetime.now)
    valid_to = fields.Datetime(required=True)
    issuer_id = fields.Many2one("res.users", string="Issued by", default=lambda self: self.env.user)
    receiver = fields.Char(string="Permit receiver (crew leader)", required=True)
    workers_count = fields.Integer(string="Workers")
    precautions = fields.Text(string="Precautions checklist")
    state = fields.Selection(
        [("draft", "Draft"), ("approved", "Approved"), ("closed", "Closed"), ("cancelled", "Cancelled")],
        default="draft", required=True, tracking=True)
    is_expired = fields.Boolean(compute="_compute_is_expired", search="_search_is_expired")

    @api.depends("valid_to", "state")
    def _compute_is_expired(self):
        now = fields.Datetime.now()
        for permit in self:
            permit.is_expired = permit.state == "approved" and bool(permit.valid_to) and permit.valid_to < now

    def _search_is_expired(self, operator, value):
        if operator not in ("=", "!="):
            raise UserError(_("Unsupported search on expired permits."))
        positive = (operator == "=") == bool(value)
        domain = [("state", "=", "approved"), ("valid_to", "<", fields.Datetime.now())]
        return domain if positive else ["!", "&"] + domain

    @api.constrains("valid_from", "valid_to")
    def _check_validity(self):
        for permit in self:
            if permit.valid_to and permit.valid_from and permit.valid_to <= permit.valid_from:
                raise ValidationError(_("The permit must end after it starts."))
            if permit.valid_to and permit.valid_from and \
                    (permit.valid_to - permit.valid_from).total_seconds() > 24 * 3600:
                raise ValidationError(_("A permit to work cannot be valid for more than 24 hours."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == _("New"):
                vals["name"] = _seq(self.env, "epc.hse.permit")
            if not vals.get("precautions") and vals.get("permit_type"):
                vals["precautions"] = "\n".join("[ ] " + c for c in PERMIT_CHECKLISTS[vals["permit_type"]])
        return super().create(vals_list)

    def action_ai_precautions(self):
        for permit in self:
            base = PERMIT_CHECKLISTS[permit.permit_type]
            answer = self.env["epc.ai.service"].ask(
                "permit_precautions",
                "You are an HSE officer on an Indian EPC construction site. Write a concise precautions checklist "
                "for this permit to work, one item per line starting with '[ ] '. Include the standard items given "
                "and add job-specific ones. No other text.",
                json.dumps({"permit_type": permit.permit_type, "location": permit.location,
                            "work": permit.work_description, "standard_items": base}),
                lambda: "\n".join("[ ] " + c for c in base), record=permit)
            permit.precautions = answer["result"].strip()
        return True

    def action_approve(self):
        for permit in self:
            if permit.state != "draft":
                continue
            if not (permit.precautions or "").strip():
                raise UserError(_("Fill the precautions checklist before approving."))
            permit.state = "approved"
        return True

    def action_close(self):
        self.filtered(lambda p: p.state == "approved").write({"state": "closed"})
        return True

    def action_cancel(self):
        self.filtered(lambda p: p.state in ("draft", "approved")).write({"state": "cancelled"})
        return True


class EpcHseIncident(models.Model):
    _name = "epc.hse.incident"
    _description = "EPC HSE Incident"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date desc, id desc"

    name = fields.Char(default=lambda self: _("New"), readonly=True, copy=False)
    project_id = fields.Many2one("project.project", required=True, index=True, ondelete="cascade",
                                 domain=[("is_epc", "=", True)])
    company_id = fields.Many2one(related="project_id.company_id", store=True, index=True)
    activity_id = fields.Many2one("epc.activity", domain="[('project_id', '=', project_id)]")
    date = fields.Datetime(required=True, default=fields.Datetime.now, tracking=True)
    incident_type = fields.Selection(INCIDENT_TYPES, required=True, default="near_miss", tracking=True)
    severity = fields.Selection(SEVERITY, compute="_compute_severity", store=True, readonly=False)
    location = fields.Char()
    description = fields.Text(required=True)
    persons_involved = fields.Integer()
    lost_days = fields.Integer(string="Lost workdays")
    immediate_action = fields.Text()
    root_cause_category = fields.Selection(ROOT_CAUSE_CATEGORIES, tracking=True)
    root_cause = fields.Text()
    corrective_actions = fields.Text()
    ai_source = fields.Selection([("llm", "AI model"), ("fallback", "Built-in engine")], string="AI Source", readonly=True, copy=False)
    state = fields.Selection([("reported", "Reported"), ("investigating", "Under investigation"),
                              ("closed", "Closed")], default="reported", required=True, tracking=True)
    risk_id = fields.Many2one("epc.risk", readonly=True, copy=False)

    @api.depends("incident_type")
    def _compute_severity(self):
        mapping = {"near_miss": "low", "first_aid": "low", "medical": "medium", "property_damage": "medium",
                   "environmental": "medium", "lti": "high", "fatality": "high"}
        for incident in self:
            incident.severity = mapping.get(incident.incident_type, "medium")

    @api.constrains("lost_days", "persons_involved")
    def _check_numbers(self):
        for incident in self:
            if incident.lost_days < 0 or incident.persons_involved < 0:
                raise ValidationError(_("Counts cannot be negative."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == _("New"):
                vals["name"] = _seq(self.env, "epc.hse.incident")
        incidents = super().create(vals_list)
        for incident in incidents.filtered(lambda i: i.severity == "high"):
            risk = self.env["epc.risk"].sudo().create({
                "name": _("HSE: %(type)s at %(loc)s", type=dict(INCIDENT_TYPES)[incident.incident_type],
                          loc=incident.location or incident.project_id.name)[:250],
                "project_id": incident.project_id.id,
                "activity_id": incident.activity_id.id,
                "category": "hse",
                "probability": "3",
                "impact": "5",
                "description": incident.description,
                "source": "manual",
            })
            incident.risk_id = risk
        return incidents

    @api.model
    def _fallback_root_cause(self, text):
        low = (text or "").lower()
        scores = {cat: sum(1 for w in words if w in low) for cat, words in ROOT_CAUSE_KEYWORDS}
        category = max(scores, key=scores.get) if any(scores.values()) else "method"
        actions = {
            "people": "Re-brief the crew on the specific rule broken; supervisor PPE/behaviour check at every shift start.",
            "equipment": "Withdraw and inspect the equipment; verify test certificates and pre-use checklists for similar items.",
            "method": "Review the method statement and permit for this task; add the missing control and re-train the crew.",
            "material": "Fix stacking/securing rules for the material; barricade drop zones and use tag lines.",
            "environment": "Add a weather/site-condition check to the daily permit; stop-work criteria for rain, wind and poor light.",
            "management": "Assign a competent supervisor; verify induction and task training records before work.",
        }
        return {
            "root_cause_category": category,
            "root_cause": "Why 1: what failed directly?\nWhy 2: why was it not prevented?\nWhy 3: which control was "
                          "missing or not followed?\nInitial classification from the report: %s." %
                          dict(ROOT_CAUSE_CATEGORIES)[category],
            "corrective_actions": actions[category],
        }

    def action_ai_root_cause(self):
        for incident in self:
            text = "%s\n%s" % (incident.description or "", incident.immediate_action or "")
            answer = self.env["epc.ai.service"].ask(
                "incident_root_cause",
                "You are an HSE investigator for construction. From the incident report, return JSON only with keys "
                "root_cause_category (one of: %s), root_cause (5-whys style, max 6 lines), corrective_actions "
                "(max 4 concrete actions)." % ", ".join(dict(ROOT_CAUSE_CATEGORIES)),
                json.dumps({"type": incident.incident_type, "location": incident.location, "report": text}),
                lambda: self._fallback_root_cause(text), record=incident, expect_json=True)
            data = answer["result"] if isinstance(answer["result"], dict) else {}
            category = data.get("root_cause_category")
            incident.write({
                "root_cause_category": category if category in dict(ROOT_CAUSE_CATEGORIES) else "method",
                "root_cause": str(data.get("root_cause") or "")[:4000],
                "corrective_actions": str(data.get("corrective_actions") or "")[:4000],
                "ai_source": answer["source"],
                "state": "investigating" if incident.state == "reported" else incident.state,
            })
        return True

    def action_close(self):
        for incident in self:
            if not incident.root_cause or not incident.corrective_actions:
                raise UserError(_("Record the root cause and corrective actions before closing %s.", incident.name))
            incident.state = "closed"
        return True


class EpcHseObservation(models.Model):
    _name = "epc.hse.observation"
    _description = "EPC Safety Observation"
    _inherit = ["mail.thread"]
    _order = "date desc, id desc"

    name = fields.Char(required=True)
    project_id = fields.Many2one("project.project", required=True, index=True, ondelete="cascade",
                                 domain=[("is_epc", "=", True)])
    company_id = fields.Many2one(related="project_id.company_id", store=True, index=True)
    activity_id = fields.Many2one("epc.activity", domain="[('project_id', '=', project_id)]")
    dpr_id = fields.Many2one("epc.dpr", string="From DPR", ondelete="set null")
    date = fields.Date(default=fields.Date.context_today, required=True)
    observation_type = fields.Selection(OBSERVATION_TYPES, required=True, default="unsafe_condition")
    severity = fields.Selection(SEVERITY, default="medium", required=True)
    location = fields.Char()
    description = fields.Text()
    source = fields.Selection([("manual", "Manual"), ("ai_photo", "AI photo review")], default="manual",
                              required=True, readonly=True)
    action_by_id = fields.Many2one("res.users", string="Action by")
    due_date = fields.Date()
    state = fields.Selection([("open", "Open"), ("closed", "Closed")], default="open", required=True, tracking=True)

    def action_close(self):
        self.write({"state": "closed"})
        return True


class ProjectProjectHse(models.Model):
    _inherit = "project.project"

    epc_shift_hours = fields.Float(string="Hours per Shift", default=8.0)
    epc_man_hours = fields.Float(string="Man-hours Worked", compute="_compute_epc_hse_kpis")
    epc_lti_count = fields.Integer(string="Lost Time Injuries", compute="_compute_epc_hse_kpis")
    epc_ltifr = fields.Float(string="LTIFR (per million hours)", compute="_compute_epc_hse_kpis", digits=(6, 2))
    epc_safe_man_hours = fields.Float(string="Safe Man-hours since last LTI", compute="_compute_epc_hse_kpis")
    epc_open_observation_count = fields.Integer(compute="_compute_epc_hse_kpis")
    epc_active_permit_count = fields.Integer(compute="_compute_epc_hse_kpis")
    epc_incident_count = fields.Integer(compute="_compute_epc_hse_kpis")

    def _compute_epc_hse_kpis(self):
        Manpower = self.env["epc.dpr.manpower"].sudo()
        Incident = self.env["epc.hse.incident"].sudo()
        for project in self:
            if not project.id or not project.is_epc:
                project.update({"epc_man_hours": 0, "epc_lti_count": 0, "epc_ltifr": 0, "epc_safe_man_hours": 0,
                                "epc_open_observation_count": 0, "epc_active_permit_count": 0,
                                "epc_incident_count": 0})
                continue
            manpower = Manpower.search([("dpr_id.project_id", "=", project.id), ("dpr_id.state", "=", "approved")])
            hours = sum(m.count for m in manpower) * (project.epc_shift_hours or 8.0)
            incidents = Incident.search([("project_id", "=", project.id)])
            ltis = incidents.filtered(lambda i: i.incident_type in ("lti", "fatality"))
            last_lti = max(ltis.mapped("date")) if ltis else False
            if last_lti:
                after = manpower.filtered(lambda m: m.dpr_id.date > last_lti.date())
                safe = sum(m.count for m in after) * (project.epc_shift_hours or 8.0)
            else:
                safe = hours
            project.epc_man_hours = hours
            project.epc_lti_count = len(ltis)
            project.epc_ltifr = len(ltis) * 1000000.0 / hours if hours else 0.0
            project.epc_safe_man_hours = safe
            project.epc_incident_count = len(incidents)
            project.epc_open_observation_count = self.env["epc.hse.observation"].sudo().search_count(
                [("project_id", "=", project.id), ("state", "=", "open")])
            project.epc_active_permit_count = self.env["epc.hse.permit"].sudo().search_count(
                [("project_id", "=", project.id), ("state", "=", "approved")])

    def action_epc_view_hse(self):
        return self._epc_action("epc.hse.incident", _("Incidents"), "list,form")

    def action_epc_view_observations(self):
        action = self._epc_action("epc.hse.observation", _("Safety Observations"), "list,form")
        action["context"]["search_default_open"] = 1
        return action

    def action_epc_view_permits(self):
        return self._epc_action("epc.hse.permit", _("Permits to Work"), "list,form")

