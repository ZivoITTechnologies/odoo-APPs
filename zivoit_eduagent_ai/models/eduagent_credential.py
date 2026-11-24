# -*- coding: utf-8 -*-
import hashlib
import hmac
import uuid

from odoo import _, api, fields, models


class EduagentCredential(models.Model):
    _name = "eduagent.credential"
    _description = "Verifiable Credential"
    _inherit = ["mail.thread"]
    _order = "issue_date desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    code = fields.Char(required=True, readonly=True, copy=False, index=True,
                       default=lambda self: uuid.uuid4().hex[:16].upper())
    partner_id = fields.Many2one("res.partner", string="Learner", required=True, index=True)
    channel_id = fields.Many2one("slide.channel", string="Course", required=True)
    issue_date = fields.Date(default=fields.Date.context_today, required=True)
    signature = fields.Char(readonly=True, copy=False)
    state = fields.Selection([("valid", "Valid"), ("revoked", "Revoked")], default="valid", tracking=True)
    revoke_reason = fields.Char()
    verify_url = fields.Char(compute="_compute_verify_url")
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)
    reviewer_id = fields.Many2one("res.users", string="Approved By", readonly=True)

    _code_unique = models.Constraint("UNIQUE(code)", "Credential codes must be unique.")

    @api.depends("partner_id", "channel_id")
    def _compute_name(self):
        for rec in self:
            rec.name = "%s - %s" % (rec.channel_id.name or "", rec.partner_id.name or "")

    def _compute_verify_url(self):
        for rec in self:
            rec.verify_url = "%s/eduagent/verify/%s" % (rec.get_base_url(), rec.code)

    def _payload(self):
        self.ensure_one()
        return "%s|%s|%s|%s" % (self.code, self.partner_id.name, self.channel_id.name, self.issue_date)

    def _sign(self):
        self.ensure_one()
        secret = (self.company_id.sudo().eduagent_credential_secret or "eduagent").encode()
        return hmac.new(secret, self._payload().encode(), hashlib.sha256).hexdigest()

    @api.model_create_multi
    def create(self, vals_list):
        recs = super().create(vals_list)
        for rec in recs:
            rec.signature = rec._sign()
        return recs

    def is_authentic(self):
        self.ensure_one()
        return self.state == "valid" and hmac.compare_digest(self.signature or "", self._sign())

    def action_revoke(self):
        self.write({"state": "revoked"})

    @api.model
    def _issue_for_completed(self, partners=None, channels=None):
        """Issue credentials to learners who completed a course (and passed the oral check if required)."""
        domain = [("member_status", "=", "completed")]
        if partners:
            domain.append(("partner_id", "in", partners.ids))
        if channels:
            domain.append(("channel_id", "in", channels.ids))
        enrolments = self.env["slide.channel.partner"].sudo().search(domain, limit=2000)
        created = self.browse()
        for enr in enrolments:
            if not enr.channel_id.eduagent_issue_credential:
                continue
            exists = self.sudo().search_count([("partner_id", "=", enr.partner_id.id),
                                               ("channel_id", "=", enr.channel_id.id)])
            if exists:
                continue
            company = enr.channel_id._eduagent_company() or self.env.company
            if company.eduagent_require_oral_check:
                passed = self.env["eduagent.oral.check"].sudo().search_count([
                    ("partner_id", "=", enr.partner_id.id), ("channel_id", "=", enr.channel_id.id),
                    ("state", "=", "passed")])
                if not passed:
                    continue
            cred = self.sudo().create({"partner_id": enr.partner_id.id, "channel_id": enr.channel_id.id,
                                       "company_id": company.id})
            cred.message_post(body=_("Credential issued. Verify at %s", cred.verify_url),
                              partner_ids=enr.partner_id.ids)
            created |= cred
        return created

    @api.model
    def _cron_issue_credentials(self):
        self._issue_for_completed()
