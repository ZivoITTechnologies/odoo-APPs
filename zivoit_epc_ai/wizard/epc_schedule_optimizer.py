from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError

MAX_STEPS = 5000


class EpcScheduleOptimizer(models.TransientModel):
    """Least-cost schedule compression ("crashing") to reach a target finish date.

    Repeatedly shortens, by one working day, the critical activity with the lowest
    cost per day saved, until the target is met or nothing more can be shortened.
    Also lists fast-tracking options (overlapping finish-to-start links).
    """

    _name = "epc.schedule.optimizer"
    _description = "EPC Schedule Optimiser"

    project_id = fields.Many2one("project.project", required=True, domain=[("is_epc", "=", True)])
    currency_id = fields.Many2one(related="project_id.currency_id")
    target_finish = fields.Date(required=True)
    current_finish = fields.Date(readonly=True)
    achieved_finish = fields.Date(readonly=True)
    total_cost = fields.Monetary(readonly=True, currency_field="currency_id")
    target_met = fields.Boolean(readonly=True)
    line_ids = fields.One2many("epc.schedule.optimizer.line", "wizard_id", readonly=True)
    fast_track_html = fields.Html(string="Fast-tracking options", readonly=True, sanitize=True)
    state = fields.Selection([("draft", "Draft"), ("proposed", "Proposed"), ("applied", "Applied")],
                             default="draft")

    @api.onchange("project_id")
    def _onchange_project_id(self):
        if self.project_id and self.project_id.epc_planned_finish:
            self.current_finish = self.project_id.epc_planned_finish

    def _network(self):
        self.ensure_one()
        Activity = self.env["epc.activity"]
        activities, links, order = Activity._cpm_network(self.project_id)
        if not activities:
            raise UserError(_("The project has no schedule activities."))
        calendar = self.project_id._epc_calendar(Activity._cpm_project_start(self.project_id, activities))
        constraints = {a.id: calendar.offset_of(a.constraint_date) for a in activities if a.constraint_date}
        return Activity, activities, links, order, calendar, constraints

    def action_optimise(self):
        self.ensure_one()
        Activity, activities, links, order, calendar, constraints = self._network()
        duration = {a.id: (0 if a.is_milestone else max(0, a.duration)) for a in activities}
        original = dict(duration)
        cpm = Activity._cpm_compute(order, links, duration, calendar, constraints)
        current_finish = calendar.finish_from_start(calendar.start, cpm["finish"])
        target_offset = calendar.offset_of(self.target_finish) + 1  # exclusive end
        by_id = {a.id: a for a in activities}

        def floor(activity):
            if activity.state == "done" or activity.is_milestone:
                return duration[activity.id]
            if activity.crash_duration and activity.crash_duration > 0:
                return min(activity.crash_duration, original[activity.id])
            return original[activity.id]

        steps = 0
        while cpm["finish"] > target_offset and steps < MAX_STEPS:
            candidates = [
                by_id[aid] for aid in duration
                if cpm["ls"][aid] - cpm["es"][aid] <= 0 and duration[aid] > max(floor(by_id[aid]), 1)
            ]
            if not candidates:
                break
            # Only pay for a day that actually brings the finish forward: an activity can be "critical"
            # by its start (e.g. it only drives successors through start-to-start links) while its
            # duration does not drive the project finish.
            accepted = None
            for activity in sorted(candidates, key=lambda a: (a.crash_cost_per_day, -duration[a.id], a.id)):
                trial = dict(duration)
                trial[activity.id] -= 1
                trial_cpm = Activity._cpm_compute(order, links, trial, calendar, constraints)
                if trial_cpm["finish"] < cpm["finish"]:
                    accepted = (trial, trial_cpm)
                    break
            if not accepted:
                # Parallel critical paths: shorten every candidate together, keep it only if it helps.
                trial = dict(duration)
                for activity in candidates:
                    trial[activity.id] -= 1
                trial_cpm = Activity._cpm_compute(order, links, trial, calendar, constraints)
                if trial_cpm["finish"] >= cpm["finish"]:
                    break
                accepted = (trial, trial_cpm)
            duration, cpm = accepted
            steps += 1

        commands = [(5, 0, 0)]
        total = 0.0
        for activity in order:
            saved = original[activity.id] - duration[activity.id]
            if saved > 0:
                cost = saved * activity.crash_cost_per_day
                total += cost
                commands.append((0, 0, {
                    "activity_id": activity.id,
                    "current_duration": original[activity.id],
                    "proposed_duration": duration[activity.id],
                    "days_saved": saved,
                    "cost": cost,
                }))
        achieved = calendar.finish_from_start(calendar.start, cpm["finish"])

        # Fast-tracking options on the (compressed) critical path
        options = []
        for link in links:
            pred, succ = link.predecessor_id, link.successor_id
            if (link.link_type == "FS" and link.lag_days <= 0 and pred.state != "done"
                    and not pred.is_milestone and not succ.is_milestone
                    and cpm["ls"][pred.id] - cpm["es"][pred.id] <= 0
                    and cpm["ls"][succ.id] - cpm["es"][succ.id] <= 0
                    and duration[pred.id] >= 4):
                overlap = duration[pred.id] // 2
                options.append(Markup("<li>%s</li>") % _(
                    "Start %(succ)s when %(pred)s is half done (change FS to SS + %(lag)s days): "
                    "up to %(days)s working days saved, with more coordination risk.",
                    succ=succ.display_name, pred=pred.display_name, lag=duration[pred.id] - overlap,
                    days=overlap))
        fast_track = (Markup("<ul>%s</ul>") % Markup("").join(options)) if options else \
            Markup("<p>%s</p>") % escape(_("No finish-to-start links on the critical path to overlap."))

        self.write({
            "line_ids": commands,
            "current_finish": current_finish,
            "achieved_finish": achieved,
            "total_cost": total,
            "target_met": cpm["finish"] <= target_offset,
            "fast_track_html": fast_track,
            "state": "proposed",
        })
        return self._reopen()

    def action_apply(self):
        self.ensure_one()
        if self.state != "proposed" or not self.line_ids:
            raise UserError(_("Nothing to apply. Run the optimiser first."))
        for line in self.line_ids:
            line.activity_id.duration = line.proposed_duration
        self.project_id.action_epc_compute_schedule()
        self.project_id.message_post(body=Markup("<p>%s</p><ul>%s</ul>") % (
            _("Schedule compressed to finish %(date)s at an extra cost of %(cost)s %(cur)s.",
              date=self.achieved_finish, cost="{:,.0f}".format(self.total_cost), cur=self.currency_id.name or ""),
            Markup("").join(Markup("<li>%s: %s → %s days</li>") % (
                l.activity_id.display_name, l.current_duration, l.proposed_duration) for l in self.line_ids)))
        self.state = "applied"
        return self._reopen()

    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            "name": _("Schedule Optimiser"),
        }


class EpcScheduleOptimizerLine(models.TransientModel):
    _name = "epc.schedule.optimizer.line"
    _description = "EPC Schedule Optimiser Proposal"

    wizard_id = fields.Many2one("epc.schedule.optimizer", required=True, ondelete="cascade")
    currency_id = fields.Many2one(related="wizard_id.currency_id")
    activity_id = fields.Many2one("epc.activity", required=True)
    current_duration = fields.Integer()
    proposed_duration = fields.Integer()
    days_saved = fields.Integer()
    cost = fields.Monetary(currency_field="currency_id")
