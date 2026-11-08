# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..lib.qbo_client import QBOError
from .qb_ui import friendly_errors


@friendly_errors
class ResPartner(models.Model):
    _name = "res.partner"
    _inherit = ["res.partner", "qb.synced.mixin"]
    _qb_tracked_fields = ("name", "email", "phone", "street", "street2", "city", "zip", "state_id",
                          "country_id", "vat", "website", "comment", "active", "is_company", "ref",
                          "property_payment_term_id", "property_supplier_payment_term_id")

    def _qb_entity(self):
        self.ensure_one()
        if self.supplier_rank and not self.customer_rank:
            return "Vendor"
        return "Customer"

    def action_qb_export(self):
        res = super().action_qb_export()
        # a partner that is both customer and vendor is exported as both
        for rec in self.filtered(lambda p: p.customer_rank and p.supplier_rank):
            conn = rec._qb_connection()
            if conn and conn._allows("Vendor", "export"):
                job = conn.with_context(qb_force=True)._enqueue("export", "Vendor", record=rec, priority_boost=True)
                job.with_context(qb_force=True)._run_one()
        return res


class ProductProduct(models.Model):
    _name = "product.product"
    _inherit = ["product.product", "qb.synced.mixin"]
    _qb_tracked_fields = ("name", "default_code", "lst_price", "list_price", "standard_price", "active",
                          "description_sale", "description_purchase", "taxes_id", "supplier_taxes_id",
                          "categ_id", "type", "sale_ok", "purchase_ok")

    def _qb_entity(self):
        return "Item"


@friendly_errors
class ProductTemplate(models.Model):
    _inherit = "product.template"

    qb_sync_state = fields.Selection(selection=lambda s: s.env["product.product"]._fields["qb_sync_state"].selection,
                                     string="QuickBooks Status", compute="_compute_qb_template_status")
    qb_id = fields.Char("QuickBooks ID", compute="_compute_qb_template_status")

    def _compute_qb_template_status(self):
        for tmpl in self:
            variant = tmpl.product_variant_ids[:1]
            tmpl.qb_sync_state = variant.qb_sync_state if variant else "not_synced"
            tmpl.qb_id = variant.qb_id if variant else False

    def write(self, vals):
        res = super().write(vals)
        tracked = set(self.env["product.product"]._qb_tracked_fields)
        if not self.env.context.get("qb_importing") and set(vals) & tracked:
            self.env["qb.mapping"]._mark_dirty(self.product_variant_ids)
        return res

    def action_qb_export(self):
        return self.product_variant_ids.action_qb_export()


class ProductCategory(models.Model):
    _name = "product.category"
    _inherit = ["product.category", "qb.synced.mixin"]
    _qb_tracked_fields = ("name", "parent_id")

    def _qb_entity(self):
        return "ItemCategory"


class AccountAccount(models.Model):
    _name = "account.account"
    _inherit = ["account.account", "qb.synced.mixin"]
    _qb_tracked_fields = ("name", "code", "active")

    def _qb_entity(self):
        return "Account"


class AccountPaymentTerm(models.Model):
    _name = "account.payment.term"
    _inherit = ["account.payment.term", "qb.synced.mixin"]
    _qb_tracked_fields = ("name", "line_ids", "active")

    def _qb_entity(self):
        return "Term"


class AccountAnalyticAccount(models.Model):
    _name = "account.analytic.account"
    _inherit = ["account.analytic.account", "qb.synced.mixin"]
    _qb_tracked_fields = ("name", "active")

    def _qb_entity(self):
        self.ensure_one()
        conn = self._qb_connection()
        if conn and self.plan_id == conn.location_plan_id:
            return "Department"
        return "Class"


@friendly_errors
class AccountMove(models.Model):
    _name = "account.move"
    _inherit = ["account.move", "qb.synced.mixin"]
    _qb_tracked_fields = ()  # documents are re-exported on (re)validation

    qb_payment_link = fields.Char("QuickBooks Payment Link", copy=False, readonly=True)

    def _qb_entity(self):
        self.ensure_one()
        return self.env["qb.exporter"].entity_for_move(self)

    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        if not self.env.context.get("qb_importing"):
            to_export = posted.filtered(lambda m: m.move_type != "entry" or (
                m.journal_id.type == "general" and not m.origin_payment_id and not m.statement_line_id))
            self.env["qb.mapping"]._mark_dirty(to_export)
            to_export._qb_auto_export()
        return posted

    def button_cancel(self):
        res = super().button_cancel()
        if not self.env.context.get("qb_importing"):
            self._qb_propagate_cancel()
        return res

    def _qb_propagate_cancel(self):
        for mapping in self._qb_mappings():
            conn = mapping.connection_id
            if mapping.state in ("voided_qb", "deleted_qb", "deleted_odoo"):
                continue
            if conn.state == "connected" and conn.odoo_delete_policy == "propagate" \
                    and conn._allows(mapping.qb_entity, "export"):
                conn._enqueue("void", mapping.qb_entity, qb_id=mapping.qb_id,
                              record=self.browse(mapping.res_id), source="auto", priority_boost=True)

    def action_qb_fetch_pdf(self):
        self.ensure_one()
        mapping = self._qb_mappings()[:1]
        if not mapping:
            raise UserError(_("This document is not linked to QuickBooks."))
        client = mapping.connection_id._get_client()
        ctx = type("C", (), {"client": client})()
        self.env["qb.importer"]._attach_pdf(ctx, mapping.qb_entity, mapping.qb_id, self)
        return mapping.connection_id._notify(_("QuickBooks"), _("PDF attached to the document."), "success")

    def action_qb_send_email(self):
        """Ask QuickBooks to e-mail the invoice (with its online payment link)."""
        for move in self:
            mapping = move._qb_mappings()[:1]
            if not mapping or mapping.qb_entity not in ("Invoice", "CreditMemo"):
                raise UserError(_("%s is not a QuickBooks invoice.", move.display_name))
            client = mapping.connection_id._get_client()
            try:
                client.send(mapping.qb_entity, mapping.qb_id, move.partner_id.email or None)
            except QBOError as exc:
                raise UserError(_("QuickBooks could not send the e-mail: %s", exc)) from exc
            move.message_post(body=_("Invoice e-mailed through QuickBooks Online."))
        return self.env["qb.connection"]._notify(_("QuickBooks"), _("E-mail sent by QuickBooks."), "success")

    def action_qb_refresh(self):
        """Re-import this document from QuickBooks."""
        for mapping in self._qb_mappings():
            mapping.connection_id._enqueue("import", mapping.qb_entity, qb_id=mapping.qb_id,
                                           priority_boost=True)._run_one()
        return True


class AccountPayment(models.Model):
    _name = "account.payment"
    _inherit = ["account.payment", "qb.synced.mixin"]

    def _qb_entity(self):
        self.ensure_one()
        return self.env["qb.exporter"].entity_for_payment(self)

    def action_post(self):
        res = super().action_post()
        if not self.env.context.get("qb_importing"):
            self._qb_auto_export()
        return res

    def action_cancel(self):
        res = super().action_cancel()
        if not self.env.context.get("qb_importing"):
            for mapping in self._qb_mappings():
                conn = mapping.connection_id
                if conn.state == "connected" and conn.odoo_delete_policy == "propagate" \
                        and conn._allows(mapping.qb_entity, "export") and mapping.state != "voided_qb":
                    conn._enqueue("void", mapping.qb_entity, qb_id=mapping.qb_id, source="auto")
        return res


class AccountPartialReconcile(models.Model):
    _inherit = "account.partial.reconcile"

    @api.model_create_multi
    def create(self, vals_list):
        partials = super().create(vals_list)
        if not self.env.context.get("qb_importing"):
            payments = (partials.debit_move_id.move_id | partials.credit_move_id.move_id).origin_payment_id
            mapped = payments.filtered(lambda p: p._qb_mappings())
            if mapped:
                self.env["qb.mapping"]._mark_dirty(mapped)
                mapped._qb_auto_export()
            payments.filtered(lambda p: not p._qb_mappings() and p.state not in ("draft", "canceled")) \
                ._qb_auto_export()
        return partials


class SaleOrder(models.Model):
    _name = "sale.order"
    _inherit = ["sale.order", "qb.synced.mixin"]
    _qb_tracked_fields = ("partner_id", "order_line", "validity_date", "date_order", "state")

    def _qb_entity(self):
        return "Estimate"

    def action_confirm(self):
        res = super().action_confirm()
        if not self.env.context.get("qb_importing"):
            self._qb_auto_export()
        return res


class PurchaseOrder(models.Model):
    _name = "purchase.order"
    _inherit = ["purchase.order", "qb.synced.mixin"]
    _qb_tracked_fields = ("partner_id", "order_line", "date_order", "state", "locked")

    def _qb_entity(self):
        return "PurchaseOrder"

    def button_confirm(self):
        res = super().button_confirm()
        if not self.env.context.get("qb_importing"):
            self._qb_auto_export()
        return res

    def button_cancel(self):
        res = super().button_cancel()
        if not self.env.context.get("qb_importing"):
            for mapping in self._qb_mappings():
                conn = mapping.connection_id
                if conn.state == "connected" and conn.odoo_delete_policy == "propagate" \
                        and conn._allows("PurchaseOrder", "export"):
                    conn._enqueue("void", "PurchaseOrder", qb_id=mapping.qb_id, source="auto")
        return res
