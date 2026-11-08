# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
from odoo import api, fields, models

from .qb_entity_config import ENTITY_SELECTION
from .qb_ui import friendly_errors


@friendly_errors
class QbMapping(models.Model):
    """Link table between one Odoo record and one QuickBooks object.

    It is the single source of truth for "is this record synced?" and stores
    the QuickBooks SyncToken used for optimistic-locking conflict detection."""
    _name = "qb.mapping"
    _description = "QuickBooks Record Mapping"
    _order = "qb_entity, id desc"
    _rec_name = "display_name"

    connection_id = fields.Many2one("qb.connection", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="connection_id.company_id", store=True)
    qb_entity = fields.Selection(ENTITY_SELECTION, string="Entity", required=True, index=True)
    qb_id = fields.Char("QuickBooks ID", required=True, index=True)
    qb_name = fields.Char("QuickBooks Name / Number")
    qb_sync_token = fields.Char("SyncToken")
    qb_last_updated = fields.Datetime("Last Updated in QuickBooks")
    res_model = fields.Char("Odoo Model", index=True)
    res_id = fields.Many2oneReference("Odoo Record ID", model_field="res_model", index=True)
    record_ref = fields.Reference(selection="_selection_target_models", compute="_compute_record_ref",
                                  inverse="_inverse_record_ref", string="Odoo Record")
    state = fields.Selection([
        ("synced", "Synced"),
        ("dirty", "Changed in Odoo"),
        ("unmapped", "Needs Mapping"),
        ("conflict", "Conflict"),
        ("deleted_qb", "Deleted in QuickBooks"),
        ("voided_qb", "Voided in QuickBooks"),
        ("deleted_odoo", "Deleted in Odoo"),
        ("error", "Error"),
    ], default="synced", required=True, index=True)
    last_sync = fields.Datetime(default=fields.Datetime.now)
    last_direction = fields.Selection([("import", "Imported"), ("export", "Exported")])
    payload_hash = fields.Char(help="Hash of the last exported payload, used to skip no-op updates.")
    suggestion_score = fields.Float(help="Smart-match confidence (0-100) when the mapping was auto-proposed.")
    note = fields.Text()

    _qb_uniq = models.Constraint(
        "UNIQUE(connection_id, qb_entity, qb_id)",
        "This QuickBooks record is already mapped.")
    _odoo_uniq = models.Index("(connection_id, res_model, res_id, qb_entity)")

    @api.model
    def _selection_target_models(self):
        models_ = ["res.partner", "product.product", "product.category", "account.account",
                   "account.tax", "account.payment.term", "account.journal",
                   "account.analytic.account", "account.move", "account.payment",
                   "sale.order", "purchase.order"]
        return [(m, self.env[m]._description) for m in models_ if m in self.env]

    @api.depends("res_model", "res_id")
    def _compute_record_ref(self):
        for rec in self:
            if rec.res_model and rec.res_id and rec.res_model in self.env:
                rec.record_ref = "%s,%s" % (rec.res_model, rec.res_id)
            else:
                rec.record_ref = False

    def _inverse_record_ref(self):
        for rec in self:
            if rec.record_ref:
                rec.res_model = rec.record_ref._name
                rec.res_id = rec.record_ref.id
                if rec.state == "unmapped":
                    rec.state = "synced"
            else:
                rec.res_id = False
                rec.state = "unmapped"

    @api.depends("qb_entity", "qb_id", "qb_name")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "%s #%s%s" % (rec.qb_entity, rec.qb_id,
                                             (" - %s" % rec.qb_name) if rec.qb_name else "")

    # ------------------------------------------------------------ helpers
    @api.model
    def _find(self, connection, qb_entity, qb_id):
        if not qb_id:
            return self.browse()
        return self.search([("connection_id", "=", connection.id), ("qb_entity", "=", qb_entity),
                            ("qb_id", "=", str(qb_id))], limit=1)

    @api.model
    def _find_record(self, connection, qb_entity, qb_id):
        mapping = self._find(connection, qb_entity, qb_id)
        if mapping and mapping.res_model and mapping.res_id:
            record = self.env[mapping.res_model].browse(mapping.res_id).exists()
            if record:
                return record
        return None

    @api.model
    def _for_record(self, connection, record, qb_entity=None):
        domain = [("connection_id", "=", connection.id), ("res_model", "=", record._name),
                  ("res_id", "=", record.id)]
        if qb_entity:
            domain.append(("qb_entity", "=", qb_entity))
        return self.search(domain, limit=1)

    @api.model
    def _link(self, connection, qb_entity, qb_obj_or_id, record, direction="import", name=None,
              payload_hash=None):
        """Create or update the mapping for ``record`` and the QBO object."""
        if isinstance(qb_obj_or_id, dict):
            qb_id = str(qb_obj_or_id.get("Id"))
            token = qb_obj_or_id.get("SyncToken")
            updated = (qb_obj_or_id.get("MetaData") or {}).get("LastUpdatedTime")
            name = name or qb_obj_or_id.get("DocNumber") or qb_obj_or_id.get("DisplayName") \
                or qb_obj_or_id.get("FullyQualifiedName") or qb_obj_or_id.get("Name")
        else:
            qb_id, token, updated = str(qb_obj_or_id), None, None
        vals = {
            "res_model": record._name if record else False,
            "res_id": record.id if record else False,
            "state": "synced" if record else "unmapped",
            "last_sync": fields.Datetime.now(),
            "last_direction": direction,
        }
        if token is not None:
            vals["qb_sync_token"] = str(token)
        if updated:
            vals["qb_last_updated"] = _parse_qb_datetime(updated)
        if name:
            vals["qb_name"] = str(name)[:200]
        if payload_hash:
            vals["payload_hash"] = payload_hash
        mapping = self._find(connection, qb_entity, qb_id)
        if mapping:
            mapping.write(vals)
        else:
            vals.update(connection_id=connection.id, qb_entity=qb_entity, qb_id=qb_id)
            mapping = self.create(vals)
        return mapping

    @api.model
    def _mark_dirty(self, records):
        if not records or self.env.context.get("qb_importing"):
            return
        mappings = self.sudo().search([("res_model", "=", records._name), ("res_id", "in", records.ids),
                                       ("state", "in", ("synced", "error"))])
        if mappings:
            mappings.write({"state": "dirty"})

    def action_open_record(self):
        self.ensure_one()
        if not self.record_ref:
            return False
        return {"type": "ir.actions.act_window", "res_model": self.res_model, "res_id": self.res_id,
                "view_mode": "form", "target": "current"}

    def action_reimport(self):
        for rec in self:
            rec.connection_id._enqueue("import", rec.qb_entity, qb_id=rec.qb_id, priority_boost=True)
        return self.connection_id[:1]._notify_queue_action()

    def action_reexport(self):
        for rec in self.filtered("record_ref"):
            rec.state = "dirty"
            rec.connection_id._enqueue("export", rec.qb_entity, record=rec.record_ref, priority_boost=True)
        return self.connection_id[:1]._notify_queue_action()

    def action_resolve_keep_odoo(self):
        return self.action_reexport()

    def action_resolve_keep_qb(self):
        self.write({"state": "synced"})
        return self.action_reimport()


def _parse_qb_datetime(value):
    """QBO returns '2026-11-05T10:12:13-08:00' -> naive UTC datetime."""
    from datetime import datetime, timezone
    if not value:
        return False
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
