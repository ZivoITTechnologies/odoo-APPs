# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
import json
from datetime import timedelta

from odoo import api, fields, models

from .qb_entity_config import ENTITY_SELECTION


class QbSyncLog(models.Model):
    _name = "qb.sync.log"
    _description = "QuickBooks Sync Log"
    _order = "id desc"

    connection_id = fields.Many2one("qb.connection", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="connection_id.company_id", store=True)
    job_id = fields.Many2one("qb.sync.job", ondelete="set null", index=True)
    level = fields.Selection([("info", "Info"), ("success", "Success"), ("warning", "Warning"),
                              ("error", "Error"), ("api", "API Call")], default="info", index=True)
    direction = fields.Selection([("import", "QuickBooks → Odoo"), ("export", "Odoo → QuickBooks"),
                                  ("system", "System")], default="system")
    qb_entity = fields.Selection(ENTITY_SELECTION, string="Entity")
    qb_id = fields.Char("QuickBooks ID")
    res_model = fields.Char()
    res_id = fields.Many2oneReference(model_field="res_model")
    message = fields.Text()
    request_method = fields.Char()
    request_url = fields.Char()
    request_body = fields.Text()
    response_status = fields.Integer()
    response_body = fields.Text()
    duration_ms = fields.Integer("Duration (ms)")
    intuit_tid = fields.Char("intuit_tid", help="Intuit transaction id – quote it to Intuit support.")

    @api.model
    def _log(self, connection, message, level="info", direction="system", job=None, qb_entity=None,
             qb_id=None, record=None, **extra):
        vals = {
            "connection_id": connection.id,
            "message": message,
            "level": level,
            "direction": direction,
            "job_id": job.id if job else False,
            "qb_entity": qb_entity or (job.qb_entity if job else False),
            "qb_id": qb_id or (job.qb_id if job else False),
        }
        if record:
            vals.update(res_model=record._name, res_id=record.id)
        elif job and job.res_model:
            vals.update(res_model=job.res_model, res_id=job.res_id)
        for key, value in extra.items():
            if key in ("request_body", "response_body") and value is not None and not isinstance(value, str):
                value = json.dumps(value, indent=1, default=str)
            vals[key] = value
        return self.sudo().create(vals)

    def action_open_record(self):
        self.ensure_one()
        if not (self.res_model and self.res_id):
            return False
        return {"type": "ir.actions.act_window", "res_model": self.res_model, "res_id": self.res_id,
                "view_mode": "form", "target": "current"}

    @api.model
    def _cron_cleanup(self):
        for conn in self.env["qb.connection"].search([]):
            limit = fields.Datetime.now() - timedelta(days=conn.log_retention_days or 30)
            self.search([("connection_id", "=", conn.id), ("create_date", "<", limit)]).unlink()
            self.env["qb.sync.job"].search([
                ("connection_id", "=", conn.id), ("state", "in", ("done", "skipped")),
                ("write_date", "<", limit)]).unlink()
