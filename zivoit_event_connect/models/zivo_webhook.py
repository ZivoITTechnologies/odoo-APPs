# -*- coding: utf-8 -*-
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import uuid
from datetime import timedelta

import requests

from odoo import api, fields, models
from odoo.modules import module as odoo_module
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

RETRY_MINUTES = [1, 5, 15, 60, 240, 720]


class ZivoWebhookTopic(models.Model):
    _name = 'zivo.webhook.topic'
    _description = 'Webhook Topic'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, help='Technical topic, e.g. event.updated')
    sequence = fields.Integer(default=10)
    description = fields.Char(translate=True)

    _code_uniq = models.Constraint('unique(code)', 'Topic code must be unique.')


class ZivoWebhook(models.Model):
    _name = 'zivo.webhook'
    _inherit = ['mail.thread']
    _description = 'Outgoing Webhook'
    _order = 'name'

    name = fields.Char(required=True, help='e.g. "WordPress cache refresh"')
    active = fields.Boolean(default=True, tracking=True)
    url = fields.Char(string='Endpoint URL', required=True, tracking=True,
                      help='HTTPS endpoint that receives a JSON POST, e.g. '
                           'https://www.example.com/wp-json/zivo/v1/webhook')
    secret = fields.Char(string='Signing Secret', required=True, copy=False,
                         default=lambda self: secrets.token_hex(24),
                         groups='zivoit_event_connect.group_event_connect_manager',
                         help='Used to sign every delivery with HMAC-SHA256 (header X-Zivo-Signature).')
    topic_ids = fields.Many2many('zivo.webhook.topic', string='Topics', required=True,
                                 default=lambda self: self.env['zivo.webhook.topic'].search(
                                     [('code', 'like', 'event.%')]))
    include_restricted = fields.Boolean(string='Include Restricted Events',
                                        help='Also push IP-restricted / invitation-only events.')
    custom_headers = fields.Text(help='Optional extra HTTP headers as JSON, e.g. {"X-Site": "main"}')
    timeout = fields.Integer(string='Timeout (s)', default=10)
    max_attempts = fields.Integer(default=6)
    delivery_ids = fields.One2many('zivo.webhook.delivery', 'webhook_id', string='Deliveries')
    delivery_count = fields.Integer(compute='_compute_stats')
    success_count = fields.Integer(compute='_compute_stats')
    failure_count = fields.Integer(compute='_compute_stats')
    pending_count = fields.Integer(compute='_compute_stats')
    last_status = fields.Char(readonly=True)
    last_delivery = fields.Datetime(readonly=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    def _compute_stats(self):
        data = {}
        for webhook, state, count in self.env['zivo.webhook.delivery']._read_group(
                [('webhook_id', 'in', self.ids)], ['webhook_id', 'state'], ['__count']):
            data.setdefault(webhook.id, {})[state] = count
        for wh in self:
            d = data.get(wh.id, {})
            wh.success_count = d.get('done', 0)
            wh.failure_count = d.get('failed', 0)
            wh.pending_count = d.get('pending', 0)
            wh.delivery_count = sum(d.values())

    @api.constrains('url')
    def _check_url(self):
        for wh in self:
            if not (wh.url or '').startswith(('http://', 'https://')):
                raise UserError(self.env._('Webhook URL must start with http:// or https://'))

    def _headers(self):
        self.ensure_one()
        try:
            extra = json.loads(self.custom_headers) if self.custom_headers else {}
        except ValueError:
            extra = {}
        return {str(k): str(v) for k, v in extra.items()} if isinstance(extra, dict) else {}

    @api.model
    def _zivo_sign(self, secret, timestamp, body):
        message = ('%s.%s' % (timestamp, body)).encode()
        return 'sha256=' + hmac.new((secret or '').encode(), message, hashlib.sha256).hexdigest()

    # ------------------------------------------------------------------
    # Queueing
    # ------------------------------------------------------------------
    @api.model
    def _zivo_enqueue(self, topic, records, force=False):
        if not records:
            return self.env['zivo.webhook.delivery']
        webhooks = self._zivo_subscribers(topic)
        if not webhooks:
            return self.env['zivo.webhook.delivery']
        if topic in ('event.unpublished',):
            payloads = [{'id': r.id, 'slug': r.zivo_slug, 'name': r.name} for r in records]
        else:
            payloads = [r._zivo_api_payload(detail=True) for r in records]
        return self._zivo_create_deliveries(webhooks, topic, payloads)

    @api.model
    def _zivo_enqueue_raw(self, topic, payloads):
        webhooks = self._zivo_subscribers(topic)
        if not webhooks or not payloads:
            return self.env['zivo.webhook.delivery']
        return self._zivo_create_deliveries(webhooks, topic, payloads)

    @api.model
    def _zivo_subscribers(self, topic):
        return self.sudo().search([('active', '=', True), ('topic_ids.code', '=', topic)])

    @api.model
    def _zivo_create_deliveries(self, webhooks, topic, payloads):
        vals_list = []
        now = fields.Datetime.now()
        for wh in webhooks:
            for payload in payloads:
                if (not wh.include_restricted and isinstance(payload, dict)
                        and payload.get('access_mode') not in (None, 'open')):
                    continue
                vals_list.append({
                    'webhook_id': wh.id,
                    'topic': topic,
                    'res_id': payload.get('id') if isinstance(payload, dict) else False,
                    'payload': json.dumps(payload, default=str),
                    'next_attempt': now,
                })
        deliveries = self.env['zivo.webhook.delivery'].sudo().create(vals_list)
        if deliveries:
            cron = self.env.ref('zivoit_event_connect.ir_cron_webhook_deliveries', raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger()
        return deliveries

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_send_ping(self):
        self.ensure_one()
        delivery = self.env['zivo.webhook.delivery'].sudo().create({
            'webhook_id': self.id,
            'topic': 'ping',
            'payload': json.dumps({'message': 'Hello from ZivoIT Event Connect', 'webhook': self.name}),
            'next_attempt': fields.Datetime.now(),
        })
        delivery._send()
        ok = delivery.state == 'done'
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'type': 'success' if ok else 'danger',
                'title': self.env._('Ping delivered') if ok else self.env._('Ping failed'),
                'message': delivery.response_code and ('HTTP %s' % delivery.response_code) or (delivery.error or ''),
                'sticky': not ok,
            },
        }

    def action_view_deliveries(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Deliveries'),
            'res_model': 'zivo.webhook.delivery',
            'view_mode': 'list,form',
            'domain': [('webhook_id', '=', self.id)],
        }

    def action_regenerate_secret(self):
        for wh in self:
            wh.sudo().secret = secrets.token_hex(24)


class ZivoWebhookDelivery(models.Model):
    _name = 'zivo.webhook.delivery'
    _description = 'Webhook Delivery'
    _order = 'id desc'
    _rec_name = 'uuid'

    uuid = fields.Char(string='Delivery ID', default=lambda self: str(uuid.uuid4()), readonly=True, copy=False)
    webhook_id = fields.Many2one('zivo.webhook', required=True, ondelete='cascade', index=True)
    topic = fields.Char(required=True, index=True)
    res_id = fields.Integer(string='Record ID')
    payload = fields.Text(required=True)
    state = fields.Selection([
        ('pending', 'Pending'),
        ('done', 'Delivered'),
        ('failed', 'Failed'),
        ('cancel', 'Cancelled'),
    ], default='pending', required=True, index=True)
    attempts = fields.Integer(readonly=True)
    next_attempt = fields.Datetime(index=True)
    sent_date = fields.Datetime(readonly=True)
    response_code = fields.Integer(readonly=True)
    response_body = fields.Text(readonly=True)
    error = fields.Text(readonly=True)
    duration_ms = fields.Integer(string='Duration (ms)', readonly=True)

    def _body(self):
        self.ensure_one()
        return json.dumps({
            'id': self.uuid,
            'topic': self.topic,
            'created_at': fields.Datetime.to_string(self.create_date or fields.Datetime.now()),
            'source': self.get_base_url(),
            'data': json.loads(self.payload),
        }, default=str, separators=(',', ':'))

    def _send(self):
        for delivery in self:
            webhook = delivery.webhook_id.sudo()
            body = delivery._body()
            timestamp = str(int(time.time()))
            headers = {
                'Content-Type': 'application/json',
                'User-Agent': 'ZivoIT-Event-Connect/19.0',
                'X-Zivo-Topic': delivery.topic,
                'X-Zivo-Delivery': delivery.uuid,
                'X-Zivo-Timestamp': timestamp,
                'X-Zivo-Signature': self.env['zivo.webhook']._zivo_sign(webhook.secret, timestamp, body),
            }
            headers.update(webhook._headers())
            start = time.time()
            vals = {'attempts': delivery.attempts + 1, 'sent_date': fields.Datetime.now()}
            try:
                response = requests.post(webhook.url, data=body.encode(), headers=headers,
                                         timeout=webhook.timeout or 10)
                vals.update(response_code=response.status_code, response_body=(response.text or '')[:2000],
                            error=False)
                success = 200 <= response.status_code < 300
            except requests.RequestException as exc:
                vals.update(response_code=0, error=str(exc)[:2000])
                success = False
            vals['duration_ms'] = int((time.time() - start) * 1000)
            if success:
                vals.update(state='done', next_attempt=False)
            elif vals['attempts'] >= (webhook.max_attempts or 1) or delivery.topic == 'ping':
                vals.update(state='failed', next_attempt=False)
            else:
                wait = RETRY_MINUTES[min(vals['attempts'] - 1, len(RETRY_MINUTES) - 1)]
                vals.update(state='pending', next_attempt=fields.Datetime.now() + timedelta(minutes=wait))
            delivery.write(vals)
            webhook.write({'last_status': ('HTTP %s' % vals.get('response_code')) if vals.get('response_code')
                           else (vals.get('error') or '')[:100],
                           'last_delivery': fields.Datetime.now()})

    @api.model
    def _cron_process(self, limit=200):
        deliveries = self.search([('state', '=', 'pending'), ('next_attempt', '<=', fields.Datetime.now())],
                                 limit=limit, order='next_attempt, id')
        testing = bool(odoo_module.current_test) or getattr(threading.current_thread(), 'testing', False)
        for delivery in deliveries:
            delivery._send()
            if not testing:
                self.env.cr.commit()
        return len(deliveries)

    @api.autovacuum
    def _gc_old_deliveries(self):
        limit = fields.Datetime.now() - timedelta(days=60)
        self.search([('state', 'in', ('done', 'cancel')), ('create_date', '<', limit)]).unlink()

    def action_retry(self):
        self.write({'state': 'pending', 'next_attempt': fields.Datetime.now()})
        self._send()

    def action_cancel(self):
        self.filtered(lambda d: d.state == 'pending').write({'state': 'cancel', 'next_attempt': False})
