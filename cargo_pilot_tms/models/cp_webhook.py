# -*- coding: utf-8 -*-
"""Outbound webhooks: other systems (Zoho Creator, EasyEcom, a data warehouse) get events as they happen.

Each delivery is signed: header ``X-CP-Signature: sha256=<hex HMAC of the body with the webhook secret>``.
Failed deliveries are retried with back-off (1, 5, 30, 120 minutes, then every 6 hours up to 8 attempts).
"""
import hashlib
import hmac
import json
import logging
import secrets
from datetime import timedelta

import requests

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)

EVENTS = [
    ('shipment.created', 'Shipment created'),
    ('shipment.status_changed', 'Shipment status changed'),
    ('pod.verified', 'Proof of delivery verified'),
    ('freight_bill.approved', 'Freight bill approved'),
    ('order.freight_allocated', 'Freight cost per order ready'),
]
BACKOFF_MINUTES = [1, 5, 30, 120, 360, 360, 360, 360]


class CpWebhook(models.Model):
    _name = 'cp.webhook'
    _description = 'Outbound Webhook'
    _order = 'name'

    name = fields.Char(required=True)
    url = fields.Char(required=True, string='Target URL')
    active = fields.Boolean(default=True)
    secret = fields.Char(default=lambda self: secrets.token_hex(24), copy=False,
                         groups='cargo_pilot_tms.group_cp_manager')
    event_shipment_created = fields.Boolean(string='Shipment created', default=True)
    event_shipment_status = fields.Boolean(string='Status changed', default=True)
    event_pod_verified = fields.Boolean(string='POD verified', default=True)
    event_bill_approved = fields.Boolean(string='Freight bill approved', default=True)
    event_order_freight = fields.Boolean(string='Freight per order', default=True)
    delivery_ids = fields.One2many('cp.webhook.delivery', 'webhook_id')
    failed_count = fields.Integer(compute='_compute_counts')
    sent_count = fields.Integer(compute='_compute_counts')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    EVENT_FIELDS = {
        'shipment.created': 'event_shipment_created',
        'shipment.status_changed': 'event_shipment_status',
        'pod.verified': 'event_pod_verified',
        'freight_bill.approved': 'event_bill_approved',
        'order.freight_allocated': 'event_order_freight',
    }

    def _compute_counts(self):
        for rec in self:
            rec.failed_count = len(rec.delivery_ids.filtered(lambda d: d.state == 'failed'))
            rec.sent_count = len(rec.delivery_ids.filtered(lambda d: d.state == 'sent'))

    @api.model
    def _cp_payload(self, event, record, extra=None):
        if record._name == 'cp.trip':
            data = record.cp_api_dict(full=event in ('order.freight_allocated', 'pod.verified'))
        elif hasattr(record, 'cp_api_dict'):
            data = record.cp_api_dict()
        else:
            data = {'id': record.id, 'name': record.display_name}
        if extra:
            data.update(extra)
        return {'event': event, 'sent_at': fields.Datetime.now(), 'data': data}

    @api.model
    def _cp_emit(self, event, record, extra=None):
        """Queue the event for every active webhook that subscribes to it."""
        if self.env.context.get('cp_no_webhook'):
            return self.browse()
        field = self.EVENT_FIELDS.get(event)
        company = record.company_id if 'company_id' in record._fields and record.company_id else self.env.company
        hooks = self.sudo().search([(field, '=', True), '|', ('company_id', '=', company.id),
                                    ('company_id', '=', False)]) if field else self.browse()
        if not hooks:
            return hooks
        body = json.dumps(self._cp_payload(event, record, extra), default=str)
        self.env['cp.webhook.delivery'].sudo().create([{
            'webhook_id': hook.id, 'event': event, 'payload': body, 'res_model': record._name, 'res_id': record.id,
        } for hook in hooks])
        return hooks

    def action_send_test(self):
        for rec in self:
            delivery = self.env['cp.webhook.delivery'].sudo().create({
                'webhook_id': rec.id, 'event': 'shipment.status_changed',
                'payload': json.dumps({'event': 'test', 'sent_at': fields.Datetime.now(),
                                       'data': {'message': 'Cargo Pilot test event'}}, default=str)})
            delivery._cp_send()
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'type': 'info', 'title': _('Test sent'),
                           'message': _('See the delivery log for the answer.')}}

    def action_view_deliveries(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Deliveries'), 'res_model': 'cp.webhook.delivery',
                'view_mode': 'list,form', 'domain': [('webhook_id', '=', self.id)]}


class CpWebhookDelivery(models.Model):
    _name = 'cp.webhook.delivery'
    _description = 'Webhook Delivery'
    _order = 'id desc'

    webhook_id = fields.Many2one('cp.webhook', required=True, ondelete='cascade', index=True)
    event = fields.Selection(EVENTS, required=True)
    payload = fields.Text(required=True)
    res_model = fields.Char()
    res_id = fields.Integer()
    state = fields.Selection([('pending', 'Pending'), ('sent', 'Sent'), ('failed', 'Failed')], default='pending',
                             index=True)
    attempts = fields.Integer()
    next_attempt = fields.Datetime(default=fields.Datetime.now, index=True)
    response_code = fields.Integer()
    response_body = fields.Text()

    def _cp_send(self):
        for rec in self:
            hook = rec.webhook_id.sudo()
            body = rec.payload.encode()
            signature = hmac.new((hook.secret or '').encode(), body, hashlib.sha256).hexdigest()
            headers = {'Content-Type': 'application/json', 'X-CP-Event': rec.event,
                       'X-CP-Signature': 'sha256=%s' % signature, 'X-CP-Delivery': str(rec.id)}
            try:
                response = requests.post(hook.url, data=body, headers=headers, timeout=15)
                ok = 200 <= response.status_code < 300
                vals = {'response_code': response.status_code, 'response_body': (response.text or '')[:2000]}
            except requests.exceptions.RequestException as err:
                ok, vals = False, {'response_code': 0, 'response_body': str(err)[:2000]}
            attempts = rec.attempts + 1
            vals['attempts'] = attempts
            if ok:
                vals['state'] = 'sent'
            elif attempts >= len(BACKOFF_MINUTES):
                vals['state'] = 'failed'
            else:
                vals['next_attempt'] = fields.Datetime.now() + timedelta(minutes=BACKOFF_MINUTES[attempts - 1])
            rec.write(vals)

    def action_retry(self):
        self.write({'state': 'pending', 'next_attempt': fields.Datetime.now()})
        self._cp_send()

    @api.model
    def _cron_send(self, limit=200):
        due = self.search([('state', '=', 'pending'), ('next_attempt', '<=', fields.Datetime.now())],
                          limit=limit, order='next_attempt')
        due._cp_send()
        return True

    @api.autovacuum
    def _gc_old(self):
        self.search([('state', '=', 'sent'), ('create_date', '<', fields.Datetime.now() - timedelta(days=30))]).unlink()
