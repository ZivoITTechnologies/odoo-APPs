# -*- coding: utf-8 -*-
import hashlib
import secrets

from odoo import _, api, fields, models


class EduagentApiKey(models.Model):
    _name = "eduagent.api.key"
    _description = "EduAgent REST API Key"

    name = fields.Char(required=True, help="What will use this key, e.g. 'AnythingMCP connector'.")
    user_id = fields.Many2one("res.users", required=True, default=lambda self: self.env.user,
                              help="Requests run with this user's access rights.")
    key_hash = fields.Char(readonly=True, copy=False)
    key_hint = fields.Char(string="Key (last 4)", readonly=True, copy=False)
    new_key = fields.Char(string="New Key (shown once)", readonly=True, copy=False,
                          help="Copy it now; only a hash is stored once you leave this form.")
    active = fields.Boolean(default=True)
    last_used = fields.Datetime(readonly=True)
    scope = fields.Selection([("read", "Read KPIs & risk"), ("chat", "Read + run agents")],
                             default="read", required=True)

    def action_generate(self):
        for rec in self:
            key = "edu_" + secrets.token_urlsafe(32)
            rec.write({"key_hash": hashlib.sha256(key.encode()).hexdigest(), "key_hint": key[-4:],
                       "new_key": key})
        return True

    def action_clear_new_key(self):
        self.write({"new_key": False})

    @api.model
    def _authenticate(self, raw_key):
        if not raw_key:
            return self.browse()
        digest = hashlib.sha256(raw_key.encode()).hexdigest()
        key = self.sudo().search([("key_hash", "=", digest), ("active", "=", True)], limit=1)
        if key:
            key.sudo().write({"last_used": fields.Datetime.now(), "new_key": False})
        return key
