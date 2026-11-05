"""Bid evaluation (TBE/CBE) and external material tracking for ERP overlay deployments."""
import base64
import io
import json
import re
from datetime import datetime, time, timedelta

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

NUMBER = re.compile(r"(?<![A-Za-z])\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|(?<![A-Za-z\d])\d+(?:\.\d+)?")
LEAD_TIME = re.compile(r"(?:deliver\w*|lead\s*time|dispatch\w*|completion)[^.\n]{0,60}?(\d+)\s*(day|week|wk|month)",
                       re.IGNORECASE)


def _num(text):
    try:
        return float(str(text).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


class EpcBidPackage(models.Model):
    _name = "epc.bid.package"
    _description = "EPC Bid Package (Technical & Commercial Evaluation)"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    name = fields.Char(default=lambda self: _("New"), readonly=True, copy=False)
    title = fields.Char(required=True, tracking=True)
    project_id = fields.Many2one("project.project", required=True, index=True, ondelete="cascade",
                                 domain=[("is_epc", "=", True)])
    company_id = fields.Many2one(related="project_id.company_id", store=True, index=True)
    currency_id = fields.Many2one(related="project_id.currency_id")
    required_date = fields.Date(string="Required at Site")
    line_ids = fields.One2many("epc.bid.package.line", "package_id", string="Items", copy=True)
    bid_ids = fields.One2many("epc.bid", "package_id", string="Bids")
    bid_count = fields.Integer(compute="_compute_bid_count")
    weight_price = fields.Float(string="Price weight", default=50.0)
    weight_delivery = fields.Float(string="Delivery weight", default=20.0)
    weight_risk = fields.Float(string="Supplier risk weight", default=15.0)
    weight_technical = fields.Float(string="Technical weight", default=15.0)
    min_technical = fields.Float(string="Minimum technical compliance (%)", default=70.0)
    recommendation = fields.Html(readonly=True, copy=False)
    recommendation_source = fields.Selection([("llm", "AI model"), ("fallback", "Built-in engine")], readonly=True,
                                             copy=False)
    awarded_bid_id = fields.Many2one("epc.bid", readonly=True, copy=False)
    purchase_order_id = fields.Many2one("purchase.order", readonly=True, copy=False)
    state = fields.Selection([("draft", "Collecting bids"), ("evaluated", "Evaluated"), ("awarded", "Awarded"),
                              ("cancel", "Cancelled")], default="draft", required=True, tracking=True)

    @api.constrains("weight_price", "weight_delivery", "weight_risk", "weight_technical")
    def _check_weights(self):
        for package in self:
            weights = (package.weight_price, package.weight_delivery, package.weight_risk, package.weight_technical)
            if min(weights) < 0 or abs(sum(weights) - 100.0) > 0.01:
                raise ValidationError(_("Evaluation weights must be positive and add up to 100."))

    def _compute_bid_count(self):
        for package in self:
            package.bid_count = len(package.bid_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code("epc.bid.package") or _("New")
        return super().create(vals_list)

    def action_evaluate(self):
        for package in self:
            bids = package.bid_ids
            if len(bids) < 1:
                raise UserError(_("Add at least one bid before evaluating."))
            if not package.line_ids:
                raise UserError(_("Add the items being bought before evaluating."))
            bids._compute_totals()
            today = fields.Date.context_today(self)
            acceptable = bids.filtered(lambda b: b.technically_acceptable and b.technical_compliance >= package.min_technical
                                       and b.total_price > 0)
            (bids - acceptable).write({"price_score": 0, "delivery_score": 0, "risk_score": 0, "total_score": 0,
                                       "rank": 0, "disqualified": True})
            if acceptable:
                best_price = min(acceptable.mapped("total_price"))
                lead_times = [b.delivery_days for b in acceptable if b.delivery_days > 0]
                best_lead = min(lead_times) if lead_times else 0
                for bid in acceptable:
                    price_score = best_price / bid.total_price * 100.0
                    delivery_score = best_lead / bid.delivery_days * 100.0 if bid.delivery_days and best_lead else 50.0
                    if package.required_date and bid.delivery_days:
                        late = (today + timedelta(days=bid.delivery_days) - package.required_date).days
                        if late > 0:
                            delivery_score = max(0.0, delivery_score - 3.0 * late)
                    risk = bid.vendor_id.commercial_partner_id.epc_supplier_risk_score
                    risk_score = 100.0 - (risk if bid.vendor_id.commercial_partner_id.epc_supplier_risk_date else 30.0)
                    total = (price_score * package.weight_price + delivery_score * package.weight_delivery
                             + risk_score * package.weight_risk + bid.technical_compliance * package.weight_technical) / 100.0
                    bid.write({"price_score": price_score, "delivery_score": delivery_score, "risk_score": risk_score,
                               "total_score": round(total, 2), "disqualified": False})
                for rank, bid in enumerate(acceptable.sorted(lambda b: (-b.total_score, b.total_price)), start=1):
                    bid.rank = rank
            package._write_recommendation()
            package.state = "evaluated"
        return True

    def _write_recommendation(self):
        self.ensure_one()
        ranked = self.bid_ids.filtered(lambda b: b.rank).sorted("rank")
        rows = [{"vendor": b.vendor_id.name, "rank": b.rank, "total_price": b.total_price,
                 "delivery_days": b.delivery_days, "technical": b.technical_compliance,
                 "supplier_risk": b.vendor_id.commercial_partner_id.epc_supplier_risk_score,
                 "score": b.total_score, "deviations": b.deviations or "", "payment_terms": b.payment_terms or ""}
                for b in ranked]
        out = [{"vendor": b.vendor_id.name, "reason": b.deviations or _("below technical threshold")}
               for b in self.bid_ids.filtered("disqualified")]

        answer = self.env["epc.ai.service"].ask(
            "bid_recommendation",
            "You are a procurement manager on an EPC project. Write a short award recommendation (max 120 words) from "
            "this technical-commercial evaluation: name the recommended vendor, why, the risks to close before award "
            "and whether delivery meets the site need date. Plain text.",
            json.dumps({"package": self.title, "required_at_site": str(self.required_date or ""),
                        "ranked_bids": rows, "disqualified": out}, default=str),
            lambda r=ranked: self._fallback_recommendation(r), record=self, max_tokens=400)
        self.write({"recommendation": Markup("<p>%s</p>") % str(answer["result"]),
                    "recommendation_source": answer["source"]})

    def _fallback_recommendation(self, ranked):
        if not ranked:
            return _("No bid passed the technical evaluation. Re-tender or relax the minimum compliance.")
        best = ranked[0]
        text = _("Recommend %(vendor)s (score %(score)s): price %(price)s, delivery %(days)s days, technical "
                 "compliance %(tech)s%%.", vendor=best.vendor_id.name, score=best.total_score,
                 price="{:,.0f}".format(best.total_price), days=best.delivery_days, tech=round(best.technical_compliance))
        if len(ranked) > 1:
            other = ranked[1]
            gap = other.total_price - best.total_price
            if gap < 0:
                text += " " + _("%(vendor)s is %(gap)s cheaper but scores lower overall (delivery %(days)s days, "
                                "technical compliance %(tech)s%%).", vendor=other.vendor_id.name,
                                gap="{:,.0f}".format(-gap), days=other.delivery_days,
                                tech=round(other.technical_compliance))
            else:
                text += " " + _("Next best is %(vendor)s at %(gap)s more.", vendor=other.vendor_id.name,
                                gap="{:,.0f}".format(gap))
        if best.deviations:
            text += " " + _("Close out these deviations before award: %s", best.deviations)
        return text


    def action_view_bids(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Bids"),
            "res_model": "epc.bid",
            "view_mode": "list,form",
            "domain": [("package_id", "=", self.id)],
            "context": {"default_package_id": self.id},
        }

    def action_cancel(self):
        self.filtered(lambda p: p.state != "awarded").write({"state": "cancel"})
        return True


class EpcBidPackageLine(models.Model):
    _name = "epc.bid.package.line"
    _description = "EPC Bid Package Item"
    _order = "package_id, sequence, id"

    package_id = fields.Many2one("epc.bid.package", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="package_id.company_id", store=True)
    sequence = fields.Integer(default=10)
    item_no = fields.Char(string="Item")
    description = fields.Char(required=True)
    product_id = fields.Many2one("product.product", domain=[("purchase_ok", "=", True)])
    quantity = fields.Float(digits="Product Unit", required=True, default=1.0)
    uom_name = fields.Char(string="Unit", default="Nos")
    mto_line_id = fields.Many2one("epc.mto.line", string="MTO Line", ondelete="set null")
    activity_id = fields.Many2one("epc.activity", string="Needed by Activity")


class EpcBid(models.Model):
    _name = "epc.bid"
    _description = "EPC Vendor Bid"
    _inherit = ["mail.thread"]
    _order = "package_id, rank, id"
    _rec_name = "vendor_id"

    package_id = fields.Many2one("epc.bid.package", required=True, ondelete="cascade", index=True)
    project_id = fields.Many2one(related="package_id.project_id", store=True)
    company_id = fields.Many2one(related="package_id.company_id", store=True)
    currency_id = fields.Many2one(related="package_id.currency_id")
    vendor_id = fields.Many2one("res.partner", required=True)
    vendor_risk = fields.Float(related="vendor_id.commercial_partner_id.epc_supplier_risk_score", string="Supplier Risk")
    offer_file = fields.Binary(string="Offer document", attachment=True)
    offer_filename = fields.Char()
    offer_text = fields.Text(string="Offer text", help="Paste the offer here if you do not have a file.")
    line_ids = fields.One2many("epc.bid.line", "bid_id", string="Prices")
    total_price = fields.Monetary(currency_field="currency_id", tracking=True)
    delivery_days = fields.Integer(string="Delivery (days)", tracking=True)
    technical_compliance = fields.Float(string="Technical compliance (%)", default=100.0)
    technically_acceptable = fields.Boolean(default=True)
    deviations = fields.Text()
    payment_terms = fields.Char()
    validity_days = fields.Integer(string="Validity (days)")
    price_score = fields.Float(readonly=True, digits=(5, 1))
    delivery_score = fields.Float(readonly=True, digits=(5, 1))
    risk_score = fields.Float(readonly=True, digits=(5, 1))
    total_score = fields.Float(string="Score", readonly=True, digits=(5, 2))
    rank = fields.Integer(readonly=True)
    disqualified = fields.Boolean(readonly=True)
    ai_source = fields.Selection([("llm", "AI model"), ("fallback", "Built-in parser")], string="AI Source", readonly=True)

    @api.constrains("technical_compliance")
    def _check_compliance(self):
        for bid in self:
            if not 0 <= bid.technical_compliance <= 100:
                raise ValidationError(_("Technical compliance is a percentage between 0 and 100."))

    def _compute_totals(self):
        for bid in self:
            if bid.line_ids and not bid.total_price:
                bid.total_price = sum(bid.line_ids.mapped("amount"))

    def _offer_content(self):
        self.ensure_one()
        if self.offer_file:
            raw = base64.b64decode(self.offer_file)
            if (self.offer_filename or "").lower().endswith(".pdf") or raw[:4] == b"%PDF":
                try:
                    from pypdf import PdfReader
                    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages)
                except Exception as exc:
                    raise UserError(_("Could not read the offer PDF: %s", exc)) from exc
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("latin-1")
        return self.offer_text or ""

    def _fallback_extract(self, text):
        items = []
        lines = text.splitlines()
        for pkg_line in self.package_id.line_ids:
            key = (pkg_line.item_no or "").strip()
            words = [w for w in re.findall(r"[A-Za-z]{4,}", pkg_line.description)][:2]
            for row in lines:
                stripped = row.strip()
                if key and not re.match(r"^%s[\s.:)|-]" % re.escape(key), stripped):
                    continue
                if not key and not all(w.lower() in stripped.lower() for w in words):
                    continue
                numbers = [_num(n) for n in NUMBER.findall(stripped[len(key):] if key else stripped)]
                numbers = [n for n in numbers if n > 0]
                unit = 0.0
                for a in numbers:
                    if any(abs(a * pkg_line.quantity - b) <= max(0.01 * b, 0.01) for b in numbers if b != a):
                        unit = a
                        break
                if not unit and numbers:
                    unit = numbers[-1]
                if unit:
                    items.append({"item_no": key, "description": pkg_line.description, "unit_price": unit})
                break
        total = 0.0
        for row in lines:
            if re.search(r"\b(grand\s+)?total\b", row, re.IGNORECASE):
                numbers = [_num(n) for n in NUMBER.findall(row)]
                if numbers:
                    total = max(numbers)
        delivery = 0
        match = LEAD_TIME.search(text)
        if match:
            value, unit = int(match.group(1)), match.group(2).lower()
            delivery = value * (7 if unit in ("week", "wk") else 30 if unit == "month" else 1)
        deviation_rows = [r.strip() for r in lines if re.search(r"deviation|exception|not\s+included|excluded", r, re.I)
                          and not re.search(r"\bno\s+deviation|\bnil\b|\bnone\b", r, re.I)]
        compliance = 100.0 - 10.0 * len(deviation_rows)
        payment = ""
        pay = re.search(r"payment[^:\n]*[:\-]\s*([^\n]+)", text, re.IGNORECASE)
        if pay:
            payment = pay.group(1).strip()[:120]
        validity = re.search(r"validity[^\d\n]{0,30}(\d+)", text, re.IGNORECASE)
        return {"items": items, "total_price": total, "delivery_days": delivery,
                "technical_compliance": max(0.0, compliance), "deviations": "\n".join(deviation_rows)[:2000],
                "payment_terms": payment, "validity_days": int(validity.group(1)) if validity else 0}

    def action_ai_extract(self):
        for bid in self:
            text = bid._offer_content()
            if not text.strip():
                raise UserError(_("Attach the vendor offer or paste its text first."))
            items = [{"item_no": l.item_no, "description": l.description, "qty": l.quantity, "unit": l.uom_name}
                     for l in bid.package_id.line_ids]
            answer = self.env["epc.ai.service"].ask(
                "bid_extract",
                "You are a procurement engineer doing a technical and commercial bid evaluation. Read the vendor "
                "offer against the items requested. Return JSON only: {items: [{item_no, description, unit_price}], "
                "total_price, delivery_days, technical_compliance (0-100, lower for each deviation), deviations, "
                "payment_terms, validity_days}. Convert weeks/months to days.",
                "Items requested:\n%s\n\nVendor offer:\n%s" % (json.dumps(items), text[:40000]),
                lambda t=text, b=bid: b._fallback_extract(t), record=bid, expect_json=True, max_tokens=2500)
            data = answer["result"] if isinstance(answer["result"], dict) else {}
            bid._apply_extraction(data)
            bid.ai_source = answer["source"]
        return True

    def _apply_extraction(self, data):
        self.ensure_one()
        package_lines = self.package_id.line_ids
        by_no = {(l.item_no or "").strip().lower(): l for l in package_lines if l.item_no}
        commands = [(5, 0, 0)]
        for item in data.get("items") or []:
            line = by_no.get(str(item.get("item_no") or "").strip().lower())
            if not line:
                line = next((l for l in package_lines
                             if str(item.get("description") or "").lower()[:20] in l.description.lower()), False)
            price = _num(item.get("unit_price"))
            if line and price:
                commands.append((0, 0, {"package_line_id": line.id, "unit_price": price}))
        vals = {"line_ids": commands}
        for field, conv in (("delivery_days", lambda v: int(round(_num(v)))), ("validity_days", lambda v: int(round(_num(v)))),
                            ("technical_compliance", lambda v: max(0.0, min(100.0, _num(v)))),
                            ("deviations", lambda v: str(v or "")[:2000]), ("payment_terms", lambda v: str(v or "")[:120])):
            if data.get(field) not in (None, ""):
                vals[field] = conv(data[field])
        self.write(vals)
        total = _num(data.get("total_price"))
        self.total_price = total or sum(self.line_ids.mapped("amount"))

    def action_award(self):
        self.ensure_one()
        package = self.package_id
        if package.state != "evaluated":
            raise UserError(_("Evaluate the bids before awarding."))
        if self.disqualified or not self.rank:
            raise UserError(_("%s did not pass the technical evaluation.", self.vendor_id.name))
        if not self.env.su and not self.env.user.has_group("zivoit_epc_ai.group_epc_manager"):
            raise UserError(_("Only EPC managers can award bids."))
        generic = self.env["epc.mto.line"]._generic_product()
        prices = {l.package_line_id.id: l.unit_price for l in self.line_ids}
        order = self.env["purchase.order"].create({
            "partner_id": self.vendor_id.id,
            "origin": "%s %s" % (package.name, package.title),
        })
        planned = fields.Date.context_today(self) + timedelta(days=self.delivery_days or 0)
        for line in package.line_ids:
            po_line = self.env["purchase.order.line"].create({
                "order_id": order.id,
                "product_id": (line.product_id or line.mto_line_id.product_id or generic).id,
                "name": "%s%s" % ("[%s] " % line.item_no if line.item_no else "", line.description),
                "product_qty": line.quantity,
                "price_unit": prices.get(line.id, 0.0),
                "date_planned": datetime.combine(planned, time(9, 0)),
                "epc_project_id": package.project_id.id,
                "epc_required_date": package.required_date or line.activity_id.planned_start,
            })
            activity = line.activity_id or line.mto_line_id.activity_id
            if activity:
                activity.purchase_line_ids = [(4, po_line.id)]
            if line.mto_line_id:
                line.mto_line_id.write({"purchase_line_id": po_line.id, "state": "rfq", "vendor_id": self.vendor_id.id})
        package.write({"awarded_bid_id": self.id, "purchase_order_id": order.id, "state": "awarded"})
        package.message_post(body=_("Awarded to %(vendor)s; purchase order %(po)s created.",
                                    vendor=self.vendor_id.name, po=order.name))
        return {"type": "ir.actions.act_window", "res_model": "purchase.order", "res_id": order.id,
                "view_mode": "form"}


class EpcBidLine(models.Model):
    _name = "epc.bid.line"
    _description = "EPC Bid Price Line"

    bid_id = fields.Many2one("epc.bid", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="bid_id.company_id", store=True)
    currency_id = fields.Many2one(related="bid_id.currency_id")
    package_line_id = fields.Many2one("epc.bid.package.line", string="Item", required=True,
                                      domain="[('package_id', '=', parent.package_id)]")
    quantity = fields.Float(related="package_line_id.quantity")
    unit_price = fields.Monetary(currency_field="currency_id")
    amount = fields.Monetary(compute="_compute_amount", store=True, currency_field="currency_id")

    @api.depends("unit_price", "package_line_id.quantity")
    def _compute_amount(self):
        for line in self:
            line.amount = line.unit_price * line.package_line_id.quantity


class EpcMtoLineSupply(models.Model):
    _inherit = "epc.mto.line"

    def action_create_bid_package(self):
        lines = self.filtered(lambda l: l.state == "confirmed")
        if not lines or len(lines.mapped("project_id")) != 1:
            raise UserError(_("Select confirmed MTO lines of a single project."))
        dates = [d for d in lines.mapped("required_date") if d]
        package = self.env["epc.bid.package"].create({
            "title": _("Material package: %s", ", ".join(lines.mapped("description"))[:80]),
            "project_id": lines.project_id.id,
            "required_date": min(dates) if dates else False,
            "line_ids": [(0, 0, {"sequence": i * 10, "item_no": str(i), "description": l.description,
                                 "product_id": l.product_id.id, "quantity": l.quantity, "uom_name": l.uom_name,
                                 "mto_line_id": l.id, "activity_id": l.activity_id.id})
                         for i, l in enumerate(lines, start=1)],
        })
        return {"type": "ir.actions.act_window", "res_model": "epc.bid.package", "res_id": package.id,
                "view_mode": "form"}


class EpcExternalMaterial(models.Model):
    """Material status mirrored from an external ERP (SAP, Oracle...) when procurement is not done in Odoo."""
    _name = "epc.external.material"
    _description = "EPC External Material Status (ERP overlay)"
    _order = "project_id, required_date, id"
    _rec_name = "description"

    project_id = fields.Many2one("project.project", required=True, index=True, ondelete="cascade",
                                 domain=[("is_epc", "=", True)])
    company_id = fields.Many2one(related="project_id.company_id", store=True, index=True)
    source_system = fields.Selection([("sap", "SAP"), ("oracle", "Oracle"), ("other", "Other ERP"),
                                      ("manual", "Manual / spreadsheet")], default="manual", required=True)
    external_ref = fields.Char(string="External PO / Line", required=True, index=True)
    description = fields.Char(required=True)
    vendor_id = fields.Many2one("res.partner", string="Vendor (if known)")
    vendor_name = fields.Char()
    quantity = fields.Float(digits="Product Unit")
    delivered_qty = fields.Float(digits="Product Unit")
    uom_name = fields.Char(string="Unit", default="Nos")
    activity_ids = fields.Many2many("epc.activity", "epc_activity_external_material_rel", "material_id", "activity_id",
                                    string="Needed by Activities", domain="[('project_id', '=', project_id)]")
    required_date = fields.Date(string="Required at Site", compute="_compute_required_date", store=True,
                                readonly=False)
    promised_date = fields.Date(string="Vendor Promised Date")
    forecast_date = fields.Date(string="Forecast Delivery", readonly=True)
    predicted_delay_days = fields.Integer(readonly=True)
    expedite_status = fields.Selection([("on_track", "On Track"), ("at_risk", "At Risk"), ("late", "Late"),
                                        ("received", "Received")], readonly=True)
    stage = fields.Selection([("ordered", "Ordered"), ("manufacturing", "Manufacturing"), ("inspection", "Inspection"),
                              ("shipped", "Shipped"), ("delivered", "Delivered")], default="ordered", required=True)
    last_sync = fields.Datetime(readonly=True)

    _ext_ref_uniq = models.Constraint("unique(project_id, source_system, external_ref)",
                                      "This external reference already exists on the project.")

    @api.depends("activity_ids.planned_start")
    def _compute_required_date(self):
        for material in self:
            starts = [d for d in material.activity_ids.mapped("planned_start") if d]
            if starts:
                material.required_date = min(starts)

    def _is_open(self):
        self.ensure_one()
        return self.stage != "delivered" and not (self.quantity and self.delivered_qty >= self.quantity)


class EpcActivityExternal(models.Model):
    _inherit = "epc.activity"

    external_material_ids = fields.Many2many(
        "epc.external.material", "epc_activity_external_material_rel", "activity_id", "material_id",
        string="External Materials (ERP)")


class ProjectProjectSupply(models.Model):
    _inherit = "project.project"

    epc_external_material_ids = fields.One2many("epc.external.material", "project_id", string="External Materials")
    epc_bid_package_count = fields.Integer(compute="_compute_epc_bid_package_count")

    def _compute_epc_bid_package_count(self):
        Package = self.env["epc.bid.package"].sudo()
        for project in self:
            project.epc_bid_package_count = Package.search_count([("project_id", "=", project.id)]) if project.id else 0

    def action_epc_view_bid_packages(self):
        return self._epc_action("epc.bid.package", _("Bid Evaluations"), "list,form")

    def action_epc_view_external_materials(self):
        return self._epc_action("epc.external.material", _("External Materials"), "list,form")
