import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import plaintext2html

EXPEDITE_STATUS = [
    ("on_track", "On Track"),
    ("at_risk", "At Risk"),
    ("late", "Late"),
    ("received", "Received"),
]


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"

    epc_project_id = fields.Many2one("project.project", string="EPC Project", index=True,
                                     domain=[("is_epc", "=", True)])
    epc_activity_ids = fields.Many2many(
        "epc.activity", "epc_activity_purchase_line_rel", "purchase_line_id", "activity_id",
        string="Needed by Activities", copy=False, groups="zivoit_epc_ai.group_epc_user")
    epc_required_date = fields.Date(string="Required at Site",
                                    help="Latest date the material is needed on site, from the schedule.")
    epc_forecast_date = fields.Date(string="Forecast Delivery", readonly=True, copy=False)
    epc_predicted_delay_days = fields.Integer(string="Predicted Delay (days)", readonly=True, copy=False)
    epc_expedite_status = fields.Selection(EXPEDITE_STATUS, string="Expediting Status", readonly=True, copy=False)
    epc_supplier_risk = fields.Float(related="partner_id.epc_supplier_risk_score", string="Supplier Risk")
    epc_expedite_note = fields.Text(string="Expediting Notes")
    epc_last_followup = fields.Date(string="Last Follow-up", copy=False)

    @api.onchange("epc_activity_ids")
    def _onchange_epc_activity_ids(self):
        starts = [d for d in self.epc_activity_ids.mapped("planned_start") if d]
        if starts and not self.epc_required_date:
            self.epc_required_date = min(starts)
        if self.epc_activity_ids and not self.epc_project_id:
            self.epc_project_id = self.epc_activity_ids[0].project_id

    def _epc_fallback_followup(self):
        lines = "\n".join(
            "- %s %s: %s %s, promised %s, forecast %s"
            % (l.order_id.name, l.name.split("\n")[0], l.product_qty - l.qty_received, l.product_uom_id.name or "",
               fields.Date.to_date(l.date_planned) if l.date_planned else "-", l.epc_forecast_date or "-")
            for l in self)
        partner = self[:1].partner_id
        return (
            "Dear %s,\n\nWe are following up on the open items below for our project %s. "
            "These materials are on our construction critical path. Please confirm the dispatch date, "
            "the current manufacturing or inspection status, and any constraint we can help resolve.\n\n%s\n\n"
            "Kindly reply within 2 working days.\n\nRegards,\n%s"
            % (partner.name, self[:1].epc_project_id.name or "", lines, self.env.user.name))

    def action_epc_draft_followup(self):
        """Draft a vendor follow-up email (as a note on the PO) — sent only after human review."""
        if not self:
            return True
        for partner in self.mapped("partner_id"):
            lines = self.filtered(lambda l: l.partner_id == partner)
            facts = [{
                "po": l.order_id.name,
                "item": l.name,
                "open_qty": l.product_qty - l.qty_received,
                "unit": l.product_uom_id.name,
                "promised": str(fields.Date.to_date(l.date_planned)) if l.date_planned else "",
                "required_at_site": str(l.epc_required_date or ""),
                "forecast": str(l.epc_forecast_date or ""),
                "status": l.epc_expedite_status or "",
            } for l in lines]
            answer = self.env["epc.ai.service"].ask(
                "expediting_followup",
                "You are an EPC expediting engineer. Write a polite, firm vendor follow-up email in plain text "
                "asking for dispatch confirmation for the open purchase order items. Mention that the items are "
                "on the construction critical path. Use only the facts given. No subject line.",
                json.dumps({"vendor": partner.name, "project": lines[:1].epc_project_id.name or "",
                            "sender": self.env.user.name, "items": facts}),
                lines._epc_fallback_followup, record=lines[:1].order_id)
            for order in lines.mapped("order_id"):
                order.sudo().message_post(
                    author_id=self.env.user.partner_id.id,
                    body=plaintext2html(_("Draft follow-up for review (not sent):\n\n%s", answer["result"])),
                    message_type="comment", subtype_xmlid="mail.mt_note")
            lines.sudo().write({"epc_last_followup": fields.Date.context_today(self)})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Follow-up drafted"),
                "message": _("Draft emails were added as notes on the purchase orders for your review."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_epc_refresh_forecast(self):
        projects = self.mapped("epc_project_id")
        if not projects:
            raise UserError(_("Link the lines to an EPC project first."))
        engine = self.env["epc.prediction.engine"]
        engine._update_supplier_scores(self.mapped("partner_id"))
        engine._forecast_purchase_lines(self)
        return True
