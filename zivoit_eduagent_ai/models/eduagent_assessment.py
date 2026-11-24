# -*- coding: utf-8 -*-
"""Project Mentor (rubric feedback), Practice & Quiz (spaced repetition) and Integrity (viva checks).
Every AI score is a *recommendation*; a human reviewer records the final decision."""
import json
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

REVIEW_INTERVALS = [1, 3, 7, 14, 30]  # days, simplified SM-2 ladder


class EduagentRubric(models.Model):
    _name = "eduagent.rubric"
    _description = "Assessment Rubric"

    name = fields.Char(required=True)
    channel_id = fields.Many2one("slide.channel", string="Course")
    instructions = fields.Text(help="Assignment brief shown to learners and given to the AI reviewer.")
    criterion_ids = fields.One2many("eduagent.rubric.criterion", "rubric_id", copy=True)
    pass_score = fields.Float(default=60.0, help="Percentage needed to pass.")


class EduagentRubricCriterion(models.Model):
    _name = "eduagent.rubric.criterion"
    _description = "Rubric Criterion"
    _order = "sequence, id"

    rubric_id = fields.Many2one("eduagent.rubric", required=True, ondelete="cascade")
    sequence = fields.Integer(default=10)
    name = fields.Char(required=True)
    description = fields.Text(help="What excellent work looks like for this criterion.")
    weight = fields.Float(default=1.0)


class EduagentSubmission(models.Model):
    _name = "eduagent.submission"
    _description = "Project / Assignment Submission"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    name = fields.Char(string="Title", required=True)
    partner_id = fields.Many2one("res.partner", string="Learner", required=True, tracking=True)
    channel_id = fields.Many2one("slide.channel", string="Course", tracking=True)
    rubric_id = fields.Many2one("eduagent.rubric")
    content = fields.Text(string="Submission (text / code)")
    attachment_ids = fields.Many2many("ir.attachment", string="Files")
    state = fields.Selection([("submitted", "Submitted"), ("ai_reviewed", "AI Reviewed"),
                              ("approved", "Approved"), ("returned", "Returned for Rework")],
                             default="submitted", tracking=True)
    ai_feedback = fields.Html(string="AI Feedback", readonly=True)
    ai_score = fields.Float(string="AI Suggested Score (%)", readonly=True)
    final_score = fields.Float(string="Final Score (%)", tracking=True)
    reviewer_id = fields.Many2one("res.users", string="Reviewed By", readonly=True, tracking=True)
    review_note = fields.Text(string="Mentor Note")
    passed = fields.Boolean(compute="_compute_passed", store=True)

    @api.depends("final_score", "state", "rubric_id.pass_score")
    def _compute_passed(self):
        for rec in self:
            rec.passed = rec.state == "approved" and rec.final_score >= (rec.rubric_id.pass_score or 60.0)

    @api.onchange("channel_id")
    def _onchange_channel(self):
        if self.channel_id and not self.rubric_id:
            self.rubric_id = self.env["eduagent.rubric"].search([("channel_id", "=", self.channel_id.id)], limit=1)

    def action_ai_review(self):
        agent = self.env.ref("zivoit_eduagent_ai.agent_mentor", raise_if_not_found=False)
        for rec in self:
            criteria = rec.rubric_id.criterion_ids
            crit_txt = "\n".join("- %s (weight %s): %s" % (c.name, c.weight, c.description or "") for c in criteria) \
                or "- Correctness\n- Clarity\n- Completeness"
            body = rec.content or ""
            for att in rec.attachment_ids.filtered(lambda a: (a.mimetype or "").startswith("text")):
                body += "\n\n# %s\n%s" % (att.name, (att.raw or b"").decode("utf-8", "ignore")[:8000])
            system = ("You are a strict but kind project mentor. Grade the submission against the rubric. "
                      'Answer JSON: {"criteria":[{"name":"","score":0-10,"comment":""}],'
                      '"strengths":[""],"improvements":[""],"overall_feedback":"","suggested_score":0-100}')
            prompt = "ASSIGNMENT:\n%s\n\nRUBRIC:\n%s\n\nSUBMISSION:\n%s" % (
                rec.rubric_id.instructions or rec.name, crit_txt, body[:16000])
            mock = {"criteria": [{"name": c.name, "score": 7, "comment": "Solid work."} for c in criteria]
                    or [{"name": "Overall", "score": 7, "comment": "Solid work."}],
                    "strengths": ["Clear structure"], "improvements": ["Add tests and edge cases"],
                    "overall_feedback": "Good submission. Address the improvements to reach excellent.",
                    "suggested_score": 70}
            res = self.env["eduagent.llm"].complete(
                system, [{"role": "user", "content": prompt}], agent=agent, json_mode=True,
                purpose="Project review", partner=rec.partner_id, high_risk=True,
                mock_response=json.dumps(mock), max_tokens=1800)
            if not res["ok"]:
                raise UserError(res["text"])
            data = res["data"]
            rows = "".join("<tr><td>%s</td><td>%s/10</td><td>%s</td></tr>" % (
                c.get("name", ""), c.get("score", ""), c.get("comment", "")) for c in data.get("criteria", []))
            html = ("<p>%s</p><table class='table table-sm'><tr><th>Criterion</th><th>Score</th>"
                    "<th>Comment</th></tr>%s</table><p><b>%s</b></p><ul>%s</ul><p><b>%s</b></p><ul>%s</ul>"
                    "<p><i>%s</i></p>") % (
                data.get("overall_feedback", ""), rows, _("Strengths"),
                "".join("<li>%s</li>" % s for s in data.get("strengths", [])), _("To improve"),
                "".join("<li>%s</li>" % s for s in data.get("improvements", [])),
                _("AI recommendation only. A mentor confirms the final score."))
            try:
                score = float(data.get("suggested_score") or 0)
            except (TypeError, ValueError):
                score = 0.0
            rec.write({"ai_feedback": html, "ai_score": score, "state": "ai_reviewed",
                       "final_score": rec.final_score or score})
            user = rec.channel_id.user_id or self.env.user
            rec.activity_schedule("mail.mail_activity_data_todo", user_id=user.id,
                                  summary=_("Confirm AI-reviewed submission"))
        return True

    def action_approve(self):
        for rec in self:
            rec.write({"state": "approved", "reviewer_id": self.env.user.id})
            rec.activity_feedback(["mail.mail_activity_data_todo"])
            rec.message_post(body=_("Final score %s%% confirmed by %s.", rec.final_score, self.env.user.name),
                             partner_ids=rec.partner_id.ids)

    def action_return(self):
        for rec in self:
            rec.write({"state": "returned", "reviewer_id": self.env.user.id})
            rec.message_post(body=_("Returned for rework: %s", rec.review_note or ""),
                             partner_ids=rec.partner_id.ids)


class EduagentPracticeAttempt(models.Model):
    _name = "eduagent.practice.attempt"
    _description = "Practice Attempt (spaced repetition)"
    _order = "id desc"

    partner_id = fields.Many2one("res.partner", required=True, index=True, ondelete="cascade")
    channel_id = fields.Many2one("slide.channel", string="Course", index=True)
    topic = fields.Char(required=True, index=True)
    question = fields.Text()
    answer = fields.Text()
    score = fields.Float(help="0 = wrong, 1 = perfect")
    create_date = fields.Datetime(readonly=True)
    next_review = fields.Date(compute="_compute_next_review", store=True)

    @api.depends("score", "topic", "partner_id")
    def _compute_next_review(self):
        for rec in self:
            prior = self.search_count([("partner_id", "=", rec.partner_id.id), ("topic", "=", rec.topic),
                                       ("score", ">=", 0.7), ("id", "<", rec.id or 0)]) if rec.id else 0
            if rec.score < 0.7:
                days = 1
            else:
                days = REVIEW_INTERVALS[min(prior + 1, len(REVIEW_INTERVALS) - 1)]
            rec.next_review = fields.Date.today() + timedelta(days=days)

    @api.model
    def _topic_mastery(self, partner, channel=None):
        if not partner:
            return {}
        domain = [("partner_id", "=", partner.id)]
        if channel:
            domain.append(("channel_id", "=", channel.id))
        mastery = {}
        for att in self.search(domain, order="id asc", limit=500):
            prev = mastery.get(att.topic, 0.5)
            mastery[att.topic] = round(0.6 * prev + 0.4 * att.score, 3)  # exponential moving average
        return mastery

    @api.model
    def _due_topics(self, partner, channel=None):
        if not partner:
            return []
        domain = [("partner_id", "=", partner.id), ("next_review", "<=", fields.Date.today())]
        if channel:
            domain.append(("channel_id", "=", channel.id))
        latest = {}
        for att in self.search(domain, order="id desc", limit=200):
            latest.setdefault(att.topic, att)
        return list(latest)[:5]


class EduagentOralCheck(models.Model):
    _name = "eduagent.oral.check"
    _description = "Integrity Check (viva / explain-your-work)"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    name = fields.Char(compute="_compute_name", store=True)
    partner_id = fields.Many2one("res.partner", string="Learner", required=True)
    channel_id = fields.Many2one("slide.channel", string="Course", required=True)
    question = fields.Text(readonly=True)
    answer = fields.Text()
    started_at = fields.Datetime(readonly=True)
    answered_at = fields.Datetime(readonly=True)
    seconds_taken = fields.Integer(compute="_compute_seconds", store=True)
    paste_detected = fields.Boolean(help="Set by the portal when the answer was pasted rather than typed.")
    ai_assessment = fields.Html(readonly=True)
    ai_understanding = fields.Float(string="AI Understanding Estimate (%)", readonly=True)
    flag_ids = fields.Char(string="Flags", readonly=True)
    state = fields.Selection([("draft", "Question Issued"), ("answered", "Answered"),
                              ("pending_review", "Pending Human Review"), ("passed", "Passed"),
                              ("failed", "Not Passed")], default="draft", tracking=True)
    reviewer_id = fields.Many2one("res.users", readonly=True, tracking=True)

    @api.depends("partner_id", "channel_id")
    def _compute_name(self):
        for rec in self:
            rec.name = _("Integrity check - %(p)s - %(c)s", p=rec.partner_id.name or "", c=rec.channel_id.name or "")

    @api.depends("started_at", "answered_at")
    def _compute_seconds(self):
        for rec in self:
            rec.seconds_taken = int((rec.answered_at - rec.started_at).total_seconds()) \
                if rec.started_at and rec.answered_at else 0

    def action_issue_question(self):
        agent = self.env.ref("zivoit_eduagent_ai.agent_integrity", raise_if_not_found=False)
        Chunk = self.env["eduagent.knowledge.chunk"]
        for rec in self:
            chunks = Chunk.retrieve(rec.channel_id.name, [rec.channel_id.id], ["slide"], limit=3)
            ctx = Chunk.format_context(chunks)
            mock = _("In your own words, explain the most important concept from '%s' and give one "
                     "real example where you would apply it.", rec.channel_id.name)
            res = self.env["eduagent.llm"].complete(
                "Create ONE open 'explain in your own words' viva question that can only be answered "
                "well by someone who understood this course material. Prefer application to a new "
                "scenario. Return only the question text.",
                [{"role": "user", "content": ctx or rec.channel_id.name}], agent=agent,
                purpose="Integrity question", partner=rec.partner_id, mock_response=mock)
            rec.write({"question": res["text"] if res["ok"] else mock, "state": "draft",
                       "started_at": fields.Datetime.now(), "answer": False, "answered_at": False})
        return True

    def action_submit_answer(self, answer, pasted=False):
        self.ensure_one()
        self.write({"answer": (answer or "")[:6000], "answered_at": fields.Datetime.now(),
                    "paste_detected": bool(pasted), "state": "answered"})
        self.action_ai_assess()

    def action_ai_assess(self):
        agent = self.env.ref("zivoit_eduagent_ai.agent_integrity", raise_if_not_found=False)
        Chunk = self.env["eduagent.knowledge.chunk"]
        for rec in self:
            chunks = Chunk.retrieve(rec.question or "", [rec.channel_id.id], ["slide"], limit=3)
            system = ("Assess whether the answer shows genuine understanding. Signs of concern: generic "
                      "text not tied to the question, textbook phrasing, contradictions, no example. "
                      'Answer JSON {"understanding":0-100,"concerns":[""],"summary":""}. '
                      "This is decision SUPPORT only; a human decides.")
            prompt = "COURSE MATERIAL:\n%s\n\nQUESTION:\n%s\n\nANSWER:\n%s" % (
                Chunk.format_context(chunks), rec.question or "", rec.answer or "")
            mock = {"understanding": 65 if len(rec.answer or "") > 120 else 35,
                    "concerns": [] if len(rec.answer or "") > 120 else ["Answer is very short"],
                    "summary": "Automatic estimate in demo mode."}
            res = self.env["eduagent.llm"].complete(
                system, [{"role": "user", "content": prompt}], agent=agent, json_mode=True,
                purpose="Integrity assessment", partner=rec.partner_id, high_risk=True,
                mock_response=json.dumps(mock))
            data = res.get("data") or mock
            flags = list(data.get("concerns") or [])
            if rec.paste_detected:
                flags.append(_("Answer was pasted"))
            if rec.seconds_taken and rec.seconds_taken < 20 and len(rec.answer or "") > 300:
                flags.append(_("Very fast for answer length (%ss)", rec.seconds_taken))
            try:
                understanding = float(data.get("understanding") or 0)
            except (TypeError, ValueError):
                understanding = 0.0
            rec.write({
                "ai_understanding": understanding,
                "flag_ids": "; ".join(str(f) for f in flags)[:500],
                "ai_assessment": "<p>%s</p>%s<p><i>%s</i></p>" % (
                    data.get("summary", ""),
                    ("<ul>%s</ul>" % "".join("<li>%s</li>" % f for f in flags)) if flags else "",
                    _("Decision support only. A reviewer must pass or fail this check.")),
                "state": "pending_review",
            })
            user = rec.channel_id.user_id or self.env.ref("base.user_admin")
            rec.activity_schedule("mail.mail_activity_data_todo", user_id=user.id,
                                  summary=_("Review integrity check"))
        return True

    def action_pass(self):
        self.write({"state": "passed", "reviewer_id": self.env.user.id})
        self.activity_feedback(["mail.mail_activity_data_todo"])
        self.env["eduagent.credential"]._issue_for_completed(partners=self.partner_id, channels=self.channel_id)

    def action_fail(self):
        self.write({"state": "failed", "reviewer_id": self.env.user.id})
        self.activity_feedback(["mail.mail_activity_data_todo"])
