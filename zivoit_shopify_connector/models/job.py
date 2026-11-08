import json
import logging
from datetime import timedelta

from psycopg2 import OperationalError

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..lib import ai as ai_lib
from ..lib.client import ShopifyError

_logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 6
BACKOFF_MINUTES = [1, 5, 15, 60, 180, 360]

JOB_TYPES = [
    ("import_products", "Import Products (page)"),
    ("import_product", "Import Product"),
    ("export_product", "Export Product"),
    ("export_prices", "Export Prices"),
    ("export_stock", "Export Stock"),
    ("import_stock", "Import Stock"),
    ("import_orders", "Import Orders (page)"),
    ("import_order", "Import Order"),
    ("export_fulfillment", "Export Fulfilment"),
    ("export_refund", "Export Refund"),
    ("import_collections", "Import Collections"),
    ("import_translations", "Import Translations"),
    ("export_translations", "Export Translations"),
    ("import_customers", "Import Customers (page)"),
    ("import_customer", "Import Customer"),
    ("export_customer", "Export Customer"),
    ("import_checkouts", "Import Abandoned Checkouts"),
    ("import_payouts", "Import Payouts"),
    ("import_markets", "Import Markets"),
    ("export_market_prices", "Export Market Prices"),
    ("import_segments", "Import Customer Segments"),
    ("product_deleted", "Product Deleted on Shopify"),
    ("app_uninstalled", "App Uninstalled"),
]


class ZivoShopifyJob(models.Model):
    _name = "zivo.shopify.job"
    _description = "Shopify Sync Job"
    _inherit = ["mail.thread"]
    _order = "id desc"

    name = fields.Char(required=True)
    instance_id = fields.Many2one("zivo.shopify.instance", string="Store", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    job_type = fields.Selection(JOB_TYPES, required=True, index=True)
    state = fields.Selection([
        ("pending", "Pending"),
        ("running", "Running"),
        ("done", "Done"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
    ], default="pending", required=True, index=True, tracking=True)
    source = fields.Selection([
        ("webhook", "Webhook"), ("cron", "Scheduler"), ("manual", "Manual"), ("odoo", "Odoo Event"),
    ], default="manual", required=True)
    priority = fields.Integer(default=10, help="Lower values run first.")
    payload = fields.Json(default=dict)
    dedupe_key = fields.Char(index=True, help="Only one open job can exist for a given key.")
    external_ref = fields.Char(string="Shopify Reference", index=True)
    attempts = fields.Integer(default=0)
    next_attempt_at = fields.Datetime(index=True, default=fields.Datetime.now)
    started_at = fields.Datetime()
    finished_at = fields.Datetime()
    duration = fields.Float(string="Duration (s)", digits=(10, 2))
    result = fields.Text()
    error_message = fields.Text()
    error_code = fields.Char()
    transient_error = fields.Boolean()
    ai_root_cause = fields.Char(string="Root Cause")
    ai_fix = fields.Text(string="Recommended Fix")
    ai_source = fields.Selection([("local", "Local Rules"), ("llm", "LLM")], string="Diagnosis By")

    _state_next_idx = models.Index("(state, next_attempt_at, priority) WHERE state = 'pending'")


    @api.model
    def _enqueue(self, instance, job_type, name, payload=None, *, dedupe_key=None, source="manual",
                 priority=10, external_ref=None, delay=0):
        """Create a job, or return the job already pending for ``dedupe_key``."""
        instance.ensure_one()
        if dedupe_key:
            existing = self.sudo().search([
                ("instance_id", "=", instance.id),
                ("dedupe_key", "=", dedupe_key),
                ("state", "=", "pending"),
            ], limit=1)
            if existing:
                if payload:
                    existing.payload = payload
                return existing
        job = self.sudo().create({
            "name": name[:200],
            "instance_id": instance.id,
            "job_type": job_type,
            "payload": payload or {},
            "dedupe_key": dedupe_key,
            "source": source,
            "priority": priority,
            "external_ref": external_ref,
            "next_attempt_at": fields.Datetime.now() + timedelta(seconds=delay),
        })
        cron = self.sudo().env.ref("zivoit_shopify_connector.cron_process_jobs", raise_if_not_found=False)
        if cron:
            cron._trigger(fields.Datetime.now() + timedelta(seconds=delay))
        return job

    @api.model
    def _cron_process(self, limit=400):
        """Scheduled entry point: run due jobs one transaction at a time."""
        self._release_stale()
        due = self.search([
            ("state", "=", "pending"),
            ("next_attempt_at", "<=", fields.Datetime.now()),
        ], order="priority, id", limit=limit)
        cron = self.env["ir.cron"]
        testing = self.env.context.get("zivo_shopify_inline")
        remaining = len(due)
        if not testing:
            cron._commit_progress(remaining=remaining)
        for job in due:
            # Two cron workers must never run the same job.
            locked = job.try_lock_for_update()
            if not locked or job.state != "pending":
                continue
            job._run()
            if testing:
                continue
            if not cron._commit_progress(1):
                break
        return True

    @api.model
    def _release_stale(self):
        """Put back in the queue jobs whose worker died mid-run."""
        limit = fields.Datetime.now() - timedelta(minutes=30)
        self.search([("state", "=", "running"), ("started_at", "<", limit)]).write({"state": "pending"})

    def _run(self):
        self.ensure_one()
        started = fields.Datetime.now()
        self.write({"state": "running", "started_at": started, "attempts": self.attempts + 1})
        try:
            with self.env.cr.savepoint():
                instance = self.instance_id.with_company(self.instance_id.company_id)
                handler = getattr(instance, f"_job_{self.job_type}")
                result = handler(self)
            self.write({
                "state": "done",
                "finished_at": fields.Datetime.now(),
                "duration": (fields.Datetime.now() - started).total_seconds(),
                "result": str(result)[:2000] if result else False,
                "error_message": False,
                "error_code": False,
                "transient_error": False,
                "ai_root_cause": False,
                "ai_fix": False,
                "ai_source": False,
            })
        except OperationalError:
            # Lock or serialization conflict: let the framework roll back and retry the run.
            raise
        except Exception as error:  # noqa: BLE001
            # The savepoint undid the work. A poison job must never block the queue behind it.
            self._handle_failure(error)

    def _handle_failure(self, error):
        self.ensure_one()
        message = getattr(error, "message", None) or (error.args[0] if error.args else repr(error))
        if not isinstance(error, (ShopifyError, UserError, ValidationError)):
            _logger.exception("Shopify job %s crashed", self.id)
            message = f"{type(error).__name__}: {message}"
        cause, fix, transient_hint = ai_lib.diagnose_error(str(message))
        transient = bool(getattr(error, "transient", False) or transient_hint)
        vals = {
            "error_message": str(message)[:4000],
            "error_code": getattr(error, "code", False) or False,
            "transient_error": transient,
            "ai_root_cause": cause,
            "ai_fix": fix,
            "ai_source": "local",
            "finished_at": fields.Datetime.now(),
        }
        if transient and self.attempts < MAX_ATTEMPTS:
            minutes = BACKOFF_MINUTES[min(self.attempts - 1, len(BACKOFF_MINUTES) - 1)]
            vals.update(state="pending", next_attempt_at=fields.Datetime.now() + timedelta(minutes=minutes))
            self.write(vals)
            cron = self.sudo().env.ref("zivoit_shopify_connector.cron_process_jobs", raise_if_not_found=False)
            if cron:
                cron._trigger(vals["next_attempt_at"])
            return
        vals["state"] = "failed"
        self.write(vals)

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def action_retry(self):
        self.check_access("read")
        jobs = self.sudo().filtered(lambda job: job.state in ("failed", "cancelled"))
        jobs.write({"state": "pending", "attempts": 0, "next_attempt_at": fields.Datetime.now()})
        cron = self.sudo().env.ref("zivoit_shopify_connector.cron_process_jobs", raise_if_not_found=False)
        if jobs and cron:
            cron._trigger()
        return True

    def action_cancel(self):
        self.check_access("read")
        self.sudo().filtered(lambda job: job.state in ("pending", "failed")).write({"state": "cancelled"})
        return True

    def action_run_now(self):
        self.check_access("read")
        for job in self.filtered(lambda j: j.state == "pending"):
            job.sudo()._run()
        return True

    def action_ai_diagnose(self):
        """Ask the configured LLM for a deeper diagnosis of the failure."""
        self.check_access("read")
        for job in self.sudo().filtered("error_message"):
            instance = job.instance_id
            if not instance._ai_available():
                raise UserError(self.env._("Configure an AI provider on the store first."))
            answer = instance._ai_complete(
                "job_doctor",
                "You are a senior Odoo and Shopify integration engineer. Answer in JSON with keys "
                "root_cause (one sentence) and fix (numbered steps a functional user can follow).",
                json.dumps({
                    "job_type": job.job_type,
                    "error": job.error_message,
                    "code": job.error_code,
                    "attempts": job.attempts,
                }),
            )
            parsed = ai_lib.extract_json(answer) or {}
            job.write({
                "ai_root_cause": str(parsed.get("root_cause") or job.ai_root_cause)[:250],
                "ai_fix": parsed.get("fix") if isinstance(parsed.get("fix"), str)
                    else "\n".join(str(step) for step in parsed.get("fix") or []) or answer,
                "ai_source": "llm",
            })
        return True

    @api.model
    def _cron_cleanup(self):
        limit = fields.Datetime.now() - timedelta(days=30)
        self.search([("state", "in", ("done", "cancelled")), ("write_date", "<", limit)], limit=5000).unlink()
        receipts = self.env["zivo.shopify.webhook.receipt"]
        receipts.search([("create_date", "<", fields.Datetime.now() - timedelta(days=7))], limit=20000).unlink()
