# -*- coding: utf-8 -*-
import ipaddress
import json
import logging
from collections import defaultdict
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)
P = 'zivoit_event_connect.%s'


class ZivoAiAlert(models.Model):
    _name = 'zivo.ai.alert'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _description = 'Audit Anomaly Alert'
    _order = 'id desc'

    name = fields.Char(required=True)
    rule = fields.Selection([
        ('mass_delete', 'Mass deletion'),
        ('access_loosened', 'Private event made open'),
        ('ip_widened', 'IP range widened'),
        ('bulk_download', 'Bulk document downloads'),
    ], required=True, index=True)
    severity = fields.Selection([('low', 'Low'), ('medium', 'Medium'), ('high', 'High')], default='medium',
                                required=True, tracking=True)
    state = fields.Selection([('new', 'New'), ('ack', 'Acknowledged'), ('dismissed', 'False Positive')],
                             default='new', required=True, tracking=True)
    user_id = fields.Many2one('res.users', string='User Involved', index=True)
    date = fields.Datetime(default=fields.Datetime.now, index=True)
    details = fields.Text()
    res_model = fields.Char()
    res_id = fields.Integer()
    record_name = fields.Char(string='Record')
    log_ids = fields.Many2many('zivo.audit.log', string='Audit Entries')
    log_count = fields.Integer(compute='_compute_log_count')
    ai_assessment = fields.Text(string='AI Risk Assessment', readonly=True)
    signature = fields.Char(index=True, help='De-duplication key')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    @api.depends('log_ids')
    def _compute_log_count(self):
        for alert in self:
            alert.log_count = len(alert.log_ids)

    def action_ack(self):
        self.write({'state': 'ack'})
        self.activity_feedback(['mail.mail_activity_data_todo'])

    def action_dismiss(self):
        self.write({'state': 'dismissed'})
        self.activity_unlink(['mail.mail_activity_data_todo'])

    def action_view_logs(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'zivo.audit.log', 'view_mode': 'list,form',
                'domain': [('id', 'in', self.log_ids.ids)], 'name': self.name}

    def action_zivo_ai_assess(self):
        service = self.env['zivo.ai.service']
        service._ensure_feature('anomaly')
        for alert in self:
            entries = [{'date': str(l.date), 'user': l.user_id.name, 'action': l.action, 'record': l.record_name,
                        'type': l.model_description, 'changes': l.changes, 'ip': l.ip_address}
                       for l in alert.log_ids[:40]]
            prompt = (
                'A security rule fired in an Odoo events system.\nRule: %s\nSeverity: %s\nDetails: %s\n'
                'Audit entries (JSON):\n%s\n\nIn 3-5 short sentences: is this likely legitimate or risky, why, '
                'and what should the administrator check or do next? Plain text.'
            ) % (dict(self._fields['rule'].selection)[alert.rule], alert.severity, alert.details,
                 json.dumps(entries, ensure_ascii=False, default=str))
            alert.ai_assessment = service._zivo_ai_complete(
                'anomaly', 'You are a pragmatic information-security analyst.', prompt, record=alert)
        return True

    # ------------------------------------------------------------------
    # Detection (deterministic rules, AI only explains)
    # ------------------------------------------------------------------
    @api.model
    def _zivo_param_int(self, key, default):
        try:
            return int(self.env['ir.config_parameter'].sudo().get_param(P % key, default))
        except (TypeError, ValueError):
            return default

    @api.model
    def _zivo_create_alert(self, vals, logs):
        signature = vals['signature']
        if self.search_count([('signature', '=', signature)]):
            return self.browse()
        vals['log_ids'] = [(6, 0, logs.ids)]
        alert = self.create(vals)
        group = self.env.ref('zivoit_event_connect.group_event_connect_manager')
        for user in group.all_user_ids.filtered(lambda u: u.active and not u.share)[:5]:
            alert.activity_schedule('mail.mail_activity_data_todo', user_id=user.id,
                                    summary=self.env._('Review security alert'), note=alert.name)
        return alert

    @api.model
    def _zivo_detect(self, since=None, until=None):
        Log = self.env['zivo.audit.log'].sudo()
        until = until or fields.Datetime.now()
        since = since or until - timedelta(hours=self._zivo_param_int('ai_anomaly_window_hours', 24))
        logs = Log.search([('date', '>=', since), ('date', '<=', until)], order='date')
        alerts = self.browse()
        # 1. mass deletion: N+ deletions by one user within one hour
        threshold = self._zivo_param_int('ai_anomaly_delete_threshold', 5)
        by_user = defaultdict(list)
        for log in logs.filtered(lambda l: l.action == 'unlink'):
            by_user[log.user_id].append(log)
        for user, entries in by_user.items():
            for i in range(len(entries)):
                window = [e for e in entries[i:] if e.date - entries[i].date <= timedelta(hours=1)]
                if len(window) >= threshold:
                    alerts |= self._zivo_create_alert({
                        'name': '%s deleted %s records within an hour' % (user.name or 'Unknown', len(window)),
                        'rule': 'mass_delete', 'severity': 'high' if len(window) >= threshold * 2 else 'medium',
                        'user_id': user.id, 'date': window[-1].date,
                        'details': ', '.join('%s "%s"' % (e.model_description, e.record_name) for e in window[:20]),
                        'signature': 'mass_delete:%s:%s' % (user.id, window[0].id),
                    }, Log.browse([e.id for e in window]))
                    break
        # 2. access loosened / 3. IP widened
        for log in logs.filtered(lambda l: l.action == 'write' and l.model == 'event.event' and l.changes_json):
            try:
                changes = json.loads(log.changes_json)
            except ValueError:
                continue
            mode = changes.get('zivo_access_mode')
            if mode and mode[0] in ('IP Restricted', 'Invitation Only') and mode[1] in ('Open', False, None):
                alerts |= self._zivo_create_alert({
                    'name': 'Private event "%s" was made open' % log.record_name,
                    'rule': 'access_loosened', 'severity': 'high', 'user_id': log.user_id.id, 'date': log.date,
                    'details': 'Access rule changed from %s to %s.' % (mode[0], mode[1] or 'Open'),
                    'res_model': 'event.event', 'res_id': log.res_id, 'record_name': log.record_name,
                    'signature': 'access_loosened:%s' % log.id,
                }, log)
            ranges = changes.get('zivo_ip_ranges')
            if ranges and ranges[1]:
                wide = []
                for raw in str(ranges[1]).replace(',', '\n').splitlines():
                    raw = raw.strip()
                    if not raw or raw.startswith('#'):
                        continue
                    try:
                        net = ipaddress.ip_network(raw, strict=False)
                    except ValueError:
                        continue
                    if (net.version == 4 and net.prefixlen < 8) or (net.version == 6 and net.prefixlen < 32):
                        wide.append(raw)
                if wide:
                    alerts |= self._zivo_create_alert({
                        'name': 'Very wide IP range on "%s"' % log.record_name,
                        'rule': 'ip_widened', 'severity': 'high' if any(w.endswith('/0') for w in wide) else 'medium',
                        'user_id': log.user_id.id, 'date': log.date,
                        'details': 'New ranges allow a very large part of the internet: %s' % ', '.join(wide),
                        'res_model': 'event.event', 'res_id': log.res_id, 'record_name': log.record_name,
                        'signature': 'ip_widened:%s' % log.id,
                    }, log)
        # 4. bulk downloads
        dl_threshold = self._zivo_param_int('ai_anomaly_download_threshold', 20)
        by_user = defaultdict(list)
        for log in logs.filtered(lambda l: l.action == 'download'):
            by_user[log.user_id].append(log)
        for user, entries in by_user.items():
            for i in range(len(entries)):
                window = [e for e in entries[i:] if e.date - entries[i].date <= timedelta(hours=1)]
                if len(window) >= dl_threshold:
                    alerts |= self._zivo_create_alert({
                        'name': '%s downloaded %s event documents within an hour' % (user.name, len(window)),
                        'rule': 'bulk_download', 'severity': 'medium', 'user_id': user.id, 'date': window[-1].date,
                        'details': ', '.join(sorted({e.record_name for e in window}))[:1000],
                        'signature': 'bulk_download:%s:%s' % (user.id, window[0].id),
                    }, Log.browse([e.id for e in window]))
                    break
        if alerts and self.env['zivo.ai.service']._feature_enabled('anomaly'):
            for alert in alerts:
                try:
                    with self.env.cr.savepoint():
                        alert.action_zivo_ai_assess()
                except Exception as exc:  # noqa: BLE001
                    _logger.info('AI alert assessment skipped: %s', exc)
        return alerts

    @api.model
    def _cron_detect(self):
        icp = self.env['ir.config_parameter'].sudo()
        last = icp.get_param(P % 'ai_anomaly_last_run')
        now = fields.Datetime.now()
        since = fields.Datetime.from_string(last) if last else now - timedelta(hours=24)
        # overlap one hour so hourly windows are not split between runs
        alerts = self._zivo_detect(since=since - timedelta(hours=1), until=now)
        icp.set_param(P % 'ai_anomaly_last_run', fields.Datetime.to_string(now))
        return alerts
