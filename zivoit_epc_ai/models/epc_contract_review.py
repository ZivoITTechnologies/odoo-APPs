import base64
import io
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

CLAUSE_TYPES = [
    ("liquidated_damages", "Liquidated damages"),
    ("payment", "Payment terms"),
    ("retention", "Retention"),
    ("price_variation", "Price variation / escalation"),
    ("performance_security", "Performance security / bank guarantee"),
    ("defect_liability", "Defect liability / warranty"),
    ("termination", "Termination"),
    ("indemnity", "Indemnity"),
    ("limitation_of_liability", "Limitation of liability"),
    ("consequential_damages", "Consequential damages"),
    ("force_majeure", "Force majeure"),
    ("dispute_resolution", "Dispute resolution"),
    ("other", "Other"),
]
CLAUSE_RISK = [("low", "Low"), ("medium", "Medium"), ("high", "High")]
RISK_RANK = {"low": 1, "medium": 2, "high": 3}


def _sentences(text):
    flat = re.sub(r"\s+", " ", text or "")
    return [s.strip() for s in re.split(r"(?<=[.;])\s+(?=[A-Z0-9(])", flat) if s.strip()]


def _pct(text):
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|per\s*cent|percent)", text, flags=re.IGNORECASE)
    return float(match.group(1)) if match else None


class EpcContractReview(models.Model):
    _name = "epc.contract.review"
    _description = "EPC Contract Risk Review"
    _inherit = ["mail.thread"]
    _order = "id desc"

    name = fields.Char(required=True, default=lambda self: _("Contract review"))
    project_id = fields.Many2one("project.project", index=True, ondelete="cascade",
                                 domain=[("is_epc", "=", True)])
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company, required=True)
    contract_file = fields.Binary(string="Contract (PDF or text)", attachment=True)
    contract_filename = fields.Char()
    contract_text = fields.Text(help="Paste the contract text, or upload a PDF/TXT and press Analyse.")
    clause_ids = fields.One2many("epc.contract.clause", "review_id", string="Clauses", copy=False)
    summary = fields.Html(sanitize=True, copy=False)
    ld_rate_pct_week = fields.Float(string="LD % per week", digits=(5, 3), copy=False)
    ld_cap_pct = fields.Float(string="LD cap %", copy=False)
    retention_pct = fields.Float(string="Retention %", copy=False)
    payment_days = fields.Integer(string="Payment term (days)", copy=False)
    overall_risk = fields.Selection(CLAUSE_RISK, compute="_compute_overall_risk", store=True)
    high_risk_count = fields.Integer(compute="_compute_overall_risk", store=True)
    state = fields.Selection([("draft", "Draft"), ("analysed", "Analysed")], default="draft", copy=False)
    ai_source = fields.Selection([("llm", "AI model"), ("fallback", "Built-in engine")], string="AI Source", readonly=True, copy=False)

    @api.depends("clause_ids.risk_level")
    def _compute_overall_risk(self):
        for review in self:
            levels = review.clause_ids.mapped("risk_level")
            review.high_risk_count = levels.count("high")
            review.overall_risk = max(levels, key=lambda l: RISK_RANK[l]) if levels else False

    # ------------------------------------------------------------------
    def _extract_text_from_file(self):
        self.ensure_one()
        if not self.contract_file:
            return ""
        raw = base64.b64decode(self.contract_file)
        name = (self.contract_filename or "").lower()
        if name.endswith(".pdf") or raw[:4] == b"%PDF":
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise UserError(_("The pypdf library is required to read PDF contracts.")) from exc
            try:
                reader = PdfReader(io.BytesIO(raw))
                return "\n".join((page.extract_text() or "") for page in reader.pages)
            except Exception as exc:
                raise UserError(_("Could not read the PDF: %s", exc)) from exc
        for encoding in ("utf-8", "latin-1"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return ""

    @api.model
    def _fallback_analyse(self, text):
        """Keyword and pattern based clause extraction."""
        sentences = _sentences(text)
        lowered_all = (text or "").lower()
        clauses = []
        result = {"clauses": clauses, "ld_rate_pct_week": None, "ld_cap_pct": None,
                  "retention_pct": None, "payment_days": None}

        def find(*keywords):
            return [s for s in sentences if any(k in s.lower() for k in keywords)]

        # Liquidated damages
        for sentence in find("liquidated damage", "delay damages", " ld "):
            low = sentence.lower()
            rate = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|per\s*cent|percent)[^.;]*?(?:per|for each|for every|every)\s*"
                             r"(?:completed\s+)?(day|week|month)", low)
            if rate and result["ld_rate_pct_week"] is None:
                value = float(rate.group(1))
                factor = {"day": 7.0, "week": 1.0, "month": 12.0 / 52.0}[rate.group(2)]
                result["ld_rate_pct_week"] = round(value * factor, 3)
            cap = re.search(r"(?:maximum|cap|not exceed|limited to|ceiling|subject to a max)[^.;%]*?"
                            r"(\d+(?:\.\d+)?)\s*(?:%|per\s*cent|percent)", low)
            if cap and result["ld_cap_pct"] is None:
                result["ld_cap_pct"] = float(cap.group(1))
        ld_sentences = find("liquidated damage", "delay damages")
        if ld_sentences:
            rate = result["ld_rate_pct_week"] or 0.0
            cap = result["ld_cap_pct"]
            risk = "high" if rate >= 1.0 or (cap or 0) >= 10 or cap is None else "medium"
            clauses.append({
                "type": "liquidated_damages", "excerpt": " ".join(ld_sentences)[:1000],
                "value": "%s%% per week%s" % (rate, (", cap %s%%" % cap) if cap is not None else ", no cap found"),
                "risk_level": risk,
                "comment": "LD rate and cap drive the cost of every week of delay." if cap is not None
                else "No LD cap was found: exposure is uncapped.",
            })

        # Retention
        retention = find("retention")
        if retention:
            pct = _pct(" ".join(retention))
            result["retention_pct"] = pct
            clauses.append({
                "type": "retention", "excerpt": " ".join(retention)[:1000],
                "value": "%s%%" % pct if pct is not None else "",
                "risk_level": "high" if (pct or 0) > 10 else "medium" if (pct or 0) > 5 else "low",
                "comment": "Retention ties up working capital until defect liability ends.",
            })

        # Payment
        payment = find("payment", "paid within", "invoice")
        days_values = []
        for sentence in payment:
            days_values += [int(d) for d in re.findall(r"(\d+)\s*(?:\(\w+\)\s*)?(?:calendar\s+|working\s+)?days", sentence.lower())]
        if payment:
            days = max(days_values) if days_values else None
            result["payment_days"] = days
            clauses.append({
                "type": "payment", "excerpt": " ".join(payment[:3])[:1000],
                "value": "%s days" % days if days else "",
                "risk_level": "high" if (days or 0) > 60 else "medium" if (days or 0) > 30 or days is None else "low",
                "comment": "Long payment cycles strain cash flow." if (days or 0) > 30 else "Payment cycle acceptable.",
            })

        # Price variation
        pv = find("price variation", "escalation", "price adjustment", "pvc")
        if pv:
            firm = any("firm" in s.lower() and "no" in s.lower() for s in pv)
            clauses.append({
                "type": "price_variation", "excerpt": " ".join(pv)[:1000], "value": "",
                "risk_level": "high" if firm else "low",
                "comment": "Prices appear firm with no escalation." if firm else "Price variation clause present.",
            })
        else:
            clauses.append({
                "type": "price_variation", "excerpt": "", "value": "not found", "risk_level": "high",
                "comment": "No price variation clause found: commodity price rises are at the contractor's risk.",
            })

        # Performance security
        perf = find("performance bank guarantee", "performance guarantee", "performance security", "contract performance guarantee")
        if perf:
            pct = _pct(" ".join(perf))
            clauses.append({
                "type": "performance_security", "excerpt": " ".join(perf)[:1000],
                "value": "%s%%" % pct if pct is not None else "",
                "risk_level": "high" if (pct or 0) > 10 else "medium",
                "comment": "Bank guarantee limits consume credit lines.",
            })

        # Defect liability
        dlp = find("defect liability", "defects liability", "warranty period", "guarantee period")
        if dlp:
            months = re.findall(r"(\d+)\s*(?:\(\w+\)\s*)?months", " ".join(dlp).lower())
            value = max(int(m) for m in months) if months else None
            clauses.append({
                "type": "defect_liability", "excerpt": " ".join(dlp)[:1000],
                "value": "%s months" % value if value else "",
                "risk_level": "high" if (value or 0) > 24 else "medium" if (value or 0) > 12 else "low",
                "comment": "Longer defect liability extends retention and warranty cost.",
            })

        # Termination
        term = find("terminat")
        if term:
            convenience = any("convenience" in s.lower() for s in term)
            clauses.append({
                "type": "termination", "excerpt": " ".join(term[:3])[:1000], "value": "",
                "risk_level": "high" if convenience else "medium",
                "comment": "Owner may terminate for convenience." if convenience else "Termination for default only.",
            })

        # Indemnity
        indemnity = find("indemnif")
        if indemnity:
            broad = any(w in " ".join(indemnity).lower() for w in ("any and all", "all claims", "unlimited", "whatsoever"))
            clauses.append({
                "type": "indemnity", "excerpt": " ".join(indemnity[:3])[:1000], "value": "",
                "risk_level": "high" if broad else "medium",
                "comment": "Broad indemnity wording." if broad else "Indemnity present; check carve-outs.",
            })

        # Limitation of liability
        lol = find("limitation of liability", "aggregate liability", "total liability", "liability shall not exceed")
        clauses.append({
            "type": "limitation_of_liability", "excerpt": " ".join(lol[:2])[:1000],
            "value": "present" if lol else "not found",
            "risk_level": "low" if lol else "high",
            "comment": "Liability is capped." if lol else "No overall liability cap found.",
        })

        # Consequential damages
        cons = find("consequential", "indirect loss", "loss of profit")
        cons_text = " ".join(cons).lower()
        if cons and ("exclu" in cons_text or "not be liable" in cons_text):
            cons_value = "excluded"
        elif cons:
            cons_value = "mentioned"
        else:
            cons_value = "not found"
        clauses.append({
            "type": "consequential_damages", "excerpt": " ".join(cons[:2])[:1000],
            "value": cons_value,
            "risk_level": "low" if cons else "medium",
            "comment": "Exclusion of consequential loss found." if cons else "No exclusion of consequential loss found.",
        })

        # Force majeure
        fm = find("force majeure")
        clauses.append({
            "type": "force_majeure", "excerpt": " ".join(fm[:2])[:1000],
            "value": "present" if fm else "not found",
            "risk_level": "low" if fm else "high",
            "comment": "Force majeure relief available." if fm else "No force majeure clause found.",
        })

        # Dispute resolution
        dispute = find("arbitration", "dispute")
        if dispute:
            clauses.append({
                "type": "dispute_resolution", "excerpt": " ".join(dispute[:2])[:1000],
                "value": "arbitration" if "arbitration" in lowered_all else "",
                "risk_level": "low" if "arbitration" in lowered_all else "medium",
                "comment": "Arbitration route available." if "arbitration" in lowered_all else "Check dispute forum.",
            })

        highs = [c for c in clauses if c["risk_level"] == "high"]
        result["summary"] = (
            "%s clauses reviewed; %s high-risk item(s): %s." % (
                len(clauses), len(highs),
                ", ".join(dict(CLAUSE_TYPES)[c["type"]] for c in highs) or "none"))
        return result

    def action_analyse(self):
        for review in self:
            text = review.contract_text or ""
            if review.contract_file and not text.strip():
                text = review._extract_text_from_file()
                review.contract_text = text
            if not text.strip():
                raise UserError(_("Paste the contract text or upload a PDF/TXT file first."))
            system = (
                "You are an EPC contracts manager reviewing a construction contract for the contractor. "
                "Return JSON only with keys: clauses [{type, excerpt, value, risk_level, comment}], "
                "ld_rate_pct_week (number or null), ld_cap_pct (number or null), retention_pct (number or null), "
                "payment_days (integer or null), summary (2-3 sentences). Allowed types: %s. "
                "risk_level is low, medium or high from the contractor's point of view. Excerpts must be quoted "
                "from the contract. Flag important clauses that are missing with excerpt empty and value 'not found'."
                % ", ".join(dict(CLAUSE_TYPES)))
            answer = self.env["epc.ai.service"].ask(
                "contract_review", system, "Contract text:\n" + text[:60000],
                lambda: review._fallback_analyse(text), record=review, expect_json=True, max_tokens=3000)
            data = answer["result"] if isinstance(answer["result"], dict) else {}
            review._apply_analysis(data)
            review.ai_source = answer["source"]
        return True

    def _apply_analysis(self, data):
        self.ensure_one()
        valid_types = dict(CLAUSE_TYPES)
        commands = [(5, 0, 0)]
        for clause in data.get("clauses") or []:
            ctype = clause.get("type") if clause.get("type") in valid_types else "other"
            risk = clause.get("risk_level") if clause.get("risk_level") in RISK_RANK else "medium"
            commands.append((0, 0, {
                "clause_type": ctype,
                "excerpt": (clause.get("excerpt") or "")[:4000],
                "value": str(clause.get("value") or "")[:120],
                "risk_level": risk,
                "comment": (clause.get("comment") or "")[:1000],
            }))

        def number(key):
            try:
                return float(data.get(key)) if data.get(key) not in (None, "") else 0.0
            except (TypeError, ValueError):
                return 0.0

        summary = str(data.get("summary") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.write({
            "clause_ids": commands,
            "ld_rate_pct_week": number("ld_rate_pct_week"),
            "ld_cap_pct": number("ld_cap_pct"),
            "retention_pct": number("retention_pct"),
            "payment_days": int(number("payment_days")),
            "summary": "<p>%s</p>" % summary,
            "state": "analysed",
        })

    def action_apply_to_project(self):
        for review in self:
            if not review.project_id:
                raise UserError(_("Link the review to an EPC project first."))
            vals = {}
            if review.ld_rate_pct_week:
                vals["epc_ld_rate_pct"] = review.ld_rate_pct_week
            if review.ld_cap_pct:
                vals["epc_ld_cap_pct"] = review.ld_cap_pct
            if review.retention_pct:
                vals["epc_retention_pct"] = review.retention_pct
            if vals:
                review.project_id.write(vals)
            Risk = self.env["epc.risk"]
            for clause in review.clause_ids.filtered(lambda c: c.risk_level == "high"):
                key = "contract:%s:%s" % (review.id, clause.clause_type)
                if Risk.search_count([("engine_key", "=", key)]):
                    continue
                Risk.create({
                    "name": _("Contract: %s", dict(CLAUSE_TYPES)[clause.clause_type]),
                    "project_id": review.project_id.id,
                    "category": "contract",
                    "probability": "3",
                    "impact": "4",
                    "description": "%s\n\n%s" % (clause.comment or "", clause.excerpt or ""),
                    "source": "contract",
                    "engine_key": key,
                })
            review.message_post(body=_("Contract terms applied to project %s.", review.project_id.name))
        return True


class EpcContractClause(models.Model):
    _name = "epc.contract.clause"
    _description = "EPC Contract Clause"
    _order = "review_id, risk_order, id"

    review_id = fields.Many2one("epc.contract.review", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="review_id.company_id", store=True)
    clause_type = fields.Selection(CLAUSE_TYPES, string="Clause", required=True)
    excerpt = fields.Text()
    value = fields.Char()
    risk_level = fields.Selection(CLAUSE_RISK, string="Risk", required=True, default="medium")
    risk_order = fields.Integer(compute="_compute_risk_order", store=True)
    comment = fields.Text(string="Assessment")

    @api.depends("risk_level")
    def _compute_risk_order(self):
        for clause in self:
            clause.risk_order = 4 - RISK_RANK.get(clause.risk_level, 2)
