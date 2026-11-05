import base64
import math
from datetime import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError

P6_LINK_TYPES = {"PR_FS": "FS", "PR_SS": "SS", "PR_FF": "FF", "PR_SF": "SF"}
P6_LINK_TYPES_REVERSE = {v: k for k, v in P6_LINK_TYPES.items()}
P6_DATE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%b-%y %H:%M", "%d-%b-%y")


def parse_xer(content):
    """Parse a Primavera XER export into ``{table: [row dict, ...]}``."""
    tables = {}
    current, columns = None, []
    for raw_line in content.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            continue
        parts = line.split("\t")
        marker = parts[0]
        if marker == "%T":
            current = parts[1].strip() if len(parts) > 1 else None
            tables.setdefault(current, [])
            columns = []
        elif marker == "%F" and current:
            columns = [c.strip() for c in parts[1:]]
        elif marker == "%R" and current and columns:
            values = parts[1:] + [""] * (len(columns) - len(parts) + 1)
            tables[current].append(dict(zip(columns, values)))
        elif marker == "%E":
            break
    return tables


def parse_p6_date(value):
    value = (value or "").strip()
    if not value:
        return False
    for fmt in P6_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return False


def _float(value):
    try:
        return float((value or "0").strip() or 0)
    except ValueError:
        return 0.0


class EpcP6Import(models.TransientModel):
    _name = "epc.p6.import"
    _description = "Primavera P6 XER Import / Export"

    project_id = fields.Many2one("project.project", required=True, domain=[("is_epc", "=", True)])
    xer_file = fields.Binary(string="XER File")
    xer_filename = fields.Char()
    hours_per_day = fields.Float(default=8.0, required=True)
    update_existing = fields.Boolean(string="Update existing activities", default=True)
    import_actuals = fields.Boolean(string="Import actual dates and % complete", default=True)
    set_baseline = fields.Boolean(string="Use target dates as baseline when empty", default=True)
    result = fields.Text(readonly=True)
    export_file = fields.Binary(readonly=True, attachment=False)
    export_filename = fields.Char(readonly=True)

    def _days(self, hours):
        per_day = self.hours_per_day or 8.0
        return int(math.ceil(max(0.0, hours) / per_day - 1e-9))

    def action_import(self):
        self.ensure_one()
        if not self.xer_file:
            raise UserError(_("Upload an XER file exported from Primavera P6."))
        raw = base64.b64decode(self.xer_file)
        content = None
        for encoding in ("utf-8", "cp1252", "latin-1"):
            try:
                content = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        tables = parse_xer(content or "")
        tasks = tables.get("TASK") or []
        if not tasks:
            raise UserError(_("No TASK table was found in this file. Is it a Primavera XER export?"))
        wbs_names = {row.get("wbs_id"): row.get("wbs_short_name") or row.get("wbs_name") for row in tables.get("PROJWBS", [])}

        Activity = self.env["epc.activity"]
        existing = {a.code: a for a in self.project_id.epc_activity_ids}
        by_task_id = {}
        created = updated = skipped = 0
        for seq, row in enumerate(tasks, start=1):
            code = (row.get("task_code") or "").strip()
            if not code:
                skipped += 1
                continue
            is_milestone = (row.get("task_type") or "") in ("TT_Mile", "TT_FinMile")
            duration = 0 if is_milestone else self._days(_float(row.get("target_drtn_hr_cnt")))
            vals = {
                "name": (row.get("task_name") or code).strip()[:250],
                "duration": duration,
                "is_milestone": is_milestone,
                "planned_start": parse_p6_date(row.get("target_start_date")),
                "planned_finish": parse_p6_date(row.get("target_end_date")),
            }
            if wbs_names.get(row.get("wbs_id")):
                vals["wbs"] = wbs_names[row.get("wbs_id")]
            if vals["planned_start"] and vals["planned_finish"] and vals["planned_finish"] < vals["planned_start"]:
                vals["planned_finish"] = vals["planned_start"]
            if self.import_actuals:
                actual_start = parse_p6_date(row.get("act_start_date"))
                actual_finish = parse_p6_date(row.get("act_end_date"))
                if actual_start:
                    vals["actual_start"] = actual_start
                if actual_finish and (not actual_start or actual_finish >= actual_start):
                    vals["actual_finish"] = actual_finish
                if row.get("phys_complete_pct") not in (None, ""):
                    vals["manual_progress"] = max(0.0, min(100.0, _float(row.get("phys_complete_pct"))))
            activity = existing.get(code)
            if activity:
                if not self.update_existing:
                    skipped += 1
                    by_task_id[row.get("task_id")] = activity
                    continue
                if "manual_progress" in vals and not activity.boq_line_id:
                    vals["progress_method"] = "manual"
                activity.write(vals)
                updated += 1
            else:
                vals.update({
                    "project_id": self.project_id.id,
                    "code": code,
                    "sequence": seq * 10,
                    "progress_method": "manual",
                })
                activity = Activity.create(vals)
                existing[code] = activity
                created += 1
            if self.set_baseline and not activity.baseline_start and activity.planned_start:
                activity.write({"baseline_start": activity.planned_start,
                                "baseline_finish": activity.planned_finish or activity.planned_start})
            by_task_id[row.get("task_id")] = activity

        Link = self.env["epc.activity.link"]
        links_created = 0
        for row in tables.get("TASKPRED") or []:
            successor = by_task_id.get(row.get("task_id"))
            predecessor = by_task_id.get(row.get("pred_task_id"))
            if not successor or not predecessor or successor == predecessor:
                continue
            link_type = P6_LINK_TYPES.get((row.get("pred_type") or "PR_FS").strip(), "FS")
            lag = int(round(_float(row.get("lag_hr_cnt")) / (self.hours_per_day or 8.0)))
            link = Link.search([("predecessor_id", "=", predecessor.id), ("successor_id", "=", successor.id)], limit=1)
            if link:
                link.write({"link_type": link_type, "lag_days": lag})
            else:
                Link.create({"predecessor_id": predecessor.id, "successor_id": successor.id,
                             "link_type": link_type, "lag_days": lag})
                links_created += 1

        message = _("P6 import: %(c)s activities created, %(u)s updated, %(s)s skipped, %(l)s new relationships.",
                    c=created, u=updated, s=skipped, l=links_created)
        self.result = message
        self.project_id.message_post(body=message)
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    # ------------------------------------------------------------------
    @api.model
    def _fmt_date(self, value):
        return value.strftime("%Y-%m-%d 08:00") if value else ""

    def _build_xer(self):
        self.ensure_one()
        project = self.project_id
        activities = project.epc_activity_ids.sorted(lambda a: (a.sequence, a.id))
        per_day = self.hours_per_day or 8.0
        proj_id = str(project.id)
        out = [
            "ERMHDR\t19.12\t%s\tProject\tZivoIT EPC\tZivoIT EPC\tdbxDatabaseNoName\tProject Management\tINR"
            % fields.Date.context_today(self).strftime("%Y-%m-%d"),
            "%T\tPROJECT",
            "%F\tproj_id\tproj_short_name",
            "%%R\t%s\t%s" % (proj_id, (project.epc_contract_ref or project.name or "").replace("\t", " ")),
            "%T\tTASK",
            "%F\ttask_id\tproj_id\twbs_id\ttask_code\ttask_name\ttask_type\tstatus_code\ttarget_drtn_hr_cnt"
            "\ttarget_start_date\ttarget_end_date\tact_start_date\tact_end_date\tphys_complete_pct",
        ]
        for activity in activities:
            status = "TK_Complete" if activity.state == "done" else (
                "TK_Active" if activity.state == "in_progress" else "TK_NotStart")
            out.append("\t".join([
                "%R", str(activity.id), proj_id, "", activity.code.replace("\t", " "),
                activity.name.replace("\t", " "), "TT_Mile" if activity.is_milestone else "TT_Task", status,
                "%g" % (0 if activity.is_milestone else activity.duration * per_day),
                self._fmt_date(activity.planned_start), self._fmt_date(activity.planned_finish),
                self._fmt_date(activity.actual_start), self._fmt_date(activity.actual_finish),
                "%g" % round(activity.progress, 2),
            ]))
        out += [
            "%T\tTASKPRED",
            "%F\ttask_pred_id\ttask_id\tpred_task_id\tproj_id\tpred_proj_id\tpred_type\tlag_hr_cnt",
        ]
        links = self.env["epc.activity.link"].search([("successor_id", "in", activities.ids)])
        for link in links:
            out.append("\t".join([
                "%R", str(link.id), str(link.successor_id.id), str(link.predecessor_id.id), proj_id, proj_id,
                P6_LINK_TYPES_REVERSE.get(link.link_type, "PR_FS"), "%g" % (link.lag_days * per_day),
            ]))
        out.append("%E")
        return "\r\n".join(out) + "\r\n"

    def action_export(self):
        self.ensure_one()
        content = self._build_xer()
        filename = "%s.xer" % (self.project_id.epc_contract_ref or "project_%s" % self.project_id.id)
        self.write({
            "export_file": base64.b64encode(content.encode("utf-8")),
            "export_filename": "".join(c if c.isalnum() or c in "._-" else "_" for c in filename),
        })
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
