from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, float_is_zero


class EpcRaBill(models.Model):
    _name = "epc.ra.bill"
    _description = "EPC Running Account Bill"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "project_id, bill_no desc, id desc"

    name = fields.Char(default=lambda self: _("New"), readonly=True, copy=False)
    project_id = fields.Many2one("project.project", required=True, index=True, ondelete="restrict",
                                 domain=[("is_epc", "=", True)], tracking=True)
    company_id = fields.Many2one(related="project_id.company_id", store=True, index=True)
    currency_id = fields.Many2one(related="project_id.currency_id")
    partner_id = fields.Many2one("res.partner", string="Client", required=True, tracking=True)
    bill_no = fields.Integer(string="RA Bill No.", readonly=True, copy=False)
    date = fields.Date(default=fields.Date.context_today, required=True)
    period_from = fields.Date()
    period_to = fields.Date(default=fields.Date.context_today)
    line_ids = fields.One2many("epc.ra.bill.line", "bill_id", string="Measured Items", copy=False)
    gross_amount = fields.Monetary(compute="_compute_amounts", store=True, currency_field="currency_id")
    retention_pct = fields.Float(string="Retention %")
    retention_amount = fields.Monetary(compute="_compute_amounts", store=True, currency_field="currency_id")
    advance_recovery_pct = fields.Float(string="Advance Recovery %")
    advance_recovery_amount = fields.Monetary(compute="_compute_amounts", store=True, currency_field="currency_id")
    other_deductions = fields.Monetary(currency_field="currency_id")
    net_amount = fields.Monetary(compute="_compute_amounts", store=True, currency_field="currency_id")
    state = fields.Selection(
        [("draft", "Draft"), ("submitted", "Submitted"), ("certified", "Certified"),
         ("invoiced", "Invoiced"), ("cancel", "Cancelled")],
        default="draft", required=True, tracking=True, copy=False)
    invoice_id = fields.Many2one("account.move", readonly=True, copy=False)
    note = fields.Text()

    @api.depends("line_ids.amount", "retention_pct", "advance_recovery_pct", "other_deductions")
    def _compute_amounts(self):
        for bill in self:
            gross = sum(bill.line_ids.mapped("amount"))
            retention = gross * bill.retention_pct / 100.0
            advance = gross * bill.advance_recovery_pct / 100.0
            bill.gross_amount = gross
            bill.retention_amount = retention
            bill.advance_recovery_amount = advance
            bill.net_amount = gross - retention - advance - bill.other_deductions

    @api.onchange("project_id")
    def _onchange_project_id(self):
        if self.project_id:
            self.partner_id = self.project_id.partner_id
            self.retention_pct = self.project_id.epc_retention_pct
            self.advance_recovery_pct = self.project_id.epc_advance_recovery_pct

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            project = self.env["project.project"].browse(vals.get("project_id"))
            if "retention_pct" not in vals:
                vals["retention_pct"] = project.epc_retention_pct
            if "advance_recovery_pct" not in vals:
                vals["advance_recovery_pct"] = project.epc_advance_recovery_pct
            if not vals.get("partner_id") and project.partner_id:
                vals["partner_id"] = project.partner_id.id
            if not vals.get("name") or vals.get("name") == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code("epc.ra.bill") or _("New")
            if not vals.get("bill_no"):
                last = self.search([("project_id", "=", project.id)], order="bill_no desc", limit=1)
                vals["bill_no"] = (last.bill_no or 0) + 1
        return super().create(vals_list)

    def unlink(self):
        if any(bill.state not in ("draft", "cancel") for bill in self):
            raise UserError(_("Only draft or cancelled RA bills can be deleted."))
        return super().unlink()

    def _previously_billed(self, boq_line):
        """Quantity of a BOQ item certified in other bills of the same project."""
        self.ensure_one()
        lines = self.env["epc.ra.bill.line"].search([
            ("boq_line_id", "=", boq_line.id),
            ("bill_id", "!=", self.id),
            ("bill_id.state", "in", ("certified", "invoiced")),
        ])
        return sum(lines.mapped("certified_qty"))

    def action_generate_lines(self):
        for bill in self:
            if bill.state != "draft":
                raise UserError(_("Lines can only be generated on draft bills."))
            bill.line_ids.unlink()
            commands = []
            for boq in bill.project_id.epc_boq_line_ids:
                previous = bill._previously_billed(boq)
                claim = boq.executed_qty - previous
                if float_compare(claim, 0.0, precision_digits=4) <= 0:
                    continue
                commands.append((0, 0, {
                    "boq_line_id": boq.id,
                    "previous_qty": previous,
                    "cumulative_qty": boq.executed_qty,
                    "claimed_qty": claim,
                    "certified_qty": claim,
                    "rate": boq.rate,
                }))
            if not commands:
                raise UserError(_("There is no executed, unbilled quantity to bill on %s.", bill.project_id.name))
            bill.write({"line_ids": commands})
        return True

    def action_submit(self):
        for bill in self:
            if not bill.line_ids:
                raise UserError(_("Generate the measured items first."))
            bill.state = "submitted"
        return True

    def action_certify(self):
        for bill in self:
            if bill.state not in ("draft", "submitted"):
                continue
            for line in bill.line_ids:
                previous = bill._previously_billed(line.boq_line_id)
                if float_compare(previous + line.certified_qty, line.boq_line_id.executed_qty,
                                 precision_digits=4) > 0:
                    raise ValidationError(_(
                        "Item %(item)s: certifying %(qty)s would bill more than the executed quantity "
                        "(%(executed)s executed, %(previous)s already billed).",
                        item=line.boq_line_id.display_name, qty=line.certified_qty,
                        executed=line.boq_line_id.executed_qty, previous=previous))
                line.previous_qty = previous
            bill.state = "certified"
        return True

    def action_cancel(self):
        for bill in self:
            if bill.state == "invoiced":
                raise UserError(_("Cancel the customer invoice first."))
            bill.state = "cancel"
        return True

    def action_reset_draft(self):
        for bill in self:
            if bill.state == "invoiced":
                raise UserError(_("An invoiced RA bill cannot be reset."))
            bill.state = "draft"
        return True

    def _prepare_invoice_lines(self):
        self.ensure_one()
        commands = []
        for line in self.line_ids.filtered(lambda l: not float_is_zero(l.certified_qty, precision_digits=4)):
            commands.append((0, 0, {
                "name": "[%s] %s — RA %s" % (line.boq_line_id.code, line.boq_line_id.name, self.bill_no),
                "quantity": line.certified_qty,
                "price_unit": line.rate,
            }))
        if self.retention_amount:
            commands.append((0, 0, {
                "name": _("Retention %s%%", self.retention_pct),
                "quantity": 1.0,
                "price_unit": -self.retention_amount,
                "tax_ids": [(5, 0, 0)],
            }))
        if self.advance_recovery_amount:
            commands.append((0, 0, {
                "name": _("Mobilisation advance recovery %s%%", self.advance_recovery_pct),
                "quantity": 1.0,
                "price_unit": -self.advance_recovery_amount,
                "tax_ids": [(5, 0, 0)],
            }))
        if self.other_deductions:
            commands.append((0, 0, {
                "name": _("Other deductions"),
                "quantity": 1.0,
                "price_unit": -self.other_deductions,
                "tax_ids": [(5, 0, 0)],
            }))
        return commands

    def action_create_invoice(self):
        moves = self.env["account.move"]
        for bill in self:
            if bill.state != "certified":
                raise UserError(_("Only certified RA bills can be invoiced."))
            if not bill.partner_id:
                raise UserError(_("Set the client on %s.", bill.name))
            move = self.env["account.move"].with_company(bill.company_id or self.env.company).create({
                "move_type": "out_invoice",
                "partner_id": bill.partner_id.id,
                "invoice_date": bill.date,
                "ref": "%s / %s" % (bill.project_id.epc_contract_ref or bill.project_id.name, bill.name),
                "invoice_origin": bill.name,
                "invoice_line_ids": bill._prepare_invoice_lines(),
            })
            bill.write({"invoice_id": move.id, "state": "invoiced"})
            moves |= move
        if len(moves) == 1:
            return {
                "type": "ir.actions.act_window",
                "res_model": "account.move",
                "res_id": moves.id,
                "view_mode": "form",
            }
        return True

    def action_view_invoice(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "res_id": self.invoice_id.id,
            "view_mode": "form",
        }


class EpcRaBillLine(models.Model):
    _name = "epc.ra.bill.line"
    _description = "EPC RA Bill Line"
    _order = "bill_id, id"

    bill_id = fields.Many2one("epc.ra.bill", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="bill_id.company_id", store=True)
    currency_id = fields.Many2one(related="bill_id.currency_id")
    boq_line_id = fields.Many2one("epc.boq.line", string="BOQ Item", required=True, ondelete="restrict", index=True)
    uom_name = fields.Char(related="boq_line_id.uom_name", string="Unit")
    contract_qty = fields.Float(related="boq_line_id.quantity")
    previous_qty = fields.Float(string="Previously Billed", digits="Product Unit", readonly=True)
    cumulative_qty = fields.Float(string="Executed to Date", digits="Product Unit", readonly=True)
    claimed_qty = fields.Float(string="Claimed This Bill", digits="Product Unit", readonly=True)
    certified_qty = fields.Float(string="Certified This Bill", digits="Product Unit")
    rate = fields.Monetary(currency_field="currency_id")
    amount = fields.Monetary(compute="_compute_amount", store=True, currency_field="currency_id")

    @api.depends("certified_qty", "rate")
    def _compute_amount(self):
        for line in self:
            line.amount = line.certified_qty * line.rate

    @api.constrains("certified_qty")
    def _check_certified_qty(self):
        for line in self:
            if line.certified_qty < 0:
                raise ValidationError(_("Certified quantity cannot be negative."))
