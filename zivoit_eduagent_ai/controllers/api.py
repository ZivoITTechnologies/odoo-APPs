# -*- coding: utf-8 -*-
"""REST API for external agents, BI tools and MCP connectors (e.g. ZivoIT AnythingMCP).

Authentication: ``Authorization: Bearer <key>`` created under EduAgent AI > Configuration > API Keys.
"""
from odoo import http
from odoo.http import request

from .main import md_to_html


class EduagentApi(http.Controller):

    def _auth(self, scope="read"):
        header = request.httprequest.headers.get("Authorization", "")
        raw = header[7:].strip() if header.lower().startswith("bearer ") else ""
        key = request.env["eduagent.api.key"].sudo()._authenticate(raw)
        if not key or (scope == "chat" and key.scope != "chat"):
            return None
        return key

    def _deny(self):
        return request.make_json_response({"error": "unauthorized"}, status=401)

    @http.route("/eduagent/api/v1/kpis", type="http", auth="public", methods=["GET"], csrf=False, sitemap=False)
    def kpis(self, days=90, **kw):
        key = self._auth()
        if not key:
            return self._deny()
        env = request.env(user=key.user_id.id)
        try:
            days = max(1, min(int(days), 730))
        except (TypeError, ValueError):
            days = 90
        return request.make_json_response(env["eduagent.insights.wizard"]._collect_kpis(days))

    @http.route("/eduagent/api/v1/learners/at-risk", type="http", auth="public", methods=["GET"],
                csrf=False, sitemap=False)
    def at_risk(self, level="high", limit=100, **kw):
        key = self._auth()
        if not key:
            return self._deny()
        env = request.env(user=key.user_id.id)
        levels = ["high"] if level == "high" else ["medium", "high"]
        try:
            limit = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            limit = 100
        rows = env["slide.channel.partner"].search(
            [("eduagent_risk_level", "in", levels), ("member_status", "in", ("joined", "ongoing")),
             "|", ("partner_id.user_ids", "=", False), ("partner_id.user_ids.share", "=", True)], order="eduagent_risk_score desc", limit=limit)
        return request.make_json_response([{
            "learner": r.partner_id.name, "email": r.partner_id.email, "course": r.channel_id.name,
            "completion": r.completion, "risk_score": r.eduagent_risk_score, "risk_level": r.eduagent_risk_level,
            "reasons": (r.eduagent_risk_reasons or "").split("\n"),
            "last_activity": r.eduagent_last_activity and r.eduagent_last_activity.isoformat(),
        } for r in rows])

    @http.route("/eduagent/api/v1/chat", type="http", auth="public", methods=["POST"], csrf=False, sitemap=False)
    def chat(self, **kw):
        key = self._auth("chat")
        if not key:
            return self._deny()
        env = request.env(user=key.user_id.id)
        data = request.get_json_data() or {}
        agent = env["eduagent.agent"].sudo().search([("code", "=", data.get("agent") or "support"),
                                                      ("active", "=", True)], limit=1)
        if not agent or not (data.get("message") or "").strip():
            return request.make_json_response({"error": "agent and message are required"}, status=400)
        Conv = env["eduagent.conversation"].sudo()
        conv = Conv.search([("token", "=", data.get("conversation_token") or "-"),
                            ("agent_id", "=", agent.id)], limit=1)
        if not conv:
            partner = env["res.partner"]
            if data.get("partner_email"):
                partner = env["res.partner"].sudo().search([("email", "=ilike", data["partner_email"])], limit=1)
            conv = Conv.create({"agent_id": agent.id, "partner_id": partner.id or key.user_id.partner_id.id,
                                "source": "api"})
        result = agent.run(conv, data["message"])
        return request.make_json_response({
            "conversation_token": conv.token, "reply": result["reply"], "reply_html": md_to_html(result["reply"]),
            "actions": result.get("actions", []), "sources": result.get("sources", [])})
