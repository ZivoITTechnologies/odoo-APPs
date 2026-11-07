# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
from odoo import _, fields, models
from odoo.exceptions import UserError


class QbSyncedMixin(models.AbstractModel):
    """Adds QuickBooks status fields, change tracking and buttons to a model."""
    _name = "qb.synced.mixin"
    _description = "QuickBooks Synced Record"

    # Fields whose modification marks the QuickBooks copy as outdated.
    _qb_tracked_fields = ()

    qb_id = fields.Char("QuickBooks ID", compute="_compute_qb_status", search="_search_qb_id")
    qb_sync_state = fields.Selection([
        ("not_synced", "Not in QuickBooks"), ("synced", "Synced"), ("dirty", "Pending update"),
        ("conflict", "Conflict"), ("error", "Error"), ("deleted_qb", "Deleted in QuickBooks"),
        ("voided_qb", "Voided"), ("deleted_odoo", "Deleted"), ("unmapped", "Needs Mapping")],
        string="QuickBooks Status", compute="_compute_qb_status", search="_search_qb_sync_state")
    qb_last_sync = fields.Datetime("Last QuickBooks Sync", compute="_compute_qb_status")
    qb_mapping_count = fields.Integer(compute="_compute_qb_status")

    def _qb_mappings(self):
        return self.env["qb.mapping"].sudo().search([("res_model", "=", self._name), ("res_id", "in", self.ids)])

    def _compute_qb_status(self):
        by_id = {}
        for m in self._qb_mappings():
            by_id.setdefault(m.res_id, []).append(m)
        for rec in self:
            maps = by_id.get(rec._origin.id or rec.id, [])
            main = maps[0] if maps else None
            rec.qb_id = ", ".join(m.qb_id for m in maps) if maps else False
            rec.qb_sync_state = main.state if main else "not_synced"
            rec.qb_last_sync = max((m.last_sync for m in maps if m.last_sync), default=False)
            rec.qb_mapping_count = len(maps)

    def _search_qb_id(self, operator, value):
        maps = self.env["qb.mapping"].sudo().search([("res_model", "=", self._name), ("qb_id", operator, value)])
        return [("id", "in", maps.mapped("res_id"))]

    def _search_qb_sync_state(self, operator, value):
        if operator not in ("=", "!=", "in", "not in"):
            raise UserError(_("Unsupported search on QuickBooks status."))
        values = [value] if isinstance(value, str) or value is False else list(value)
        positive = operator in ("=", "in")
        all_maps = self.env["qb.mapping"].sudo().search([("res_model", "=", self._name)])
        if "not_synced" in values:
            ids = all_maps.mapped("res_id")
            return [("id", "not in" if positive else "in", ids)]
        ids = all_maps.filtered(lambda m: m.state in values).mapped("res_id")
        return [("id", "in" if positive else "not in", ids)]

    # ------------------------------------------------------------ tracking
    def write(self, vals):
        res = super().write(vals)
        if self._qb_tracked_fields and not self.env.context.get("qb_importing") \
                and set(vals) & set(self._qb_tracked_fields):
            self.env["qb.mapping"]._mark_dirty(self)
        return res

    def unlink(self):
        if not self.env.context.get("qb_importing"):
            for mapping in self._qb_mappings():
                conn = mapping.connection_id
                if conn.state == "connected" and conn.odoo_delete_policy == "propagate" \
                        and conn._allows(mapping.qb_entity, "export"):
                    conn._enqueue("delete", mapping.qb_entity, qb_id=mapping.qb_id, source="auto")
                mapping.write({"state": "deleted_odoo", "res_id": False})
        return super().unlink()

    # ------------------------------------------------------------- helpers
    def _qb_entity(self):
        """Return the QuickBooks entity name for this record (override)."""
        raise NotImplementedError

    def _qb_connection(self):
        self.ensure_one()
        company = self.company_id if "company_id" in self._fields and self.company_id else self.env.company
        conn = self.env["qb.connection"].search([("company_id", "=", company.id), ("state", "=", "connected")],
                                                 limit=1)
        return conn

    def _qb_auto_export(self):
        """Queue (and trigger) an export after a business event."""
        for rec in self:
            entity = rec._qb_entity()
            if not entity:
                continue
            conn = rec._qb_connection()
            if not conn or not conn.auto_export_on_post or not conn._allows(entity, "export") \
                    or not conn._config(entity).auto_sync:
                continue
            conn._enqueue("export", entity, record=rec, source="auto", priority_boost=True)
            cron = self.env.ref("zivoit_quickbooks_connector.ir_cron_qb_process_queue", raise_if_not_found=False)
            if cron:
                cron._trigger()

    def action_qb_export(self):
        """Button: send to QuickBooks now (synchronously)."""
        results = []
        for rec in self:
            entity = rec._qb_entity()
            conn = rec._qb_connection()
            if not conn:
                raise UserError(_("No connected QuickBooks connection for company %s.", rec.env.company.name))
            if not entity:
                raise UserError(_("This document type is not synchronized with QuickBooks."))
            job = conn.with_context(qb_force=True)._enqueue("export", entity, record=rec, source="manual",
                                                             priority_boost=True)
            job.with_context(qb_force=True)._run_one()
            results.append(job)
        failed = [j for j in results if j.state in ("failed", "conflict")]
        if failed:
            raise UserError(_("QuickBooks export failed:\n%s", "\n".join(j.error or "" for j in failed)))
        return self.env["qb.connection"]._notify(_("QuickBooks"), _("Sent to QuickBooks."), "success")

    def action_qb_view_mappings(self):
        action = self.env["ir.actions.act_window"]._for_xml_id("zivoit_quickbooks_connector.action_qb_mapping")
        action["domain"] = [("res_model", "=", self._name), ("res_id", "in", self.ids)]
        return action
