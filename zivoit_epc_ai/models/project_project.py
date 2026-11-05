import json
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

RISK_LEVELS = [
    ("low", "Low"),
    ("medium", "Medium"),
    ("high", "High"),
    ("critical", "Critical"),
]

EPC_SEGMENTS = [
    ("td", "Transmission & Distribution"),
    ("power", "Power Generation"),
    ("renewables", "Renewables"),
    ("oil_gas", "Oil & Gas / Pipelines"),
    ("roads", "Roads & Highways"),
    ("rail", "Railways & Metro"),
    ("buildings", "Buildings & Factories"),
    ("water", "Water & Irrigation"),
    ("industrial", "Industrial / Process Plants"),
    ("other", "Other"),
]


class ProjectProject(models.Model):
    _inherit = "project.project"

    epc_currency_id = fields.Many2one("res.currency", string="EPC Currency", compute="_compute_epc_currency_id",
                                      store=True,
                                      help="Stored company currency so contract values can be summed in pivots.")
    is_epc = fields.Boolean(string="EPC Project", default=False, tracking=True)
    epc_contract_ref = fields.Char(string="Contract Reference", tracking=True)
    epc_segment = fields.Selection(EPC_SEGMENTS, string="Segment")
    epc_contract_value = fields.Monetary(aggregator="sum", string="Contract Value", currency_field="epc_currency_id", tracking=True)
    epc_ld_rate_pct = fields.Float(string="LD Rate (% per week)", digits=(5, 3),
                                   help="Liquidated damages as % of contract value per week of delay.")
    epc_ld_cap_pct = fields.Float(string="LD Cap (%)", default=10.0)
    epc_retention_pct = fields.Float(string="Retention (%)", default=5.0)
    epc_advance_recovery_pct = fields.Float(string="Advance Recovery (%)", default=0.0,
                                            help="Mobilisation advance recovered from each RA bill.")
    epc_status_date = fields.Date(string="Status (Data) Date",
                                  help="Date used for progress and earned value. Empty means today.")

    epc_boq_line_ids = fields.One2many("epc.boq.line", "project_id", string="BOQ")
    epc_activity_ids = fields.One2many("epc.activity", "project_id", string="Schedule Activities")
    epc_dpr_ids = fields.One2many("epc.dpr", "project_id", string="Daily Progress Reports")
    epc_risk_ids = fields.One2many("epc.risk", "project_id", string="Risks")
    epc_document_ids = fields.One2many("epc.document", "project_id", string="Documents")
    epc_ra_bill_ids = fields.One2many("epc.ra.bill", "project_id", string="RA Bills")
    epc_purchase_line_ids = fields.One2many("purchase.order.line", "epc_project_id", string="Procurement Lines")

    epc_activity_count = fields.Integer(compute="_compute_epc_counts")
    epc_dpr_count = fields.Integer(compute="_compute_epc_counts")
    epc_open_risk_count = fields.Integer(compute="_compute_epc_counts")
    epc_document_count = fields.Integer(compute="_compute_epc_counts")
    epc_ra_bill_count = fields.Integer(compute="_compute_epc_counts")
    epc_purchase_line_count = fields.Integer(compute="_compute_epc_counts")
    epc_boq_count = fields.Integer(compute="_compute_epc_counts")

    # Earned value (live)
    epc_bac = fields.Monetary(string="Budget at Completion (BAC)", compute="_compute_epc_evm", currency_field="currency_id")
    epc_pv = fields.Monetary(string="Planned Value (PV)", compute="_compute_epc_evm", currency_field="currency_id")
    epc_ev = fields.Monetary(string="Earned Value (EV)", compute="_compute_epc_evm", currency_field="currency_id")
    epc_ac = fields.Monetary(string="Actual Cost (AC)", compute="_compute_epc_evm", currency_field="currency_id")
    epc_spi = fields.Float(string="SPI", compute="_compute_epc_evm", digits=(6, 2))
    epc_cpi = fields.Float(string="CPI", compute="_compute_epc_evm", digits=(6, 2))
    epc_planned_progress = fields.Float(string="Planned %", compute="_compute_epc_evm", digits=(6, 1))
    epc_actual_progress = fields.Float(string="Actual %", compute="_compute_epc_evm", digits=(6, 1))
    epc_baseline_finish = fields.Date(string="Baseline Finish", compute="_compute_epc_dates")
    epc_planned_finish = fields.Date(string="Planned Finish", compute="_compute_epc_dates")

    # Prediction snapshot (written by the prediction engine)
    epc_predicted_finish = fields.Date(string="Predicted Finish", readonly=True, copy=False)
    epc_predicted_delay_days = fields.Integer(aggregator="max", string="Predicted Delay (days)", readonly=True, copy=False)
    epc_delay_probability = fields.Float(string="Delay Probability (%)", readonly=True, copy=False, digits=(5, 1))
    epc_risk_level = fields.Selection(RISK_LEVELS, string="AI Risk Level", readonly=True, copy=False)
    epc_eac_p50 = fields.Monetary(aggregator="sum", string="Forecast Cost P50", readonly=True, copy=False, currency_field="epc_currency_id")
    epc_eac_p80 = fields.Monetary(aggregator="sum", string="Forecast Cost P80", readonly=True, copy=False, currency_field="epc_currency_id")
    epc_ld_exposure = fields.Monetary(aggregator="sum", string="LD Exposure", readonly=True, copy=False, currency_field="epc_currency_id")
    epc_prediction_date = fields.Datetime(string="Last Prediction", readonly=True, copy=False)
    epc_prediction_drivers = fields.Html(string="Prediction Drivers", readonly=True, copy=False, sanitize=True)
    epc_ai_report = fields.Html(string="AI Progress Report", copy=False, sanitize=True)
    epc_mc_p50_finish = fields.Date(string="Finish P50 (simulated)", readonly=True, copy=False)
    epc_mc_p80_finish = fields.Date(string="Finish P80 (simulated)", readonly=True, copy=False)
    epc_mc_p90_finish = fields.Date(string="Finish P90 (simulated)", readonly=True, copy=False)
    epc_mc_on_time_probability = fields.Float(string="Chance to Meet Baseline (%)", readonly=True, copy=False,
                                              digits=(5, 1))
    epc_mc_iterations = fields.Integer(string="Simulation Runs", readonly=True, copy=False)

    # ------------------------------------------------------------------
    @api.depends("epc_activity_ids", "epc_dpr_ids", "epc_risk_ids.state", "epc_document_ids",
                 "epc_ra_bill_ids", "epc_purchase_line_ids", "epc_boq_line_ids")
    def _compute_epc_counts(self):
        for project in self:
            project.epc_activity_count = len(project.epc_activity_ids)
            project.epc_dpr_count = len(project.epc_dpr_ids)
            project.epc_open_risk_count = len(project.epc_risk_ids.filtered(lambda r: r.state != "closed"))
            project.epc_document_count = len(project.epc_document_ids)
            project.epc_ra_bill_count = len(project.epc_ra_bill_ids)
            project.epc_purchase_line_count = len(project.sudo().epc_purchase_line_ids)
            project.epc_boq_count = len(project.epc_boq_line_ids)

    @api.depends("company_id")
    def _compute_epc_currency_id(self):
        for project in self:
            project.epc_currency_id = (project.company_id or self.env.company).currency_id

    def _epc_effective_status_date(self):
        self.ensure_one()
        return self.epc_status_date or fields.Date.context_today(self)

    @api.depends("epc_activity_ids.budget", "epc_activity_ids.actual_cost", "epc_activity_ids.progress",
                 "epc_activity_ids.planned_start", "epc_activity_ids.planned_finish",
                 "epc_activity_ids.baseline_start", "epc_activity_ids.baseline_finish", "epc_status_date")
    def _compute_epc_evm(self):
        for project in self:
            activities = project.epc_activity_ids
            status_date = project._epc_effective_status_date()
            bac = sum(activities.mapped("budget"))
            pv = sum(a._get_planned_value(status_date) for a in activities)
            ev = sum(a.budget * a.progress / 100.0 for a in activities)
            ac = sum(activities.mapped("actual_cost"))
            project.epc_bac = bac
            project.epc_pv = pv
            project.epc_ev = ev
            project.epc_ac = ac
            project.epc_spi = ev / pv if pv else (1.0 if bac else 0.0)
            project.epc_cpi = ev / ac if ac else (1.0 if ev else 0.0)
            if bac:
                project.epc_planned_progress = pv / bac * 100.0
                project.epc_actual_progress = ev / bac * 100.0
            else:
                weights = len(activities) or 1
                project.epc_planned_progress = sum(a._get_planned_percent(status_date) for a in activities) / weights
                project.epc_actual_progress = sum(activities.mapped("progress")) / weights

    @api.depends("epc_activity_ids.baseline_finish", "epc_activity_ids.planned_finish")
    def _compute_epc_dates(self):
        for project in self:
            baselines = [d for d in project.epc_activity_ids.mapped("baseline_finish") if d]
            planned = [d for d in project.epc_activity_ids.mapped("planned_finish") if d]
            project.epc_baseline_finish = max(baselines) if baselines else False
            project.epc_planned_finish = max(planned) if planned else False

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _epc_check(self):
        for project in self:
            if not project.is_epc:
                raise UserError(_("Project %s is not flagged as an EPC project.", project.display_name))

    def action_epc_compute_schedule(self):
        self._epc_check()
        for project in self:
            project.epc_activity_ids._run_cpm()
        return True

    def action_epc_set_baseline(self):
        self._epc_check()
        for project in self:
            for activity in project.epc_activity_ids:
                activity.write({
                    "baseline_start": activity.planned_start,
                    "baseline_finish": activity.planned_finish,
                })
            project.message_post(body=_("Schedule baseline captured for %s activities.", len(project.epc_activity_ids)))
        return True

    def action_epc_run_prediction(self):
        self._epc_check()
        self.env["epc.prediction.engine"].run_for_projects(self)
        return True

    def action_epc_generate_report(self):
        self._epc_check()
        for project in self:
            project._epc_generate_ai_report()
        return True

    def action_epc_open_assistant(self):
        self.ensure_one()
        self._epc_check()
        return {
            "type": "ir.actions.act_window",
            "name": _("Project AI Assistant"),
            "res_model": "epc.ai.assistant",
            "view_mode": "form",
            "target": "new",
            "context": {"default_project_id": self.id},
        }

    def action_epc_open_optimizer(self):
        self.ensure_one()
        self._epc_check()
        default_target = self.epc_baseline_finish or self.epc_planned_finish
        return {
            "type": "ir.actions.act_window",
            "name": _("Schedule Optimiser"),
            "res_model": "epc.schedule.optimizer",
            "view_mode": "form",
            "target": "new",
            "context": {"default_project_id": self.id, "default_target_finish": default_target,
                        "default_current_finish": self.epc_planned_finish},
        }

    def action_epc_import_p6(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Import Primavera P6 (XER)"),
            "res_model": "epc.p6.import",
            "view_mode": "form",
            "target": "new",
            "context": {"default_project_id": self.id},
        }

    def action_epc_export_p6(self):
        self.ensure_one()
        self._epc_check()
        wizard = self.env["epc.p6.import"].create({"project_id": self.id})
        wizard.action_export()
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/epc.p6.import/%s/export_file/%s?download=true" % (wizard.id, wizard.export_filename),
            "target": "self",
        }

    def _epc_action(self, model, name, view_mode="list,form", extra_context=None):
        self.ensure_one()
        context = {"default_project_id": self.id}
        context.update(extra_context or {})
        return {
            "type": "ir.actions.act_window",
            "name": name,
            "res_model": model,
            "view_mode": view_mode,
            "domain": [("project_id", "=", self.id)],
            "context": context,
        }

    def action_epc_view_activities(self):
        return self._epc_action("epc.activity", _("Activities"), "list,form,graph,pivot")

    def action_epc_view_boq(self):
        return self._epc_action("epc.boq.line", _("Bill of Quantities"), "list,form")

    def action_epc_view_dprs(self):
        return self._epc_action("epc.dpr", _("Daily Progress Reports"), "list,form,calendar")

    def action_epc_view_risks(self):
        return self._epc_action("epc.risk", _("Risks"), "list,kanban,form", {"search_default_open": 1})

    def action_epc_view_documents(self):
        return self._epc_action("epc.document", _("Document Register"), "list,form")

    def action_epc_view_ra_bills(self):
        return self._epc_action("epc.ra.bill", _("RA Bills"), "list,form")

    def action_epc_view_purchase_lines(self):
        action = self._epc_action("purchase.order.line", _("Expediting"), "list,form")
        action["domain"] = [("epc_project_id", "=", self.id)]
        action["views"] = [(self.env.ref("zivoit_epc_ai.view_purchase_line_epc_expediting_list").id, "list"), (False, "form")]
        action["context"] = {"default_epc_project_id": self.id}
        return action

    # ------------------------------------------------------------------
    # AI progress report
    # ------------------------------------------------------------------
    def _epc_report_context(self):
        """Structured facts about the project for the AI and the fallback."""
        self.ensure_one()
        status_date = self._epc_effective_status_date()
        activities = self.epc_activity_ids
        late = activities.filtered(lambda a: a.predicted_slip_days > 0 and a.progress < 100)
        late = late.sorted(lambda a: -a.predicted_slip_days)[:8]
        critical = activities.filtered(lambda a: a.is_critical and a.progress < 100)[:8]
        risks = self.epc_risk_ids.filtered(lambda r: r.state != "closed").sorted(lambda r: -r.score)[:8]
        week_ago = status_date - timedelta(days=7)
        recent_dprs = self.epc_dpr_ids.filtered(lambda d: d.date and d.date > week_ago and d.state == "approved")
        hindrances = recent_dprs.mapped("hindrance_ids")
        late_docs = self.epc_document_ids.filtered("is_late")
        late_po = self.sudo().epc_purchase_line_ids.filtered(lambda l: l.epc_expedite_status in ("at_risk", "late"))
        return {
            "project": self.name,
            "contract_ref": self.epc_contract_ref or "",
            "segment": dict(EPC_SEGMENTS).get(self.epc_segment, ""),
            "status_date": str(status_date),
            "planned_progress_pct": round(self.epc_planned_progress, 1),
            "actual_progress_pct": round(self.epc_actual_progress, 1),
            "spi": round(self.epc_spi, 2),
            "cpi": round(self.epc_cpi, 2),
            "bac": round(self.epc_bac, 2),
            "eac_p50": round(self.epc_eac_p50, 2),
            "eac_p80": round(self.epc_eac_p80, 2),
            "baseline_finish": str(self.epc_baseline_finish or ""),
            "predicted_finish": str(self.epc_predicted_finish or ""),
            "predicted_delay_days": self.epc_predicted_delay_days,
            "delay_probability_pct": round(self.epc_delay_probability, 1),
            "ld_exposure": round(self.epc_ld_exposure, 2),
            "currency": self.currency_id.name or "",
            "late_activities": [
                {"code": a.code, "name": a.name, "progress": round(a.progress, 1),
                 "slip_days": a.predicted_slip_days, "drivers": a.prediction_drivers or ""}
                for a in late
            ],
            "critical_open_activities": [{"code": a.code, "name": a.name} for a in critical],
            "top_risks": [
                {"name": r.name, "level": r.level, "score": r.score, "mitigation": r.mitigation or ""}
                for r in risks
            ],
            "hindrances_last_7_days": [
                {"type": h.hindrance_type, "hours_lost": h.hours_lost, "description": h.description or ""}
                for h in hindrances
            ],
            "late_documents": [{"number": d.doc_number, "title": d.title} for d in late_docs[:8]],
            "materials_at_risk": [
                {"po": l.order_id.name, "item": l.name, "vendor": l.partner_id.name,
                 "delay_days": l.epc_predicted_delay_days}
                for l in late_po[:8]
            ],
        }

    def _epc_fallback_report(self, ctx):
        """Deterministic HTML progress report used when no LLM is configured."""
        def esc(value):
            return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

        def money(value):
            return "{:,.0f}".format(value or 0.0)

        status = "on track"
        if ctx["predicted_delay_days"] > 0:
            status = "at risk of a %s-day delay" % ctx["predicted_delay_days"]
        parts = [
            "<h3>Progress report — %s (status date %s)</h3>" % (esc(ctx["project"]), esc(ctx["status_date"])),
            "<p><b>Summary:</b> actual progress %s%% against %s%% planned (SPI %s, CPI %s). "
            "The project is %s; predicted finish %s against baseline %s "
            "(delay probability %s%%).</p>" % (
                ctx["actual_progress_pct"], ctx["planned_progress_pct"], ctx["spi"], ctx["cpi"], status,
                esc(ctx["predicted_finish"] or "n/a"), esc(ctx["baseline_finish"] or "n/a"),
                ctx["delay_probability_pct"]),
            "<p><b>Cost:</b> budget %s %s; forecast at completion P50 %s, P80 %s. LD exposure %s.</p>" % (
                esc(ctx["currency"]), money(ctx["bac"]), money(ctx["eac_p50"]), money(ctx["eac_p80"]),
                money(ctx["ld_exposure"])),
        ]
        if ctx["late_activities"]:
            parts.append("<p><b>Activities forecast to slip:</b></p><ul>")
            for act in ctx["late_activities"]:
                parts.append("<li>%s %s — %s days (%s%% done). %s</li>" % (
                    esc(act["code"]), esc(act["name"]), act["slip_days"], act["progress"], esc(act["drivers"])))
            parts.append("</ul>")
        if ctx["top_risks"]:
            parts.append("<p><b>Top open risks:</b></p><ul>")
            for risk in ctx["top_risks"]:
                parts.append("<li>[%s] %s%s</li>" % (
                    esc(risk["level"]), esc(risk["name"]),
                    (" — mitigation: %s" % esc(risk["mitigation"])) if risk["mitigation"] else ""))
            parts.append("</ul>")
        if ctx["materials_at_risk"]:
            parts.append("<p><b>Materials at risk:</b></p><ul>")
            for mat in ctx["materials_at_risk"]:
                parts.append("<li>%s %s from %s — %s days late forecast</li>" % (
                    esc(mat["po"]), esc(mat["item"]), esc(mat["vendor"]), mat["delay_days"]))
            parts.append("</ul>")
        if ctx["late_documents"]:
            parts.append("<p><b>Documents overdue:</b> %s</p>" % esc(
                ", ".join("%s %s" % (d["number"], d["title"]) for d in ctx["late_documents"])))
        if ctx["hindrances_last_7_days"]:
            hours = sum(h["hours_lost"] for h in ctx["hindrances_last_7_days"])
            parts.append("<p><b>Hindrances (7 days):</b> %s events, %s hours lost.</p>" % (
                len(ctx["hindrances_last_7_days"]), round(hours, 1)))
        parts.append("<p><i>Generated by the ZivoIT EPC built-in engine. Review before sharing.</i></p>")
        return "".join(parts)

    def _epc_generate_ai_report(self):
        self.ensure_one()
        ctx = self._epc_report_context()
        system = (
            "You are a senior EPC project controls manager. Write a concise monthly progress report "
            "for client and management in simple HTML (h3, p, ul, li, b only). Use only the facts given. "
            "Sections: Summary, Schedule, Cost, Key risks and mitigations, Procurement and engineering, "
            "Actions required. Do not invent numbers."
        )
        prompt = "Project facts (JSON):\n" + json.dumps(ctx, default=str)
        answer = self.env["epc.ai.service"].ask(
            "progress_report", system, prompt, lambda: self._epc_fallback_report(ctx), record=self)
        html = answer["result"]
        if answer["source"] == "llm":
            html = html.replace("```html", "").replace("```", "").strip()
        self.epc_ai_report = html
        self.message_post(body=_("Progress report drafted (%s). Review it on the AI Insights tab.",
                                 _("AI model") if answer["source"] == "llm" else _("built-in engine")))
        return html
