# -*- coding: utf-8 -*-
"""Insights agent: management asks questions in plain language, answered from live KPIs."""
import json
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html_escape


class EduagentInsightsWizard(models.TransientModel):
    _name = "eduagent.insights.wizard"
    _description = "Ask the Insights Agent"

    question = fields.Text(required=True, default="Which courses have the highest dropout risk and what should we do this week?")
    period_days = fields.Integer(string="Look-back (days)", default=90)
    answer = fields.Html(readonly=True)
    kpi_json = fields.Text(readonly=True)

    @api.model
    def _collect_kpis(self, days=90):
        env = self.env
        since = fields.Datetime.now() - timedelta(days=days)
        Enrol = env["slide.channel.partner"].sudo()
        enrol_domain = [("create_date", ">=", since)]
        learners = ["|", ("partner_id.user_ids", "=", False), ("partner_id.user_ids.share", "=", True)]
        enrolments = Enrol.search(enrol_domain + learners)
        all_active = Enrol.search([("member_status", "in", ("joined", "ongoing"))] + learners)
        completed = enrolments.filtered(lambda e: e.member_status == "completed")
        per_course = {}
        for e in Enrol.search([("member_status", "in", ("joined", "ongoing", "completed"))] + learners, limit=20000):
            c = per_course.setdefault(e.channel_id.name, {"learners": 0, "completed": 0, "high_risk": 0,
                                                          "avg_completion": 0.0})
            c["learners"] += 1
            c["completed"] += 1 if e.member_status == "completed" else 0
            c["high_risk"] += 1 if e.eduagent_risk_level == "high" else 0
            c["avg_completion"] += e.completion or 0
        for c in per_course.values():
            c["avg_completion"] = round(c["avg_completion"] / max(c["learners"], 1), 1)
            c["completion_rate_pct"] = round(100.0 * c["completed"] / max(c["learners"], 1), 1)
        top = sorted(per_course.items(), key=lambda kv: kv[1]["learners"], reverse=True)[:15]

        Lead = env["crm.lead"].sudo().with_context(active_test=False)
        leads = Lead.search([("create_date", ">=", since)])
        ai_leads = leads.filtered("eduagent_conversation_id")
        won = leads.filtered(lambda l: l.stage_id.is_won) if "stage_id" in Lead._fields else Lead.browse()
        orders = env["sale.order"].sudo().search([("state", "=", "sale"), ("date_order", ">=", since)])
        Inst = env["eduagent.installment"].sudo()
        overdue = Inst.search([("state", "=", "overdue")])
        nudges = env["eduagent.nudge"].sudo().search([("create_date", ">=", since)])
        Log = env["eduagent.usage.log"].sudo()
        logs = Log.search_read([("create_date", ">=", since)], ["total_tokens", "cost", "status"])
        tickets = env["eduagent.support.ticket"].sudo().search([("create_date", ">=", since)])
        conv = env["eduagent.conversation"].sudo().search([("create_date", ">=", since)])
        return {
            "period_days": days,
            "new_enrolments": len(enrolments),
            "completed_in_period": len(completed),
            "active_learners": len(all_active),
            "risk_distribution": {lvl: len(all_active.filtered(lambda e, l=lvl: e.eduagent_risk_level == l))
                                  for lvl in ("low", "medium", "high")},
            "courses": dict(top),
            "leads": len(leads), "ai_captured_leads": len(ai_leads), "won_leads": len(won),
            "lead_to_win_pct": round(100.0 * len(won) / max(len(leads), 1), 1),
            "confirmed_orders": len(orders), "revenue": round(sum(orders.mapped("amount_total")), 2),
            "currency": env.company.currency_id.name,
            "overdue_instalments": len(overdue), "overdue_amount": round(sum(overdue.mapped("amount")), 2),
            "nudges_sent": len(nudges), "nudges_re_engaged": len(nudges.filtered("re_engaged")),
            "support_tickets": len(tickets),
            "ai_conversations": len(conv), "ai_human_handoffs": len(conv.filtered(lambda c: c.state == "human")),
            "ai_tokens": sum(l["total_tokens"] for l in logs), "ai_cost_usd": round(sum(l["cost"] for l in logs), 4),
            "credentials_issued": env["eduagent.credential"].sudo().search_count([("create_date", ">=", since)]),
        }

    def action_ask(self):
        self.ensure_one()
        agent = self.env.ref("zivoit_eduagent_ai.agent_insights", raise_if_not_found=False)
        kpis = self._collect_kpis(self.period_days or 90)
        mock = "<p>%s</p><ul>%s</ul>" % (
            _("Demo mode summary of your key numbers:"),
            "".join("<li><b>%s</b>: %s</li>" % (html_escape(k), html_escape(json.dumps(v)))
                    for k, v in kpis.items() if not isinstance(v, dict)))
        res = self.env["eduagent.llm"].complete(
            "You are the management analyst of an education company. Answer the question using ONLY "
            "the KPI JSON. Lead with the answer and its number, then 3-5 concrete actions for this week. "
            "Output clean HTML (p, ul, li, b, table). If the data cannot answer, say what is missing.",
            [{"role": "user", "content": "KPIS:\n%s\n\nQUESTION: %s" % (json.dumps(kpis), self.question)}],
            agent=agent, purpose="Insights", mock_response=mock, max_tokens=1500)
        if not res["ok"]:
            raise UserError(res["text"])
        text = res["text"]
        if text.strip().startswith("```"):
            text = text.strip().strip("`").replace("html\n", "", 1)
        self.write({"answer": text, "kpi_json": json.dumps(kpis, indent=2)})
        return {"type": "ir.actions.act_window", "name": _("Ask the Insights Agent"), "res_model": self._name, "res_id": self.id,
                "view_mode": "form", "target": "new"}
