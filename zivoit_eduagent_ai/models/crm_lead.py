# -*- coding: utf-8 -*-
from odoo import _, api, fields, models


class CrmLead(models.Model):
    _inherit = "crm.lead"

    eduagent_conversation_id = fields.Many2one("eduagent.conversation", string="AI Conversation")
    eduagent_goal = fields.Text(string="Learning Goal")
    eduagent_experience = fields.Char(string="Experience / Background")
    eduagent_interest = fields.Char(string="Interest")
    eduagent_budget = fields.Char(string="Budget")
    eduagent_timeline = fields.Char(string="Start Timeline")
    eduagent_channel_ids = fields.Many2many("slide.channel", string="Recommended Courses")
    eduagent_score = fields.Integer(string="AI Lead Score", help="0-100 fit and intent score from the "
                                                                 "Admissions agent.")
    eduagent_transcript = fields.Html(compute="_compute_eduagent_transcript", string="Chat Transcript")

    def _compute_eduagent_transcript(self):
        for lead in self:
            msgs = lead.eduagent_conversation_id.message_ids_eduagent.sorted("id")
            lead.eduagent_transcript = "".join(
                "<p><b>%s:</b> %s</p>" % (dict(m._fields["role"].selection).get(m.role),
                                          (m.content or "").replace("<", "&lt;").replace("\n", "<br/>"))
                for m in msgs) or False

    def _eduagent_compute_score(self):
        """Transparent score: contactability + clarity of intent + urgency + budget."""
        for lead in self:
            score = 0
            if lead.email_from:
                score += 20
            if lead.phone:
                score += 20
            if lead.eduagent_goal:
                score += 20
            if lead.eduagent_channel_ids or lead.eduagent_interest:
                score += 15
            timeline = (lead.eduagent_timeline or "").lower()
            if any(w in timeline for w in ("now", "immediately", "this week", "this month", "asap", "next week")):
                score += 15
            if lead.eduagent_budget:
                score += 10
            lead.eduagent_score = min(score, 100)
            if "priority" in lead._fields:
                lead.priority = "3" if score >= 80 else ("2" if score >= 60 else ("1" if score >= 40 else "0"))
