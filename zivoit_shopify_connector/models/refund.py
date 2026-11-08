import logging
import uuid

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Command

from ..lib import queries as Q
from .order import parse_dt

_logger = logging.getLogger(__name__)


class ZivoShopifyRefund(models.Model):
    _name = "zivo.shopify.refund"
    _description = "Shopify Refund"
    _order = "id desc"

    order_id = fields.Many2one("sale.order", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="order_id.company_id", store=True, index=True)
    shopify_gid = fields.Char(required=True, index=True)
    note = fields.Char()
    amount = fields.Float(digits=(16, 4))
    move_id = fields.Many2one("account.move", string="Credit Note", ondelete="set null", index=True)
    return_picking_id = fields.Many2one("stock.picking", ondelete="set null")
    state = fields.Selection([
        ("credited", "Credit Note Created"),
        ("absorbed", "Reflected on Order"),
        ("exported", "Created from Odoo"),
    ], default="credited", required=True)

    _gid_unique = models.Constraint("UNIQUE(order_id, shopify_gid)", "Refund already imported.")


class ZivoShopifyReturn(models.Model):
    _name = "zivo.shopify.return"
    _description = "Shopify Return"
    _order = "id desc"

    name = fields.Char(required=True)
    order_id = fields.Many2one("sale.order", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="order_id.company_id", store=True, index=True)
    shopify_gid = fields.Char(required=True, index=True)
    status = fields.Char()
    summary = fields.Text()
    line_data = fields.Json()
    picking_id = fields.Many2one("stock.picking", string="Odoo Return", ondelete="set null")

    _gid_unique = models.Constraint("UNIQUE(order_id, shopify_gid)", "Return already imported.")

    def action_create_picking(self):
        for record in self.filtered(lambda r: not r.picking_id):
            quantities = {}
            for item in record.line_data or []:
                quantities[item["line_gid"]] = quantities.get(item["line_gid"], 0) + item["quantity"]
            record.picking_id = record.order_id._zivo_create_return_picking(quantities)
        return True


class SaleOrderRefund(models.Model):
    _inherit = "sale.order"

    zivo_refund_ids = fields.One2many("zivo.shopify.refund", "order_id", string="Shopify Refunds")
    zivo_return_ids = fields.One2many("zivo.shopify.return", "order_id", string="Shopify Returns")

    @staticmethod
    def _zivo_is_pure_cancel(refund):
        """Items cancelled before shipping, with no other amount refunded separately."""
        items = (refund.get("refundLineItems") or {}).get("nodes") or []
        return bool(items) and all(item.get("restockType") == "CANCEL" for item in items) \
            and not (refund.get("refundShippingLines") or {}).get("nodes")

    def _zivo_refunded_quantities(self, node):
        # Pure cancellations must lower the order; other refunds stay visible as credit notes.
        refunded = {}
        for refund in node.get("refunds") or []:
            if self._zivo_is_pure_cancel(refund):
                continue
            for item in (refund.get("refundLineItems") or {}).get("nodes") or []:
                gid = item["lineItem"]["id"]
                refunded[gid] = refunded.get(gid, 0) + item["quantity"]
        return refunded

    @api.model
    def _zivo_create_order(self, instance, node):
        order = super()._zivo_create_order(instance, node)
        # Cancellations that happened before the import are already in the quantities.
        for refund in node.get("refunds") or []:
            if self._zivo_is_pure_cancel(refund):
                self.env["zivo.shopify.refund"].create({
                    "order_id": order.id, "shopify_gid": refund["id"], "state": "absorbed", "note": refund.get("note"),
                    "amount": self._zivo_money(instance, refund.get("totalRefundedSet")),
                })
        return order

    def _zivo_refresh(self, node):
        # Refunds first: a credit note frees invoiced quantity for the edit step.
        self.ensure_one()
        if not node.get("cancelledAt"):
            self._zivo_apply_refunds(node)
        return super()._zivo_refresh(node)

    def _zivo_apply_refunds(self, node):
        self.ensure_one()
        Refund = self.env["zivo.shopify.refund"]
        instance = self.zivo_instance_id
        for data in node.get("refunds") or []:
            if self.zivo_refund_ids.filtered(lambda r: r.shopify_gid == data["id"]):
                continue
            invoices = self.invoice_ids.filtered(lambda m: m.state == "posted" and m.move_type == "out_invoice")
            amount = self._zivo_money(instance, data.get("totalRefundedSet"))
            pure_cancel = self._zivo_is_pure_cancel(data)
            if not invoices and not pure_cancel and self.state != "cancel" and not self.currency_id.is_zero(amount):
                # Nothing to credit yet. A later order refresh will pick this refund up.
                continue
            move = self.env["account.move"]
            if invoices and not self.currency_id.is_zero(amount):
                move = self._zivo_create_credit_note(data, invoices, amount)
                self._zivo_pay_credit_note(move, data, node)
            picking = self._zivo_return_for_refund(data)
            Refund.create({
                "order_id": self.id, "shopify_gid": data["id"], "note": data.get("note"), "amount": amount,
                "move_id": move.id, "return_picking_id": picking.id,
                "state": "credited" if move else "absorbed",
            })

    def _zivo_create_credit_note(self, data, invoices, amount):
        self.ensure_one()
        instance = self.zivo_instance_id
        lines = []
        for item in (data.get("refundLineItems") or {}).get("nodes") or []:
            sale_line = self.order_line.filtered(lambda l: l.zivo_line_gid == item["lineItem"]["id"])[:1]
            if not sale_line:
                continue
            lines.append(Command.create({
                "product_id": sale_line.product_id.id,
                "name": sale_line.name,
                "quantity": item["quantity"],
                "price_unit": sale_line.price_unit,
                "discount": sale_line.discount,
                "tax_ids": [Command.set(sale_line.tax_ids.ids)],
                "sale_line_ids": [Command.set(sale_line.ids)],
            }))
        delivery_lines = self.order_line.filtered("is_delivery")
        for shipping in (data.get("refundShippingLines") or {}).get("nodes") or []:
            net = self._zivo_money(instance, shipping.get("subtotalAmountSet"))
            tax = self._zivo_money(instance, shipping.get("taxAmountSet"))
            source = delivery_lines[:1]
            included = any(source.tax_ids.mapped("price_include"))
            lines.append(Command.create({
                "product_id": instance.shipping_product_id.id,
                "name": self.env._("Shipping refund"),
                "quantity": 1,
                "price_unit": net + tax if included else net,
                "tax_ids": [Command.set(source.tax_ids.ids)],
            }))
        reference = invoices[:1]
        move = self.env["account.move"].with_company(self.company_id).create({
            "move_type": "out_refund",
            "partner_id": self.partner_invoice_id.id,
            "currency_id": self.currency_id.id,
            "invoice_origin": self.name,
            "reversed_entry_id": reference.id,
            "journal_id": reference.journal_id.id,
            "invoice_date": (parse_dt(data.get("createdAt")) or fields.Datetime.now()).date(),
            "ref": f"Shopify refund {self.zivo_shopify_name}",
            "narration": data.get("note") or False,
            "invoice_line_ids": lines,
            "zivo_refund_gid": data["id"],
        })
        difference = self.currency_id.round(amount - move.amount_total)
        if not self.currency_id.is_zero(difference):
            # Partial refunds and goodwill amounts have no matching order line.
            move.write({"invoice_line_ids": [Command.create({
                "product_id": instance.adjustment_product_id.id,
                "name": data.get("note") or self.env._("Refund adjustment"),
                "quantity": 1,
                "price_unit": difference,
                "tax_ids": [Command.clear()],
            })]})
        move.action_post()
        return move

    def _zivo_pay_credit_note(self, move, data, node):
        self.ensure_one()
        workflow = self._zivo_workflow(node)
        if not workflow.register_payment or not workflow.payment_journal_id:
            return
        refunded = sum(
            self._zivo_money(self.zivo_instance_id, t.get("amountSet"))
            for t in (data.get("transactions") or {}).get("nodes") or []
            if t.get("kind") == "REFUND" and t.get("status") == "SUCCESS"
        )
        amount = min(refunded, move.amount_residual)
        if self.currency_id.is_zero(amount):
            return
        lines = move.line_ids.filtered(lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled)
        self.env["account.payment.register"].with_context(active_model="account.move.line", active_ids=lines.ids).create({
            "journal_id": workflow.payment_journal_id.id,
            "amount": amount,
            "communication": f"Refund {self.zivo_shopify_name}",
            "payment_difference_handling": "open",
        })._create_payments()

    def _zivo_return_for_refund(self, data):
        """Bring goods back when Shopify says they were physically returned."""
        self.ensure_one()
        quantities = {}
        for item in (data.get("refundLineItems") or {}).get("nodes") or []:
            if item.get("restockType") == "RETURN" and item.get("restocked"):
                quantities[item["lineItem"]["id"]] = quantities.get(item["lineItem"]["id"], 0) + item["quantity"]
        if not quantities:
            return self.env["stock.picking"]
        return self._zivo_create_return_picking(quantities)

    def _zivo_create_return_picking(self, quantities):
        self.ensure_one()
        delivered = self.picking_ids.filtered(lambda p: p.state == "done" and p.picking_type_code == "outgoing")
        for picking in delivered:
            relevant = picking.move_ids.filtered(lambda m: m.sale_line_id.zivo_line_gid in quantities and m.state == "done")
            if not relevant:
                continue
            wizard = self.env["stock.return.picking"].with_context(active_id=picking.id, active_model="stock.picking").create({"picking_id": picking.id})
            wanted = False
            for line in wizard.product_return_moves:
                quantity = min(quantities.get(line.move_id.sale_line_id.zivo_line_gid, 0), line.move_id.quantity)
                line.write({"quantity": quantity, "to_refund": True})
                wanted = wanted or quantity > 0
            if not wanted:
                continue
            new_picking = wizard._create_return()
            for move in new_picking.move_ids:
                move.quantity = move.product_uom_qty
                move.picked = True
            new_picking.with_context(zivo_no_fulfillment_export=True)._action_done()
            return new_picking
        return self.env["stock.picking"]

    def _zivo_apply_returns(self, node):
        self.ensure_one()
        Return = self.env["zivo.shopify.return"]
        for data in (node.get("returns") or {}).get("nodes") or []:
            items = [
                {
                    "line_gid": line["fulfillmentLineItem"]["lineItem"]["id"],
                    "quantity": line["quantity"],
                    "reason": (line.get("returnReasonDefinition") or {}).get("name") or line.get("customerNote") or "",
                }
                for line in (data.get("returnLineItems") or {}).get("nodes") or [] if line.get("fulfillmentLineItem")
            ]
            vals = {
                "name": data["name"], "status": data.get("status"), "line_data": items,
                "summary": "\n".join(f"{item['quantity']} x {item['line_gid'].rsplit('/', 1)[-1]}: {item['reason']}" for item in items),
            }
            record = self.zivo_return_ids.filtered(lambda r: r.shopify_gid == data["id"])
            if record:
                record.write(vals)
            else:
                Return.create({**vals, "order_id": self.id, "shopify_gid": data["id"]})


class AccountMove(models.Model):
    _inherit = "account.move"

    zivo_refund_gid = fields.Char(string="Shopify Refund", copy=False, index=True, readonly=True)
    zivo_order_id = fields.Many2one("sale.order", compute="_compute_zivo_order")

    @api.depends("invoice_line_ids.sale_line_ids", "reversed_entry_id")
    def _compute_zivo_order(self):
        for move in self:
            orders = move.invoice_line_ids.sale_line_ids.order_id | move.reversed_entry_id.invoice_line_ids.sale_line_ids.order_id
            move.zivo_order_id = orders.filtered("zivo_shopify_gid")[:1]

    def action_zivo_refund_on_shopify(self):
        for move in self:
            if move.move_type != "out_refund" or move.state != "posted" or move.zivo_refund_gid or not move.zivo_order_id:
                raise UserError(self.env._("Only posted credit notes of Shopify orders, not yet refunded, can be sent."))
            move.zivo_order_id.zivo_instance_id.sudo()._enqueue(
                "export_refund", f"Refund {move.name}", {"move": move.id}, dedupe_key=f"refund:{move.id}", source="odoo",
            )
        return True


class ZivoShopifyInstanceRefund(models.Model):
    _inherit = "zivo.shopify.instance"

    def _job_export_refund(self, job):
        move = self.env["account.move"].browse(job.payload["move"]).exists()
        if not move or move.zivo_refund_gid:
            return "Nothing to do"
        order = move.zivo_order_id
        client = self._client()
        lines = []
        for line in move.invoice_line_ids.filtered("sale_line_ids"):
            sale_line = line.sale_line_ids[:1]
            if sale_line.zivo_line_gid:
                lines.append({"lineItemId": sale_line.zivo_line_gid, "quantity": int(line.quantity), "restockType": "NO_RESTOCK"})
        suggested = client.execute(Q.SUGGESTED_REFUND, {"id": order.zivo_shopify_gid, "lines": lines}, expected_cost=200)["order"]["suggestedRefund"]
        transactions = []
        for data in suggested.get("suggestedTransactions") or []:
            parent = data.get("parentTransaction") or {}
            transactions.append({
                "orderId": order.zivo_shopify_gid,
                "kind": "REFUND",
                "gateway": data["gateway"],
                "amount": data["amountSet"]["shopMoney"]["amount"],
                "parentId": parent.get("id"),
            })
        input_vals = {
            "orderId": order.zivo_shopify_gid,
            "note": move.name,
            "notify": False,
            "refundLineItems": lines,
            "transactions": transactions,
        }
        payload = client.mutate(Q.REFUND_CREATE, {"input": input_vals, "key": f"zivo-refund-{move.id}-{uuid.uuid5(uuid.NAMESPACE_DNS, move.name)}"}, "refundCreate")
        gid = payload["refund"]["id"]
        move.zivo_refund_gid = gid
        self.env["zivo.shopify.refund"].create({
            "order_id": order.id, "shopify_gid": gid, "note": move.name, "amount": move.amount_total,
            "move_id": move.id, "state": "exported",
        })
        return gid
