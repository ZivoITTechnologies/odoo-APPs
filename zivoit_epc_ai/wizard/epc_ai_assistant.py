import json
import re

from odoo import _, fields, models
from odoo.exceptions import UserError
from markupsafe import Markup

from odoo.tools import html_escape

INTENTS = [
    ("ld", ["liquidated", " ld", "ld ", "penalt"]),
    ("billing", ["bill", "invoice", "billing", "cash", "unbilled", " ra "]),
    ("material", ["material", "procure", "vendor", "supplier", "delivery", "purchase", " po", "conductor", "steel"]),
    ("document", ["document", "drawing", "afc", "engineering", "transmittal"]),
    ("cost", ["cost", "budget", "eac", "overrun", "spend", "cpi", "money", "margin"]),
    ("critical", ["critical", "float", "path"]),
    ("risk", ["risk", "threat", "issue", "worry", "concern"]),
    ("delay", ["delay", "late", "finish", "complete", "when", "slip", "schedule", "spi", "behind"]),
    ("progress", ["progress", "status", "summary", "how is", "overview", "update"]),
]


class EpcAiAssistant(models.TransientModel):
    _name = "epc.ai.assistant"
    _description = "EPC Project AI Assistant"

    project_id = fields.Many2one("project.project", required=True, domain=[("is_epc", "=", True)])
    question = fields.Text(required=True)
    answer = fields.Html(readonly=True, sanitize=True)
    source = fields.Selection([("llm", "AI model"), ("fallback", "Built-in engine")], readonly=True)

    def _detect_intents(self, question):
        text = " %s " % re.sub(r"\s+", " ", (question or "").lower())
        positions = {}
        for intent, words in INTENTS:
            hits = [text.find(w) for w in words if w in text]
            if hits:
                positions[intent] = min(hits)
        found = sorted(positions, key=lambda intent: positions[intent])
        return found[:3] or ["progress"]

    def _fallback_answer(self, ctx):
        sections = [self._fallback_section(intent, ctx) for intent in self._detect_intents(self.question)]
        return "".join(sections) + "<p><i>Answered by the ZivoIT EPC built-in engine.</i></p>"

    def _fallback_section(self, intent, ctx):
        project = self.project_id
        cur = ctx["currency"]
        e = html_escape

        def money(value):
            return "{:,.0f}".format(value or 0.0)

        def bullets(items):
            return "<ul>%s</ul>" % "".join("<li>%s</li>" % item for item in items) if items else ""

        if intent == "delay":
            items = ["%s %s: %s days late (%s%% done). %s" % (
                e(a["code"]), e(a["name"]), a["slip_days"], a["progress"], e(a["drivers"]))
                for a in ctx["late_activities"]]
            body = "<p>Predicted finish <b>%s</b> against baseline %s: <b>%s days</b> %s, " \
                   "delay probability %s%%. SPI is %s.</p>%s" % (
                       e(ctx["predicted_finish"] or "not yet predicted"), e(ctx["baseline_finish"] or "-"),
                       abs(ctx["predicted_delay_days"]), "late" if ctx["predicted_delay_days"] > 0 else "early or on time",
                       ctx["delay_probability_pct"], ctx["spi"], bullets(items))
        elif intent == "cost":
            body = "<p>Budget at completion %s %s. Forecast at completion P50 <b>%s</b>, P80 <b>%s</b>. " \
                   "CPI is %s (%s).</p>" % (
                       e(cur), money(ctx["bac"]), money(ctx["eac_p50"]), money(ctx["eac_p80"]), ctx["cpi"],
                       "spending more than earned" if ctx["cpi"] and ctx["cpi"] < 1 else "within budget so far")
        elif intent == "risk":
            items = ["[%s] %s%s" % (e(r["level"]), e(r["name"]),
                                    (" — mitigation: %s" % e(r["mitigation"])) if r["mitigation"] else "")
                     for r in ctx["top_risks"]]
            body = "<p>%s open risk(s), highest first:</p>%s" % (len(ctx["top_risks"]), bullets(items)) \
                if items else "<p>No open risks are recorded.</p>"
        elif intent == "critical":
            items = ["%s %s" % (e(a["code"]), e(a["name"])) for a in ctx["critical_open_activities"]]
            body = "<p>Open activities on the critical path:</p>%s" % bullets(items) if items \
                else "<p>No open critical activities. Run <i>Compute Schedule</i> to refresh float.</p>"
        elif intent == "material":
            items = ["%s %s from %s: forecast %s days late" % (
                e(m["po"]), e(m["item"]), e(m["vendor"]), m["delay_days"]) for m in ctx["materials_at_risk"]]
            vendors = project.sudo().epc_purchase_line_ids.mapped("partner_id.commercial_partner_id")
            vendor_items = ["%s: risk score %s, on-time %s%%" % (
                e(v.name), v.epc_supplier_risk_score, round(v.epc_on_time_rate)) for v in vendors]
            body = "<p>Materials at risk:</p>%s<p>Supplier scores:</p>%s" % (
                bullets(items) or "<p>None.</p>", bullets(vendor_items) or "<p>No vendors linked.</p>")
        elif intent == "document":
            items = ["%s %s" % (e(d["number"]), e(d["title"])) for d in ctx["late_documents"]]
            body = "<p>Documents past their planned AFC date:</p>%s" % bullets(items) if items \
                else "<p>No documents are overdue.</p>"
        elif intent == "billing":
            unbilled = sum(l.unbilled_qty * l.rate for l in project.epc_boq_line_ids)
            certified = sum(project.epc_ra_bill_ids.filtered(
                lambda b: b.state in ("certified", "invoiced")).mapped("gross_amount"))
            body = "<p>Certified to date: <b>%s %s</b>. Executed but not yet billed: <b>%s %s</b>.</p>" % (
                e(cur), money(certified), e(cur), money(unbilled))
        elif intent == "ld":
            body = "<p>LD terms: %s%% per week, cap %s%%. Exposure at the predicted delay: <b>%s %s</b>.</p>" % (
                project.epc_ld_rate_pct, project.epc_ld_cap_pct, e(cur), money(ctx["ld_exposure"]))
        else:
            body = "<p>Progress %s%% actual vs %s%% planned (SPI %s, CPI %s). Predicted finish %s " \
                   "(%s days vs baseline). %s open risk(s); %s material item(s) and %s document(s) at risk.</p>" % (
                       ctx["actual_progress_pct"], ctx["planned_progress_pct"], ctx["spi"], ctx["cpi"],
                       e(ctx["predicted_finish"] or "-"), ctx["predicted_delay_days"], len(ctx["top_risks"]),
                       len(ctx["materials_at_risk"]), len(ctx["late_documents"]))
        return body

    def action_ask(self):
        self.ensure_one()
        if not (self.question or "").strip():
            raise UserError(_("Type a question first."))
        ctx = self.project_id._epc_report_context()
        system = (
            "You are the project controls assistant for an EPC project. Answer the user's question using only "
            "the project facts provided. Be specific with numbers, dates and activity codes. If the facts do not "
            "contain the answer, say so. Answer in simple HTML (p, ul, li, b only), at most 200 words."
        )
        prompt = "Project facts (JSON):\n%s\n\nQuestion: %s" % (json.dumps(ctx, default=str), self.question)
        result = self.env["epc.ai.service"].ask(
            "project_assistant", system, prompt, lambda: self._fallback_answer(ctx), record=self.project_id)
        answer = result["result"]
        if result["source"] == "llm":
            answer = answer.replace("```html", "").replace("```", "")
        self.write({"answer": answer, "source": result["source"]})
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            "name": _("Project AI Assistant"),
        }

    def action_post_to_project(self):
        self.ensure_one()
        if not self.answer:
            raise UserError(_("Ask a question first."))
        self.project_id.message_post(
            body=Markup("<p><b>Q:</b> %s</p>%s") % (self.question, Markup(self.answer or "")))
        return {"type": "ir.actions.act_window_close"}
