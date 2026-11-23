# -*- coding: utf-8 -*-
import json

from odoo import api, fields, models
from odoo.http import request

SKIP_FIELDS = {
    'write_date', 'write_uid', 'create_date', 'create_uid', '__last_update',
    'message_main_attachment_id', 'message_follower_ids', 'message_ids', 'activity_ids',
    'website_message_ids', 'access_token',
}


def _request_ip():
    try:
        if request and request.httprequest:
            return request.httprequest.remote_addr
    except RuntimeError:
        return False
    return False


class ZivoAuditLog(models.Model):
    _name = 'zivo.audit.log'
    _description = 'Event Connect Audit Log'
    _order = 'id desc'
    _rec_name = 'record_name'

    date = fields.Datetime(default=fields.Datetime.now, required=True, readonly=True, index=True)
    user_id = fields.Many2one('res.users', string='User', readonly=True, index=True, ondelete='set null')
    action = fields.Selection([
        ('create', 'Created'),
        ('write', 'Updated'),
        ('unlink', 'Deleted'),
        ('version', 'New Version'),
        ('download', 'Downloaded'),
        ('workflow', 'Workflow'),
        ('api', 'API Access'),
        ('ai', 'AI Suggestion'),
    ], required=True, readonly=True, index=True)
    model = fields.Char(readonly=True, index=True)
    model_description = fields.Char(string='Document Type', readonly=True)
    res_id = fields.Integer(string='Record ID', readonly=True, index=True)
    record_name = fields.Char(string='Record', readonly=True)
    changes = fields.Text(readonly=True)
    changes_json = fields.Text(string='Changes (JSON)', readonly=True)
    ip_address = fields.Char(string='IP Address', readonly=True)
    company_id = fields.Many2one('res.company', readonly=True, default=lambda self: self.env.company)

    @api.model
    def _zivo_log(self, records, action, changes=None, note=None):
        """Create one log line per record. ``changes`` is ``{res_id: {field: (old, new)}}``."""
        if self.env.context.get('zivo_audit_skip'):
            return self.browse()
        vals_list = []
        ip = _request_ip()
        model_desc = records._description if records else False
        for rec in records:
            rec_changes = (changes or {}).get(rec.id) or {}
            lines = []
            for fname, (old, new) in rec_changes.items():
                label = rec._fields[fname].string if fname in rec._fields else fname
                lines.append('%s: %s → %s' % (label, old if old not in (False, None, '') else '∅',
                                              new if new not in (False, None, '') else '∅'))
            if note:
                lines.append(note)
            vals_list.append({
                'user_id': self.env.uid,
                'action': action,
                'model': rec._name,
                'model_description': model_desc,
                'res_id': rec.id,
                'record_name': rec.display_name if rec.exists() or action != 'unlink' else str(rec.id),
                'changes': '\n'.join(lines) or False,
                'changes_json': json.dumps(rec_changes, default=str) if rec_changes else False,
                'ip_address': ip,
            })
        return self.sudo().create(vals_list)

    def action_open_record(self):
        self.ensure_one()
        record = self.env[self.model].browse(self.res_id).exists() if self.model in self.env else False
        if not record:
            return False
        return {
            'type': 'ir.actions.act_window',
            'res_model': self.model,
            'res_id': self.res_id,
            'view_mode': 'form',
            'target': 'current',
        }


class ZivoAuditMixin(models.AbstractModel):
    _name = 'zivo.audit.mixin'
    _description = 'Event Connect Audit Mixin'

    # Restrict tracking to these fields; empty means "all stored, non-computed fields"
    _zivo_audit_fields = []

    def _zivo_audit_tracked(self, field_names):
        result = []
        for fname in field_names:
            field = self._fields.get(fname)
            if not field or fname in SKIP_FIELDS or not field.store:
                continue
            if self._zivo_audit_fields and fname not in self._zivo_audit_fields:
                continue
            if field.compute and field.readonly and not field.inverse:
                # pure computed stored field: tracked through its dependencies
                continue
            result.append(fname)
        return result

    def _zivo_audit_value(self, fname):
        self.ensure_one()
        field = self._fields[fname]
        value = self[fname]
        if field.type == 'many2one':
            return value.display_name or False
        if field.type in ('one2many', 'many2many'):
            return ', '.join(value.mapped('display_name')) or False
        if field.type == 'selection':
            selection = field._description_selection(self.env)
            return dict(selection).get(value, value)
        if field.type == 'binary':
            return '(file)' if value else False
        if field.type == 'html':
            return (str(value)[:200] + '…') if value and len(str(value)) > 200 else value
        if field.type in ('date', 'datetime'):
            return str(value) if value else False
        return value

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if not self.env.context.get('zivo_audit_skip'):
            self.env['zivo.audit.log']._zivo_log(records, 'create')
        return records

    def write(self, vals):
        if self.env.context.get('zivo_audit_skip'):
            return super().write(vals)
        tracked = self._zivo_audit_tracked(vals.keys())
        old_values = {}
        if tracked:
            for rec in self:
                old_values[rec.id] = {f: rec._zivo_audit_value(f) for f in tracked}
        res = super().write(vals)
        if tracked:
            changes = {}
            for rec in self:
                rec_changes = {}
                for f in tracked:
                    new = rec._zivo_audit_value(f)
                    old = old_values[rec.id][f]
                    if old != new:
                        rec_changes[f] = (old, new)
                if rec_changes:
                    changes[rec.id] = rec_changes
            if changes:
                self.env['zivo.audit.log']._zivo_log(self.browse(list(changes)), 'write', changes)
        return res

    def unlink(self):
        if not self.env.context.get('zivo_audit_skip'):
            logs = self.env['zivo.audit.log']
            for rec in self:
                logs._zivo_log(rec, 'unlink', note='Record "%s" deleted' % rec.display_name)
        return super().unlink()
