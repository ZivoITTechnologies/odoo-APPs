import base64
import io
import json
import re
from datetime import datetime, time

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

MTO_UNITS = ("nos", "no", "pcs", "ea", "each", "set", "sets", "m", "mtr", "rm", "km", "kg", "mt", "t", "ton",
             "tons", "m2", "sqm", "m3", "cum", "l", "ltr", "lot", "job")
BOM_ROW = re.compile(
    r"^\s*(?P<code>[A-Za-z]{0,4}[-/]?\d+[A-Za-z]?)[\s.:)|-]+(?P<desc>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>%s)\b" % "|".join(sorted(MTO_UNITS, key=len, reverse=True)),
    re.IGNORECASE)


def _float(value):
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


class EpcMtoLine(models.Model):
    _name = "epc.mto.line"
    _description = "EPC Material Take-Off Line"
    _order = "project_id, document_id, sequence, id"

    project_id = fields.Many2one("project.project", required=True, index=True, ondelete="cascade",
                                 domain=[("is_epc", "=", True)])
    company_id = fields.Many2one(related="project_id.company_id", store=True, index=True)
    currency_id = fields.Many2one(related="project_id.currency_id")
    sequence = fields.Integer(default=10)
    document_id = fields.Many2one("epc.document", string="Source Drawing", ondelete="set null",
                                  domain="[('project_id', '=', project_id)]")
    item_code = fields.Char(string="Item / Tag")
    description = fields.Char(required=True)
    product_id = fields.Many2one("product.product", domain=[("purchase_ok", "=", True)])
    quantity = fields.Float(digits="Product Unit", required=True)
    uom_name = fields.Char(string="Unit", default="Nos")
    boq_line_id = fields.Many2one("epc.boq.line", string="BOQ Item", domain="[('project_id', '=', project_id)]",
                                  index=True)
    activity_id = fields.Many2one("epc.activity", string="Needed by Activity",
                                  domain="[('project_id', '=', project_id)]")
    required_date = fields.Date(compute="_compute_required_date", store=True, readonly=False)
    vendor_id = fields.Many2one("res.partner", string="Preferred Vendor")
    estimated_rate = fields.Monetary(currency_field="currency_id")
    estimated_amount = fields.Monetary(compute="_compute_estimated_amount", store=True, currency_field="currency_id")
    source = fields.Selection([("manual", "Manual"), ("ai_drawing", "AI from drawing"), ("bim", "BIM / IFC model")],
                              default="manual", required=True, readonly=True)
    state = fields.Selection([("draft", "Draft"), ("confirmed", "Confirmed"), ("rfq", "RFQ created"),
                              ("cancel", "Cancelled")], default="draft", required=True)
    purchase_line_id = fields.Many2one("purchase.order.line", readonly=True, copy=False)

    @api.depends("activity_id.planned_start")
    def _compute_required_date(self):
        for line in self:
            if line.activity_id.planned_start:
                line.required_date = line.activity_id.planned_start

    @api.depends("quantity", "estimated_rate")
    def _compute_estimated_amount(self):
        for line in self:
            line.estimated_amount = line.quantity * line.estimated_rate

    @api.constrains("quantity")
    def _check_quantity(self):
        for line in self:
            if line.quantity < 0:
                raise ValidationError(_("MTO quantity cannot be negative."))

    def action_confirm(self):
        self.filtered(lambda l: l.state == "draft").write({"state": "confirmed"})
        return True

    def action_cancel(self):
        self.filtered(lambda l: l.state in ("draft", "confirmed")).write({"state": "cancel"})
        return True

    @api.model
    def _generic_product(self):
        product = self.env.ref("zivoit_epc_ai.product_epc_mto_material", raise_if_not_found=False)
        if not product:
            product = self.env["product.product"].search([("default_code", "=", "EPC-MTO")], limit=1)
        if not product:
            product = self.env["product.product"].create({
                "name": "EPC material (from MTO)", "default_code": "EPC-MTO", "type": "consu", "purchase_ok": True})
        return product

    def action_create_rfqs(self):
        lines = self.filtered(lambda l: l.state == "confirmed")
        if not lines:
            raise UserError(_("Select confirmed MTO lines (draft lines must be confirmed first)."))
        missing = lines.filtered(lambda l: not l.vendor_id)
        if missing:
            raise UserError(_("Set a preferred vendor on: %s", ", ".join(missing.mapped("description"))))
        generic = self._generic_product()
        orders = self.env["purchase.order"]
        for (vendor, project), group in self._group_lines(lines).items():
            order = self.env["purchase.order"].create({
                "partner_id": vendor.id,
                "origin": _("MTO %s", project.epc_contract_ref or project.name),
            })
            for line in group:
                planned = line.required_date or fields.Date.context_today(self)
                po_line = self.env["purchase.order.line"].create({
                    "order_id": order.id,
                    "product_id": (line.product_id or generic).id,
                    "name": "%s%s" % ("[%s] " % line.item_code if line.item_code else "", line.description),
                    "product_qty": line.quantity,
                    "price_unit": line.estimated_rate,
                    "date_planned": datetime.combine(planned, time(9, 0)),
                    "epc_project_id": project.id,
                    "epc_required_date": line.required_date,
                })
                line.write({"purchase_line_id": po_line.id, "state": "rfq"})
                if line.activity_id:
                    line.activity_id.purchase_line_ids = [(4, po_line.id)]
            orders |= order
        return {
            "type": "ir.actions.act_window",
            "name": _("RFQs from MTO"),
            "res_model": "purchase.order",
            "view_mode": "list,form",
            "domain": [("id", "in", orders.ids)],
        }

    @api.model
    def _group_lines(self, lines):
        groups = {}
        for line in lines:
            groups.setdefault((line.vendor_id, line.project_id), self.browse())
            groups[(line.vendor_id, line.project_id)] |= line
        return groups


class EpcBoqLineDesign(models.Model):
    _inherit = "epc.boq.line"

    mto_line_ids = fields.One2many("epc.mto.line", "boq_line_id", string="Design Quantities (MTO)")
    mto_qty = fields.Float(string="Design Qty (MTO)", compute="_compute_mto_qty", store=True, digits="Product Unit")
    mto_variance_pct = fields.Float(string="Design vs BOQ (%)", compute="_compute_mto_qty", store=True,
                                    digits=(6, 1),
                                    help="Positive = drawings need more than the BOQ quantity (possible variation).")

    @api.depends("quantity", "mto_line_ids.quantity", "mto_line_ids.state")
    def _compute_mto_qty(self):
        for line in self:
            qty = sum(line.mto_line_ids.filtered(lambda m: m.state != "cancel").mapped("quantity"))
            line.mto_qty = qty
            line.mto_variance_pct = (qty - line.quantity) / line.quantity * 100.0 if qty and line.quantity else 0.0


class EpcDocumentDesign(models.Model):
    _inherit = "epc.document"

    drawing_file = fields.Binary(string="Drawing / BOM file", attachment=True)
    drawing_filename = fields.Char()
    mto_line_ids = fields.One2many("epc.mto.line", "document_id", string="Material Take-Off")
    mto_count = fields.Integer(compute="_compute_mto_count")

    def _compute_mto_count(self):
        for doc in self:
            doc.mto_count = len(doc.mto_line_ids)

    def _drawing_content(self):
        """Return (text, images) from the attached drawing."""
        self.ensure_one()
        if not self.drawing_file:
            raise UserError(_("Attach the drawing or BOM (PDF, image or text) first."))
        raw = base64.b64decode(self.drawing_file)
        name = (self.drawing_filename or "").lower()
        if name.endswith(".pdf") or raw[:4] == b"%PDF":
            try:
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(raw))
                return "\n".join((page.extract_text() or "") for page in reader.pages), []
            except Exception as exc:
                raise UserError(_("Could not read the PDF: %s", exc)) from exc
        for ext, mime in ((".png", "image/png"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"),
                          (".webp", "image/webp")):
            if name.endswith(ext):
                return "", [(mime, base64.b64encode(raw).decode())]
        try:
            return raw.decode("utf-8"), []
        except UnicodeDecodeError:
            return raw.decode("latin-1"), []

    @api.model
    def _fallback_mto(self, text):
        rows = []
        for line in (text or "").splitlines():
            match = BOM_ROW.match(line)
            if not match:
                continue
            rows.append({
                "item_code": match.group("code"),
                "description": match.group("desc").strip(" .:-|")[:200],
                "quantity": _float(match.group("qty")),
                "unit": match.group("unit"),
            })
        return {"items": rows}

    def action_ai_extract_mto(self):
        for doc in self:
            text, images = doc._drawing_content()
            boq = [{"code": b.code, "name": b.name, "unit": b.uom_name} for b in doc.project_id.epc_boq_line_ids]
            system = (
                "You read engineering drawings and their bill of materials for an EPC project. Extract every "
                "material line. Return JSON only: {\"items\": [{item_code, description, quantity, unit, boq_code}]}. "
                "boq_code is the matching BOQ item code from the list or empty. Quantities are numbers.")
            prompt = "BOQ items:\n%s\n\nDrawing %s rev %s text:\n%s" % (
                json.dumps(boq), doc.doc_number, doc.revision, text[:40000] or "(see image)")
            if images:
                fallback_note = _("This drawing is an image; configure an AI vision provider to read it.")
                answer = self.env["epc.ai.service"].ask(
                    "mto_extract", system, prompt, lambda note=fallback_note: {"items": [], "note": note},
                    record=doc, expect_json=True, max_tokens=3000, images=images)
            else:
                answer = self.env["epc.ai.service"].ask(
                    "mto_extract", system, prompt, lambda t=text: self._fallback_mto(t),
                    record=doc, expect_json=True, max_tokens=3000)
            data = answer["result"] if isinstance(answer["result"], dict) else {}
            items = data.get("items") or []
            if not items:
                raise UserError(data.get("note") or _(
                    "No material lines were recognised. Expected rows like '1  Angle 75x75x6  120 kg'."))
            by_code = {b.code.lower(): b for b in doc.project_id.epc_boq_line_ids}
            doc.mto_line_ids.filtered(lambda l: l.source == "ai_drawing" and l.state == "draft").unlink()
            commands = []
            for seq, item in enumerate(items, start=1):
                quantity = _float(item.get("quantity"))
                if quantity <= 0 or not item.get("description"):
                    continue
                boq = by_code.get(str(item.get("boq_code") or "").lower())
                commands.append((0, 0, {
                    "project_id": doc.project_id.id,
                    "sequence": seq * 10,
                    "item_code": str(item.get("item_code") or "")[:64],
                    "description": str(item["description"])[:200],
                    "quantity": quantity,
                    "uom_name": str(item.get("unit") or "Nos")[:16],
                    "boq_line_id": boq.id if boq else False,
                    "source": "ai_drawing",
                }))
            doc.write({"mto_line_ids": commands})
            doc.message_post(body=_("%(count)s material lines extracted (%(src)s). Review and confirm them.",
                                    count=len(commands),
                                    src=_("AI model") if answer["source"] == "llm" else _("built-in parser")))
        return True

    def action_view_mto(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Material Take-Off"),
            "res_model": "epc.mto.line",
            "view_mode": "list",
            "domain": [("document_id", "=", self.id)],
            "context": {"default_document_id": self.id, "default_project_id": self.project_id.id},
        }


class EpcChangeOrder(models.Model):
    _name = "epc.change.order"
    _description = "EPC Change Order / Variation"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    name = fields.Char(default=lambda self: _("New"), readonly=True, copy=False)
    title = fields.Char(required=True, tracking=True)
    project_id = fields.Many2one("project.project", required=True, index=True, ondelete="cascade",
                                 domain=[("is_epc", "=", True)])
    company_id = fields.Many2one(related="project_id.company_id", store=True, index=True)
    currency_id = fields.Many2one(related="project_id.currency_id")
    origin = fields.Selection(
        [("client_instruction", "Client instruction"), ("design_change", "Design change"),
         ("site_condition", "Unforeseen site condition"), ("scope_addition", "Scope addition"),
         ("statutory", "Statutory / regulatory"), ("internal", "Internal (contractor's own)")],
        default="client_instruction", required=True)
    description = fields.Text()
    document_id = fields.Many2one("epc.document", string="Related Drawing", domain="[('project_id', '=', project_id)]")
    boq_change_ids = fields.One2many("epc.change.order.boq", "change_id", string="Quantity Changes")
    activity_change_ids = fields.One2many("epc.change.order.activity", "change_id", string="Schedule Changes")
    cost_impact = fields.Monetary(compute="_compute_cost_impact", store=True, currency_field="currency_id")
    time_impact_days = fields.Integer(string="Finish Date Impact (working days)", readonly=True, copy=False)
    affects_contract_value = fields.Boolean(default=True,
                                            help="Approved cost is added to the contract value (claimable from client).")
    ai_justification = fields.Text(readonly=True, copy=False)
    ai_source = fields.Selection([("llm", "AI model"), ("fallback", "Built-in engine")], string="AI Source", readonly=True, copy=False)
    state = fields.Selection([("draft", "Draft"), ("submitted", "Submitted"), ("approved", "Approved"),
                              ("rejected", "Rejected")], default="draft", required=True, tracking=True)

    @api.depends("boq_change_ids.amount")
    def _compute_cost_impact(self):
        for change in self:
            change.cost_impact = sum(change.boq_change_ids.mapped("amount"))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code("epc.change.order") or _("New")
        return super().create(vals_list)

    def _fallback_assess(self, text):
        """Pick up 'CODE +qty' and 'CODE +n days' mentions (codes are matched case-sensitively)."""
        project = self.project_id
        text = text or ""
        boq_changes, activity_changes = [], []
        for boq in project.epc_boq_line_ids:
            match = re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])[^.;\n]*?([+-]?\d+(?:\.\d+)?)" % re.escape(boq.code), text)
            if match:
                boq_changes.append({"boq_code": boq.code, "qty_delta": _float(match.group(1))})
        for activity in project.epc_activity_ids:
            match = re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])[^.;,\n]*?([+-]?\d+)\s*(?:working\s+)?days?"
                              % re.escape(activity.code), text)
            if match:
                activity_changes.append({"activity_code": activity.code, "duration_delta": int(match.group(1))})
        return {"boq_changes": boq_changes, "activity_changes": activity_changes,
                "justification": _("Quantities and durations taken from the codes mentioned in the description. "
                                   "Check rates and add items the text did not name.")}

    def action_ai_assess(self):
        for change in self:
            if change.state != "draft":
                raise UserError(_("Only draft change orders can be assessed."))
            text = "%s\n%s" % (change.title, change.description or "")
            context = {
                "boq": [{"code": b.code, "name": b.name, "unit": b.uom_name, "qty": b.quantity, "rate": b.rate}
                        for b in change.project_id.epc_boq_line_ids],
                "activities": [{"code": a.code, "name": a.name, "duration": a.duration}
                               for a in change.project_id.epc_activity_ids],
            }
            answer = self.env["epc.ai.service"].ask(
                "change_order_assess",
                "You are an EPC contracts and planning engineer. Estimate the impact of this change on the BOQ and "
                "schedule. Return JSON only: {boq_changes: [{boq_code, qty_delta, rate}], new_items: [{code, name, "
                "unit, qty, rate}], activity_changes: [{activity_code, duration_delta}], justification}. Use only "
                "codes from the lists; durations in working days.",
                "Project data:\n%s\n\nChange request:\n%s" % (json.dumps(context), text),
                lambda t=text: change._fallback_assess(t), record=change, expect_json=True, max_tokens=2000)
            data = answer["result"] if isinstance(answer["result"], dict) else {}
            change._apply_assessment(data)
            change.write({"ai_justification": str(data.get("justification") or "")[:4000],
                          "ai_source": answer["source"]})
        return True

    def _apply_assessment(self, data):
        self.ensure_one()
        boq_by_code = {b.code.lower(): b for b in self.project_id.epc_boq_line_ids}
        act_by_code = {a.code.lower(): a for a in self.project_id.epc_activity_ids}
        boq_cmds, act_cmds = [(5, 0, 0)], [(5, 0, 0)]
        for item in data.get("boq_changes") or []:
            boq = boq_by_code.get(str(item.get("boq_code") or "").lower())
            delta = _float(item.get("qty_delta"))
            if boq and delta:
                rate = _float(item.get("rate")) or boq.rate
                boq_cmds.append((0, 0, {"boq_line_id": boq.id, "qty_delta": delta, "rate": rate}))
        for item in data.get("new_items") or []:
            qty = _float(item.get("qty"))
            if qty and item.get("name"):
                boq_cmds.append((0, 0, {"new_code": str(item.get("code") or "")[:32], "new_name": str(item["name"])[:200],
                                        "new_uom": str(item.get("unit") or "Nos")[:16], "qty_delta": qty,
                                        "rate": _float(item.get("rate"))}))
        for item in data.get("activity_changes") or []:
            activity = act_by_code.get(str(item.get("activity_code") or "").lower())
            try:
                delta = int(round(float(item.get("duration_delta") or 0)))
            except (TypeError, ValueError):
                delta = 0
            if activity and delta:
                act_cmds.append((0, 0, {"activity_id": activity.id, "duration_delta": delta}))
        self.write({"boq_change_ids": boq_cmds, "activity_change_ids": act_cmds})
        self._compute_time_impact()

    def _compute_time_impact(self):
        """Finish-date impact: rerun CPM in memory with the proposed durations."""
        Activity = self.env["epc.activity"]
        for change in self:
            activities, links, order = Activity._cpm_network(change.project_id)
            if not activities:
                change.time_impact_days = 0
                continue
            calendar = change.project_id._epc_calendar(Activity._cpm_project_start(change.project_id, activities))
            base = {a.id: (0 if a.is_milestone else max(0, a.duration)) for a in activities}
            constraints = {a.id: calendar.offset_of(a.constraint_date) for a in activities if a.constraint_date}
            before = Activity._cpm_compute(order, links, base, calendar, constraints)["finish"]
            changed = dict(base)
            for line in change.activity_change_ids:
                changed[line.activity_id.id] = max(0, changed[line.activity_id.id] + line.duration_delta)
            after = Activity._cpm_compute(order, links, changed, calendar, constraints)["finish"]
            change.time_impact_days = after - before

    def action_recompute_impact(self):
        self._compute_time_impact()
        return True

    def action_submit(self):
        for change in self:
            if not change.boq_change_ids and not change.activity_change_ids:
                raise UserError(_("Add quantity or schedule changes (or use AI Assess) before submitting."))
            change._compute_time_impact()
            change.state = "submitted"
        return True

    def action_reject(self):
        self.filtered(lambda c: c.state in ("draft", "submitted")).write({"state": "rejected"})
        return True

    def action_approve(self):
        if not self.env.su and not self.env.user.has_group("zivoit_epc_ai.group_epc_manager"):
            raise UserError(_("Only EPC managers can approve change orders."))
        Boq = self.env["epc.boq.line"]
        for change in self:
            if change.state != "submitted":
                raise UserError(_("Submit %s before approving it.", change.name))
            lines = []
            for line in change.boq_change_ids:
                if line.boq_line_id:
                    new_qty = line.boq_line_id.quantity + line.qty_delta
                    if new_qty < 0:
                        raise UserError(_("%s would become negative.", line.boq_line_id.display_name))
                    line.boq_line_id.quantity = new_qty
                    lines.append("%s: %+g" % (line.boq_line_id.display_name, line.qty_delta))
                else:
                    code = line.new_code or "%s-%s" % (change.name.split("/")[-1], line.id)
                    created = Boq.create({
                        "project_id": change.project_id.id, "code": code, "name": line.new_name,
                        "uom_name": line.new_uom or "Nos", "quantity": line.qty_delta, "rate": line.rate})
                    line.boq_line_id = created
                    lines.append(_("new item %(code)s %(name)s", code=code, name=line.new_name))
            for line in change.activity_change_ids:
                line.activity_id.duration = max(0, line.activity_id.duration + line.duration_delta)
                lines.append("%s: %+d days" % (line.activity_id.display_name, line.duration_delta))
            if change.activity_change_ids:
                change.project_id.action_epc_compute_schedule()
            if change.affects_contract_value and change.cost_impact:
                change.project_id.epc_contract_value += change.cost_impact
            change.state = "approved"
            change.project_id.message_post(body=Markup("<p>%s</p><ul>%s</ul>") % (
                _("Change order %(name)s approved: %(title)s.", name=change.name, title=change.title),
                Markup("").join(Markup("<li>%s</li>") % l for l in lines)))
        return True


class EpcChangeOrderBoq(models.Model):
    _name = "epc.change.order.boq"
    _description = "EPC Change Order Quantity Change"

    change_id = fields.Many2one("epc.change.order", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="change_id.company_id", store=True)
    currency_id = fields.Many2one(related="change_id.currency_id")
    boq_line_id = fields.Many2one("epc.boq.line", string="Existing BOQ Item",
                                  domain="[('project_id', '=', parent.project_id)]")
    new_code = fields.Char(string="New Item No.")
    new_name = fields.Char(string="New Item Description")
    new_uom = fields.Char(string="Unit")
    qty_delta = fields.Float(string="Quantity Change", digits="Product Unit", required=True)
    rate = fields.Monetary(currency_field="currency_id")
    amount = fields.Monetary(compute="_compute_amount", store=True, currency_field="currency_id")

    @api.depends("qty_delta", "rate")
    def _compute_amount(self):
        for line in self:
            line.amount = line.qty_delta * line.rate

    @api.onchange("boq_line_id")
    def _onchange_boq_line_id(self):
        if self.boq_line_id and not self.rate:
            self.rate = self.boq_line_id.rate

    @api.constrains("boq_line_id", "new_name")
    def _check_item(self):
        for line in self:
            if not line.boq_line_id and not line.new_name:
                raise ValidationError(_("Choose an existing BOQ item or describe the new item."))


class EpcChangeOrderActivity(models.Model):
    _name = "epc.change.order.activity"
    _description = "EPC Change Order Schedule Change"

    change_id = fields.Many2one("epc.change.order", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="change_id.company_id", store=True)
    activity_id = fields.Many2one("epc.activity", required=True,
                                  domain="[('project_id', '=', parent.project_id)]")
    current_duration = fields.Integer(related="activity_id.duration", string="Current Duration")
    duration_delta = fields.Integer(string="Duration Change (days)", required=True)


class ProjectProjectDesign(models.Model):
    _inherit = "project.project"

    epc_change_order_count = fields.Integer(compute="_compute_epc_design_counts")
    epc_mto_count = fields.Integer(compute="_compute_epc_design_counts")
    epc_approved_variations = fields.Monetary(string="Approved Variations", compute="_compute_epc_design_counts",
                                              currency_field="currency_id")

    def _compute_epc_design_counts(self):
        Change = self.env["epc.change.order"].sudo()
        Mto = self.env["epc.mto.line"].sudo()
        for project in self:
            if not project.id:
                project.update({"epc_change_order_count": 0, "epc_mto_count": 0, "epc_approved_variations": 0.0})
                continue
            changes = Change.search([("project_id", "=", project.id)])
            project.epc_change_order_count = len(changes)
            project.epc_approved_variations = sum(changes.filtered(lambda c: c.state == "approved").mapped("cost_impact"))
            project.epc_mto_count = Mto.search_count([("project_id", "=", project.id), ("state", "!=", "cancel")])

    def action_epc_view_change_orders(self):
        return self._epc_action("epc.change.order", _("Change Orders"), "list,form")

    def action_epc_view_mto(self):
        return self._epc_action("epc.mto.line", _("Material Take-Off"), "list,form")

    def action_epc_import_bim(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Import BIM Model (IFC)"),
            "res_model": "epc.bim.import",
            "view_mode": "form",
            "target": "new",
            "context": {"default_project_id": self.id},
        }
