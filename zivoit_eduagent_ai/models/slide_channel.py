# -*- coding: utf-8 -*-
from odoo import _, fields, models


class SlideChannel(models.Model):
    _inherit = "slide.channel"

    eduagent_tutor_enabled = fields.Boolean(string="AI Tutor on Course Pages", default=True)
    eduagent_issue_credential = fields.Boolean(string="Issue Verifiable Credential on Completion", default=True)
    eduagent_skill_ids = fields.Many2many("eduagent.skill", string="Skills Taught")
    eduagent_last_indexed = fields.Datetime(string="Knowledge Indexed On", readonly=True)
    eduagent_chunk_count = fields.Integer(compute="_compute_eduagent_stats", string="Knowledge Chunks")
    eduagent_at_risk_count = fields.Integer(compute="_compute_eduagent_stats", string="At-risk Learners")
    eduagent_credential_count = fields.Integer(compute="_compute_eduagent_stats", string="Credentials")

    def _eduagent_company(self):
        self.ensure_one()
        return self.website_id.company_id or self.env.company

    def _compute_eduagent_stats(self):
        Chunk = self.env["eduagent.knowledge.chunk"].sudo()
        Enrol = self.env["slide.channel.partner"].sudo()
        Cred = self.env["eduagent.credential"].sudo()
        for rec in self:
            rec.eduagent_chunk_count = Chunk.search_count([("channel_id", "=", rec.id)])
            rec.eduagent_at_risk_count = Enrol.search_count([("channel_id", "=", rec.id),
                                                             ("eduagent_risk_level", "=", "high")])
            rec.eduagent_credential_count = Cred.search_count([("channel_id", "=", rec.id)])

    def action_eduagent_index(self):
        for rec in self:
            self.env["eduagent.knowledge.chunk"].index_channel(rec)
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("Knowledge indexed"), "type": "success", "sticky": False,
                       "message": _("%s chunk(s) available to the AI tutor.", sum(self.mapped("eduagent_chunk_count")))},
        }

    def action_eduagent_view_risk(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("zivoit_eduagent_ai.action_eduagent_risk")
        action["domain"] = [("channel_id", "=", self.id)]
        return action

    def action_eduagent_view_chunks(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("zivoit_eduagent_ai.action_eduagent_knowledge")
        action["domain"] = [("channel_id", "=", self.id)]
        return action

    def action_eduagent_view_credentials(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("zivoit_eduagent_ai.action_eduagent_credential")
        action["domain"] = [("channel_id", "=", self.id)]
        return action

    def _cron_eduagent_reindex(self):
        stale = self.search([("website_published", "=", True)])
        stale = stale.filtered(lambda c: not c.eduagent_last_indexed or c.write_date > c.eduagent_last_indexed)
        for channel in stale[:50]:
            self.env["eduagent.knowledge.chunk"].index_channel(channel)
