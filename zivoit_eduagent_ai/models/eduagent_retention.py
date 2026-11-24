# -*- coding: utf-8 -*-
"""Retention Coach: dropout-risk scoring, personalised nudges and mentor escalation."""
import json
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.tools import html_escape

# staff (internal users) are auto-enrolled as course responsibles: they are not learners
LEARNER_DOMAIN = ["|", ("partner_id.user_ids", "=", False), ("partner_id.user_ids.share", "=", True)]


class SlideChannelPartner(models.Model):
    _inherit = "slide.channel.partner"

    eduagent_risk_score = fields.Float(string="Dropout Risk", readonly=True, aggregator="avg")
    eduagent_risk_level = fields.Selection([("low", "Low"), ("medium", "Medium"), ("high", "High")],
                                           string="Risk Level", readonly=True, index=True)
    eduagent_risk_reasons = fields.Text(string="Risk Reasons", readonly=True)
    eduagent_last_activity = fields.Datetime(string="Last Activity", readonly=True)
    eduagent_last_nudge = fields.Datetime(string="Last Nudge", readonly=True)
    eduagent_nudge_count = fields.Integer(string="Nudges Sent", readonly=True)
    eduagent_risk_date = fields.Datetime(string="Risk Computed On", readonly=True)

    def _eduagent_compute_risk(self):
        """Explainable heuristic score 0-100. Weights are tuned to be conservative."""
        SlidePartner = self.env["slide.slide.partner"].sudo()
        Installment = self.env["eduagent.installment"].sudo()
        now = fields.Datetime.now()
        for enr in self:
            company = enr.channel_id._eduagent_company() or self.env.company
            expected_days = max(company.eduagent_expected_course_days or 60, 1)
            sp = SlidePartner.search([("channel_id", "=", enr.channel_id.id),
                                      ("partner_id", "=", enr.partner_id.id)])
            dates = [d for d in sp.mapped("write_date") if d]
            last = max(dates) if dates else enr.create_date or now
            idle_days = max((now - last).days, 0)
            enrolled_days = max((now - (enr.create_date or now)).days, 0)
            expected_progress = min(100.0, 100.0 * enrolled_days / expected_days)
            gap = max(0.0, expected_progress - (enr.completion or 0))
            struggling = len(sp.filtered(lambda s: s.quiz_attempts_count >= 3 and not s.completed))
            overdue = Installment.search_count([("partner_id", "=", enr.partner_id.id),
                                                ("state", "=", "overdue")])
            reasons = []
            score = 0.0
            if idle_days >= 3:
                pts = min(40.0, idle_days * 40.0 / 21.0)
                score += pts
                reasons.append(_("Inactive for %s days", idle_days))
            if gap > 10:
                pts = min(30.0, gap * 0.4)
                score += pts
                reasons.append(_("%(c)s%% done vs %(e)s%% expected", c=enr.completion or 0,
                                 e=int(expected_progress)))
            if struggling:
                score += min(15.0, struggling * 7.5)
                reasons.append(_("Struggling with %s quiz(zes)", struggling))
            if overdue:
                score += 15.0
                reasons.append(_("Overdue fee instalment"))
            score = round(min(100.0, score), 1)
            level = "high" if score >= 65 else ("medium" if score >= 35 else "low")
            enr.sudo().write({
                "eduagent_risk_score": score, "eduagent_risk_level": level,
                "eduagent_risk_reasons": "\n".join(reasons) or _("On track"),
                "eduagent_last_activity": last, "eduagent_risk_date": now,
            })

    def _eduagent_send_nudge(self, force=False):
        agent = self.env.ref("zivoit_eduagent_ai.agent_retention", raise_if_not_found=False)
        Nudge = self.env["eduagent.nudge"].sudo()
        now = fields.Datetime.now()
        for enr in self:
            if not enr.partner_id.email:
                continue
            if not force and enr.eduagent_last_nudge and enr.eduagent_last_nudge > now - timedelta(days=5):
                continue
            next_slide = enr.channel_id.slide_ids.filtered(lambda s: not s.is_category)[:1]
            done = self.env["slide.slide.partner"].sudo().search([
                ("channel_id", "=", enr.channel_id.id), ("partner_id", "=", enr.partner_id.id),
                ("completed", "=", True)]).mapped("slide_id")
            pending = enr.channel_id.slide_ids.filtered(lambda s: not s.is_category and s not in done)
            next_slide = pending[:1] or next_slide
            facts = {
                "learner_first_name": (enr.partner_id.name or "").split(" ")[0],
                "course": enr.channel_id.name, "progress_percent": enr.completion,
                "risk_reasons": enr.eduagent_risk_reasons, "next_lesson": next_slide.name or "",
            }
            mock = {"subject": _("Your next step in %s", enr.channel_id.name),
                    "body": _("Hi %(n)s, you're %(p)s%% through %(c)s. Your next lesson, \"%(l)s\", takes "
                              "about 15 minutes. Shall we do it today?", n=facts["learner_first_name"],
                              p=enr.completion, c=enr.channel_id.name, l=facts["next_lesson"])}
            res = self.env["eduagent.llm"].complete(
                "Write a short, warm, specific re-engagement email (max 90 words) that removes friction: "
                "name ONE small next step. No guilt, no pressure. "
                'Answer JSON {"subject":"","body":""}.',
                [{"role": "user", "content": json.dumps(facts)}], agent=agent, tier="fast", json_mode=True,
                purpose="Retention nudge", partner=enr.partner_id, mock_response=json.dumps(mock))
            data = res.get("data") if res.get("ok") else mock
            subject = (data or {}).get("subject") or mock["subject"]
            body = (data or {}).get("body") or mock["body"]
            url = enr.channel_id.get_base_url() + (next_slide.website_url or enr.channel_id.website_url or "")
            html = "<p>%s</p><p><a href='%s'>%s</a></p><p style='color:#888;font-size:11px'>%s</p>" % (
                html_escape(body).replace("\n", "<br/>"), url, _("Continue learning"),
                html_escape(enr.channel_id._eduagent_company().eduagent_disclosure_text or ""))
            mail = self.env["mail.mail"].sudo().create({
                "subject": subject, "body_html": html, "email_to": enr.partner_id.email,
                "recipient_ids": [(4, enr.partner_id.id)], "auto_delete": False,
            })
            Nudge.create({"enrollment_id": enr.id, "partner_id": enr.partner_id.id,
                          "channel_id": enr.channel_id.id, "subject": subject, "body": body,
                          "risk_score": enr.eduagent_risk_score, "mail_id": mail.id})
            enr.sudo().write({"eduagent_last_nudge": now, "eduagent_nudge_count": enr.eduagent_nudge_count + 1})

    def _eduagent_escalate(self):
        for enr in self.filtered(lambda e: e.eduagent_risk_level == "high"):
            user = enr.channel_id.user_id or self.env.ref("base.user_admin")
            channel = enr.channel_id.sudo()
            summary = _("At-risk learner: %s", enr.partner_id.name)
            already = self.env["mail.activity"].sudo().search_count([
                ("res_model", "=", "slide.channel"), ("res_id", "=", channel.id), ("summary", "=", summary)])
            if not already:
                channel.activity_schedule("mail.mail_activity_data_todo", user_id=user.id, summary=summary,
                                          note=enr.eduagent_risk_reasons or "")

    def action_eduagent_nudge_now(self):
        self._eduagent_compute_risk()
        self._eduagent_send_nudge(force=True)

    def action_eduagent_recompute(self):
        self._eduagent_compute_risk()

    @api.model
    def _cron_eduagent_retention(self):
        enrolments = self.sudo().search([("member_status", "in", ("joined", "ongoing")),
                                         ("active", "=", True)] + LEARNER_DOMAIN, limit=5000)
        enrolments._eduagent_compute_risk()
        enrolments._eduagent_escalate()
        at_risk = enrolments.filtered(lambda e: e.eduagent_risk_level in ("medium", "high")
                                      and (e.channel_id._eduagent_company() or self.env.company).eduagent_auto_nudge)
        at_risk._eduagent_send_nudge()


class EduagentNudge(models.Model):
    _name = "eduagent.nudge"
    _description = "Retention Nudge"
    _order = "id desc"

    enrollment_id = fields.Many2one("slide.channel.partner", ondelete="set null")
    partner_id = fields.Many2one("res.partner", string="Learner")
    channel_id = fields.Many2one("slide.channel", string="Course")
    subject = fields.Char()
    body = fields.Text()
    risk_score = fields.Float()
    mail_id = fields.Many2one("mail.mail", ondelete="set null")
    create_date = fields.Datetime(string="Sent On", readonly=True)
    re_engaged = fields.Boolean(compute="_compute_re_engaged",
                                help="Learner completed a lesson within 7 days after the nudge.")

    def _compute_re_engaged(self):
        SP = self.env["slide.slide.partner"].sudo()
        for rec in self:
            if not rec.create_date:
                rec.re_engaged = False
                continue
            rec.re_engaged = bool(SP.search_count([
                ("partner_id", "=", rec.partner_id.id), ("channel_id", "=", rec.channel_id.id),
                ("completed", "=", True), ("write_date", ">=", rec.create_date),
                ("write_date", "<=", rec.create_date + timedelta(days=7))]))
