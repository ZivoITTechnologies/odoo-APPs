import logging

from odoo import fields, models
from odoo.exceptions import UserError
from odoo.tools import float_is_zero

from ..lib import queries as Q

_logger = logging.getLogger(__name__)

OPEN_FO_STATUSES = ("OPEN", "IN_PROGRESS", "SCHEDULED")


class ZivoShopifyFulfillment(models.Model):
    _name = "zivo.shopify.fulfillment"
    _description = "Shopify Fulfilment"
    _order = "id desc"

    order_id = fields.Many2one("sale.order", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="order_id.company_id", store=True, index=True)
    picking_id = fields.Many2one("stock.picking", ondelete="set null", index=True)
    shopify_gid = fields.Char(required=True, index=True)
    origin = fields.Selection([("odoo", "Created from Odoo"), ("shopify", "Created on Shopify")], required=True)
    status = fields.Char()
    tracking_company = fields.Char()
    tracking_number = fields.Char()
    tracking_url = fields.Char()

    _gid_unique = models.Constraint("UNIQUE(order_id, shopify_gid)", "Fulfilment already recorded.")


class StockPicking(models.Model):
    _inherit = "stock.picking"

    zivo_export_state = fields.Selection([
        ("todo", "To Export"), ("done", "Exported"), ("skip", "Not Applicable"),
    ], string="Shopify Fulfilment", copy=False, default=False, tracking=True)
    zivo_order_gid = fields.Char(related="sale_id.zivo_shopify_gid")
    zivo_return_gid = fields.Char(string="Shopify Return", copy=False, readonly=True)

    def _action_done(self):
        result = super()._action_done()
        if self.env.context.get("zivo_no_fulfillment_export"):
            return result
        for picking in self:
            order = picking.sale_id
            if not order.zivo_shopify_gid or picking.picking_type_code != "outgoing" or picking.return_id:
                continue
            instance = order.zivo_instance_id
            workflow_needs_refresh = instance.workflow_ids.filtered("create_invoice")
            if instance.auto_export_fulfillment:
                picking.zivo_export_state = "todo"
                instance.sudo()._enqueue(
                    "export_fulfillment", f"Fulfil {order.zivo_shopify_name} ({picking.name})", {"picking": picking.id},
                    dedupe_key=f"fulfillment:{picking.id}", source="odoo", priority=4, external_ref=order.zivo_shopify_gid,
                )
            if workflow_needs_refresh:
                # Products invoiced on delivery only become invoiceable now.
                instance.sudo()._enqueue(
                    "import_order", f"Invoice after delivery {order.zivo_shopify_name}", {"gid": order.zivo_shopify_gid},
                    dedupe_key=f"order:{order.zivo_shopify_gid}", source="odoo", priority=6,
                )
        return result

    def action_zivo_export_fulfillment(self):
        for picking in self.filtered(lambda p: p.state == "done" and p.sale_id.zivo_shopify_gid and p.picking_type_code == "outgoing"):
            picking.sale_id.zivo_instance_id.sudo()._enqueue(
                "export_fulfillment", f"Fulfil {picking.sale_id.zivo_shopify_name}", {"picking": picking.id},
                dedupe_key=f"fulfillment:{picking.id}", source="manual", priority=4,
            )
        return True

    def action_zivo_export_return(self):
        for picking in self:
            picking._zivo_create_shopify_return()
        return True

    def _zivo_create_shopify_return(self):
        """Declare on Shopify a customer return handled in Odoo."""
        self.ensure_one()
        order = self.sale_id
        if not order.zivo_shopify_gid or not self.return_id or self.picking_type_code != "incoming":
            raise UserError(self.env._("Only customer returns of Shopify orders can be exported."))
        instance = order.zivo_instance_id
        client = instance._client()
        node = instance._fetch_order(order.zivo_shopify_gid, client)
        by_line = {}
        for fulfillment in node.get("fulfillments") or []:
            for item in (fulfillment.get("fulfillmentLineItems") or {}).get("nodes") or []:
                by_line.setdefault(item["lineItem"]["id"], []).append(item)
        lines = []
        for move in self.move_ids.filtered(lambda m: m.state != "cancel"):
            gid = move.sale_line_id.zivo_line_gid
            remaining = int(move.quantity or move.product_uom_qty)
            for item in by_line.get(gid, []):
                if remaining <= 0:
                    break
                quantity = min(remaining, item["quantity"] or 0)
                if quantity:
                    lines.append({"fulfillmentLineItemId": item["id"], "quantity": quantity,
                                  "returnReason": "UNKNOWN", "returnReasonNote": self.name})
                    remaining -= quantity
        if not lines:
            raise UserError(self.env._("No fulfilled Shopify line matches this return."))
        payload = client.mutate(Q.RETURN_CREATE, {"input": {"orderId": order.zivo_shopify_gid, "returnLineItems": lines, "notifyCustomer": False}}, "returnCreate")
        self.zivo_return_gid = payload["return"]["id"]
        return self.zivo_return_gid


class SaleOrderFulfillment(models.Model):
    _inherit = "sale.order"

    zivo_fulfillment_ids = fields.One2many("zivo.shopify.fulfillment", "order_id", string="Shopify Fulfilments")

    def _zivo_apply_fulfillments(self, node):
        """Reflect in Odoo the shipments that were created directly on Shopify."""
        self.ensure_one()
        Fulfillment = self.env["zivo.shopify.fulfillment"]
        for data in node.get("fulfillments") or []:
            if data.get("status") != "SUCCESS":
                continue
            tracking = (data.get("trackingInfo") or [{}])
            tracking = tracking[0] if tracking else {}
            record = self.zivo_fulfillment_ids.filtered(lambda f: f.shopify_gid == data["id"])
            values = {
                "status": data.get("status"),
                "tracking_company": tracking.get("company"),
                "tracking_number": tracking.get("number"),
                "tracking_url": tracking.get("url"),
            }
            if record:
                record.write(values)
                continue
            picking = self._zivo_deliver_lines(data, tracking)
            Fulfillment.create({
                **values, "order_id": self.id, "shopify_gid": data["id"], "origin": "shopify", "picking_id": picking.id,
            })

    def _zivo_deliver_lines(self, fulfillment, tracking):
        """Validate the Odoo delivery with the quantities shipped by Shopify."""
        self.ensure_one()
        wanted = {}
        for item in (fulfillment.get("fulfillmentLineItems") or {}).get("nodes") or []:
            line = self.order_line.filtered(lambda l: l.zivo_line_gid == item["lineItem"]["id"])[:1]
            if line:
                wanted[line] = wanted.get(line, 0) + (item.get("quantity") or 0)
        pickings = self.picking_ids.filtered(lambda p: p.state not in ("done", "cancel") and p.picking_type_code == "outgoing")
        touched = self.env["stock.picking"]
        for line, quantity in wanted.items():
            remaining = quantity
            for move in pickings.move_ids.filtered(lambda m: m.sale_line_id == line and m.state not in ("done", "cancel")):
                if float_is_zero(remaining, precision_digits=3):
                    break
                done = min(remaining, move.product_uom_qty)
                move.quantity = done
                move.picked = True
                remaining -= done
                touched |= move.picking_id
        for picking in touched:
            picking.move_ids.filtered(lambda m: not m.picked and m.state not in ("done", "cancel")).write({"quantity": 0})
            if tracking.get("number"):
                picking.carrier_tracking_ref = tracking["number"]
            picking.with_context(zivo_no_fulfillment_export=True, skip_backorder=True)._action_done()
            picking.zivo_export_state = "skip"
        return touched[:1]


class ZivoShopifyInstanceFulfillment(models.Model):
    _inherit = "zivo.shopify.instance"

    def _job_export_fulfillment(self, job):
        picking = self.env["stock.picking"].browse(job.payload["picking"]).exists()
        if not picking or picking.state != "done":
            return "Delivery not validated"
        if picking.zivo_export_state == "done":
            return "Already exported"
        order = picking.sale_id
        client = self._client()
        data = client.execute(Q.FULFILLMENT_ORDERS, {"id": order.zivo_shopify_gid}, expected_cost=600)["order"]
        for fulfillment_order in data["fulfillmentOrders"]["nodes"]:
            lines = fulfillment_order["lineItems"]
            if (lines.get("pageInfo") or {}).get("hasNextPage"):
                lines["nodes"] = list(client.iter_connection(
                    Q.FULFILLMENT_ORDER_LINES, {"id": fulfillment_order["id"]}, ["fulfillmentOrder", "lineItems"]))
        fulfillment_orders = [
            fo for fo in data["fulfillmentOrders"]["nodes"]
            if fo["status"] in OPEN_FO_STATUSES
            and any(a["action"] == "CREATE_FULFILLMENT" for a in fo.get("supportedActions") or [])
        ]
        remaining = {}
        for move in picking.move_ids.filtered(lambda m: m.state == "done" and m.sale_line_id.zivo_line_gid):
            gid = move.sale_line_id.zivo_line_gid
            remaining[gid] = remaining.get(gid, 0) + int(move.quantity)
        if not remaining:
            picking.zivo_export_state = "skip"
            return "Nothing to fulfil on Shopify"

        warehouse = picking.picking_type_id.warehouse_id
        preferred = self.location_ids.filtered(lambda l: warehouse in l.warehouse_ids).mapped("shopify_gid")
        fulfillment_orders.sort(key=lambda fo: 0 if (fo.get("assignedLocation") or {}).get("location", {}).get("id") in preferred else 1)

        created = []
        for fulfillment_order in fulfillment_orders:
            items = []
            for line in fulfillment_order["lineItems"]["nodes"]:
                gid = line["lineItem"]["id"]
                quantity = min(remaining.get(gid, 0), line["remainingQuantity"])
                if quantity > 0:
                    items.append({"id": line["id"], "quantity": quantity})
                    remaining[gid] -= quantity
            if not items:
                continue
            fulfillment = {
                "notifyCustomer": self.notify_customer_on_fulfillment,
                "lineItemsByFulfillmentOrder": [{
                    "fulfillmentOrderId": fulfillment_order["id"], "fulfillmentOrderLineItems": items,
                }],
            }
            tracking = picking._zivo_tracking_input()
            if tracking:
                fulfillment["trackingInfo"] = tracking
            payload = client.mutate(Q.FULFILLMENT_CREATE, {"fulfillment": fulfillment}, "fulfillmentCreate")
            gid = payload["fulfillment"]["id"]
            created.append(gid)
            self.env["zivo.shopify.fulfillment"].create({
                "order_id": order.id, "picking_id": picking.id, "shopify_gid": gid, "origin": "odoo",
                "status": payload["fulfillment"].get("status"),
                "tracking_company": (tracking or {}).get("company"),
                "tracking_number": (tracking or {}).get("number"),
                "tracking_url": (tracking or {}).get("url"),
            })
        if not created:
            raise UserError(self.env._("No fulfillment order is open on Shopify for %s.", order.zivo_shopify_name))
        picking.zivo_export_state = "done"
        unassigned = {gid: qty for gid, qty in remaining.items() if qty > 0}
        if unassigned:
            picking.message_post(body=self.env._("Some quantities could not be fulfilled on Shopify: %s", unassigned))
        return ", ".join(created)


class StockPickingTracking(models.Model):
    _inherit = "stock.picking"

    def _zivo_tracking_input(self):
        self.ensure_one()
        if not self.carrier_tracking_ref:
            return None
        data = {"number": self.carrier_tracking_ref}
        if self.carrier_id:
            data["company"] = self.carrier_id.name
        if self.carrier_tracking_url and self.carrier_tracking_url.startswith("http"):
            data["url"] = self.carrier_tracking_url
        return data
