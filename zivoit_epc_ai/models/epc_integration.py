"""Enterprise integration: external references, import templates, portfolio snapshot and the REST API service."""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError

_logger = logging.getLogger(__name__)

EXTERNAL_SYSTEMS = [("sap", "SAP"), ("oracle", "Oracle"), ("primavera", "Primavera P6"), ("other", "Other")]


def _template(label, filename):
    return {"label": label, "template": "/zivoit_epc_ai/static/templates/%s" % filename}


class ProjectProjectIntegration(models.Model):
    _inherit = "project.project"

    epc_external_system = fields.Selection(EXTERNAL_SYSTEMS, string="System of Record")
    epc_external_ref = fields.Char(string="External Project ID", index=True, copy=False,
                                   help="Project definition / WBS element in SAP PS, Oracle project number or P6 ID.")
    # Stored KPI snapshot written by the prediction engine so portfolio pivots and graphs can aggregate them.
    epc_snap_spi = fields.Float(aggregator="avg", string="SPI (snapshot)", readonly=True, copy=False, digits=(6, 2))
    epc_snap_cpi = fields.Float(aggregator="avg", string="CPI (snapshot)", readonly=True, copy=False, digits=(6, 2))
    epc_snap_progress = fields.Float(aggregator="avg", string="Actual % (snapshot)", readonly=True, copy=False, digits=(6, 1))
    epc_snap_planned = fields.Float(aggregator="avg", string="Planned % (snapshot)", readonly=True, copy=False, digits=(6, 1))
    epc_snap_open_risks = fields.Integer(aggregator="sum", string="Open Risks (snapshot)", readonly=True, copy=False)

    _epc_external_ref_uniq = models.Constraint(
        "unique(company_id, epc_external_system, epc_external_ref)",
        "This external project ID is already linked to another project.")

    def _epc_write_snapshot(self):
        for project in self:
            project.sudo().write({
                "epc_snap_spi": project.epc_spi,
                "epc_snap_cpi": project.epc_cpi,
                "epc_snap_progress": project.epc_actual_progress,
                "epc_snap_planned": project.epc_planned_progress,
                "epc_snap_open_risks": project.epc_open_risk_count,
            })


class EpcActivityIntegration(models.Model):
    _inherit = "epc.activity"

    external_ref = fields.Char(string="External ID", index=True, copy=False,
                               help="Activity ID in Primavera P6 / network activity in SAP PS.")

    @api.model
    def get_import_templates(self):
        return [_template(_("Schedule activities template (CSV)"), "epc_activity_template.csv")]


class EpcBoqLineIntegration(models.Model):
    _inherit = "epc.boq.line"

    @api.model
    def get_import_templates(self):
        return [_template(_("Bill of Quantities template (CSV)"), "epc_boq_template.csv")]


class EpcMtoLineIntegration(models.Model):
    _inherit = "epc.mto.line"

    @api.model
    def get_import_templates(self):
        return [_template(_("Material take-off template (CSV)"), "epc_mto_template.csv")]


class EpcExternalMaterialIntegration(models.Model):
    _inherit = "epc.external.material"

    @api.model
    def get_import_templates(self):
        return [_template(_("ERP material status template (CSV)"), "epc_external_material_template.csv")]


class EpcPredictionEngineIntegration(models.AbstractModel):
    _inherit = "epc.prediction.engine"

    def run_for_projects(self, projects):
        summary = super().run_for_projects(projects)
        projects.filtered("is_epc")._epc_write_snapshot()
        return summary


class EpcApi(models.AbstractModel):
    """Business logic behind the REST API. Runs as the API key's user, so record rules and ACLs apply."""
    _name = "epc.api"
    _description = "ZivoIT EPC AI integration API"

    MATERIAL_FIELDS = ("description", "quantity", "delivered_qty", "uom_name", "promised_date", "stage", "vendor_name")

    @api.model
    def _check_user(self, manager=False):
        group = "zivoit_epc_ai.group_epc_manager" if manager else "zivoit_epc_ai.group_epc_user"
        if not self.env.user.has_group(group):
            raise AccessError(_("This API key's user lacks the %s role.",
                                _("EPC Manager") if manager else _("EPC User")))

    @api.model
    def _project(self, project_id):
        project = self.env["project.project"].browse(int(project_id)).exists()
        if not project or not project.is_epc:
            raise MissingError(_("EPC project %s not found.", project_id))
        project.check_access("read")
        return project

    @api.model
    def _date(self, value):
        return str(value) if value else None

    @api.model
    def project_summary(self, project):
        return {
            "id": project.id,
            "name": project.name,
            "contract_ref": project.epc_contract_ref or None,
            "external_system": project.epc_external_system or None,
            "external_ref": project.epc_external_ref or None,
            "company": project.company_id.name,
            "currency": project.currency_id.name,
            "contract_value": project.epc_contract_value,
            "status_date": self._date(project.epc_status_date),
            "baseline_finish": self._date(project.epc_baseline_finish),
            "planned_finish": self._date(project.epc_planned_finish),
            "predicted_finish": self._date(project.epc_predicted_finish),
            "predicted_delay_days": project.epc_predicted_delay_days,
            "delay_probability_pct": project.epc_delay_probability,
            "risk_level": project.epc_risk_level or None,
            "finish_p50": self._date(project.epc_mc_p50_finish),
            "finish_p80": self._date(project.epc_mc_p80_finish),
            "finish_p90": self._date(project.epc_mc_p90_finish),
            "on_time_probability_pct": project.epc_mc_on_time_probability,
            "spi": project.epc_spi,
            "cpi": project.epc_cpi,
            "planned_progress_pct": project.epc_planned_progress,
            "actual_progress_pct": project.epc_actual_progress,
            "eac_p50": project.epc_eac_p50,
            "eac_p80": project.epc_eac_p80,
            "ld_exposure": project.epc_ld_exposure,
            "last_prediction": str(project.epc_prediction_date) if project.epc_prediction_date else None,
        }

    @api.model
    def list_projects(self, params=None):
        self._check_user()
        params = params or {}
        domain = [("is_epc", "=", True)]
        if params.get("external_ref"):
            domain.append(("epc_external_ref", "=", params["external_ref"]))
        if params.get("contract_ref"):
            domain.append(("epc_contract_ref", "=", params["contract_ref"]))
        projects = self.env["project.project"].search(domain, order="id")
        return {"projects": [self.project_summary(p) for p in projects]}

    @api.model
    def get_project(self, project_id):
        self._check_user()
        return self.project_summary(self._project(project_id))

    @api.model
    def get_activities(self, project_id):
        self._check_user()
        project = self._project(project_id)
        return {"activities": [{
            "id": a.id, "code": a.code, "external_ref": a.external_ref or None, "name": a.name, "wbs": a.wbs or None,
            "discipline": a.discipline or None, "duration": a.duration, "state": a.state, "progress_pct": a.progress,
            "planned_start": self._date(a.planned_start), "planned_finish": self._date(a.planned_finish),
            "baseline_finish": self._date(a.baseline_finish), "actual_start": self._date(a.actual_start),
            "actual_finish": self._date(a.actual_finish), "total_float": a.total_float, "critical": a.is_critical,
            "forecast_start": self._date(a.forecast_start), "forecast_finish": self._date(a.forecast_finish),
            "delay_probability_pct": a.delay_probability, "risk_level": a.risk_level or None,
            "criticality_index_pct": a.criticality_index, "drivers": a.prediction_drivers or None,
        } for a in project.epc_activity_ids]}

    @api.model
    def get_risks(self, project_id):
        self._check_user()
        project = self._project(project_id)
        risks = self.env["epc.risk"].search([("project_id", "=", project.id), ("state", "!=", "closed")])
        return {"risks": [{
            "id": r.id, "name": r.name, "category": r.category, "state": r.state, "source": r.source,
            "probability": r.probability, "impact": r.impact, "score": r.score,
            "activity_code": r.activity_id.code or None,
        } for r in risks]}

    @api.model
    def _activity_by_code(self, project, codes):
        acts = self.env["epc.activity"].search([("project_id", "=", project.id), "|",
                                                ("code", "in", codes), ("external_ref", "in", codes)])
        result = {}
        for act in acts:
            result[act.code] = act
            if act.external_ref:
                result[act.external_ref] = act
        return result

    @api.model
    def push_materials(self, project_id, payload):
        self._check_user()
        project = self._project(project_id)
        items = (payload or {}).get("materials")
        if not isinstance(items, list) or not items:
            raise ValidationError(_("Send {\"materials\": [...]} with at least one item."))
        Material = self.env["epc.external.material"]
        codes = [c for item in items for c in (item.get("activity_codes") or [])]
        acts = self._activity_by_code(project, codes)
        created = updated = 0
        errors = []
        now = fields.Datetime.now()
        for index, item in enumerate(items):
            system = item.get("source_system") or "other"
            ref = item.get("external_ref")
            if not ref:
                errors.append({"index": index, "error": "external_ref is required"})
                continue
            if system not in dict(Material._fields["source_system"].selection):
                errors.append({"index": index, "error": "unknown source_system %s" % system})
                continue
            vals = {k: item[k] for k in self.MATERIAL_FIELDS if k in item}
            if "uom" in item:
                vals["uom_name"] = item["uom"]
            if item.get("vendor_ref"):
                vendor = self.env["res.partner"].search([("ref", "=", item["vendor_ref"])], limit=1)
                if vendor:
                    vals["vendor_id"] = vendor.id
            missing = [c for c in item.get("activity_codes") or [] if c not in acts]
            if missing:
                errors.append({"index": index, "error": "unknown activity codes %s" % ", ".join(missing)})
            if "activity_codes" in item:
                vals["activity_ids"] = [(6, 0, list({acts[c].id for c in item["activity_codes"] if c in acts}))]
            vals["last_sync"] = now
            existing = Material.search([("project_id", "=", project.id), ("source_system", "=", system),
                                        ("external_ref", "=", ref)], limit=1)
            try:
                with self.env.cr.savepoint():
                    if existing:
                        existing.write(vals)
                        updated += 1
                    else:
                        vals.update({"project_id": project.id, "source_system": system, "external_ref": ref})
                        vals.setdefault("description", ref)
                        Material.create(vals)
                        created += 1
            except (ValidationError, UserError, ValueError) as exc:
                errors.append({"index": index, "error": str(exc)})
        return {"created": created, "updated": updated, "errors": errors}

    @api.model
    def push_progress(self, project_id, payload):
        """Create a DPR from measured quantities and/or update activity dates and manual progress."""
        self._check_user()
        project = self._project(project_id)
        payload = payload or {}
        lines = payload.get("lines") or []
        updates = payload.get("activities") or []
        if not lines and not updates:
            raise ValidationError(_("Send \"lines\" (measured quantities) and/or \"activities\" (status updates)."))
        codes = [l.get("activity_code") for l in lines] + [u.get("code") for u in updates]
        acts = self._activity_by_code(project, [c for c in codes if c])
        unknown = sorted({c for c in codes if c not in acts})
        if unknown:
            raise ValidationError(_("Unknown activity codes: %s", ", ".join(map(str, unknown))))
        result = {"dpr": None, "activities_updated": 0}
        if lines:
            dpr = self.env["epc.dpr"].create({
                "project_id": project.id,
                "date": payload.get("date") or fields.Date.context_today(self),
                "notes_raw": payload.get("remarks") or False,
                "line_ids": [(0, 0, {"activity_id": acts[l["activity_code"]].id, "quantity": float(l.get("quantity") or 0)})
                             for l in lines],
                "manpower_ids": [(0, 0, {"trade": m.get("trade") or "General", "count": int(m.get("count") or 0)})
                                 for m in payload.get("manpower") or []],
            })
            if payload.get("submit"):
                dpr.action_submit()
            result["dpr"] = {"id": dpr.id, "name": dpr.name, "state": dpr.state}
        for update in updates:
            activity = acts[update["code"]]
            vals = {}
            for key in ("actual_start", "actual_finish"):
                if key in update:
                    vals[key] = update[key] or False
            if "progress" in update:
                if activity.progress_method != "manual":
                    raise ValidationError(_("%s measures progress from DPR quantities; send quantities in \"lines\".",
                                            activity.code))
                vals["manual_progress"] = float(update["progress"])
            if vals:
                activity.write(vals)
                result["activities_updated"] += 1
        project.sudo().message_post(author_id=self.env.user.partner_id.id, body=_("Progress received through the integration API (%(lines)s quantities, "
                                    "%(acts)s activity updates).", lines=len(lines), acts=result["activities_updated"]))
        return result

    @api.model
    def run_prediction(self, project_id):
        self._check_user(manager=True)
        project = self._project(project_id)
        self.env["epc.prediction.engine"].run_for_projects(project)
        return self.project_summary(project)
