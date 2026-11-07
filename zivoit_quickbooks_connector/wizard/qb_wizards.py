# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare

from ..lib.qbo_client import QBOClient
from ..models.qb_entity_config import ENTITIES


def _entity_selection(direction):
    idx = 4 if direction == "import" else 5
    return [(e[0], e[1]) for e in ENTITIES if e[idx]]


class QbImportWizard(models.TransientModel):
    _name = "qb.import.wizard"
    _description = "QuickBooks Import Wizard"

    connection_id = fields.Many2one("qb.connection", required=True)
    mode = fields.Selection([("all", "Everything (recommended order)"), ("select", "Selected entities")],
                            default="all", required=True)
    entity_ids = fields.Many2many("qb.entity.config", string="Entities",
                                  domain="[('connection_id','=',connection_id)]")
    date_from = fields.Date("Transactions from")
    date_to = fields.Date("Transactions to")
    run_now = fields.Boolean("Process immediately", default=False,
                             help="Process the queue right away instead of in the background. "
                                  "Use only for small volumes.")

    @api.onchange("connection_id")
    def _onchange_connection(self):
        if self.connection_id and not self.date_from:
            self.date_from = self.connection_id.import_start_date

    def action_import(self):
        self.ensure_one()
        conn = self.connection_id
        entities = None if self.mode == "all" else self.entity_ids.filtered("can_import").mapped("entity")
        if self.mode == "select" and not entities:
            raise UserError(_("Select at least one entity to import."))
        queued = conn._full_import(entities=entities, date_from=self.date_from, date_to=self.date_to,
                                   source="wizard")
        if self.run_now:
            while conn._process_queue():
                pass
        return conn._notify(_("Import queued"), _("%s QuickBooks record(s) queued for import.", queued), "success")


class QbExportWizard(models.TransientModel):
    _name = "qb.export.wizard"
    _description = "QuickBooks Export Wizard"

    connection_id = fields.Many2one("qb.connection", required=True)
    entity_ids = fields.Many2many("qb.entity.config", string="Entities",
                                  domain="[('connection_id','=',connection_id),('can_export','=',True)]")
    include_history = fields.Boolean("Ignore export start date",
                                     help="Also export documents dated before the connection's export start date.")
    run_now = fields.Boolean("Process immediately", default=False)

    def action_export(self):
        self.ensure_one()
        conn = self.connection_id
        entities = self.entity_ids.mapped("entity") or None
        queued = conn._queue_pending_exports(entities=entities, force=self.include_history, source="wizard")
        if self.run_now:
            while conn._process_queue():
                pass
        return conn._notify(_("Export queued"), _("%s Odoo record(s) queued for export.", queued), "success")


class QbIntegrityWizard(models.TransientModel):
    _name = "qb.integrity.wizard"
    _description = "QuickBooks Integrity Check"

    connection_id = fields.Many2one("qb.connection", required=True)
    date_from = fields.Date(default=lambda s: fields.Date.today().replace(day=1))
    date_to = fields.Date(default=fields.Date.today)
    check_balance = fields.Boolean("Compare open balances", default=True)
    line_ids = fields.One2many("qb.integrity.line", "wizard_id", readonly=True)
    state = fields.Selection([("draft", "Draft"), ("done", "Done")], default="draft")
    checked_count = fields.Integer(readonly=True)
    issue_count = fields.Integer(readonly=True)

    def action_check(self):
        """Compare every synced document's total (and open balance) with
        QuickBooks and list missing / orphan / mismatching documents."""
        self.ensure_one()
        conn = self.connection_id
        client = conn._get_client()
        Mapping = self.env["qb.mapping"]
        self.line_ids.unlink()
        lines, checked = [], 0
        for entity in ("Invoice", "CreditMemo", "Bill", "VendorCredit", "Payment", "BillPayment"):
            where = "TxnDate >= '%s' AND TxnDate <= '%s'" % (self.date_from, self.date_to)
            remote = {r["Id"]: r for r in client.query_all(entity, where=where)}
            maps = Mapping.search([("connection_id", "=", conn.id), ("qb_entity", "=", entity),
                                   ("res_id", "!=", False)])
            for qb_id, data in remote.items():
                checked += 1
                mapping = maps.filtered(lambda m: m.qb_id == qb_id)[:1]
                record = mapping.record_ref if mapping else None
                if not record:
                    lines.append({"entity": entity, "qb_id": qb_id, "qb_ref": data.get("DocNumber"),
                                  "issue": "missing_odoo", "qb_amount": data.get("TotalAmt")})
                    continue
                odoo_total = record.amount_total if record._name == "account.move" else record.amount
                odoo_state = record.state
                if float_compare(abs(odoo_total), float(data.get("TotalAmt") or 0), precision_digits=2):
                    lines.append({"entity": entity, "qb_id": qb_id, "qb_ref": data.get("DocNumber"),
                                  "res_model": record._name, "res_id": record.id, "issue": "amount",
                                  "qb_amount": data.get("TotalAmt"), "odoo_amount": odoo_total})
                elif self.check_balance and record._name == "account.move" and "Balance" in data and \
                        odoo_state == "posted" and float_compare(abs(record.amount_residual),
                                                                 float(data["Balance"] or 0), precision_digits=2):
                    lines.append({"entity": entity, "qb_id": qb_id, "qb_ref": data.get("DocNumber"),
                                  "res_model": record._name, "res_id": record.id, "issue": "balance",
                                  "qb_amount": data.get("Balance"), "odoo_amount": record.amount_residual})
            for mapping in maps.filtered(lambda m: m.qb_id not in remote and m.state == "synced"):
                record = mapping.record_ref
                date = getattr(record, "invoice_date", None) or getattr(record, "date", None) if record else None
                if record and date and self.date_from <= date <= self.date_to:
                    checked += 1
                    lines.append({"entity": entity, "qb_id": mapping.qb_id, "res_model": record._name,
                                  "res_id": record.id, "issue": "missing_qb",
                                  "odoo_amount": record.amount_total if record._name == "account.move"
                                  else record.amount})
        self.write({"line_ids": [(0, 0, l) for l in lines], "state": "done", "checked_count": checked,
                    "issue_count": len(lines)})
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id, "view_mode": "form",
                "target": "new"}

    def action_fix_all(self):
        self.ensure_one()
        for line in self.line_ids:
            line.action_fix()
        return self.action_check()


class QbIntegrityLine(models.TransientModel):
    _name = "qb.integrity.line"
    _description = "QuickBooks Integrity Issue"

    wizard_id = fields.Many2one("qb.integrity.wizard", ondelete="cascade")
    entity = fields.Char()
    qb_id = fields.Char("QuickBooks ID")
    qb_ref = fields.Char("QuickBooks No.")
    res_model = fields.Char()
    res_id = fields.Integer()
    issue = fields.Selection([("missing_odoo", "Missing in Odoo"), ("missing_qb", "Missing in QuickBooks"),
                              ("amount", "Total differs"), ("balance", "Open balance differs")])
    qb_amount = fields.Float()
    odoo_amount = fields.Float()
    difference = fields.Float(compute="_compute_diff")

    def _compute_diff(self):
        for rec in self:
            rec.difference = abs(rec.odoo_amount or 0) - abs(rec.qb_amount or 0)

    def action_fix(self):
        for rec in self:
            conn = rec.wizard_id.connection_id
            if rec.issue in ("missing_odoo", "amount", "balance") and rec.qb_id:
                conn.with_context(qb_force=True)._enqueue("import", rec.entity, qb_id=rec.qb_id,
                                                          priority_boost=True)._run_one()
            elif rec.issue == "missing_qb" and rec.res_model:
                record = self.env[rec.res_model].browse(rec.res_id)
                conn.with_context(qb_force=True)._enqueue("export", rec.entity, record=record,
                                                          priority_boost=True).with_context(qb_force=True)._run_one()
        return True

    def action_open(self):
        self.ensure_one()
        if not self.res_model:
            return False
        return {"type": "ir.actions.act_window", "res_model": self.res_model, "res_id": self.res_id,
                "view_mode": "form", "target": "current"}


class QbTokenWizard(models.TransientModel):
    _name = "qb.token.wizard"
    _description = "QuickBooks Manual Token Entry"

    connection_id = fields.Many2one("qb.connection", required=True)
    realm_id = fields.Char("Company ID (Realm)", required=True)
    refresh_token = fields.Char(required=True)

    def action_apply(self):
        self.ensure_one()
        conn = self.connection_id.sudo()
        conn.realm_id = self.realm_id.strip()
        client = QBOClient(conn.client_id, conn.client_secret, realm_id=conn.realm_id,
                           environment=conn.environment, refresh_token=self.refresh_token.strip(),
                           token_saver=conn._save_tokens)
        client.refresh()
        conn._after_connect()
        return conn._notify(_("Connected"), _("Connected to %s.", conn.qb_company_name or conn.realm_id), "success")
