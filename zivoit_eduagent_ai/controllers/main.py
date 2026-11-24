# -*- coding: utf-8 -*-
import re
from urllib.parse import quote

from odoo import _, fields, http
from odoo.http import request
from odoo.tools import html_escape

LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+|/[^\s)]*)\)")
BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
CODE_RE = re.compile(r"`([^`]+)`")
HUB_ORDER = ["tutor", "practice", "support", "career", "fee"]


def md_to_html(text):
    """Very small, safe markdown subset: bold, inline code, links, bullet lists, paragraphs."""
    out, in_list = [], False
    for line in (text or "").split("\n"):
        esc = str(html_escape(line))
        esc = LINK_RE.sub(lambda m: '<a href="%s" target="_blank" rel="noopener">%s</a>' % (m.group(2), m.group(1)), esc)
        esc = BOLD_RE.sub(r"<b>\1</b>", esc)
        esc = CODE_RE.sub(r"<code>\1</code>", esc)
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>%s</li>" % esc.strip()[2:])
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if stripped:
            out.append("<p>%s</p>" % esc)
    if in_list:
        out.append("</ul>")
    return "".join(out)


def _id_from_slug(value):
    match = re.search(r"(\d+)$", str(value or ""))
    return int(match.group(1)) if match else False


class EduagentController(http.Controller):

    # ------------------------------------------------------------------ chat API used by the widget
    @http.route("/eduagent/chat", type="http", auth="public", methods=["POST"], website=True, sitemap=False)
    def chat(self, **kw):
        data = request.get_json_data() or {}
        code = data.get("agent") or "admissions"
        message = (data.get("message") or "").strip()
        user = request.env.user
        is_public = user._is_public()
        Agent = request.env["eduagent.agent"].sudo()
        agent = Agent.search([("code", "=", code), ("active", "=", True)], limit=1)
        if not agent:
            return request.make_json_response({"error": _("This assistant is not available.")}, status=404)
        if is_public and not agent.public_access:
            return request.make_json_response({
                "error": "login",
                "message": _("Please sign in to chat with the %s.", agent.name),
                "login_url": "/web/login?redirect=%s" % quote(request.httprequest.referrer or "/my/eduagent")})
        if not is_public and not (agent.portal_access or agent.public_access):
            return request.make_json_response({"error": _("This assistant is not available.")}, status=403)

        partner = user.partner_id
        Conv = request.env["eduagent.conversation"].sudo()
        conv = Conv.browse()
        token = data.get("token")
        if token:
            conv = Conv.search([("token", "=", token), ("agent_id", "=", agent.id)], limit=1)
            if conv and not is_public and conv.partner_id != partner:
                if conv.partner_id._is_public_eduagent():
                    conv.partner_id = partner  # visitor signed in: attach conversation
                else:
                    conv = Conv.browse()
        channel = request.env["slide.channel"].sudo()
        slide = request.env["slide.slide"].sudo()
        if data.get("slide_id"):
            slide = slide.browse(_id_from_slug(data["slide_id"])).exists()
            channel = slide.channel_id
        elif data.get("channel_id"):
            channel = channel.browse(_id_from_slug(data["channel_id"])).exists()
        if channel and code == "tutor":
            if not channel.eduagent_tutor_enabled:
                return request.make_json_response({"error": _("The AI tutor is disabled for this course.")})
            enrolled = request.env["slide.channel.partner"].sudo().search_count(
                [("channel_id", "=", channel.id), ("partner_id", "=", partner.id)])
            if not enrolled and not user._is_internal():
                return request.make_json_response({"error": _("Join the course to use its AI tutor.")})
        if not conv:
            conv = Conv.create({
                "agent_id": agent.id, "partner_id": partner.id,
                "channel_id": channel.id or False, "slide_id": slide.id or False,
                "source": "portal" if not is_public else "website",
                "company_id": request.website.company_id.id if request.website else request.env.company.id,
            })
        if not message:
            welcome = agent.welcome_message or _("Hi! How can I help?")
            return request.make_json_response({
                "token": conv.token, "reply_html": md_to_html(welcome), "history": self._history(conv),
                "disclosure": request.env.company.eduagent_disclosure_text or ""})
        result = agent.with_company(conv.company_id or request.env.company).run(conv, message)
        return request.make_json_response({
            "token": conv.token,
            "reply_html": md_to_html(result["reply"]),
            "sources": result.get("sources", []),
            "human": bool(result.get("human")),
            "actions": [a.get("type") for a in result.get("actions", [])],
        })

    def _history(self, conv):
        return [{"role": m.role, "html": md_to_html(m.content)}
                for m in conv.message_ids_eduagent.sorted("id")[-30:]]

    # ------------------------------------------------------------------ public admissions page
    @http.route("/eduagent", type="http", auth="public", website=True, sitemap=True)
    def admissions_page(self, **kw):
        return request.render("zivoit_eduagent_ai.admissions_page", {
            "disclosure": request.env.company.eduagent_disclosure_text})

    # ------------------------------------------------------------------ learner hub
    @http.route("/my/eduagent", type="http", auth="user", website=True)
    def learner_hub(self, **kw):
        env = request.env
        partner = env.user.partner_id
        Enrol = env["slide.channel.partner"].sudo()
        enrolments = Enrol.search([("partner_id", "=", partner.id),
                                   ("member_status", "in", ("joined", "ongoing", "completed"))])
        mastery = env["eduagent.practice.attempt"].sudo()._topic_mastery(partner)
        values = {
            "page_name": "eduagent_hub",
            "enrolments": enrolments,
            "credentials": env["eduagent.credential"].sudo().search([("partner_id", "=", partner.id)]),
            "installments": env["eduagent.installment"].sudo().search(
                [("partner_id", "child_of", partner.commercial_partner_id.id)], order="due_date"),
            "submissions": env["eduagent.submission"].sudo().search([("partner_id", "=", partner.id)], limit=20),
            "oral_checks": env["eduagent.oral.check"].sudo().search([("partner_id", "=", partner.id)], limit=10),
            "mastery": sorted(mastery.items(), key=lambda kv: kv[1])[:8],
            "agents": env["eduagent.agent"].sudo().search([("portal_access", "=", True),
                                                          ("code", "in", HUB_ORDER)]).sorted(
                lambda a: HUB_ORDER.index(a.code)),
            "disclosure": env.company.eduagent_disclosure_text,
            "message": kw.get("message"),
        }
        return request.render("zivoit_eduagent_ai.learner_hub", values)

    @http.route("/my/eduagent/submit", type="http", auth="user", methods=["POST"], website=True)
    def submit_project(self, **post):
        env = request.env
        partner = env.user.partner_id
        channel_id = _id_from_slug(post.get("channel_id"))
        enrolled = env["slide.channel.partner"].sudo().search(
            [("partner_id", "=", partner.id), ("channel_id", "=", channel_id)], limit=1)
        if not enrolled or not (post.get("content") or "").strip():
            return request.redirect("/my/eduagent?message=%s" % quote(_("Choose one of your courses and add your work.")))
        sub = env["eduagent.submission"].sudo().create({
            "name": (post.get("title") or _("Project submission"))[:120],
            "partner_id": partner.id, "channel_id": channel_id,
            "rubric_id": env["eduagent.rubric"].sudo().search([("channel_id", "=", channel_id)], limit=1).id,
            "content": post.get("content")[:50000],
        })
        sub.action_ai_review()
        return request.redirect("/my/eduagent?message=%s#projects" % quote(_(
            "Submitted. AI feedback is ready; your mentor will confirm the score.")))

    @http.route("/my/eduagent/oral/request", type="http", auth="user", methods=["POST"], website=True)
    def oral_request(self, **post):
        env = request.env
        partner = env.user.partner_id
        channel_id = _id_from_slug(post.get("channel_id"))
        enrolled = env["slide.channel.partner"].sudo().search(
            [("partner_id", "=", partner.id), ("channel_id", "=", channel_id)], limit=1)
        if not enrolled:
            return request.redirect("/my/eduagent")
        check = env["eduagent.oral.check"].sudo().create({"partner_id": partner.id, "channel_id": channel_id})
        check.action_issue_question()
        return request.redirect("/my/eduagent/oral/%s" % check.id)

    @http.route("/my/eduagent/oral/<int:check_id>", type="http", auth="user", methods=["GET", "POST"], website=True)
    def oral_answer(self, check_id, **post):
        check = request.env["eduagent.oral.check"].sudo().browse(check_id).exists()
        if not check or check.partner_id != request.env.user.partner_id:
            raise request.not_found()
        if request.httprequest.method == "POST" and check.state == "draft":
            check.action_submit_answer(post.get("answer"), pasted=post.get("pasted") == "1")
            return request.redirect("/my/eduagent?message=%s" % quote(_(
                "Answer received. A reviewer will confirm the result.")))
        return request.render("zivoit_eduagent_ai.oral_check_page", {"check": check, "page_name": "eduagent_hub"})

    # ------------------------------------------------------------------ credential verification
    @http.route("/eduagent/verify/<string:code>", type="http", auth="public", website=True, sitemap=False)
    def verify(self, code, **kw):
        cred = request.env["eduagent.credential"].sudo().search([("code", "=", (code or "").upper())], limit=1)
        return request.render("zivoit_eduagent_ai.verify_page", {
            "cred": cred, "authentic": bool(cred) and cred.is_authentic(), "code": code})

    @http.route("/eduagent/verify", type="http", auth="public", website=True, sitemap=False)
    def verify_form(self, code=None, **kw):
        if code:
            return request.redirect("/eduagent/verify/%s" % re.sub(r"[^A-Za-z0-9]", "", code))
        return request.render("zivoit_eduagent_ai.verify_page", {"cred": False, "authentic": False, "code": ""})

    # ------------------------------------------------------------------ feedback on AI answers
    @http.route("/eduagent/feedback", type="http", auth="public", methods=["POST"], website=True, sitemap=False)
    def feedback(self, **kw):
        data = request.get_json_data() or {}
        conv = request.env["eduagent.conversation"].sudo().search([("token", "=", data.get("token") or "-")], limit=1)
        if conv:
            last = conv.message_ids_eduagent.filtered(lambda m: m.role == "assistant").sorted("id")[-1:]
            if last and data.get("value") in ("up", "down"):
                last.feedback = data["value"]
        return request.make_json_response({"ok": True})
