# -*- coding: utf-8 -*-
"""Whitelisted actions agents may trigger. Each returns a dict that is stored on the message
and may carry an ``append`` text shown to the user."""
from datetime import timedelta

from odoo import _, fields, models
from odoo.tools.misc import format_date, formatLang

from .eduagent_llm import EMAIL_RE


class EduagentAgentActions(models.Model):
    _inherit = "eduagent.agent"

    # ---------------------------------------------------------------- admissions
    def _action_capture_lead(self, conversation, params):
        Lead = self.env["crm.lead"].sudo()
        email = (params.get("email") or "").strip()
        if email and not EMAIL_RE.fullmatch(email):
            email = ""
        phone = (params.get("phone") or "").strip()[:32]
        partner = conversation.partner_id if not conversation.partner_id._is_public_eduagent() else False
        vals = {k: v for k, v in {
            "contact_name": (params.get("name") or "").strip()[:120] or False,
            "email_from": email or False,
            "phone": phone or False,
            "eduagent_goal": (params.get("goal") or "").strip()[:500] or False,
            "eduagent_experience": (params.get("experience") or "").strip()[:200] or False,
            "eduagent_interest": (params.get("interest") or "").strip()[:200] or False,
            "eduagent_budget": (params.get("budget") or "").strip()[:100] or False,
            "eduagent_timeline": (params.get("timeline") or "").strip()[:100] or False,
        }.items() if v}
        lead = conversation.lead_id
        if not lead and email:
            lead = Lead.search([("email_from", "=ilike", email), ("active", "=", True)], limit=1)
        if lead:
            lead.write(vals)
        else:
            if not (email or phone or partner):
                return {"type": "capture_lead", "status": "skipped: no contact details yet"}
            team = self.env.company.eduagent_admissions_team_id
            vals.update({
                "name": _("AI Admissions: %s", vals.get("contact_name") or email or phone or partner.name),
                "type": "lead",
                "partner_id": partner.id if partner else False,
                "team_id": team.id if team else False,
                "user_id": team.user_id.id if team and team.user_id else False,
                "eduagent_conversation_id": conversation.id,
                "eduagent_channel_ids": [(6, 0, conversation.recommended_channel_ids.ids)],
                "description": _("Captured by the EduAgent Admissions agent."),
            })
            lead = Lead.create(vals)
        conversation.lead_id = lead
        lead._eduagent_compute_score()
        return {"type": "capture_lead", "lead_id": lead.id, "score": lead.eduagent_score}

    def _action_recommend_courses(self, conversation, params):
        interest = params.get("interest") or ""
        chunks = self.env["eduagent.knowledge.chunk"].retrieve(interest, None, ["course", "slide"], limit=8,
                                                               min_ratio=0.75)
        channels = chunks.mapped("channel_id").filtered("website_published")[:3]
        if not channels:
            channels = self.env["slide.channel"].sudo().search(
                [("website_published", "=", True), ("name", "ilike", interest[:40])], limit=3)
        if not channels:
            return {"type": "recommend_courses", "courses": []}
        base = channels[:1].get_base_url()
        conversation.recommended_channel_ids = [(4, c.id) for c in channels]
        if conversation.lead_id:
            conversation.lead_id.sudo().eduagent_channel_ids = [(4, c.id) for c in channels]
        lines = ["- [%s](%s%s)" % (c.name, base, c.website_url) for c in channels]
        return {"type": "recommend_courses", "courses": channels.ids,
                "append": _("**Recommended programs:**\n%s", "\n".join(lines))}

    def _action_book_counselling(self, conversation, params):
        lead = conversation.lead_id
        if not lead:
            return {"type": "book_counselling", "status": "need contact details first",
                    "append": _("Please share your email or phone so a counsellor can call you.")}
        user = lead.user_id or self.handoff_user_id or lead.team_id.user_id or self.env.ref("base.user_admin")
        lead.sudo().activity_schedule(
            "mail.mail_activity_data_call", date_deadline=fields.Date.today() + timedelta(days=1),
            summary=_("Counselling call (AI booked)"),
            note=_("Preferred time: %s", params.get("preferred_time") or "-"), user_id=user.id)
        return {"type": "book_counselling", "lead_id": lead.id,
                "append": _("I've asked a counsellor to call you (preferred time: %s).",
                            params.get("preferred_time") or _("any time"))}

    # ---------------------------------------------------------------- human hand-off / support
    def _action_escalate_to_human(self, conversation, params):
        conversation.write({"state": "human"})
        user = self.handoff_user_id or self.env.ref("base.user_admin")
        conversation.user_id = user
        conversation.activity_schedule(
            "mail.mail_activity_data_todo", user_id=user.id,
            summary=_("AI hand-off: %s", (params.get("reason") or "")[:80]))
        return {"type": "escalate_to_human",
                "append": _("I've passed this to our team; a person will reply here shortly.")}

    def _action_create_ticket(self, conversation, params):
        partner = conversation.partner_id if not conversation.partner_id._is_public_eduagent() else False
        category = params.get("category") if params.get("category") in (
            "technical", "billing", "academic", "other") else "other"
        ticket = self.env["eduagent.support.ticket"].sudo().create({
            "name": (params.get("subject") or _("Support request"))[:120],
            "description": params.get("description") or "",
            "category": category,
            "partner_id": partner.id if partner else False,
            "channel_id": conversation.channel_id.id,
            "conversation_id": conversation.id,
            "user_id": self.handoff_user_id.id or False,
        })
        return {"type": "create_ticket", "ticket_id": ticket.id,
                "append": _("Ticket **%s** created. We'll update you by email.", ticket.reference)}

    # ---------------------------------------------------------------- fees
    def _action_send_payment_link(self, conversation, params):
        partner = conversation.partner_id
        if not partner or partner._is_public_eduagent():
            return {"type": "send_payment_link", "append": _("Please sign in to see your fee schedule.")}
        moves = self.env["account.move"].sudo().search([
            ("partner_id", "child_of", partner.commercial_partner_id.id),
            ("move_type", "=", "out_invoice"), ("state", "=", "posted"),
            ("payment_state", "in", ("not_paid", "partial"))], limit=3, order="invoice_date_due")
        if not moves:
            return {"type": "send_payment_link", "append": _("You have no unpaid invoices right now.")}
        base = moves[:1].get_base_url()
        lines = ["- %s: %s due %s - [Pay now](%s%s)" % (
            m.name, formatLang(self.env, m.amount_residual, currency_obj=m.currency_id),
            format_date(self.env, m.invoice_date_due) if m.invoice_date_due else "-", base,
            m.get_portal_url()) for m in moves]
        return {"type": "send_payment_link", "invoice_ids": moves.ids,
                "append": _("**Your open invoices:**\n%s", "\n".join(lines))}

    # ---------------------------------------------------------------- practice
    def _action_log_practice(self, conversation, params):
        partner = conversation.partner_id
        if not partner or partner._is_public_eduagent():
            return False
        try:
            score = float(params.get("score", 1.0 if params.get("correct") else 0.0))
        except (TypeError, ValueError):
            score = 0.0
        attempt = self.env["eduagent.practice.attempt"].sudo().create({
            "partner_id": partner.id,
            "channel_id": conversation.channel_id.id,
            "topic": (params.get("topic") or "general")[:80].strip().lower(),
            "question": (params.get("question") or "")[:1000],
            "answer": (params.get("answer") or "")[:2000],
            "score": max(0.0, min(1.0, score)),
        })
        return {"type": "log_practice", "attempt_id": attempt.id, "score": attempt.score}

    # ---------------------------------------------------------------- career
    def _action_update_skills(self, conversation, params):
        partner = conversation.partner_id
        if not partner or partner._is_public_eduagent():
            return False
        profile = self.env["eduagent.skill.profile"].sudo()._get_or_create(partner)
        Skill = self.env["eduagent.skill"].sudo()
        skills = Skill.browse()
        for name in (params.get("skills") or [])[:20]:
            if isinstance(name, str) and name.strip():
                skills |= Skill._get_or_create(name)
        vals = {"skill_ids": [(4, s.id) for s in skills]}
        if params.get("headline"):
            vals["headline"] = params["headline"][:160]
        if params.get("target_role"):
            vals["target_role"] = params["target_role"][:120]
        profile.write(vals)
        return {"type": "update_skills", "profile_id": profile.id}

    def _action_suggest_jobs(self, conversation, params):
        partner = conversation.partner_id
        if not partner or partner._is_public_eduagent():
            return {"type": "suggest_jobs", "append": _("Sign in to get job matches.")}
        profile = self.env["eduagent.skill.profile"].sudo()._get_or_create(partner)
        profile.action_refresh_from_courses()
        matches = profile.action_compute_matches()
        top = matches.sorted("score", reverse=True)[:5]
        if not top:
            return {"type": "suggest_jobs", "append": _("No matching openings yet. Complete more courses "
                                                        "to unlock matches.")}
        lines = ["- **%s** at %s - match %d%%" % (m.job_id.name, m.job_id.employer_id.name or "-",
                                                  m.score) for m in top]
        return {"type": "suggest_jobs", "append": _("**Matching openings:**\n%s", "\n".join(lines))}
