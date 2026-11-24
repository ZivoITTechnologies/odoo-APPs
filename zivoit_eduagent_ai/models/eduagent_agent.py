# -*- coding: utf-8 -*-
import json
import logging
import re

from odoo import _, api, fields, models
from odoo.tools import html2plaintext
from odoo.tools.misc import formatLang

from .eduagent_llm import EMAIL_RE, PHONE_RE

_logger = logging.getLogger(__name__)

AGENT_CODES = [
    ("admissions", "Admissions Agent"),
    ("fee", "Fee & EMI Agent"),
    ("course_builder", "Course Builder"),
    ("tutor", "Course Tutor"),
    ("practice", "Practice & Quiz"),
    ("mentor", "Project Mentor"),
    ("integrity", "Integrity Agent"),
    ("retention", "Retention Coach"),
    ("support", "Support Agent"),
    ("career", "Career Agent"),
    ("insights", "Insights Agent"),
]

STAGES = [("acquire", "Acquire"), ("learn", "Learn"), ("assess", "Assess"),
          ("retain", "Retain"), ("outcome", "Outcome")]

JSON_CONTRACT = """
Answer ONLY with a JSON object of this shape:
{"reply": "<message to show the user, markdown allowed>",
 "actions": [{"type": "<one of: %s>", "...": "parameters"}],
 "sources": ["S1", "S2"]}
Use an empty actions list when no action is needed. Only use the action types listed.
"""

ACTION_HELP = {
    "capture_lead": '{"type":"capture_lead","name":"","email":"","phone":"","goal":"","experience":"",'
                    '"interest":"","budget":"","timeline":""}  (when you learn contact details or goals)',
    "recommend_courses": '{"type":"recommend_courses","interest":"keywords"}  (to list matching courses)',
    "book_counselling": '{"type":"book_counselling","preferred_time":"text"}  (learner wants a call)',
    "escalate_to_human": '{"type":"escalate_to_human","reason":"text"}  (you cannot help or user asks for a human)',
    "create_ticket": '{"type":"create_ticket","subject":"","description":"","category":"technical|billing|academic|other"}',
    "send_payment_link": '{"type":"send_payment_link"}  (learner asks how/where to pay)',
    "log_practice": '{"type":"log_practice","topic":"","question":"","answer":"","correct":true,'
                    '"score":0-1}  (after grading a practice answer)',
    "update_skills": '{"type":"update_skills","skills":["skill"],"headline":"","target_role":""}',
    "suggest_jobs": '{"type":"suggest_jobs"}  (learner wants job matches)',
}


class EduagentAgent(models.Model):
    _name = "eduagent.agent"
    _description = "EduAgent AI Agent"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    code = fields.Selection(AGENT_CODES, required=True)
    stage = fields.Selection(STAGES, required=True, group_expand=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    description = fields.Text(translate=True)
    system_prompt = fields.Text(required=True)
    provider_id = fields.Many2one("eduagent.provider", string="Provider Override",
                                  help="Leave empty to use the company default provider.")
    model_tier = fields.Selection([("fast", "Fast / cheap model"), ("chat", "Main model")],
                                  default="chat", required=True)
    temperature = fields.Float(default=0.3)
    use_rag = fields.Boolean(string="Use Course Knowledge (RAG)")
    rag_sources = fields.Char(default="slide,course,faq",
                              help="Comma separated chunk types: slide, course, faq, manual")
    allowed_actions = fields.Char(help="Comma separated action types the agent may trigger.")
    public_access = fields.Boolean(string="Available to Website Visitors")
    portal_access = fields.Boolean(string="Available in Learner Hub", default=True)
    max_history = fields.Integer(default=12)
    welcome_message = fields.Text(translate=True)
    handoff_user_id = fields.Many2one("res.users", string="Human Hand-off To")
    conversation_count = fields.Integer(compute="_compute_stats")
    message_count = fields.Integer(compute="_compute_stats")
    tokens_this_month = fields.Integer(compute="_compute_stats")

    def _compute_stats(self):
        Conv = self.env["eduagent.conversation"].sudo()
        Msg = self.env["eduagent.message"].sudo()
        start = fields.Date.today().replace(day=1)
        for agent in self:
            agent.conversation_count = Conv.search_count([("agent_id", "=", agent.id)])
            agent.message_count = Msg.search_count([("conversation_id.agent_id", "=", agent.id)])
            logs = self.env["eduagent.usage.log"].sudo().search_read(
                [("agent_id", "=", agent.id), ("create_date", ">=", start)], ["total_tokens"])
            agent.tokens_this_month = sum(r["total_tokens"] for r in logs)

    def action_view_conversations(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("zivoit_eduagent_ai.action_eduagent_conversation")
        action["domain"] = [("agent_id", "=", self.id)]
        return action

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    def _allowed(self):
        self.ensure_one()
        return [a.strip() for a in (self.allowed_actions or "").split(",") if a.strip()]

    def _build_system(self, conversation, user_text):
        self.ensure_one()
        context_parts = []
        partner = conversation.partner_id
        if partner and not partner._is_public_eduagent():
            context_parts.append(_("Learner: %(name)s", name=partner.name))
        if conversation.channel_id:
            context_parts.append(_("Current course: %s", conversation.channel_id.name))
        hook = getattr(self, "_context_%s" % self.code, None)
        if hook:
            extra = hook(conversation, user_text)
            if extra:
                context_parts.append(extra)
        sources = self.env["eduagent.knowledge.chunk"]
        if self.use_rag:
            channel_ids = conversation._eduagent_channel_scope()
            types = [t.strip() for t in (self.rag_sources or "").split(",") if t.strip()]
            chunks = self.env["eduagent.knowledge.chunk"].retrieve(user_text, channel_ids, types, limit=4)
            if chunks:
                context_parts.append("KNOWLEDGE:\n" + self.env["eduagent.knowledge.chunk"].format_context(chunks))
                sources = chunks
        allowed = self._allowed()
        contract = JSON_CONTRACT % (", ".join(allowed) or "none")
        if allowed:
            contract += "\nAction formats:\n" + "\n".join(ACTION_HELP[a] for a in allowed if a in ACTION_HELP)
        system = "%s\n\nCONTEXT:\n%s\n%s" % (self.system_prompt, "\n\n".join(context_parts) or "-", contract)
        return system, sources

    def run(self, conversation, user_text):
        """Process one user message for ``conversation``. Returns dict(reply, actions, sources)."""
        self.ensure_one()
        conversation = conversation.sudo()
        user_text = (user_text or "").strip()[:4000]
        Msg = self.env["eduagent.message"].sudo()
        Msg.create({"conversation_id": conversation.id, "role": "user", "content": user_text})
        if conversation.state == "human":
            return {"reply": _("A team member has taken over this conversation and will reply here."),
                    "actions": [], "sources": [], "human": True}

        system, sources = self._build_system(conversation, user_text)
        history = conversation.message_ids_eduagent.sorted("id")[-(self.max_history or 12):]
        messages = [{"role": "user" if m.role == "user" else "assistant", "content": m.content}
                    for m in history if m.role in ("user", "assistant", "human")]
        partner = conversation.partner_id if not conversation.partner_id._is_public_eduagent() else None
        res = self.env["eduagent.llm"].complete(
            system, messages, agent=self, tier=self.model_tier, json_mode=True,
            purpose=self.name, partner=partner, temperature=self.temperature,
            mock_response=self._mock_reply(conversation, user_text, sources))
        data = res.get("data") or {}
        reply = data.get("reply") or res.get("text") or ""
        if not isinstance(reply, str):
            reply = json.dumps(reply)
        results = []
        if res.get("ok"):
            allowed = self._allowed()
            actions = [a for a in (data.get("actions") or [])[:5] if isinstance(a, dict)]
            if "capture_lead" in allowed:
                actions = self._merge_contact_details(actions, user_text)
            for action in actions:
                atype = action.get("type")
                if atype not in allowed:
                    continue
                method = getattr(self, "_action_%s" % atype, None)
                if not method:
                    continue
                try:
                    with self.env.cr.savepoint():
                        out = method(conversation, action)
                except Exception as exc:  # pylint: disable=broad-except
                    _logger.exception("EduAgent action %s failed", atype)
                    out = {"type": atype, "error": str(exc)}
                if out:
                    results.append(out)
                    if out.get("append"):
                        reply = (reply + "\n\n" + out["append"]).strip()
        sources = self._cited_sources(sources, data.get("sources"))
        Msg.create({
            "conversation_id": conversation.id, "role": "assistant", "content": reply,
            "actions_json": json.dumps(results) if results else False,
            "source_ids": [(6, 0, sources.ids)] if sources else False,
        })
        conversation.last_message_date = fields.Datetime.now()
        return {"reply": reply, "actions": results,
                "sources": [{"title": s.name, "url": s.url} for s in sources] if sources else []}

    @staticmethod
    def _cited_sources(chunks, cited):
        """Keep only the knowledge chunks the model actually cited (e.g. ["S1", "S3"])."""
        if not chunks or not isinstance(cited, list):
            return chunks.browse()
        picked = []
        for ref in cited:
            match = re.match(r"\s*\[?S?(\d+)\]?\s*$", str(ref), re.I)
            if match:
                idx = int(match.group(1)) - 1
                if 0 <= idx < len(chunks) and chunks[idx].id not in picked:
                    picked.append(chunks[idx].id)
        return chunks.browse(picked)

    @staticmethod
    def _merge_contact_details(actions, raw_text):
        """PII masking hides emails/phones from the LLM, so contact details are read from the raw
        message here (never sent to the provider) and merged into the lead-capture action."""
        email = EMAIL_RE.search(raw_text or "")
        phone = PHONE_RE.search(raw_text or "")
        if not (email or phone):
            return actions
        capture = next((a for a in actions if a.get("type") == "capture_lead"), None)
        if not capture:
            capture = {"type": "capture_lead"}
            actions = [capture] + actions
        for key, match in (("email", email), ("phone", phone)):
            current = str(capture.get(key) or "")
            if match and (not current or current.startswith("[")):
                capture[key] = match.group(0)
        return actions

    # ------------------------------------------------------------------
    # Agent-specific context providers
    # ------------------------------------------------------------------
    def _catalog_text(self, limit=25):
        channels = self.env["slide.channel"].sudo().search(
            [("website_published", "=", True)], limit=limit, order="total_slides desc, id desc")
        lines = []
        for c in channels:
            price = ""
            if "product_id" in c._fields and c.product_id:
                price = " | price %s %s" % (c.product_id.list_price, c.product_id.currency_id.name or "")
            lines.append("- %s (id %s)%s: %s" % (c.name, c.id, price,
                                                 (html2plaintext(c.description or "")[:220]).replace("\n", " ")))
        return "COURSE CATALOG:\n" + ("\n".join(lines) or "-")

    def _context_admissions(self, conversation, text):
        lead = conversation.lead_id
        known = ""
        if lead:
            known = _("Known about this prospect: name %(n)s, email %(e)s, phone %(p)s, goal %(g)s",
                      n=lead.contact_name or lead.partner_name or "?", e=lead.email_from or "?",
                      p=lead.phone or "?", g=lead.eduagent_goal or "?")
        return self._catalog_text() + ("\n" + known if known else "")

    def _context_fee(self, conversation, text):
        partner = conversation.partner_id
        if not partner or partner._is_public_eduagent():
            return _("Visitor is not signed in: explain fee options generally and invite them to sign in.")
        inst = self.env["eduagent.installment"].sudo().search(
            [("partner_id", "child_of", partner.commercial_partner_id.id), ("state", "!=", "paid")],
            order="due_date", limit=6)
        lines = ["- %s: %s due %s (%s)" % (i.order_id.name, formatLang(self.env, i.amount, currency_obj=i.currency_id),
                                           i.due_date, i.state) for i in inst]
        plans = self.env["eduagent.emi.plan"].sudo().search([])
        plan_lines = ["- %s: %s instalments every %s month(s), %s%% down payment" % (
            p.name, p.installment_count, p.interval_months, p.down_payment_pct) for p in plans]
        return "OPEN INSTALMENTS:\n%s\nEMI PLANS:\n%s" % ("\n".join(lines) or "-", "\n".join(plan_lines) or "-")

    def _context_practice(self, conversation, text):
        partner = conversation.partner_id
        Attempt = self.env["eduagent.practice.attempt"].sudo()
        mastery = Attempt._topic_mastery(partner, conversation.channel_id)
        weak = sorted(mastery.items(), key=lambda kv: kv[1])[:5]
        due = Attempt._due_topics(partner, conversation.channel_id)
        return _("Topic mastery (0-1): %(m)s\nTopics due for spaced review: %(d)s\n"
                 "Ask ONE question at a time. When the learner answers, grade it, give short feedback, "
                 "log it with log_practice, then ask the next question on the weakest topic.",
                 m=", ".join("%s=%.2f" % kv for kv in weak) or "no history yet",
                 d=", ".join(due) or "none")

    def _context_career(self, conversation, text):
        partner = conversation.partner_id
        if not partner or partner._is_public_eduagent():
            return ""
        profile = self.env["eduagent.skill.profile"].sudo()._get_or_create(partner)
        return _("Skills: %(s)s\nHeadline: %(h)s\nTarget role: %(t)s\nCompleted courses: %(c)s",
                 s=", ".join(profile.skill_ids.mapped("name")) or "-", h=profile.headline or "-",
                 t=profile.target_role or "-", c=", ".join(profile.completed_channel_ids.mapped("name")) or "-")

    def _context_support(self, conversation, text):
        partner = conversation.partner_id
        if not partner or partner._is_public_eduagent():
            return ""
        tickets = self.env["eduagent.support.ticket"].sudo().search(
            [("partner_id", "=", partner.id)], limit=3, order="id desc")
        return "RECENT TICKETS:\n" + ("\n".join("- %s [%s]" % (t.name, t.state) for t in tickets) or "-")

    # ------------------------------------------------------------------
    # Demo / offline replies (used by the 'Demo' provider)
    # ------------------------------------------------------------------
    def _mock_reply(self, conversation, text, sources):
        code = self.code
        actions = []
        low = (text or "").lower()
        if code == "admissions":
            email = EMAIL_RE.search(text or "")
            phone = PHONE_RE.search(text or "")
            if email or phone:
                actions.append({"type": "capture_lead", "email": email.group(0) if email else "",
                                "phone": phone.group(0) if phone else "", "goal": text[:200]})
            actions.append({"type": "recommend_courses", "interest": text[:120]})
            if "call" in low or "counsel" in low:
                actions.append({"type": "book_counselling", "preferred_time": text[:80]})
            reply = _("Thanks! Here are programs that match what you shared. Leave your email or phone "
                      "and a counsellor will reach out.")
        elif code == "fee":
            actions.append({"type": "send_payment_link"})
            reply = _("Here is a summary of your fee schedule and how to pay.")
        elif code == "support":
            if "ticket" in low or "human" in low or "problem" in low:
                actions.append({"type": "create_ticket", "subject": text[:60], "description": text,
                                "category": "other"})
            reply = _("Here is what I found in our help articles.")
        elif code == "practice":
            reply = _("Practice question: explain, in your own words, the key idea of the lesson you "
                      "studied last.")
            if len(text) > 25:
                actions.append({"type": "log_practice", "topic": "general", "question": "self-explanation",
                                "answer": text[:300], "correct": True, "score": 0.7})
        elif code == "career":
            actions.append({"type": "suggest_jobs"})
            reply = _("Based on your completed courses, here are openings that match your skills.")
        else:
            reply = _("(Demo mode) I'm the %s. Configure an AI provider to get real answers.", self.name)
        cited = ["S%d" % (i + 1) for i in range(min(len(sources), 2))] if sources else []
        return json.dumps({"reply": reply, "actions": actions, "sources": cited})


class ResPartner(models.Model):
    _inherit = "res.partner"

    eduagent_conversation_ids = fields.One2many("eduagent.conversation", "partner_id")

    def _is_public_eduagent(self):
        if not self:
            return True
        public = self.env.ref("base.public_partner", raise_if_not_found=False)
        return bool(public and self.id == public.id)
