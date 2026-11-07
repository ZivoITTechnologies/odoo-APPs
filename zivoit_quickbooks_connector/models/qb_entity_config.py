# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
from odoo import api, fields, models

# (QBO entity, label, odoo model, sequence, can_import, can_export, cdc_name)
ENTITIES = [
    ("Account", "Chart of Accounts", "account.account", 10, True, True, "Account"),
    ("TaxCode", "Taxes", "account.tax", 12, True, False, "TaxCode"),
    ("Term", "Payment Terms", "account.payment.term", 14, True, True, "Term"),
    ("PaymentMethod", "Payment Methods", "account.journal", 16, True, False, "PaymentMethod"),
    ("Class", "Classes", "account.analytic.account", 18, True, True, "Class"),
    ("Department", "Locations", "account.analytic.account", 19, True, True, "Department"),
    ("Customer", "Customers", "res.partner", 20, True, True, "Customer"),
    ("Vendor", "Vendors", "res.partner", 22, True, True, "Vendor"),
    ("ItemCategory", "Product Categories", "product.category", 28, True, True, None),
    ("Item", "Products", "product.product", 30, True, True, "Item"),
    ("Estimate", "Estimates", "sale.order", 40, True, True, "Estimate"),
    ("PurchaseOrder", "Purchase Orders", "purchase.order", 42, True, True, "PurchaseOrder"),
    ("Invoice", "Invoices", "account.move", 50, True, True, "Invoice"),
    ("CreditMemo", "Credit Notes", "account.move", 52, True, True, "CreditMemo"),
    ("SalesReceipt", "Sales Receipts", "account.move", 54, True, False, "SalesReceipt"),
    ("Bill", "Vendor Bills", "account.move", 56, True, True, "Bill"),
    ("VendorCredit", "Vendor Credits", "account.move", 58, True, True, "VendorCredit"),
    ("JournalEntry", "Journal Entries", "account.move", 60, True, True, "JournalEntry"),
    ("Payment", "Customer Payments", "account.payment", 70, True, True, "Payment"),
    ("BillPayment", "Vendor Payments", "account.payment", 72, True, True, "BillPayment"),
]
ENTITY_SELECTION = [(e[0], e[1]) for e in ENTITIES] + [("TaxCodePurchase", "Taxes (Purchase)")]
ENTITY_INFO = {e[0]: e for e in ENTITIES}
ENTITY_SEQUENCE = {e[0]: e[3] for e in ENTITIES}
ENTITY_SEQUENCE["TaxCodePurchase"] = 12
# Lower-case → canonical name (webhook CloudEvents types are lower-case)
ENTITY_CANONICAL = {e[0].lower(): e[0] for e in ENTITIES}

DIRECTIONS = [
    ("both", "Two-way"),
    ("import", "QuickBooks → Odoo"),
    ("export", "Odoo → QuickBooks"),
    ("none", "Disabled"),
]


class QbEntityConfig(models.Model):
    _name = "qb.entity.config"
    _description = "QuickBooks Entity Sync Settings"
    _order = "sequence, id"

    connection_id = fields.Many2one("qb.connection", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="connection_id.company_id", store=True)
    entity = fields.Selection(ENTITY_SELECTION, required=True)
    name = fields.Char(compute="_compute_label")
    odoo_model = fields.Char(compute="_compute_label")
    sequence = fields.Integer(compute="_compute_info", store=True)
    can_import = fields.Boolean(compute="_compute_info", store=True)
    can_export = fields.Boolean(compute="_compute_info", store=True)
    direction = fields.Selection(DIRECTIONS, required=True, default="both")
    auto_sync = fields.Boolean(
        default=True,
        help="Include this entity in automatic (scheduled / webhook) synchronization.")
    import_filter = fields.Char(
        help="Optional QuickBooks query WHERE clause applied on full imports, "
             "e.g. Active = true")
    last_import = fields.Datetime(readonly=True)
    last_export = fields.Datetime(readonly=True)
    mapped_count = fields.Integer(compute="_compute_counts")
    error_count = fields.Integer(compute="_compute_counts")

    _entity_uniq = models.Constraint(
        "UNIQUE(connection_id, entity)",
        "Each QuickBooks entity can only be configured once per connection.")

    @api.depends("entity")
    def _compute_label(self):
        for rec in self:
            info = ENTITY_INFO.get(rec.entity)
            rec.name = info[1] if info else rec.entity
            rec.odoo_model = info[2] if info else False

    @api.depends("entity")
    def _compute_info(self):
        for rec in self:
            info = ENTITY_INFO.get(rec.entity)
            rec.sequence = info[3] if info else 99
            rec.can_import = bool(info and info[4])
            rec.can_export = bool(info and info[5])

    def _compute_counts(self):
        Mapping = self.env["qb.mapping"]
        Job = self.env["qb.sync.job"]
        for rec in self:
            rec.mapped_count = Mapping.search_count([
                ("connection_id", "=", rec.connection_id.id), ("qb_entity", "=", rec.entity)])
            rec.error_count = Job.search_count([
                ("connection_id", "=", rec.connection_id.id), ("qb_entity", "=", rec.entity),
                ("state", "in", ("failed", "conflict"))])

    def allows(self, direction):
        self.ensure_one()
        if self.direction == "none":
            return False
        if direction == "import":
            return self.can_import and self.direction in ("both", "import")
        return self.can_export and self.direction in ("both", "export")

    def action_view_mappings(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "zivoit_quickbooks_connector.action_qb_mapping")
        action["domain"] = [("connection_id", "=", self.connection_id.id), ("qb_entity", "=", self.entity)]
        return action

    def action_import_now(self):
        for rec in self:
            rec.connection_id._full_import([rec.entity])
        return self.connection_id._notify_queue_action()

    def action_export_now(self):
        for rec in self:
            rec.connection_id._queue_pending_exports(entities=[rec.entity], force=True)
        return self.connection_id._notify_queue_action()
