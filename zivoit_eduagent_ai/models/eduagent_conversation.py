# -*- coding: utf-8 -*-
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EduagentConversation(models.Model):
    _name = "eduagent.conversation"
    _description = "EduAgent Conversation"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "last_message_date desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    agent_id = fields.Many2one("eduagent.agent", required=True, ondelete="restrict", index=True)
    agent_code = fields.Selection(related="agent_id.code", store=True)
    partner_id = fields.Many2one("res.partner", string="Learner / Visitor", index=True)
    channel_id = fields.Many2one("slide.channel", string="Course")
    slide_id = fields.Many2one("slide.slide", string="Lesson")
    lead_id = fields.Many2one("crm.lead", string="Lead / Opportunity")
    recommended_channel_ids = fields.Many2many("slide.channel", "eduagent_conversation_recommended_rel",
                                               string="Recommended Courses")
    user_id = fields.Many2one("res.users", string="Assigned Staff", tracking=True)
    token = fields.Char(default=lambda self: uuid.uuid4().hex, copy=False, index=True)
    source = fields.Selection([("website", "Website"), ("portal", "Learner Hub"),
                               ("backend", "Backend"), ("api", "API")], default="website")
    state = fields.Selection([("ai", "AI handling"), ("human", "Human took over"), ("closed", "Closed")],
                             default="ai", tracking=True)
    message_ids_eduagent = fields.One2many("eduagent.message", "conversation_id", string="Chat Messages")
    message_count = fields.Integer(compute="_compute_message_count")
    last_message_date = fields.Datetime(default=fields.Datetime.now)
    staff_reply = fields.Text(string="Reply as staff")
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)

    @api.depends("agent_id", "partner_id", "lead_id.contact_name", "lead_id.email_from")
    def _compute_name(self):
        for rec in self:
            who = rec.partner_id.name if rec.partner_id and not rec.partner_id._is_public_eduagent() \
                else (rec.lead_id.contact_name or rec.lead_id.email_from or _("Visitor"))
            rec.name = "%s - %s" % (rec.agent_id.name or "", who)

    @api.depends("message_ids_eduagent")
    def _compute_message_count(self):
        for rec in self:
            rec.message_count = len(rec.message_ids_eduagent)

    def _eduagent_channel_scope(self):
        """Courses whose knowledge the agent may use for this conversation."""
        self.ensure_one()
        if self.channel_id:
            return self.channel_id.ids
        partner = self.partner_id
        if partner and not partner._is_public_eduagent():
            enrolled = self.env["slide.channel.partner"].sudo().search([("partner_id", "=", partner.id)])
            if enrolled:
                return enrolled.mapped("channel_id").ids
        return self.env["slide.channel"].sudo().search([("website_published", "=", True)]).ids

    def action_take_over(self):
        for rec in self:
            rec.write({"state": "human", "user_id": self.env.user.id})

    def action_back_to_ai(self):
        self.write({"state": "ai"})

    def action_close(self):
        self.write({"state": "closed"})

    def action_send_staff_reply(self):
        for rec in self:
            if not rec.staff_reply:
                raise UserError(_("Write a reply first."))
            self.env["eduagent.message"].create({
                "conversation_id": rec.id, "role": "human", "content": rec.staff_reply,
                "author_id": self.env.user.partner_id.id})
            rec.write({"staff_reply": False, "state": "human", "user_id": rec.user_id.id or self.env.user.id,
                       "last_message_date": fields.Datetime.now()})


class EduagentMessage(models.Model):
    _name = "eduagent.message"
    _description = "EduAgent Message"
    _order = "id"

    conversation_id = fields.Many2one("eduagent.conversation", required=True, ondelete="cascade", index=True)
    role = fields.Selection([("user", "Learner"), ("assistant", "AI"), ("human", "Staff"),
                             ("system", "System")], required=True)
    content = fields.Text(required=True)
    author_id = fields.Many2one("res.partner")
    actions_json = fields.Text(string="Actions Taken")
    source_ids = fields.Many2many("eduagent.knowledge.chunk", string="Sources")
    feedback = fields.Selection([("up", "Helpful"), ("down", "Not helpful")])
    create_date = fields.Datetime(readonly=True)
