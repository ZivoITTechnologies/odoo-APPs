from odoo import api, fields, models


class EpcBoqLine(models.Model):
    _name = "epc.boq.line"
    _description = "EPC Bill of Quantities Line"
    _order = "project_id, sequence, code, id"
    _rec_names_search = ["code", "name"]

    project_id = fields.Many2one("project.project", required=True, ondelete="cascade", index=True,
                                 domain=[("is_epc", "=", True)])
    company_id = fields.Many2one(related="project_id.company_id", store=True, index=True)
    currency_id = fields.Many2one(related="project_id.currency_id")
    sequence = fields.Integer(default=10)
    code = fields.Char(string="Item No.", required=True)
    name = fields.Char(string="Description", required=True)
    uom_name = fields.Char(string="Unit", default="Nos")
    quantity = fields.Float(string="Contract Qty", digits="Product Unit", required=True, default=0.0)
    rate = fields.Monetary(string="Rate", currency_field="currency_id")
    amount = fields.Monetary(string="Amount", compute="_compute_amount", store=True, currency_field="currency_id")
    activity_ids = fields.One2many("epc.activity", "boq_line_id", string="Activities")
    dpr_line_ids = fields.One2many("epc.dpr.line", "boq_line_id", string="DPR Lines")
    ra_bill_line_ids = fields.One2many("epc.ra.bill.line", "boq_line_id", string="RA Bill Lines")
    executed_qty = fields.Float(string="Executed Qty", compute="_compute_executed", store=True, digits="Product Unit",
                                help="Quantity measured in approved Daily Progress Reports.")
    executed_amount = fields.Monetary(compute="_compute_executed", store=True, currency_field="currency_id")
    progress = fields.Float(string="Executed %", compute="_compute_executed", store=True, digits=(6, 1))
    billed_qty = fields.Float(string="Billed Qty", compute="_compute_billed", store=True, digits="Product Unit",
                              help="Quantity certified in RA bills.")
    unbilled_qty = fields.Float(string="Executed, Not Billed", compute="_compute_billed", store=True,
                                digits="Product Unit")

    _code_project_uniq = models.Constraint(
        "unique(project_id, code)",
        "BOQ item numbers must be unique within a project.",
    )

    @api.depends("quantity", "rate")
    def _compute_amount(self):
        for line in self:
            line.amount = line.quantity * line.rate

    @api.depends("quantity", "rate", "dpr_line_ids.quantity", "dpr_line_ids.dpr_id.state")
    def _compute_executed(self):
        for line in self:
            qty = sum(line.dpr_line_ids.filtered(lambda l: l.dpr_id.state == "approved").mapped("quantity"))
            line.executed_qty = qty
            line.executed_amount = qty * line.rate
            line.progress = min(100.0, qty / line.quantity * 100.0) if line.quantity else 0.0

    @api.depends("executed_qty", "ra_bill_line_ids.certified_qty", "ra_bill_line_ids.bill_id.state")
    def _compute_billed(self):
        for line in self:
            billed = sum(line.ra_bill_line_ids.filtered(
                lambda l: l.bill_id.state in ("certified", "invoiced")).mapped("certified_qty"))
            line.billed_qty = billed
            line.unbilled_qty = max(0.0, line.executed_qty - billed)

    @api.depends("code", "name")
    def _compute_display_name(self):
        for line in self:
            line.display_name = "[%s] %s" % (line.code, line.name) if line.code else line.name
