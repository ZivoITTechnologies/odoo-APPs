import json
import re

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

HINDRANCE_TYPES = [
    ("weather", "Weather"),
    ("material", "Material shortage / late delivery"),
    ("drawing", "Drawing / engineering hold"),
    ("manpower", "Manpower shortage"),
    ("equipment", "Equipment breakdown"),
    ("right_of_way", "Right of way / land / local issue"),
    ("permit", "Permit / shutdown / clearance"),
    ("client", "Client / PMC hold"),
    ("other", "Other"),
]

WEATHER = [
    ("clear", "Clear"),
    ("cloudy", "Cloudy"),
    ("rain", "Rain"),
    ("heavy_rain", "Heavy rain"),
    ("heat", "Extreme heat"),
    ("wind", "High wind"),
    ("fog", "Fog"),
]

HINDRANCE_KEYWORDS = [
    ("right_of_way", ["right of way", "row issue", "row problem", "villager", "farmer", "compensation",
                      "land owner", "landowner", "local issue", "local resistance", "crop"]),
    ("weather", ["rain", "storm", "wind", "heat", "fog", "lightning", "flood"]),
    ("material", ["material", "shortage", "not received", "awaiting delivery", "awaiting supply", "short supply",
                  "stock out", "delivery pending", "not delivered"]),
    ("drawing", ["drawing", "design hold", "engineering hold", "revision awaited", "afc"]),
    ("equipment", ["breakdown", "crane down", "machine down", "equipment failure", "repair", "out of order"]),
    ("permit", ["permit", "shutdown", "clearance", "outage", "line clear"]),
    ("client", ["client", "pmc", "inspection pending", "approval pending", "hold by"]),
    ("manpower", ["manpower shortage", "labour shortage", "labor shortage", "absent", "strike"]),
]

WEATHER_KEYWORDS = [
    ("heavy_rain", ["heavy rain", "heavy rains", "downpour"]),
    ("rain", ["rain", "drizzle", "shower"]),
    ("wind", ["high wind", "strong wind", "windy", "storm"]),
    ("heat", ["extreme heat", "heat wave", "heatwave"]),
    ("fog", ["fog", "foggy"]),
    ("cloudy", ["cloudy", "overcast"]),
    ("clear", ["clear", "sunny", "fine weather"]),
]

TRADES = [
    "welders", "welder", "fitters", "fitter", "riggers", "rigger", "helpers", "helper", "labourers", "labourer",
    "labour", "laborers", "labor", "workers", "worker", "electricians", "electrician", "masons", "mason",
    "carpenters", "carpenter", "operators", "operator", "supervisors", "supervisor", "linemen", "lineman",
    "erectors", "erector", "bar benders", "bar bender", "khalasi", "engineers", "engineer", "surveyors", "surveyor",
]

STOP_WORDS = {"of", "and", "the", "for", "to", "in", "on", "at", "a", "an", "with", "by", "work", "works", "no",
              "nos", "at", "loc", "location"}

NUMBER_RE = r"(\d+(?:[.,]\d+)?)"

UNIT_WORDS = [
    "nos", "no", "numbers", "m", "mtr", "mtrs", "meter", "meters", "metre", "metres", "rm", "rmt", "km", "kms",
    "ckm", "cum", "m3", "sqm", "m2", "mt", "t", "ton", "tons", "tonne", "tonnes", "kg", "sets", "set",
    "locations", "locs", "towers", "foundations", "spans", "joints", "inch-dia", "id",
]


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _to_float(value):
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


class EpcDpr(models.Model):
    _name = "epc.dpr"
    _description = "EPC Daily Progress Report"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date desc, id desc"

    name = fields.Char(string="Reference", default=lambda self: _("New"), copy=False, readonly=True)
    project_id = fields.Many2one("project.project", required=True, index=True, ondelete="cascade",
                                 domain=[("is_epc", "=", True)], tracking=True)
    company_id = fields.Many2one(related="project_id.company_id", store=True, index=True)
    date = fields.Date(required=True, default=fields.Date.context_today, tracking=True)
    supervisor_id = fields.Many2one("res.users", string="Site Supervisor", required=True,
                                    default=lambda self: self.env.user)
    weather = fields.Selection(WEATHER, default="clear")
    state = fields.Selection(
        [("draft", "Draft"), ("submitted", "Submitted"), ("approved", "Approved")],
        default="draft", required=True, tracking=True, copy=False)
    notes_raw = fields.Text(
        string="Site Notes",
        help="Type or paste the supervisor's notes or voice transcript. "
             "'AI Draft DPR' converts them into measured quantities, manpower and hindrances.")
    ai_summary = fields.Html(string="AI Summary", sanitize=True, copy=False)
    ai_source = fields.Selection([("llm", "AI model"), ("fallback", "Built-in engine")], string="AI Source", readonly=True, copy=False)
    line_ids = fields.One2many("epc.dpr.line", "dpr_id", string="Measured Work", copy=True)
    manpower_ids = fields.One2many("epc.dpr.manpower", "dpr_id", string="Manpower", copy=True)
    hindrance_ids = fields.One2many("epc.dpr.hindrance", "dpr_id", string="Hindrances", copy=True)
    manpower_total = fields.Integer(compute="_compute_totals", store=True)
    hours_lost_total = fields.Float(string="Hours Lost", compute="_compute_totals", store=True)
    approved_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    plant_ids = fields.One2many("epc.dpr.plant", "dpr_id", string="Plant & Equipment", copy=True)
    photo_ids = fields.Many2many("ir.attachment", "epc_dpr_photo_rel", "dpr_id", "attachment_id",
                                 string="Site Photos", copy=False)
    photo_review = fields.Html(string="AI Photo Review", sanitize=True, copy=False, readonly=True)
    photo_review_source = fields.Selection([("llm", "AI vision model"), ("fallback", "Not analysed")],
                                           readonly=True, copy=False)
    observation_ids = fields.One2many("epc.hse.observation", "dpr_id", string="Safety Observations")

    _dpr_uniq = models.Constraint(
        "unique(project_id, date, supervisor_id)",
        "A supervisor can file only one DPR per project per day.",
    )

    @api.depends("manpower_ids.count", "hindrance_ids.hours_lost")
    def _compute_totals(self):
        for dpr in self:
            dpr.manpower_total = sum(dpr.manpower_ids.mapped("count"))
            dpr.hours_lost_total = sum(dpr.hindrance_ids.mapped("hours_lost"))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code("epc.dpr") or _("New")
        return super().create(vals_list)

    def unlink(self):
        if any(dpr.state == "approved" for dpr in self):
            raise UserError(_("Approved DPRs cannot be deleted. Reset them to draft first."))
        return super().unlink()

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def action_submit(self):
        for dpr in self:
            if dpr.state != "draft":
                continue
            if not dpr.line_ids and not dpr.hindrance_ids:
                raise UserError(_("Add measured work or hindrances before submitting %s.", dpr.name))
            dpr.state = "submitted"
        return True

    def action_approve(self):
        for dpr in self:
            if dpr.state == "approved":
                continue
            if dpr.state == "draft":
                dpr.action_submit()
            breakdown_cmds = []
            existing = {h.description for h in dpr.hindrance_ids}
            for log in dpr.plant_ids.filtered(lambda l: l.hours_breakdown > 0):
                description = _("%(plant)s breakdown", plant=log.plant_id.name)
                if description not in existing:
                    breakdown_cmds.append((0, 0, {
                        "hindrance_type": "equipment", "activity_id": log.activity_id.id,
                        "hours_lost": log.hours_breakdown, "description": description}))
            vals = {"state": "approved", "approved_by_id": self.env.user.id}
            if breakdown_cmds:
                vals["hindrance_ids"] = breakdown_cmds
            dpr.write(vals)
            activities = dpr.line_ids.mapped("activity_id")
            for activity in activities:
                if not activity.actual_start or activity.actual_start > dpr.date:
                    activity.actual_start = dpr.date
            self.env.flush_all()
            for activity in activities:
                if activity.progress >= 100.0 and not activity.actual_finish:
                    activity.actual_finish = dpr.date
        return True

    def action_reset_draft(self):
        for dpr in self:
            dpr.state = "draft"
        return True

    # ------------------------------------------------------------------
    # AI draft
    # ------------------------------------------------------------------
    def _activity_catalogue(self):
        self.ensure_one()
        activities = self.project_id.epc_activity_ids.filtered(lambda a: a.state != "done" or a.progress < 100)
        return activities or self.project_id.epc_activity_ids

    def _fallback_parse(self, notes, activities):
        """Rule-based parser for site notes written in English."""
        result = {"lines": [], "manpower": [], "hindrances": [], "weather": False, "summary": ""}
        text = notes or ""
        lowered = _norm(text)

        for weather, words in WEATHER_KEYWORDS:
            if any(w in lowered for w in words):
                result["weather"] = weather
                break

        trade_pattern = r"(\d+)\s*(?:nos\.?\s*)?(%s)\b" % "|".join(re.escape(t) for t in TRADES)
        manpower = {}
        for count, trade in re.findall(trade_pattern, lowered):
            key = trade.rstrip("s") if not trade.endswith("ss") else trade
            manpower[key] = manpower.get(key, 0) + int(count)
        result["manpower"] = [{"trade": t, "count": c} for t, c in manpower.items()]

        sentences = [s.strip() for s in re.split(r"[\n;]+|(?<=[.!?])\s+", text) if s.strip()]
        catalogue = []
        for activity in activities:
            name_tokens = {t for t in re.findall(r"[a-z]+", _norm(activity.name)) if t not in STOP_WORDS and len(t) > 2}
            catalogue.append((activity, _norm(activity.code), name_tokens))

        used = set()
        for sentence in sentences:
            low = _norm(sentence)
            best, best_score = None, 0.0
            for activity, code, tokens in catalogue:
                if code and re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(code), low):
                    best, best_score = activity, 10.0
                    break
                if tokens:
                    sentence_tokens = set(re.findall(r"[a-z]+", low))
                    hits = len(tokens & sentence_tokens)
                    score = hits / len(tokens)
                    if hits >= min(2, len(tokens)) and score > best_score and score >= 0.5:
                        best, best_score = activity, score
            has_hindrance_words = any(any(w in low for w in words) for _t, words in HINDRANCE_KEYWORDS)
            if best and best.id not in used:
                quantity = self._extract_quantity(low, best, trade_pattern)
                if quantity > 0:
                    used.add(best.id)
                    result["lines"].append({
                        "activity_code": best.code, "quantity": quantity, "remarks": sentence[:250]})
                    if not has_hindrance_words:
                        continue
            if has_hindrance_words:
                htype = "other"
                for candidate, words in HINDRANCE_KEYWORDS:
                    if any(w in low for w in words):
                        htype = candidate
                        break
                hours = re.search(r"(\d+(?:\.\d+)?)\s*(?:hrs|hours|hr|h)\b", low)
                result["hindrances"].append({
                    "type": htype,
                    "hours_lost": _to_float(hours.group(1)) if hours else 0.0,
                    "activity_code": best.code if best else "",
                    "description": sentence[:250],
                })
        total_qty_lines = len(result["lines"])
        result["summary"] = (
            "%s work item(s) measured, %s worker(s) on site, %s hindrance(s) recorded."
            % (total_qty_lines, sum(m["count"] for m in result["manpower"]), len(result["hindrances"])))
        return result

    @api.model
    def _extract_quantity(self, low, activity, trade_pattern):
        """Pick the measured quantity in a sentence, preferring a number followed by a unit."""
        cleaned = low
        if activity.code:
            cleaned = cleaned.replace(_norm(activity.code), " ")
        cleaned = re.sub(trade_pattern, " ", cleaned)
        cleaned = re.sub(r"\b\d+(?:\.\d+)?\s*(?:hrs|hours|hr|h)\b", " ", cleaned)
        cleaned = re.sub(r"\b(?:loc(?:ation)?|tower|ap|t)\s*(?:no\.?\s*)?[-#]?\s*\d+[a-z]?\b", " ", cleaned)
        units = set(UNIT_WORDS)
        if activity.uom_name:
            units.add(_norm(activity.uom_name).rstrip("."))
        unit_re = NUMBER_RE + r"\s*(?:%s)\b" % "|".join(re.escape(u) for u in sorted(units, key=len, reverse=True))
        match = re.search(unit_re, cleaned)
        if match:
            return _to_float(match.group(1))
        numbers = re.findall(NUMBER_RE, cleaned)
        return _to_float(numbers[0]) if numbers else 0.0

    def _apply_parsed(self, parsed, activities):
        self.ensure_one()
        by_code = {_norm(a.code): a for a in activities}
        existing_activities = set(self.line_ids.mapped("activity_id").ids)
        line_cmds, manpower_cmds, hindrance_cmds = [], [], []
        for line in parsed.get("lines") or []:
            activity = by_code.get(_norm(line.get("activity_code")))
            quantity = _to_float(line.get("quantity"))
            if not activity or quantity <= 0 or activity.id in existing_activities:
                continue
            existing_activities.add(activity.id)
            line_cmds.append((0, 0, {
                "activity_id": activity.id,
                "quantity": quantity,
                "remarks": (line.get("remarks") or "")[:250],
            }))
        existing_trades = {_norm(t) for t in self.manpower_ids.mapped("trade")}
        for item in parsed.get("manpower") or []:
            trade = (item.get("trade") or "").strip()
            count = int(_to_float(item.get("count")))
            if trade and count > 0 and _norm(trade) not in existing_trades:
                existing_trades.add(_norm(trade))
                manpower_cmds.append((0, 0, {"trade": trade[:64], "count": count}))
        valid_types = dict(HINDRANCE_TYPES)
        existing_desc = {_norm(d) for d in self.hindrance_ids.mapped("description")}
        for item in parsed.get("hindrances") or []:
            description = (item.get("description") or "").strip()
            if _norm(description) in existing_desc:
                continue
            existing_desc.add(_norm(description))
            htype = item.get("type") if item.get("type") in valid_types else "other"
            activity = by_code.get(_norm(item.get("activity_code")))
            hindrance_cmds.append((0, 0, {
                "hindrance_type": htype,
                "hours_lost": max(0.0, _to_float(item.get("hours_lost"))),
                "activity_id": activity.id if activity else False,
                "description": description[:250] or valid_types[htype],
            }))
        vals = {}
        if line_cmds:
            vals["line_ids"] = line_cmds
        if manpower_cmds:
            vals["manpower_ids"] = manpower_cmds
        if hindrance_cmds:
            vals["hindrance_ids"] = hindrance_cmds
        if parsed.get("weather") in dict(WEATHER):
            vals["weather"] = parsed["weather"]
        return vals

    def action_ai_draft(self):
        for dpr in self:
            if dpr.state != "draft":
                raise UserError(_("Only draft DPRs can be filled by the AI assistant."))
            if not (dpr.notes_raw or "").strip():
                raise UserError(_("Enter the site notes first."))
            activities = dpr._activity_catalogue()
            catalogue = [
                {"code": a.code, "name": a.name, "unit": a.uom_name or "", "planned_qty": a.planned_qty,
                 "executed_qty": a.executed_qty}
                for a in activities
            ]
            system = (
                "You convert construction site notes (English, Hindi or Telugu, possibly a voice transcript) "
                "into a structured Daily Progress Report for an EPC project. Only use activity codes from the "
                "catalogue. Return JSON only with keys: lines [{activity_code, quantity, remarks}], "
                "manpower [{trade, count}], hindrances [{type, hours_lost, activity_code, description}], "
                "weather, summary. Allowed hindrance types: %s. Allowed weather: %s. Quantities are numbers "
                "in the activity unit for today only. Summary: 2-3 sentences in English."
                % (", ".join(dict(HINDRANCE_TYPES)), ", ".join(dict(WEATHER))))
            prompt = "Activity catalogue:\n%s\n\nSite notes for %s:\n%s" % (
                json.dumps(catalogue, default=str), dpr.date, dpr.notes_raw)
            answer = self.env["epc.ai.service"].ask(
                "dpr_draft", system, prompt,
                lambda: dpr._fallback_parse(dpr.notes_raw, activities),
                record=dpr, expect_json=True)
            parsed = answer["result"] if isinstance(answer["result"], dict) else {}
            vals = dpr._apply_parsed(parsed, activities)
            summary = parsed.get("summary") or ""
            vals["ai_summary"] = "<p>%s</p>" % (
                str(summary).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            vals["ai_source"] = answer["source"]
            dpr.write(vals)
            dpr.message_post(body=_("DPR drafted from site notes by the %s. Please verify the quantities.",
                                    _("AI model") if answer["source"] == "llm" else _("built-in engine")))
        return True


    # ------------------------------------------------------------------
    # AI photo review (vision)
    # ------------------------------------------------------------------
    MAX_PHOTOS = 6
    MAX_PHOTO_BYTES = 5 * 1024 * 1024

    def _photo_payload(self):
        self.ensure_one()
        payload = []
        for attachment in self.photo_ids:
            if not (attachment.mimetype or "").startswith("image/"):
                continue
            if attachment.file_size and attachment.file_size > self.MAX_PHOTO_BYTES:
                continue
            data = attachment.datas
            if not data:
                continue
            payload.append((attachment.mimetype, data.decode() if isinstance(data, bytes) else data))
            if len(payload) >= self.MAX_PHOTOS:
                break
        return payload

    def action_ai_photo_review(self):
        for dpr in self:
            images = dpr._photo_payload()
            if not images:
                raise UserError(_("Attach site photos (JPG/PNG up to 5 MB) first."))
            catalogue = [{"code": a.code, "name": a.name, "progress": round(a.progress, 1)}
                         for a in dpr._activity_catalogue()]
            system = (
                "You are a construction site manager and safety officer reviewing site photos from an EPC project. "
                "Return JSON only with keys: summary (2-3 sentences), activity_updates [{activity_code, observation, "
                "estimated_progress_pct}], safety_findings [{observation_type, severity, description}]. "
                "observation_type is one of: unsafe_act, unsafe_condition, ppe, housekeeping, environment, "
                "good_practice. severity is low, medium or high. Report missing helmets, harnesses, vests, unsafe "
                "scaffolds, open excavations, poor barricading and housekeeping. Only use activity codes from the list.")
            prompt = "Activities:\n%s\nDate %s. Review the attached photos." % (json.dumps(catalogue), dpr.date)

            manual_note = _("No AI vision provider is configured, so %s photo(s) were stored for manual "
                            "review. Configure OpenAI or Anthropic in Settings to analyse photos.", len(images))

            def fallback(note=manual_note):
                return {"summary": note, "activity_updates": [], "safety_findings": []}

            answer = self.env["epc.ai.service"].ask(
                "dpr_photo_review", system, prompt, fallback, record=dpr, expect_json=True,
                max_tokens=1500, images=images)
            data = answer["result"] if isinstance(answer["result"], dict) else {}
            by_code = {_norm(a.code): a for a in dpr.project_id.epc_activity_ids}
            valid_types = {"unsafe_act", "unsafe_condition", "ppe", "housekeeping", "environment", "good_practice"}
            existing = {_norm(o.description) for o in dpr.observation_ids}
            Observation = self.env["epc.hse.observation"]
            for finding in data.get("safety_findings") or []:
                description = str(finding.get("description") or "").strip()[:500]
                if not description or _norm(description) in existing:
                    continue
                existing.add(_norm(description))
                Observation.create({
                    "name": description[:80],
                    "description": description,
                    "project_id": dpr.project_id.id,
                    "dpr_id": dpr.id,
                    "date": dpr.date,
                    "observation_type": finding.get("observation_type") if finding.get("observation_type") in valid_types else "unsafe_condition",
                    "severity": finding.get("severity") if finding.get("severity") in ("low", "medium", "high") else "medium",
                    "source": "ai_photo",
                })
            items = []
            for update in data.get("activity_updates") or []:
                activity = by_code.get(_norm(update.get("activity_code")))
                if not activity:
                    continue
                items.append("%s: %s (estimated %s%%)" % (
                    activity.display_name, str(update.get("observation") or "")[:300],
                    update.get("estimated_progress_pct", "?")))
            body = Markup("<p>%s</p>") % str(data.get("summary") or "")
            if items:
                body += Markup("<p><b>%s</b></p><ul>%s</ul>") % (
                    _("Progress seen in photos (check against measured quantities):"),
                    Markup("").join(Markup("<li>%s</li>") % i for i in items))
            dpr.write({"photo_review": body, "photo_review_source": answer["source"]})
        return True


class EpcDprLine(models.Model):
    _name = "epc.dpr.line"
    _description = "EPC DPR Measured Work"
    _order = "dpr_id, id"

    dpr_id = fields.Many2one("epc.dpr", required=True, ondelete="cascade", index=True)
    project_id = fields.Many2one(related="dpr_id.project_id", store=True, index=True)
    company_id = fields.Many2one(related="dpr_id.company_id", store=True)
    date = fields.Date(related="dpr_id.date", store=True)
    state = fields.Selection(related="dpr_id.state", store=True)
    activity_id = fields.Many2one("epc.activity", required=True, index=True, ondelete="restrict",
                                  domain="[('project_id', '=', project_id)]")
    boq_line_id = fields.Many2one("epc.boq.line", string="BOQ Item", compute="_compute_boq_line_id",
                                  store=True, readonly=False, index=True,
                                  domain="[('project_id', '=', project_id)]")
    uom_name = fields.Char(related="activity_id.uom_name", string="Unit")
    quantity = fields.Float(digits="Product Unit", required=True)
    remarks = fields.Char()

    @api.depends("activity_id")
    def _compute_boq_line_id(self):
        for line in self:
            line.boq_line_id = line.activity_id.boq_line_id

    @api.constrains("quantity")
    def _check_quantity(self):
        for line in self:
            if line.quantity < 0:
                raise ValidationError(_("Measured quantity cannot be negative."))

    @api.constrains("activity_id", "dpr_id")
    def _check_project(self):
        for line in self:
            if line.activity_id.project_id != line.dpr_id.project_id:
                raise ValidationError(_("The activity must belong to the DPR's project."))


class EpcDprManpower(models.Model):
    _name = "epc.dpr.manpower"
    _description = "EPC DPR Manpower"

    dpr_id = fields.Many2one("epc.dpr", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="dpr_id.company_id", store=True)
    trade = fields.Char(required=True)
    count = fields.Integer(required=True, default=1)
    subcontractor_id = fields.Many2one("res.partner", string="Subcontractor")


class EpcDprHindrance(models.Model):
    _name = "epc.dpr.hindrance"
    _description = "EPC DPR Hindrance"

    dpr_id = fields.Many2one("epc.dpr", required=True, ondelete="cascade", index=True)
    project_id = fields.Many2one(related="dpr_id.project_id", store=True, index=True)
    company_id = fields.Many2one(related="dpr_id.company_id", store=True)
    date = fields.Date(related="dpr_id.date", store=True)
    state = fields.Selection(related="dpr_id.state", store=True)
    hindrance_type = fields.Selection(HINDRANCE_TYPES, string="Type", required=True, default="other")
    activity_id = fields.Many2one("epc.activity", domain="[('project_id', '=', project_id)]", ondelete="set null")
    hours_lost = fields.Float(string="Hours Lost")
    description = fields.Char(required=True)
