# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
import logging
import traceback
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..lib.qbo_client import QBOAuthError, QBOError
from .qb_entity_config import ENTITY_SELECTION, ENTITY_SEQUENCE

_logger = logging.getLogger(__name__)


class _QbFlow(Exception):
    """Control-flow exception. Work done inside the job savepoint is rolled
    back; ``followup`` (a callable) is executed afterwards so that queued
    follow-up jobs or mapping states survive the rollback."""

    def __init__(self, message="", followup=None):
        super().__init__(message)
        self.followup = followup

    def run_followup(self):
        if self.followup:
            self.followup()


class QbConflict(_QbFlow):
    """Raised when both sides changed and the policy says: ask a human."""


class QbSkip(_QbFlow):
    """Raised by handlers to finish a job without error (nothing to do)."""


class QbSyncJob(models.Model):
    _name = "qb.sync.job"
    _description = "QuickBooks Sync Queue Job"
    _order = "priority, id"

    name = fields.Char(compute="_compute_name", store=True)
    connection_id = fields.Many2one("qb.connection", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="connection_id.company_id", store=True)
    direction = fields.Selection([("import", "QuickBooks → Odoo"), ("export", "Odoo → QuickBooks"),
                                  ("delete", "Delete in QuickBooks"), ("void", "Void in QuickBooks")],
                                 required=True, index=True)
    qb_entity = fields.Selection(ENTITY_SELECTION, string="Entity", required=True, index=True)
    qb_id = fields.Char("QuickBooks ID", index=True)
    operation = fields.Char(help="Webhook / CDC operation (Create, Update, Delete, Void, Merge...)")
    res_model = fields.Char(index=True)
    res_id = fields.Many2oneReference(model_field="res_model", index=True)
    payload = fields.Json(help="Cached QuickBooks object (from CDC) to avoid an extra API read.")
    state = fields.Selection([
        ("pending", "Pending"), ("running", "Running"), ("done", "Done"), ("failed", "Failed"),
        ("conflict", "Conflict"), ("skipped", "Skipped")], default="pending", required=True, index=True)
    priority = fields.Integer(default=50, index=True)
    attempts = fields.Integer(default=0)
    max_attempts = fields.Integer(default=5)
    next_attempt = fields.Datetime(default=fields.Datetime.now, index=True)
    date_done = fields.Datetime()
    error = fields.Text()
    error_detail = fields.Text()
    source = fields.Selection([("manual", "Manual"), ("cron", "Scheduler"), ("webhook", "Webhook"),
                               ("cdc", "Change Data Capture"), ("auto", "Automatic (Odoo event)"),
                               ("wizard", "Wizard")], default="manual")
    log_ids = fields.One2many("qb.sync.log", "job_id")

    @api.depends("direction", "qb_entity", "qb_id", "res_model", "res_id")
    def _compute_name(self):
        for job in self:
            ref = job.qb_id and "QB#%s" % job.qb_id or "%s,%s" % (job.res_model, job.res_id)
            job.name = "%s %s %s" % (dict(self._fields["direction"].selection).get(job.direction),
                                     job.qb_entity, ref)

    # ---------------------------------------------------------------- run
    def _run_one(self):
        """Process one job inside a savepoint. Never raises."""
        self.ensure_one()
        job = self
        conn = job.connection_id
        job.write({"state": "running", "attempts": job.attempts + 1})
        try:
            with self.env.cr.savepoint():
                if job.direction == "import":
                    self.env["qb.importer"]._run_job(job)
                else:
                    self.env["qb.exporter"]._run_job(job)
            job.write({"state": "done", "date_done": fields.Datetime.now(), "error": False,
                       "error_detail": False})
        except QbSkip as exc:
            exc.run_followup()
            job.write({"state": "skipped", "date_done": fields.Datetime.now(), "error": str(exc) or False})
        except QbConflict as exc:
            exc.run_followup()
            job.write({"state": "conflict", "error": str(exc)})
            self.env["qb.sync.log"]._log(conn, str(exc), level="warning", job=job,
                                         direction=job._log_direction())
        except QBOAuthError as exc:
            job.write({"state": "pending", "error": str(exc),
                       "next_attempt": fields.Datetime.now() + timedelta(minutes=30),
                       "attempts": job.attempts - 1})
            conn.sudo().write({"state": "expired", "last_error": str(exc)})
            self.env["qb.sync.log"]._log(conn, str(exc), level="error", job=job,
                                         direction=job._log_direction())
        except QBOError as exc:
            self._handle_failure(exc, transient=exc.is_transient)
        except (UserError, ValidationError) as exc:
            self._handle_failure(exc, transient=False)
        except Exception as exc:  # pylint: disable=broad-except
            _logger.exception("QuickBooks job %s crashed", job.id)
            self._handle_failure(exc, transient=False, detail=traceback.format_exc())

    def _log_direction(self):
        return "import" if self.direction == "import" else "export"

    def _handle_failure(self, exc, transient, detail=None):
        job = self
        message = str(exc.args[0]) if getattr(exc, "args", None) else str(exc)
        if transient and job.attempts < job.max_attempts:
            delay = min(5 * (2 ** (job.attempts - 1)), 360)  # 5, 10, 20, 40… minutes
            job.write({"state": "pending", "error": message,
                       "next_attempt": fields.Datetime.now() + timedelta(minutes=delay)})
            level = "warning"
        else:
            job.write({"state": "failed", "error": message, "error_detail": detail or False})
            level = "error"
            if job.res_model and job.res_id and job.res_model in self.env:
                mapping = self.env["qb.mapping"].search([
                    ("connection_id", "=", job.connection_id.id), ("res_model", "=", job.res_model),
                    ("res_id", "=", job.res_id), ("qb_entity", "=", job.qb_entity)], limit=1)
                if mapping:
                    mapping.state = "error"
        payload = getattr(exc, "payload", None)
        self.env["qb.sync.log"]._log(job.connection_id, message, level=level, job=job,
                                     direction=job._log_direction(), response_body=payload)

    def _process(self, limit=200, cron=False):
        processed = 0
        for job in self:
            job._run_one()
            processed += 1
            if cron and self.env.context.get("ir_cron_progress_id"):
                remaining = self.env["ir.cron"]._commit_progress(1)
                if remaining <= 0:
                    break
            if processed >= limit:
                break
        return processed

    @api.model
    def _cron_process_queue(self):
        now = fields.Datetime.now()
        # recover jobs stuck in "running" (worker killed)
        self.search([("state", "=", "running"), ("write_date", "<", now - timedelta(minutes=30))]).write(
            {"state": "pending"})
        for conn in self.env["qb.connection"].search([("state", "=", "connected")]):
            jobs = self.search([("connection_id", "=", conn.id), ("state", "=", "pending"),
                                ("next_attempt", "<=", now)], limit=conn.batch_size or 200,
                               order="priority, id")
            jobs._process(limit=conn.batch_size or 200, cron=True)

    # ------------------------------------------------------------ actions
    def action_retry(self):
        self.write({"state": "pending", "next_attempt": fields.Datetime.now(), "attempts": 0,
                    "error": False, "error_detail": False})
        return True

    def action_run_now(self):
        self.filtered(lambda j: j.state in ("pending", "failed", "conflict")).write({"state": "pending"})
        self.sorted(lambda j: (j.priority, j.id))._process(limit=len(self))
        return True

    def action_skip(self):
        self.write({"state": "skipped", "date_done": fields.Datetime.now()})
        return True

    def action_force_odoo(self):
        """Conflict resolution: overwrite QuickBooks with Odoo data."""
        for job in self:
            vals = {"state": "pending", "attempts": 0, "next_attempt": fields.Datetime.now()}
            if job.direction == "import" and job.res_model:
                vals["direction"] = "export"
            job.with_context(qb_force=True).write(vals)
        return self.with_context(qb_force=True).action_run_now()

    def action_force_qb(self):
        """Conflict resolution: overwrite Odoo with QuickBooks data."""
        for job in self:
            vals = {"state": "pending", "attempts": 0, "next_attempt": fields.Datetime.now()}
            if job.direction == "export":
                mapping = self.env["qb.mapping"].search([
                    ("connection_id", "=", job.connection_id.id), ("res_model", "=", job.res_model),
                    ("res_id", "=", job.res_id), ("qb_entity", "=", job.qb_entity)], limit=1)
                vals.update(direction="import", qb_id=mapping.qb_id)
            job.write(vals)
        return self.with_context(qb_force=True).action_run_now()

    def action_open_record(self):
        self.ensure_one()
        if not (self.res_model and self.res_id):
            return False
        return {"type": "ir.actions.act_window", "res_model": self.res_model, "res_id": self.res_id,
                "view_mode": "form", "target": "current"}

    @api.model
    def _default_priority(self, entity, boost=False):
        return ENTITY_SEQUENCE.get(entity, 50) - (100 if boost else 0)
