# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class EduagentFaq(models.Model):
    _name = "eduagent.faq"
    _description = "EduAgent FAQ / Policy Article"
    _order = "sequence, id"

    name = fields.Char(string="Question / Title", required=True, translate=True)
    answer = fields.Html(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    category = fields.Selection([("admissions", "Admissions"), ("fees", "Fees & Refunds"),
                                 ("platform", "Platform / Technical"), ("academic", "Academic"),
                                 ("certificates", "Certificates"), ("career", "Career")],
                                default="platform")
    channel_id = fields.Many2one("slide.channel", string="Course (optional)")

    def action_reindex_all(self):
        self.env["eduagent.knowledge.chunk"].index_faqs()


class EduagentSupportTicket(models.Model):
    _name = "eduagent.support.ticket"
    _description = "EduAgent Learner Support Ticket"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    reference = fields.Char(readonly=True, copy=False, default="/")
    name = fields.Char(string="Subject", required=True, tracking=True)
    description = fields.Text()
    category = fields.Selection([("technical", "Technical"), ("billing", "Billing"),
                                 ("academic", "Academic"), ("other", "Other")], default="other", tracking=True)
    priority = fields.Selection([("0", "Normal"), ("1", "High")], default="0")
    state = fields.Selection([("new", "New"), ("in_progress", "In Progress"), ("waiting", "Waiting on Learner"),
                              ("done", "Solved")], default="new", tracking=True)
    partner_id = fields.Many2one("res.partner", string="Learner", tracking=True)
    channel_id = fields.Many2one("slide.channel", string="Course")
    conversation_id = fields.Many2one("eduagent.conversation", string="AI Conversation")
    user_id = fields.Many2one("res.users", string="Assigned To", tracking=True)
    helpdesk_ticket_ref = fields.Char(string="Helpdesk Ticket", readonly=True,
                                      help="Set when the Odoo Helpdesk app is installed and a ticket was mirrored.")
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("reference", "/") == "/":
                vals["reference"] = self.env["ir.sequence"].next_by_code("eduagent.support.ticket") or "/"
        tickets = super().create(vals_list)
        for ticket in tickets:
            if ticket.partner_id:
                ticket.message_subscribe(partner_ids=ticket.partner_id.ids)
            ticket._mirror_to_helpdesk()
        return tickets

    def _mirror_to_helpdesk(self):
        """If Odoo Helpdesk (Enterprise) is installed, mirror the ticket there."""
        if "helpdesk.ticket" not in self.env:
            return
        for ticket in self:
            try:
                with self.env.cr.savepoint():
                    hd = self.env["helpdesk.ticket"].sudo().create({
                        "name": "[%s] %s" % (ticket.reference, ticket.name),
                        "description": ticket.description or "",
                        "partner_id": ticket.partner_id.id or False,
                    })
                    ticket.helpdesk_ticket_ref = "helpdesk.ticket,%s" % hd.id
            except Exception as exc:  # pylint: disable=broad-except
                _logger.info("Helpdesk mirroring skipped: %s", exc)

    def action_start(self):
        self.write({"state": "in_progress", "user_id": self.env.user.id})

    def action_done(self):
        self.write({"state": "done"})
