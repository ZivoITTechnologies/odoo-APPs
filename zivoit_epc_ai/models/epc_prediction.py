"""ZivoIT EPC prediction engine.

The engine forecasts every activity's start and finish from:

* measured progress rate (activities in progress)
* schedule logic (FS/SS/FF/SF with lag) and predecessors' forecasts
* material availability (linked purchase lines and supplier delivery history)
* engineering readiness (linked documents not yet approved for construction)
* duration overrun factors learned from completed activities, per discipline

It then scores the probability of delay with a transparent logistic model,
explains each forecast with its drivers, rolls up to project finish, cost at
completion (P50/P80) and LD exposure, and raises early-warning risks.
"""
import logging
import math
import random
from collections import defaultdict
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PRIOR_P50 = 1.0
PRIOR_P80 = 1.25
PRIOR_WEIGHT = 5
SUPPLIER_PRIOR_SCORE = 30.0
SUPPLIER_PRIOR_WEIGHT = 3

# Logistic model weights (intercept first). Documented in the README.
WEIGHTS = {
    "intercept": -2.0,
    "slip_ratio": 3.2,
    "critical": 0.8,
    "progress_gap": 2.0,
    "material": 1.0,
    "document": 0.9,
    "hindrance": 0.8,
    "supplier": 0.8,
    "quality": 0.6,
}


def _sigmoid(value):
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, value))))


def _quantile(values, q):
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


class EpcPredictionModel(models.Model):
    _name = "epc.prediction.model"
    _description = "EPC Learned Duration Factors"
    _order = "discipline"

    discipline = fields.Selection(selection="_selection_discipline", string="Discipline",
                                  help="Empty = all disciplines")
    sample_count = fields.Integer(string="Completed Activities", readonly=True)
    p50_factor = fields.Float(string="P50 Duration Factor", digits=(6, 3), readonly=True)
    p80_factor = fields.Float(string="P80 Duration Factor", digits=(6, 3), readonly=True)
    mean_factor = fields.Float(string="Mean Factor (raw)", digits=(6, 3), readonly=True)
    updated_on = fields.Datetime(readonly=True)

    @api.model
    def _selection_discipline(self):
        return self.env["epc.activity"]._fields["discipline"].selection

    def action_relearn(self):
        self.env["epc.prediction.engine"].learn_from_history()
        return {"type": "ir.actions.client", "tag": "reload"}


class EpcPredictionEngine(models.AbstractModel):
    _name = "epc.prediction.engine"
    _description = "EPC Prediction Engine"

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    @api.model
    def _thresholds(self):
        icp = self.env["ir.config_parameter"].sudo()
        try:
            days = int(icp.get_param("zivoit_epc_ai.alert_delay_days", "7") or 7)
        except ValueError:
            days = 7
        try:
            probability = int(icp.get_param("zivoit_epc_ai.alert_probability", "60") or 60)
        except ValueError:
            probability = 60
        return max(1, days), max(1, min(99, probability))

    # ------------------------------------------------------------------
    # Learning from history
    # ------------------------------------------------------------------
    @api.model
    def learn_from_history(self):
        """Learn duration overrun factors from completed activities."""
        activities = self.env["epc.activity"].sudo().search([
            ("actual_start", "!=", False), ("actual_finish", "!=", False),
            ("duration", ">", 0), ("is_milestone", "=", False),
        ])
        ratios = defaultdict(list)
        for activity in activities:
            actual = (activity.actual_finish - activity.actual_start).days + 1
            if actual <= 0:
                continue
            ratio = max(0.2, min(5.0, actual / float(activity.duration)))
            ratios[activity.discipline].append(ratio)
            ratios[False].append(ratio)
        Model = self.env["epc.prediction.model"].sudo()
        now = fields.Datetime.now()
        disciplines = [False] + [d for d, _label in self.env["epc.activity"]._fields["discipline"].selection]
        for discipline in disciplines:
            values = ratios.get(discipline, [])
            n = len(values)
            if n:
                emp50 = _quantile(values, 0.5)
                emp80 = _quantile(values, 0.8)
                mean = sum(values) / n
            else:
                emp50, emp80, mean = PRIOR_P50, PRIOR_P80, 0.0
            p50 = (n * emp50 + PRIOR_WEIGHT * PRIOR_P50) / (n + PRIOR_WEIGHT)
            p80 = (n * emp80 + PRIOR_WEIGHT * PRIOR_P80) / (n + PRIOR_WEIGHT)
            p50 = max(0.5, min(3.0, p50))
            p80 = max(p50, min(4.0, p80))
            vals = {"sample_count": n, "p50_factor": p50, "p80_factor": p80, "mean_factor": mean, "updated_on": now}
            row = Model.search([("discipline", "=", discipline)], limit=1)
            if row:
                row.write(vals)
            else:
                Model.create(dict(vals, discipline=discipline))
        return True

    @api.model
    def _get_duration_factors(self):
        Model = self.env["epc.prediction.model"].sudo()
        if not Model.search_count([]):
            self.learn_from_history()
        factors = {}
        default = (PRIOR_P50, PRIOR_P80)
        for row in Model.search([]):
            factors[row.discipline or False] = (row.p50_factor or PRIOR_P50, row.p80_factor or PRIOR_P80)
        global_factor = factors.get(False, default)
        return defaultdict(lambda: global_factor, factors)

    # ------------------------------------------------------------------
    # Supply chain
    # ------------------------------------------------------------------
    @api.model
    def _line_delivery_date(self, line):
        moves = line.move_ids.filtered(lambda m: m.state == "done" and m.location_dest_id.usage != "supplier")
        dates = [m.date for m in moves if m.date]
        return fields.Date.to_date(max(dates)) if dates else False

    @api.model
    def _update_supplier_scores(self, partners):
        """Score each vendor from its own delivery history (0 = reliable, 100 = risky)."""
        today = fields.Date.context_today(self)
        Line = self.env["purchase.order.line"].sudo()
        for partner in partners.filtered(lambda p: p.id):
            commercial = partner.commercial_partner_id
            lines = Line.search([
                ("partner_id", "child_of", commercial.id),
                ("state", "in", ("purchase", "done")),
                ("display_type", "=", False),
                ("date_planned", "!=", False),
            ])
            delays = []
            on_time = 0
            for line in lines:
                promised = fields.Date.to_date(line.date_planned)
                if line.qty_received > 0:
                    delivered = self._line_delivery_date(line)
                    if not delivered:
                        continue
                    delay = (delivered - promised).days
                elif promised < today and line.product_qty > 0:
                    delay = (today - promised).days
                else:
                    continue
                delays.append(max(0, delay))
                if delay <= 0:
                    on_time += 1
            n = len(delays)
            if n:
                on_time_rate = on_time / n * 100.0
                avg_delay = sum(delays) / n
                raw = (100.0 - on_time_rate) * 0.6 + min(avg_delay, 30.0) / 30.0 * 40.0
            else:
                on_time_rate, avg_delay, raw = 0.0, 0.0, SUPPLIER_PRIOR_SCORE
            score = (n * raw + SUPPLIER_PRIOR_WEIGHT * SUPPLIER_PRIOR_SCORE) / (n + SUPPLIER_PRIOR_WEIGHT)
            (commercial | partner).sudo().write({
                "epc_on_time_rate": on_time_rate,
                "epc_avg_delay_days": avg_delay,
                "epc_delivery_samples": n,
                "epc_supplier_risk_score": round(max(0.0, min(100.0, score)), 1),
                "epc_supplier_risk_date": fields.Datetime.now(),
            })
        return True

    @api.model
    def _forecast_purchase_lines(self, lines):
        today = fields.Date.context_today(self)
        for line in lines.filtered(lambda l: not l.display_type and l.date_planned):
            promised = fields.Date.to_date(line.date_planned)
            vendor = line.partner_id.commercial_partner_id
            if line.product_qty and line.qty_received >= line.product_qty:
                delivered = self._line_delivery_date(line) or promised
                line.sudo().write({
                    "epc_forecast_date": delivered,
                    "epc_predicted_delay_days": max(0, (delivered - promised).days),
                    "epc_expedite_status": "received",
                })
                continue
            risk = vendor.epc_supplier_risk_score or SUPPLIER_PRIOR_SCORE
            expected_delay = vendor.epc_avg_delay_days * (1.0 if risk >= 50 else 0.5)
            forecast = max(promised, today) + timedelta(days=int(round(expected_delay)))
            predicted = max(0, (forecast - promised).days)
            deadline = line.epc_required_date or promised
            if forecast > deadline:
                status = "late" if (promised < today or (forecast - deadline).days > 7) else "at_risk"
            elif predicted > 0 or risk >= 60:
                status = "at_risk"
            else:
                status = "on_track"
            line.sudo().write({
                "epc_forecast_date": forecast,
                "epc_predicted_delay_days": predicted,
                "epc_expedite_status": status,
            })
        return True

    @api.model
    def _forecast_external_materials(self, materials):
        """Forecast deliveries tracked in an external ERP using the same supplier-history logic."""
        today = fields.Date.context_today(self)
        for material in materials:
            if not material._is_open():
                material.sudo().write({"forecast_date": material.forecast_date or material.promised_date,
                                       "predicted_delay_days": 0, "expedite_status": "received"})
                continue
            promised = material.promised_date
            if not promised:
                material.sudo().write({"forecast_date": False, "predicted_delay_days": 0,
                                       "expedite_status": "at_risk" if material.required_date else False})
                continue
            vendor = material.vendor_id.commercial_partner_id
            risk = vendor.epc_supplier_risk_score if vendor else SUPPLIER_PRIOR_SCORE
            avg_delay = vendor.epc_avg_delay_days if vendor else 0.0
            expected = avg_delay * (1.0 if risk >= 50 else 0.5)
            if material.stage == "shipped":
                expected = min(expected, 3)
            forecast = max(promised, today) + timedelta(days=int(round(expected)))
            predicted = max(0, (forecast - promised).days)
            deadline = material.required_date or promised
            if forecast > deadline:
                status = "late" if (promised < today or (forecast - deadline).days > 7) else "at_risk"
            elif predicted > 0 or risk >= 60:
                status = "at_risk"
            else:
                status = "on_track"
            material.sudo().write({"forecast_date": forecast, "predicted_delay_days": predicted,
                                   "expedite_status": status})
        return True

    # ------------------------------------------------------------------
    # Activity forecasting
    # ------------------------------------------------------------------
    @api.model
    def _successor_start(self, calendar, link, pred_start, pred_finish, pred_duration, duration):
        """Earliest start of a successor through one link, in working days."""
        lag = link.lag_days
        if link.link_type == "FS":
            return calendar.shift(pred_finish, lag + (1 if pred_duration > 0 else 0))
        if link.link_type == "SS":
            return calendar.shift(pred_start, lag)
        if link.link_type == "FF":
            return calendar.shift(pred_finish, lag - max(duration - 1, 0))
        return calendar.shift(pred_start, lag - max(duration - 1, 0))  # SF

    def _forecast_project(self, project, factors):
        Activity = self.env["epc.activity"]
        status_date = project._epc_effective_status_date()
        activities, links, order = Activity._cpm_network(project)
        calendar = project._epc_calendar(Activity._cpm_project_start(project, activities) if activities else None)
        preds = defaultdict(list)
        for link in links:
            preds[link.successor_id.id].append(link)

        since = status_date - timedelta(days=14)
        hindrance_hours = defaultdict(float)
        for hindrance in self.env["epc.dpr.hindrance"].search([
            ("project_id", "=", project.id), ("date", ">=", since), ("date", "<=", status_date),
            ("state", "in", ("submitted", "approved")), ("activity_id", "!=", False),
        ]):
            hindrance_hours[hindrance.activity_id.id] += hindrance.hours_lost

        open_ncr = defaultdict(int)
        for ncr in self.env["epc.qa.ncr"].sudo().search([
                ("project_id", "=", project.id), ("state", "in", ("open", "action")),
                ("severity", "in", ("major", "critical")), ("activity_id", "!=", False)]):
            open_ncr[ncr.activity_id.id] += 1

        threshold_days, threshold_prob = self._thresholds()
        forecasts = {}
        for activity in order:
            drivers = []
            duration = 0 if activity.is_milestone else max(0, activity.duration)
            ref_start, ref_finish = activity._get_reference_dates()
            if not ref_start or not ref_finish:
                forecasts[activity.id] = {"skip": True}
                continue
            p50, _p80 = factors[activity.discipline]
            material_late = 0.0
            document_late = 0.0
            supplier = 0.0
            progress_gap = 0.0
            ready = None
            remaining = None

            if activity.state == "done":
                start = activity.actual_start or ref_start
                finish = activity.actual_finish or max(start, status_date)
            elif activity.state == "in_progress":
                start = activity.actual_start or ref_start
                elapsed = max(1, calendar.workdays_between(start, status_date))
                progress = activity.progress
                if progress > 0:
                    rate = progress / elapsed
                    remaining = int(math.ceil((100.0 - progress) / rate))
                    planned_pct = activity._get_planned_percent(status_date)
                    progress_gap = max(0.0, (planned_pct - progress) / 100.0)
                    if progress_gap > 0.1:
                        drivers.append(_("%(actual)s%% done vs %(planned)s%% planned",
                                         actual=round(progress), planned=round(planned_pct)))
                else:
                    remaining = int(math.ceil(duration * p50))
                    drivers.append(_("started %(days)s days ago with no measured progress", days=elapsed))
                    progress_gap = activity._get_planned_percent(status_date) / 100.0
                # progress is measured up to and including the status date
                finish = calendar.shift(status_date, max(remaining, 0))
            else:
                start = calendar.next_workday(ref_start)
                ready = start
                for link in preds[activity.id]:
                    pred = forecasts.get(link.predecessor_id.id)
                    if not pred or pred.get("skip"):
                        continue
                    candidate = self._successor_start(
                        calendar, link, pred["start"], pred["finish"], pred["duration"], duration)
                    if candidate > start:
                        start = candidate
                        if pred["slip"] > 0:
                            drivers.append(_("predecessor %(code)s forecast %(days)s days late",
                                             code=link.predecessor_id.code, days=pred["slip"]))
                if start < status_date:
                    if ref_start < status_date:
                        drivers.append(_("planned to start %(date)s, not started yet", date=ref_start))
                    start = calendar.next_workday(status_date)
                ready = max(ready, calendar.next_workday(status_date))
                for line in activity.purchase_line_ids.filtered(
                        lambda l: not l.display_type and l.product_qty and l.qty_received < l.product_qty):
                    arrival = line.epc_forecast_date or (fields.Date.to_date(line.date_planned) if line.date_planned else False)
                    supplier = max(supplier, (line.partner_id.commercial_partner_id.epc_supplier_risk_score or 0.0) / 100.0)
                    if arrival:
                        ready = max(ready, calendar.shift(arrival, 1))
                    if arrival and calendar.shift(arrival, 1) > start:
                        start = calendar.shift(arrival, 1)
                        material_late = 1.0
                        drivers.append(_("material %(item)s from %(vendor)s forecast %(date)s",
                                         item=(line.product_id.display_name or line.name or "")[:40],
                                         vendor=line.partner_id.name, date=arrival))
                for material in activity.sudo().external_material_ids.filtered(lambda m: m._is_open()):
                    arrival = material.forecast_date or material.promised_date
                    supplier = max(supplier, (material.vendor_id.commercial_partner_id.epc_supplier_risk_score or 0.0) / 100.0)
                    if arrival:
                        ready = max(ready, calendar.shift(arrival, 1))
                    if arrival and calendar.shift(arrival, 1) > start:
                        start = calendar.shift(arrival, 1)
                        material_late = 1.0
                        drivers.append(_("material %(item)s (%(ref)s) forecast %(date)s",
                                         item=material.description[:40], ref=material.external_ref, date=arrival))
                for document in activity.document_ids.filtered(lambda d: d.status != "afc"):
                    if document.planned_afc_date and document.planned_afc_date >= status_date:
                        expected = document.planned_afc_date
                    else:
                        expected = status_date + timedelta(days=14)
                        document_late = 1.0
                    ready = max(ready, calendar.shift(expected, 1))
                    if calendar.shift(expected, 1) > start:
                        start = calendar.shift(expected, 1)
                        drivers.append(_("drawing %(doc)s not yet AFC (expected %(date)s)",
                                         doc=document.doc_number, date=expected))
                forecast_duration = int(math.ceil(duration * p50)) if duration else 0
                if p50 > 1.05 and duration:
                    drivers.append(_("past %(disc)s work ran %(pct)s%% longer than planned",
                                     disc=activity.discipline, pct=round((p50 - 1) * 100)))
                finish = calendar.finish_from_start(start, forecast_duration)

            slip = (finish - ref_finish).days
            if activity.state == "done" and slip > 0:
                drivers.append(_("completed %(days)s days after baseline", days=slip))
            if open_ncr.get(activity.id) and activity.state != "done":
                drivers.append(_("%(count)s open major NCR(s)", count=open_ncr[activity.id]))
            if hindrance_hours.get(activity.id):
                drivers.append(_("%(hours)s hours lost to hindrances in 14 days",
                                 hours=round(hindrance_hours[activity.id], 1)))
            if activity.state == "done":
                probability = 100.0 if slip > 0 else 0.0
            else:
                features = {
                    "slip_ratio": max(-1.0, min(3.0, slip / float(max(duration, 5)))),
                    "critical": 1.0 if activity.total_float <= 0 else 0.0,
                    "progress_gap": progress_gap,
                    "material": material_late,
                    "document": document_late,
                    "hindrance": min(1.0, hindrance_hours.get(activity.id, 0.0) / 40.0),
                    "supplier": supplier,
                    "quality": 1.0 if open_ncr.get(activity.id) else 0.0,
                }
                z = WEIGHTS["intercept"] + sum(WEIGHTS[k] * v for k, v in features.items())
                probability = _sigmoid(z) * 100.0
            if activity.state == "done":
                level = "low"
            elif slip >= 3 * threshold_days or (activity.total_float <= 0 and slip >= threshold_days
                                               and probability >= max(threshold_prob, 75)):
                level = "critical"
            elif slip >= threshold_days and probability >= threshold_prob:
                level = "high"
            elif slip > 0 or probability >= 40:
                level = "medium"
            else:
                level = "low"
            forecasts[activity.id] = {
                "start": start, "finish": finish, "duration": duration, "slip": slip,
                "probability": probability, "level": level, "drivers": drivers,
                "state": activity.state, "ready": ready, "remaining": remaining,
                "discipline": activity.discipline,
            }
            activity.sudo().write({
                "forecast_start": start,
                "forecast_finish": finish,
                "predicted_slip_days": slip,
                "delay_probability": round(probability, 1),
                "risk_level": level,
                "prediction_drivers": "; ".join(drivers) if drivers else (_("on plan") if slip <= 0 else ""),
            })
        return forecasts

    # ------------------------------------------------------------------
    # Monte Carlo schedule risk analysis
    # ------------------------------------------------------------------
    @api.model
    def _mc_iterations(self, activity_count):
        icp = self.env["ir.config_parameter"].sudo()
        try:
            wanted = int(icp.get_param("zivoit_epc_ai.mc_iterations", "500") or 500)
        except ValueError:
            wanted = 500
        wanted = max(50, min(5000, wanted))
        budget = max(100, 200000 // max(activity_count, 1))
        return min(wanted, budget)

    def _monte_carlo(self, project, forecasts, factors):
        """Simulate the remaining schedule; return finish percentiles and criticality.

        Durations of open work are drawn from triangular distributions shaped by the
        learned P50/P80 overrun factors of each discipline. Logic and readiness
        constraints (materials, drawings, status date) are respected in every run.
        """
        Activity = self.env["epc.activity"]
        activities, links, order = Activity._cpm_network(project)
        if not activities:
            return {}
        calendar = project._epc_calendar(Activity._cpm_project_start(project, activities))
        status_date = project._epc_effective_status_date()
        status_off = calendar.offset_of(status_date)
        preds = defaultdict(list)
        for link in links:
            preds[link.successor_id.id].append(link)

        fixed, sampled = {}, {}
        for activity in order:
            f = forecasts.get(activity.id) or {"skip": True}
            d = 0 if activity.is_milestone else max(0, activity.duration)
            if f.get("skip"):
                sampled[activity.id] = (status_off, d, d, d)
                continue
            p50, p80 = factors[activity.discipline]
            high_factor = max(p80 * 1.15, p50 * 1.3)
            if f["state"] == "done":
                es = calendar.offset_of(f["start"])
                ef = calendar.offset_of(f["finish"]) + (1 if d > 0 else 0)
                fixed[activity.id] = (es, max(es, ef))
            elif f["state"] == "in_progress":
                rem = max(1, f["remaining"] or 1)
                es = calendar.offset_of(f["start"])
                sampled[activity.id] = ("progress", es, rem * 0.8, rem, rem * max(high_factor / max(p50, 0.5), 1.2))
            else:
                ready = calendar.offset_of(f["ready"]) if f.get("ready") else status_off
                if d == 0:
                    sampled[activity.id] = (ready, 0, 0, 0)
                else:
                    sampled[activity.id] = (ready, d * 0.9, d * p50, d * high_factor)

        iterations = self._mc_iterations(len(order))
        rng = random.Random(project.id)
        finishes = []
        critical_count = defaultdict(int)
        for _i in range(iterations):
            es, ef, driver = {}, {}, {}
            for activity in order:
                aid = activity.id
                if aid in fixed:
                    es[aid], ef[aid] = fixed[aid]
                    driver[aid] = None
                    continue
                spec = sampled[aid]
                if spec[0] == "progress":
                    _tag, start, low, mode, high = spec
                    es[aid] = start
                    ef[aid] = status_off + 1 + rng.triangular(low, high, mode)
                    driver[aid] = None
                    continue
                ready, low, mode, high = spec
                dur = rng.triangular(low, high, mode) if high > low else mode
                start, drv = float(ready), None
                for link in preds[aid]:
                    p = link.predecessor_id.id
                    lag = link.lag_days
                    if link.link_type == "FS":
                        candidate = ef[p] + lag
                    elif link.link_type == "SS":
                        candidate = es[p] + lag
                    elif link.link_type == "FF":
                        candidate = ef[p] + lag - dur
                    else:
                        candidate = es[p] + lag - dur
                    if candidate > start:
                        start, drv = candidate, p
                es[aid] = start
                ef[aid] = start + dur
                driver[aid] = drv
            last = max(ef, key=lambda k: ef[k])
            finishes.append(ef[last])
            node, seen = last, set()
            while node and node not in seen:
                seen.add(node)
                critical_count[node] += 1
                node = driver.get(node)

        finishes.sort()

        def pct(q):
            value = finishes[min(len(finishes) - 1, int(math.ceil(q * len(finishes))) - 1)]
            return calendar.date_at(max(int(math.ceil(value)) - 1, 0))

        reference = project.epc_baseline_finish or project.epc_planned_finish
        on_time = 0.0
        if reference:
            limit = calendar.offset_of(reference) + 1
            on_time = sum(1 for f in finishes if f <= limit + 1e-9) / len(finishes) * 100.0
        for activity in order:
            activity.sudo().criticality_index = round(critical_count[activity.id] / iterations * 100.0, 1)
        return {
            "epc_mc_p50_finish": pct(0.5),
            "epc_mc_p80_finish": pct(0.8),
            "epc_mc_p90_finish": pct(0.9),
            "epc_mc_on_time_probability": round(on_time, 1),
            "epc_mc_iterations": iterations,
        }

    # ------------------------------------------------------------------
    # Project roll-up
    # ------------------------------------------------------------------
    def _project_rollup(self, project, forecasts):
        valid = {aid: f for aid, f in forecasts.items() if not f.get("skip")}
        reference_finish = project.epc_baseline_finish or project.epc_planned_finish
        if not valid or not reference_finish:
            return {}
        finish = max(f["finish"] for f in valid.values())
        delay = (finish - reference_finish).days
        activities = self.env["epc.activity"].browse(list(valid))
        open_activities = activities.filtered(lambda a: a.state != "done")
        driving = open_activities.filtered(
            lambda a: valid[a.id]["finish"] >= reference_finish or a.total_float <= 0)
        candidates = driving or open_activities
        probability = max((valid[a.id]["probability"] for a in candidates), default=0.0)
        if not open_activities:
            probability = 100.0 if delay > 0 else 0.0

        bac, ev, ac = project.epc_bac, project.epc_ev, project.epc_ac
        cpi, spi = project.epc_cpi, project.epc_spi
        if ev > 0 and ac > 0 and cpi > 0:
            eac50 = ac + (bac - ev) / cpi
            eac80 = ac + (bac - ev) / max(0.3, cpi * min(spi or 1.0, 1.0))
        else:
            eac50 = bac
            eac80 = bac * 1.05
        eac80 = max(eac50, eac80)

        ld = 0.0
        if delay > 0 and project.epc_contract_value and project.epc_ld_rate_pct:
            weeks = math.ceil(delay / 7.0)
            pct = weeks * project.epc_ld_rate_pct
            if project.epc_ld_cap_pct:
                pct = min(pct, project.epc_ld_cap_pct)
            ld = project.epc_contract_value * pct / 100.0

        threshold_days, threshold_prob = self._thresholds()
        if not open_activities:
            level = "low"  # completed: nothing left to put at risk
        elif delay >= 3 * threshold_days and probability >= threshold_prob:
            level = "critical"
        elif delay >= threshold_days and probability >= threshold_prob:
            level = "high"
        elif delay > 0 or probability >= 40:
            level = "medium"
        else:
            level = "low"

        top = sorted(
            (a for a in open_activities if valid[a.id]["slip"] > 0),
            key=lambda a: (-valid[a.id]["slip"], -valid[a.id]["probability"]))[:5]

        def esc(value):
            return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        default_driver = _("rate of progress")
        if top:
            items = "".join(
                "<li><b>%s %s</b>: %s days, %s%% probability — %s</li>" % (
                    esc(a.code), esc(a.name), valid[a.id]["slip"], round(valid[a.id]["probability"]),
                    esc("; ".join(valid[a.id]["drivers"]) or default_driver))
                for a in top)
            drivers_html = "<ul>%s</ul>" % items
        else:
            drivers_html = "<p>%s</p>" % esc(_("No activity is forecast to slip."))
        return {
            "epc_predicted_finish": finish,
            "epc_predicted_delay_days": delay,
            "epc_delay_probability": round(probability, 1),
            "epc_risk_level": level,
            "epc_eac_p50": eac50,
            "epc_eac_p80": eac80,
            "epc_ld_exposure": ld,
            "epc_prediction_date": fields.Datetime.now(),
            "epc_prediction_drivers": drivers_html,
        }

    # ------------------------------------------------------------------
    # Early warnings
    # ------------------------------------------------------------------
    def _upsert_engine_risk(self, key, vals, notify_user=None):
        Risk = self.env["epc.risk"].sudo()
        risk = Risk.search([("engine_key", "=", key)], limit=1)
        if risk:
            update = dict(vals)
            if risk.state == "closed":
                update["state"] = "open"
            risk.write(update)
            return risk, False
        risk = Risk.create(dict(vals, engine_key=key, source="engine", state="open"))
        notify_user = (notify_user or self.env["res.users"]).filtered(lambda u: u.active and not u.share)[:1]
        if notify_user:
            risk.activity_schedule(
                "mail.mail_activity_data_todo",
                summary=_("Early warning: %s", vals["name"])[:255],
                note=vals.get("description", ""),
                user_id=notify_user.id,
            )
        return risk, True

    def _close_engine_risk(self, key, reason):
        risk = self.env["epc.risk"].sudo().search([("engine_key", "=", key), ("state", "!=", "closed")], limit=1)
        if risk:
            risk.write({"state": "closed"})
            risk.message_post(body=reason)

    def _raise_alerts(self, project, forecasts):
        threshold_days, _threshold_prob = self._thresholds()

        def probability_band(prob):
            return str(max(1, min(5, int(math.ceil(prob / 20.0)))))

        def impact_band(days):
            for limit, band in ((7, "2"), (14, "3"), (30, "4")):
                if days < limit:
                    return band
            return "5"

        for activity in project.epc_activity_ids:
            forecast = forecasts.get(activity.id)
            key = "engine:activity:%s" % activity.id
            if not forecast or forecast.get("skip"):
                continue
            if activity.state != "done" and forecast["level"] in ("high", "critical") and not activity.is_milestone:
                self._upsert_engine_risk(key, {
                    "name": _("%(code)s %(name)s forecast %(days)s days late",
                              code=activity.code, name=activity.name, days=forecast["slip"])[:250],
                    "project_id": project.id,
                    "activity_id": activity.id,
                    "category": "schedule",
                    "probability": probability_band(forecast["probability"]),
                    "impact": impact_band(forecast["slip"]),
                    "description": "; ".join(forecast["drivers"]) or _("Rate of progress below plan."),
                }, notify_user=activity.responsible_id | project.user_id)
            else:
                self._close_engine_risk(key, _("Closed automatically: forecast back within tolerance."))

        project_key = "engine:project:%s" % project.id
        if project.epc_risk_level in ("high", "critical"):
            self._upsert_engine_risk(project_key, {
                "name": _("Project completion forecast %(days)s days late", days=project.epc_predicted_delay_days),
                "project_id": project.id,
                "category": "schedule",
                "probability": probability_band(project.epc_delay_probability),
                "impact": impact_band(project.epc_predicted_delay_days),
                "description": _("Predicted finish %(finish)s against baseline %(baseline)s. LD exposure %(ld)s.",
                                 finish=project.epc_predicted_finish, baseline=project.epc_baseline_finish,
                                 ld=round(project.epc_ld_exposure, 2)),
            }, notify_user=project.user_id)
        else:
            self._close_engine_risk(project_key, _("Closed automatically: project forecast back within tolerance."))

        for line in project.sudo().epc_purchase_line_ids:
            key = "engine:po_line:%s" % line.id
            if line.epc_expedite_status == "late" or (
                    line.epc_expedite_status == "at_risk" and line.epc_predicted_delay_days >= threshold_days):
                self._upsert_engine_risk(key, {
                    "name": _("%(po)s %(item)s from %(vendor)s forecast %(days)s days late",
                              po=line.order_id.name, item=(line.product_id.display_name or line.name)[:60],
                              vendor=line.partner_id.name, days=line.epc_predicted_delay_days)[:250],
                    "project_id": project.id,
                    "purchase_line_id": line.id,
                    "category": "procurement",
                    "probability": "4" if line.epc_expedite_status == "late" else "3",
                    "impact": impact_band(line.epc_predicted_delay_days),
                    "description": _("Supplier risk score %(score)s; forecast delivery %(date)s.",
                                     score=line.partner_id.commercial_partner_id.epc_supplier_risk_score,
                                     date=line.epc_forecast_date),
                }, notify_user=line.order_id.user_id | project.user_id)
            else:
                self._close_engine_risk(key, _("Closed automatically: delivery back on track."))

        for material in project.sudo().epc_external_material_ids:
            key = "engine:ext_material:%s" % material.id
            if material.expedite_status == "late" or (
                    material.expedite_status == "at_risk" and material.predicted_delay_days >= threshold_days):
                self._upsert_engine_risk(key, {
                    "name": _("%(ref)s %(item)s forecast %(days)s days late",
                              ref=material.external_ref, item=material.description[:60],
                              days=material.predicted_delay_days)[:250],
                    "project_id": project.id,
                    "category": "procurement",
                    "probability": "4" if material.expedite_status == "late" else "3",
                    "impact": impact_band(max(material.predicted_delay_days, 1)),
                    "description": _("Tracked in %(system)s; promised %(promised)s, forecast %(date)s, needed %(need)s.",
                                     system=dict(material._fields["source_system"].selection)[material.source_system],
                                     promised=material.promised_date, date=material.forecast_date,
                                     need=material.required_date),
                }, notify_user=project.user_id)
            else:
                self._close_engine_risk(key, _("Closed automatically: delivery back on track."))

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------
    def run_for_projects(self, projects):
        factors = self._get_duration_factors()
        summary = {}
        for project in projects.filtered("is_epc"):
            purchase_lines = project.sudo().epc_purchase_line_ids
            if purchase_lines:
                self._update_supplier_scores(purchase_lines.mapped("partner_id"))
                self._forecast_purchase_lines(purchase_lines)
            externals = project.sudo().epc_external_material_ids
            if externals:
                vendors = externals.mapped("vendor_id")
                if vendors:
                    self._update_supplier_scores(vendors)
                self._forecast_external_materials(externals)
            try:
                forecasts = self._forecast_project(project, factors)
            except UserError as exc:
                project.message_post(body=_("Prediction skipped: %s", exc))
                _logger.warning("EPC prediction skipped for project %s: %s", project.id, exc)
                continue
            rollup = self._project_rollup(project, forecasts)
            if rollup:
                rollup.update(self._monte_carlo(project, forecasts, factors))
                project.sudo().write(rollup)
            self._raise_alerts(project, forecasts)
            summary[project.id] = rollup
        return summary

    @api.model
    def _cron_run_predictions(self):
        projects = self.env["project.project"].search([("is_epc", "=", True), ("active", "=", True)])
        for project in projects:
            try:
                with self.env.cr.savepoint():
                    self.run_for_projects(project)
            except Exception:
                _logger.exception("EPC prediction failed for project %s", project.id)
        return True

    @api.model
    def _cron_learn(self):
        return self.learn_from_history()
