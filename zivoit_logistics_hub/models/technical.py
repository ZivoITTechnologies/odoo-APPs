# -*- coding: utf-8 -*-
import json
import logging
import traceback
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.modules import module as odoo_module

from ..providers.base import NORMALIZED_STATUSES, OPERATIONS, ProviderError

_logger = logging.getLogger(__name__)


class LogisticsApiLog(models.Model):
    _name = 'zivo.logistics.api.log'
    _description = 'Courier API Call Log'
    _order = 'id desc'
    _rec_name = 'operation'

    account_id = fields.Many2one('zivo.logistics.account', ondelete='set null', index=True)
    company_id = fields.Many2one('res.company')
    provider = fields.Char()
    operation = fields.Char(index=True)
    method = fields.Char()
    url = fields.Char(string='URL')
    request_headers = fields.Text()
    request_body = fields.Text()
    response_status = fields.Integer()
    response_body = fields.Text()
    duration_ms = fields.Integer(string='Duration (ms)')
    success = fields.Boolean(index=True)
    error = fields.Text()
    res_model = fields.Char()
    res_id = fields.Integer()

    @api.model
    def _write_log(self, values):
        """Persist an API log. Outside of tests a separate cursor is used so failed calls stay logged
        even when the business transaction rolls back."""
        allowed = set(self._fields)
        values = {k: v for k, v in values.items() if k in allowed}
        if odoo_module.current_test or not self.env.registry.ready:
            self.sudo().create(values)
            return
        try:
            with self.env.registry.cursor() as cr:
                self.with_env(self.env(cr=cr)).sudo().create(values)
        except Exception:  # account not yet committed, etc.
            self.sudo().create(values)

    def action_open_record(self):
        self.ensure_one()
        if not self.res_model or not self.res_id:
            return False
        return {'type': 'ir.actions.act_window', 'res_model': self.res_model, 'res_id': self.res_id, 'view_mode': 'form'}

    @api.model
    def _cron_purge(self):
        days = int(self.env['ir.config_parameter'].sudo().get_param('zivoit_logistics_hub.log_retention_days', 30))
        self.search([('create_date', '<', fields.Datetime.now() - timedelta(days=days))]).unlink()


class LogisticsQueueJob(models.Model):
    """Lightweight asynchronous job queue (no queue_job dependency) with exponential back-off."""
    _name = 'zivo.logistics.queue.job'
    _description = 'Logistics Job Queue'
    _order = 'priority, next_run_at, id'

    name = fields.Char(required=True)
    operation = fields.Selection([
        ('create_shipment', 'Create Shipment'), ('track', 'Track'), ('cancel', 'Cancel'), ('pickup', 'Schedule Pickup'),
        ('webhook', 'Process Webhook'), ('label', 'Fetch Label'), ('notify', 'Customer Notification'),
    ], required=True, index=True)
    res_model = fields.Char(required=True)
    res_id = fields.Integer(required=True)
    method = fields.Char(required=True, help='Method called on the record, receives **payload')
    payload = fields.Text(default='{}')
    state = fields.Selection([('pending', 'Pending'), ('running', 'Running'), ('done', 'Done'), ('failed', 'Failed (will retry)'),
                              ('dead', 'Dead (needs attention)'), ('cancelled', 'Cancelled')],
                             default='pending', required=True, index=True)
    priority = fields.Integer(default=10)
    attempts = fields.Integer(default=0)
    max_attempts = fields.Integer(default=5)
    next_run_at = fields.Datetime(default=fields.Datetime.now, index=True)
    done_at = fields.Datetime()
    last_error = fields.Text()
    result = fields.Text()
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    account_id = fields.Many2one('zivo.logistics.account', ondelete='set null')

    @api.model
    def enqueue(self, record, method, operation, payload=None, priority=10, account=None, delay_minutes=0):
        record.ensure_one()
        return self.sudo().create({
            'name': '%s: %s' % (dict(self._fields['operation'].selection).get(operation), record.display_name),
            'operation': operation, 'res_model': record._name, 'res_id': record.id, 'method': method,
            'payload': json.dumps(payload or {}, default=str), 'priority': priority,
            'account_id': account.id if account else False,
            'company_id': record.company_id.id if 'company_id' in record._fields and record.company_id else self.env.company.id,
            'next_run_at': fields.Datetime.now() + timedelta(minutes=delay_minutes),
        })

    def run(self):
        for job in self:
            if job.state in ('done', 'cancelled', 'running'):
                continue
            job.write({'state': 'running', 'attempts': job.attempts + 1})
            record = self.env[job.res_model].browse(job.res_id).exists()
            try:
                if not record:
                    raise ProviderError('Record %s,%s no longer exists.' % (job.res_model, job.res_id))
                with self.env.cr.savepoint():
                    result = getattr(record.with_company(job.company_id), job.method)(**json.loads(job.payload or '{}'))
                job.write({'state': 'done', 'done_at': fields.Datetime.now(), 'last_error': False,
                           'result': str(result)[:2000] if result is not None else False})
            except Exception as exc:  # noqa: BLE001 - any failure is recorded on the job
                if isinstance(exc, ProviderError):
                    retryable = exc.retryable
                else:
                    retryable = not isinstance(exc, UserError)
                message = getattr(exc, 'message', None) or str(exc)
                if not isinstance(exc, ProviderError):
                    _logger.warning('ZivoIT job %s failed: %s', job.id, traceback.format_exc())
                dead = not retryable or job.attempts >= job.max_attempts
                job.write({
                    'state': 'dead' if dead else 'failed', 'last_error': message,
                    'next_run_at': fields.Datetime.now() + timedelta(minutes=2 ** job.attempts),
                })
        return True

    @api.model
    def _cron_process(self, limit=100):
        jobs = self.search([('state', 'in', ('pending', 'failed')), ('next_run_at', '<=', fields.Datetime.now())],
                           limit=limit)
        for job in jobs:
            job.run()
            if not odoo_module.current_test:
                self.env.cr.commit()  # isolate jobs: one bad job never blocks the others
        # recover jobs stuck in "running" after a server crash
        self.search([('state', '=', 'running'), ('write_date', '<', fields.Datetime.now() - timedelta(hours=1))]).write(
            {'state': 'failed'})
        self.search([('state', '=', 'done'), ('done_at', '<', fields.Datetime.now() - timedelta(days=15))]).unlink()

    def action_retry(self):
        self.write({'state': 'pending', 'next_run_at': fields.Datetime.now(), 'max_attempts': max(self.mapped('max_attempts') + [5])})
        return True

    def action_run_now(self):
        self.filtered(lambda j: j.state in ('pending', 'failed', 'dead')).write({'state': 'pending'})
        self.run()
        return True

    def action_cancel(self):
        self.write({'state': 'cancelled'})

    def action_open_record(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': self.res_model, 'res_id': self.res_id, 'view_mode': 'form'}


class LogisticsStatusMap(models.Model):
    _name = 'zivo.logistics.status.map'
    _description = 'Carrier Status Mapping'
    _order = 'provider, raw_status'
    _rec_name = 'raw_status'

    provider = fields.Char(required=True, default='*', index=True,
                           help='Provider code (shiprocket, delhivery, ...) or * for all carriers.')
    raw_status = fields.Char(required=True, index=True)
    normalized_status = fields.Selection(NORMALIZED_STATUSES, required=True)
    source = fields.Selection([('seed', 'Standard'), ('ai', 'AI suggested'), ('manual', 'Manual')], default='manual')
    confidence = fields.Float(default=1.0)
    verified = fields.Boolean(default=True)
    hit_count = fields.Integer(readonly=True)

    _provider_raw_uniq = models.Constraint('UNIQUE(provider, raw_status)', 'This status is already mapped.')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('raw_status'):
                vals['raw_status'] = vals['raw_status'].strip().lower()
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('raw_status'):
            vals['raw_status'] = vals['raw_status'].strip().lower()
        if 'normalized_status' in vals and 'verified' not in vals:
            vals.update(verified=True, source='manual')
        return super().write(vals)

    @api.model
    def _resolve(self, provider, raw_status, description=''):
        """Map → (normalized, mapping record). Unknown statuses are classified by AI and stored
        as *unverified* suggestions the operations team can approve."""
        key = (raw_status or '').strip().lower()
        if not key:
            return None, self.browse()
        mapping = self.sudo().search([('provider', 'in', (provider, '*')), ('raw_status', '=', key)],
                                     order='provider desc', limit=1)
        if not mapping:
            normalized, confidence, source = self.env['zivo.logistics.ai']._classify_status(provider, raw_status, description)
            if not normalized:
                return None, self.browse()
            try:
                with self.env.cr.savepoint():
                    mapping = self.sudo().create({'provider': provider, 'raw_status': key, 'normalized_status': normalized,
                                                  'source': source, 'confidence': confidence, 'verified': False})
            except Exception:
                mapping = self.sudo().search([('provider', '=', provider), ('raw_status', '=', key)], limit=1)
        mapping.sudo().hit_count += 1
        return mapping.normalized_status, mapping

    def action_verify(self):
        self.write({'verified': True})

    @api.model
    def _ops_selection(self):
        return [(o, o) for o in OPERATIONS]
