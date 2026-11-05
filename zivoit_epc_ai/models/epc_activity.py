from collections import defaultdict, deque

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .project_project import RISK_LEVELS

DISCIPLINES = [
    ("general", "General / Management"),
    ("engineering", "Engineering & Design"),
    ("procurement", "Procurement"),
    ("civil", "Civil"),
    ("structural", "Structural / Erection"),
    ("mechanical", "Mechanical"),
    ("piping", "Piping"),
    ("electrical", "Electrical"),
    ("instrumentation", "Instrumentation & Control"),
    ("stringing", "Line Stringing"),
    ("commissioning", "Testing & Commissioning"),
]

LINK_TYPES = [
    ("FS", "Finish to Start"),
    ("SS", "Start to Start"),
    ("FF", "Finish to Finish"),
    ("SF", "Start to Finish"),
]


class EpcActivity(models.Model):
    _name = "epc.activity"
    _description = "EPC Schedule Activity"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "project_id, sequence, planned_start, code, id"
    _rec_names_search = ["code", "name"]

    project_id = fields.Many2one("project.project", required=True, ondelete="cascade", index=True,
                                 domain=[("is_epc", "=", True)], tracking=True)
    company_id = fields.Many2one(related="project_id.company_id", store=True, index=True)
    currency_id = fields.Many2one(related="project_id.currency_id")
    sequence = fields.Integer(default=10)
    code = fields.Char(string="Activity ID", required=True, tracking=True)
    name = fields.Char(string="Activity", required=True, tracking=True)
    wbs = fields.Char(string="WBS", help="WBS path, e.g. 1.2.3 or TL-FDN")
    discipline = fields.Selection(DISCIPLINES, default="general", required=True)
    is_milestone = fields.Boolean(string="Milestone")
    responsible_id = fields.Many2one("res.users", string="Responsible", default=lambda self: self.env.user)

    duration = fields.Integer(string="Duration (days)", default=1, tracking=True)
    constraint_date = fields.Date(string="Start No Earlier Than")
    planned_start = fields.Date(tracking=True)
    planned_finish = fields.Date(tracking=True)
    baseline_start = fields.Date(copy=False)
    baseline_finish = fields.Date(copy=False)
    actual_start = fields.Date(copy=False, tracking=True)
    actual_finish = fields.Date(copy=False, tracking=True)

    predecessor_link_ids = fields.One2many("epc.activity.link", "successor_id", string="Predecessors")
    successor_link_ids = fields.One2many("epc.activity.link", "predecessor_id", string="Successors")

    early_start = fields.Integer(readonly=True, copy=False, help="Early start offset in days from project start")
    early_finish = fields.Integer(readonly=True, copy=False)
    late_start = fields.Integer(readonly=True, copy=False)
    late_finish = fields.Integer(readonly=True, copy=False)
    total_float = fields.Integer(string="Total Float (days)", readonly=True, copy=False)
    is_critical = fields.Boolean(string="Critical", readonly=True, copy=False)

    boq_line_id = fields.Many2one("epc.boq.line", string="BOQ Item", index=True,
                                  domain="[('project_id', '=', project_id)]")
    uom_name = fields.Char(related="boq_line_id.uom_name", string="Unit")
    progress_method = fields.Selection(
        [("quantity", "Measured quantity (DPR)"), ("manual", "Manual %")],
        default="quantity", required=True)
    planned_qty = fields.Float(string="Planned Qty", digits="Product Unit")
    manual_progress = fields.Float(string="Manual Progress %")
    dpr_line_ids = fields.One2many("epc.dpr.line", "activity_id", string="DPR Lines")
    executed_qty = fields.Float(compute="_compute_progress", store=True, digits="Product Unit")
    progress = fields.Float(string="Progress %", compute="_compute_progress", store=True, digits=(6, 1),
                            tracking=True)
    state = fields.Selection(
        [("not_started", "Not Started"), ("in_progress", "In Progress"), ("done", "Completed")],
        compute="_compute_state", store=True)

    budget = fields.Monetary(string="Budget (BAC)", currency_field="currency_id")
    crash_duration = fields.Integer(string="Shortest Duration (days)",
                                    help="Minimum achievable duration with extra resources. Empty or 0 = cannot be shortened.")
    crash_cost_per_day = fields.Monetary(string="Cost per Day Saved", currency_field="currency_id",
                                         help="Extra cost to shorten this activity by one day (overtime, extra crews).")
    actual_cost = fields.Monetary(string="Actual Cost", currency_field="currency_id")

    purchase_line_ids = fields.Many2many(
        "purchase.order.line", "epc_activity_purchase_line_rel", "activity_id", "purchase_line_id",
        string="Required Materials",
        help="Purchase order lines whose delivery this activity needs before it can start.")
    document_ids = fields.Many2many(
        "epc.document", "epc_activity_document_rel", "activity_id", "document_id",
        string="Required Documents",
        help="Drawings and documents that must be approved for construction (AFC) before this activity starts.")

    # Prediction snapshot
    forecast_start = fields.Date(readonly=True, copy=False)
    forecast_finish = fields.Date(readonly=True, copy=False)
    predicted_slip_days = fields.Integer(string="Predicted Slip (days)", readonly=True, copy=False)
    delay_probability = fields.Float(string="Delay Probability (%)", readonly=True, copy=False, digits=(5, 1))
    risk_level = fields.Selection(RISK_LEVELS, string="AI Risk", readonly=True, copy=False)
    prediction_drivers = fields.Text(string="Drivers", readonly=True, copy=False)
    criticality_index = fields.Float(string="Criticality (%)", readonly=True, copy=False, digits=(5, 1),
                                     help="Share of simulation runs in which this activity drove the finish date.")

    _code_project_uniq = models.Constraint(
        "unique(project_id, code)",
        "Activity IDs must be unique within a project.",
    )
    _duration_positive = models.Constraint(
        "check(duration >= 0)",
        "Duration cannot be negative.",
    )

    # ------------------------------------------------------------------
    @api.depends("code", "name")
    def _compute_display_name(self):
        for activity in self:
            activity.display_name = "[%s] %s" % (activity.code, activity.name) if activity.code else activity.name

    @api.depends("progress_method", "manual_progress", "planned_qty", "actual_finish",
                 "dpr_line_ids.quantity", "dpr_line_ids.dpr_id.state")
    def _compute_progress(self):
        for activity in self:
            executed = sum(activity.dpr_line_ids.filtered(lambda l: l.dpr_id.state == "approved").mapped("quantity"))
            activity.executed_qty = executed
            if activity.actual_finish:
                progress = 100.0
            elif activity.progress_method == "manual":
                progress = activity.manual_progress
            elif activity.planned_qty:
                progress = executed / activity.planned_qty * 100.0
            else:
                progress = 0.0
            activity.progress = max(0.0, min(100.0, progress))

    @api.depends("progress", "actual_start", "actual_finish")
    def _compute_state(self):
        for activity in self:
            if activity.actual_finish or activity.progress >= 100.0:
                activity.state = "done"
            elif activity.actual_start or activity.progress > 0:
                activity.state = "in_progress"
            else:
                activity.state = "not_started"

    @api.onchange("boq_line_id")
    def _onchange_boq_line_id(self):
        if self.boq_line_id and not self.planned_qty:
            self.planned_qty = self.boq_line_id.quantity
            if not self.budget:
                self.budget = self.boq_line_id.amount

    @api.onchange("is_milestone")
    def _onchange_is_milestone(self):
        if self.is_milestone:
            self.duration = 0

    @api.constrains("planned_start", "planned_finish")
    def _check_planned_dates(self):
        for activity in self:
            if activity.planned_start and activity.planned_finish and activity.planned_finish < activity.planned_start:
                raise ValidationError(_("Activity %s finishes before it starts.", activity.display_name))

    @api.constrains("actual_start", "actual_finish")
    def _check_actual_dates(self):
        for activity in self:
            if activity.actual_start and activity.actual_finish and activity.actual_finish < activity.actual_start:
                raise ValidationError(_("Activity %s: actual finish is before actual start.", activity.display_name))

    @api.constrains("manual_progress")
    def _check_manual_progress(self):
        for activity in self:
            if not 0.0 <= activity.manual_progress <= 100.0:
                raise ValidationError(_("Manual progress must be between 0 and 100%."))

    # ------------------------------------------------------------------
    # Earned value helpers
    # ------------------------------------------------------------------
    def _get_reference_dates(self):
        self.ensure_one()
        start = self.baseline_start or self.planned_start
        finish = self.baseline_finish or self.planned_finish
        return start, finish

    def _get_planned_percent(self, status_date):
        self.ensure_one()
        start, finish = self._get_reference_dates()
        if not start or not finish:
            return 0.0
        if status_date < start:
            return 0.0
        if status_date >= finish:
            return 100.0
        total_days = (finish - start).days + 1
        elapsed = (status_date - start).days + 1
        return max(0.0, min(100.0, elapsed / total_days * 100.0))

    def _get_planned_value(self, status_date):
        self.ensure_one()
        return self.budget * self._get_planned_percent(status_date) / 100.0

    # ------------------------------------------------------------------
    # CPM engine
    # ------------------------------------------------------------------
    @api.model
    def _topological_order(self, activities, links):
        """Return activities in dependency order; raise if the network has a cycle."""
        in_degree = {a.id: 0 for a in activities}
        successors = defaultdict(list)
        for link in links:
            successors[link.predecessor_id.id].append(link)
            in_degree[link.successor_id.id] += 1
        queue = deque(sorted((a for a in activities if in_degree[a.id] == 0), key=lambda a: (a.sequence, a.id)))
        order = []
        while queue:
            activity = queue.popleft()
            order.append(activity)
            for link in successors[activity.id]:
                in_degree[link.successor_id.id] -= 1
                if in_degree[link.successor_id.id] == 0:
                    queue.append(link.successor_id)
        if len(order) != len(activities):
            looped = activities.filtered(lambda a: in_degree[a.id] > 0)
            raise UserError(_(
                "The schedule logic contains a loop. Check the links between: %s",
                ", ".join(looped.mapped("code"))))
        return order

    @api.model
    def _cpm_project_start(self, project, activities):
        if project.date_start:
            return project.date_start
        dates = [d for d in activities.mapped("planned_start") + activities.mapped("constraint_date") if d]
        return min(dates) if dates else fields.Date.context_today(self)

    @api.model
    def _cpm_compute(self, order, links, duration, calendar, constraint_offsets=None):
        """Pure CPM on working-day offsets.

        :param order: activities in topological order
        :param duration: {activity id: working days}
        :param constraint_offsets: {activity id: earliest start offset}
        :return: dict with es, ef, ls, lf per activity id and the project finish offset
        """
        preds = defaultdict(list)
        succs = defaultdict(list)
        for link in links:
            preds[link.successor_id.id].append(link)
            succs[link.predecessor_id.id].append(link)
        constraint_offsets = constraint_offsets or {}
        early_start, early_finish = {}, {}
        for activity in order:
            aid = activity.id
            d = duration[aid]
            es = max(0, constraint_offsets.get(aid, 0))
            for link in preds[aid]:
                p = link.predecessor_id.id
                lag = link.lag_days
                if link.link_type == "FS":
                    es = max(es, early_finish[p] + lag)
                elif link.link_type == "SS":
                    es = max(es, early_start[p] + lag)
                elif link.link_type == "FF":
                    es = max(es, early_finish[p] + lag - d)
                else:  # SF
                    es = max(es, early_start[p] + lag - d)
            early_start[aid] = es
            early_finish[aid] = es + d
        project_finish = max(early_finish.values()) if early_finish else 0
        late_start, late_finish = {}, {}
        for activity in reversed(order):
            aid = activity.id
            d = duration[aid]
            lf = project_finish
            for link in succs[aid]:
                s = link.successor_id.id
                lag = link.lag_days
                if link.link_type == "FS":
                    lf = min(lf, late_start[s] - lag)
                elif link.link_type == "SS":
                    lf = min(lf, late_start[s] - lag + d)
                elif link.link_type == "FF":
                    lf = min(lf, late_finish[s] - lag)
                else:  # SF
                    lf = min(lf, late_finish[s] - lag + d)
            late_finish[aid] = lf
            late_start[aid] = lf - d
        return {
            "es": early_start, "ef": early_finish, "ls": late_start, "lf": late_finish,
            "finish": project_finish,
        }

    @api.model
    def _cpm_network(self, project):
        """Activities in logic order and their internal links for one project."""
        activities = project.epc_activity_ids
        links = self.env["epc.activity.link"].search([("successor_id", "in", activities.ids)])
        links = links.filtered(lambda l: l.predecessor_id in activities)
        order = self._topological_order(activities, links)
        return activities, links, order

    def _run_cpm(self):
        """Forward and backward pass over the activity network, per project, in working days."""
        results = {}
        for project in self.mapped("project_id"):
            activities, links, order = self._cpm_network(project)
            if not activities:
                continue
            calendar = project._epc_calendar(self._cpm_project_start(project, activities))
            duration = {a.id: (0 if a.is_milestone else max(0, a.duration)) for a in activities}
            constraints = {
                a.id: calendar.offset_of(a.constraint_date)
                for a in activities if a.constraint_date
            }
            cpm = self._cpm_compute(order, links, duration, calendar, constraints)
            for activity in order:
                aid = activity.id
                total_float = cpm["ls"][aid] - cpm["es"][aid]
                start_date = calendar.date_at(cpm["es"][aid])
                finish_date = calendar.finish_from_start(start_date, duration[aid])
                activity.write({
                    "early_start": cpm["es"][aid],
                    "early_finish": cpm["ef"][aid],
                    "late_start": cpm["ls"][aid],
                    "late_finish": cpm["lf"][aid],
                    "total_float": total_float,
                    "is_critical": total_float <= 0,
                    "planned_start": start_date,
                    "planned_finish": finish_date,
                })
            results[project.id] = {
                "start": calendar.start,
                "finish": calendar.finish_from_start(calendar.start, cpm["finish"]),
                "duration": cpm["finish"],
                "critical": [a.code for a in order if cpm["ls"][a.id] - cpm["es"][a.id] <= 0],
            }
        return results

    def action_view_dpr_lines(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Measured Progress"),
            "res_model": "epc.dpr.line",
            "view_mode": "list",
            "domain": [("activity_id", "=", self.id)],
        }


class EpcActivityLink(models.Model):
    _name = "epc.activity.link"
    _description = "EPC Activity Relationship"
    _order = "successor_id, predecessor_id"

    predecessor_id = fields.Many2one("epc.activity", required=True, ondelete="cascade", index=True)
    successor_id = fields.Many2one("epc.activity", required=True, ondelete="cascade", index=True)
    project_id = fields.Many2one(related="successor_id.project_id", store=True, index=True)
    company_id = fields.Many2one(related="successor_id.company_id", store=True)
    link_type = fields.Selection(LINK_TYPES, string="Type", default="FS", required=True)
    lag_days = fields.Integer(string="Lag (days)", default=0)

    _link_uniq = models.Constraint(
        "unique(predecessor_id, successor_id)",
        "This relationship already exists.",
    )
    _no_self_link = models.Constraint(
        "check(predecessor_id != successor_id)",
        "An activity cannot depend on itself.",
    )

    @api.constrains("predecessor_id", "successor_id")
    def _check_same_project(self):
        for link in self:
            if link.predecessor_id.project_id != link.successor_id.project_id:
                raise ValidationError(_("Linked activities must belong to the same project."))
