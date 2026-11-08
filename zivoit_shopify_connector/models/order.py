import logging
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tools import float_compare, float_is_zero, float_round

from ..lib import ai as ai_lib, queries as Q

_logger = logging.getLogger(__name__)

SUCCESS = "SUCCESS"
PAYMENT_KINDS = ("SALE", "CAPTURE")


def parse_dt(value):
    if not value:
        return False
    return fields.Datetime.to_datetime(value.replace("T", " ").replace("Z", "")[:19])


class ZivoShopifyTransaction(models.Model):
    _name = "zivo.shopify.transaction"
    _description = "Shopify Order Transaction"
    _order = "id desc"

    order_id = fields.Many2one("sale.order", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="order_id.company_id", store=True, index=True)
    shopify_gid = fields.Char(required=True, index=True)
    kind = fields.Char()
    status = fields.Char()
    gateway = fields.Char()
    amount = fields.Float(digits=(16, 4))
    currency_id = fields.Many2one(related="order_id.currency_id")
    fee = fields.Float(digits=(16, 4))
    payment_id = fields.Many2one("account.payment", ondelete="set null", index=True)

    _gid_unique = models.Constraint("UNIQUE(order_id, shopify_gid)", "Transaction already imported.")


class SaleOrder(models.Model):
    _inherit = "sale.order"

    zivo_instance_id = fields.Many2one("zivo.shopify.instance", string="Shopify Store", index=True, copy=False, readonly=True)
    zivo_shopify_gid = fields.Char(string="Shopify ID", index=True, copy=False, readonly=True)
    zivo_shopify_name = fields.Char(string="Shopify Order", index=True, copy=False, readonly=True)
    zivo_financial_status = fields.Char(string="Financial Status", copy=False, readonly=True)
    zivo_fulfillment_status = fields.Char(string="Fulfilment Status", copy=False, readonly=True)
    zivo_sales_channel = fields.Char(string="Sales Channel", copy=False, readonly=True, index=True)
    zivo_gateways = fields.Char(string="Gateways", copy=False, readonly=True)
    zivo_tags = fields.Char(string="Shopify Tags", copy=False, readonly=True)
    zivo_updated_at = fields.Datetime(string="Shopify Updated", copy=False, readonly=True)
    zivo_cancelled_on_shopify = fields.Boolean(copy=False, readonly=True)
    zivo_amount_mismatch = fields.Float(string="Unreconciled Difference", copy=False, readonly=True)
    zivo_transaction_ids = fields.One2many("zivo.shopify.transaction", "order_id", string="Shopify Transactions")
    zivo_fees = fields.Float(string="Gateway Fees", compute="_compute_zivo_fees", store=True)
    zivo_risk_score = fields.Integer(string="AI Risk Score", copy=False, readonly=True)
    zivo_risk_level = fields.Selection([("low", "Low"), ("medium", "Medium"), ("high", "High")], string="Risk", copy=False, readonly=True)
    zivo_risk_reasons = fields.Text(string="Risk Explanation", copy=False, readonly=True)
    zivo_risk_hold = fields.Boolean(string="On Risk Hold", copy=False, tracking=True)
    zivo_routing_note = fields.Char(string="Routing Decision", copy=False, readonly=True)
    zivo_market_id = fields.Many2one("zivo.shopify.market", string="Market", copy=False, readonly=True)

    _zivo_gid_unique = models.Constraint("UNIQUE(zivo_instance_id, zivo_shopify_gid)", "This Shopify order was already imported.")

    @api.depends("zivo_transaction_ids.fee")
    def _compute_zivo_fees(self):
        for order in self:
            order.zivo_fees = sum(order.zivo_transaction_ids.mapped("fee"))

    # ------------------------------------------------------------------
    # Master data helpers
    # ------------------------------------------------------------------
    @api.model
    def _zivo_get_pricelist(self, instance, currency):
        pricelist = instance.pricelist_id
        if pricelist and pricelist.currency_id == currency:
            return pricelist
        Pricelist = self.env["product.pricelist"]
        found = Pricelist.search([("currency_id", "=", currency.id), ("company_id", "in", [instance.company_id.id, False])], limit=1)
        return found or Pricelist.create({
            "name": f"Shopify {currency.name}", "currency_id": currency.id, "company_id": instance.company_id.id,
        })

    @api.model
    def _zivo_get_tax(self, instance, rate, title, included, exclude=None):
        """Find the Odoo tax of a Shopify tax line, creating it when allowed.

        ``exclude`` keeps two lines of equal rate apart, such as CGST 9% + SGST 9%.
        """
        percent = float_round((rate or 0.0) * 100, precision_digits=4)
        Tax = self.env["account.tax"].with_company(instance.company_id)
        domain = [
            ("type_tax_use", "=", "sale"), ("amount_type", "=", "percent"),
            ("amount", "=", percent), ("company_id", "=", instance.company_id.id),
        ]
        candidates = Tax.search(domain).filtered(lambda t: t.price_include == bool(included))
        if exclude:
            candidates -= exclude
        title_lower = (title or "").lower()
        named = candidates.filtered(lambda t: title_lower and title_lower in t.name.lower())
        tax = (named or candidates)[:1]
        if tax:
            return tax
        if not instance.auto_create_taxes:
            raise UserError(self.env._(
                "No sales tax of %(rate)s%% found for Shopify tax %(title)s, and Auto-create Taxes is off.",
                rate=percent, title=title,
            ))
        suffix = " incl." if included else ""
        group = self.env["account.tax.group"].search([("name", "=", "Shopify Taxes"), ("company_id", "=", instance.company_id.id)], limit=1) \
            or self.env["account.tax.group"].create({"name": "Shopify Taxes", "company_id": instance.company_id.id})
        return Tax.create({
            "tax_group_id": group.id,
            "name": f"Shopify {title or 'Tax'} {percent:g}%{suffix}",
            "amount": percent,
            "amount_type": "percent",
            "type_tax_use": "sale",
            "company_id": instance.company_id.id,
            "price_include_override": "tax_included" if included else "tax_excluded",
        })

    @api.model
    def _zivo_money(self, instance, money_bag):
        key = "presentmentMoney" if instance.use_presentment_currency else "shopMoney"
        return float(((money_bag or {}).get(key) or {}).get("amount") or 0.0)

    @api.model
    def _zivo_taxes(self, instance, tax_lines, included):
        taxes = self.env["account.tax"]
        for line in tax_lines or []:
            if line.get("rate"):
                taxes |= self._zivo_get_tax(instance, line["rate"], line.get("title"), included, exclude=taxes)
        return taxes

    @api.model
    def _zivo_line_product(self, instance, line):
        Variant = self.env["zivo.shopify.variant"]
        gid = (line.get("variant") or {}).get("id")
        if not gid:
            return instance.custom_item_product_id
        variant = Variant.search([("instance_id", "=", instance.id), ("shopify_gid", "=", gid)], limit=1)
        if variant.product_id:
            return variant.product_id
        sku = line.get("sku")
        if sku:
            product = self.env["product.product"].search(
                [("default_code", "=", sku), ("company_id", "in", [instance.company_id.id, False])], limit=1)
            if product:
                if variant:
                    variant.product_id = product
                return product
        # Unknown variant: read its product from Shopify once, then link.
        if not variant:
            parent = instance._client().execute(Q.VARIANT_PRODUCT, {"id": gid}, expected_cost=20).get("productVariant") or {}
            product_gid = (parent.get("product") or {}).get("id")
            if product_gid:
                node = instance._client().execute(Q.PRODUCT, {"id": product_gid}, expected_cost=400).get("product")
                if node:
                    self.env["zivo.shopify.product"]._upsert_from_node(instance, node)
                    variant = Variant.search([("instance_id", "=", instance.id), ("shopify_gid", "=", gid)], limit=1)
                    if variant.product_id:
                        return variant.product_id
        raise UserError(self.env._(
            "Unmapped variant: no Odoo product for %(title)s (SKU %(sku)s). Open AI Product Matches to link it.",
            title=line.get("name"), sku=sku or "-",
        ))

    # ------------------------------------------------------------------
    # Import entry point
    # ------------------------------------------------------------------
    @api.model
    def _zivo_should_import(self, instance, node):
        if node.get("test") and instance.skip_test_orders:
            return False, "Test order skipped"
        if node.get("sourceName") == "pos" and not instance.import_pos_orders:
            return False, "POS order skipped"
        if instance.import_orders_after and parse_dt(node.get("createdAt")) < instance.import_orders_after:
            return False, "Older than the import start date"
        if instance.order_status_filter == "paid" and node.get("displayFinancialStatus") not in (
                "PAID", "PARTIALLY_PAID", "PARTIALLY_REFUNDED", "REFUNDED"):
            return False, "Waiting for payment (financial filter)"
        return True, ""

    @api.model
    def _zivo_import_order(self, instance, node):
        order = self.search([("zivo_instance_id", "=", instance.id), ("zivo_shopify_gid", "=", node["id"])], limit=1)
        if order:
            order._zivo_refresh(node)
            return order, "updated"
        allowed, reason = self._zivo_should_import(instance, node)
        if not allowed:
            return self.browse(), reason
        if node.get("cancelledAt"):
            return self.browse(), "Cancelled before import"
        order = self._zivo_create_order(instance, node)
        order._zivo_compute_risk(node)
        order._zivo_run_workflow(node)
        order._zivo_sync_children(node)
        self.env["crm.lead"]._zivo_mark_converted(order)
        return order, "created"

    @api.model
    def _zivo_currency(self, instance, node):
        code = node["presentmentCurrencyCode"] if instance.use_presentment_currency else node["currencyCode"]
        currency = self.env["res.currency"].with_context(active_test=False).search([("name", "=", code)], limit=1)
        if not currency:
            raise UserError(self.env._("Currency %s does not exist in Odoo.", code))
        if not currency.active:
            currency.active = True
        return currency

    def _zivo_refunded_quantities(self, node):
        refunded = {}
        for refund in node.get("refunds") or []:
            for item in (refund.get("refundLineItems") or {}).get("nodes") or []:
                gid = item["lineItem"]["id"]
                refunded[gid] = refunded.get(gid, 0) + item["quantity"]
        return refunded

    def _zivo_line_quantity(self, line, refunded):
        # currentQuantity reflects order edits and refunds. Adding refunds back keeps the
        # sold quantity on the order, so refunds can appear as credit notes.
        return (line.get("currentQuantity") or 0) + refunded.get(line["id"], 0)

    def _zivo_line_vals(self, instance, line, quantity, included):
        product = self._zivo_line_product(instance, line)
        unit = self._zivo_money(instance, line.get("originalUnitPriceSet"))
        discount_amount = self._zivo_money(instance, line.get("totalDiscountSet"))
        base = unit * (line.get("quantity") or quantity or 1)
        discount = float_round(discount_amount / base * 100, precision_digits=6) if base else 0.0
        taxes = self._zivo_taxes(instance, line.get("taxLines"), included)
        return {
            "product_id": product.id,
            "name": line.get("name") or product.display_name,
            "product_uom_qty": quantity,
            "price_unit": unit,
            "discount": discount,
            "tax_ids": [Command.set(taxes.ids)],
            "zivo_line_gid": line["id"],
        }

    @api.model
    def _zivo_market(self, instance, country_code):
        if not country_code:
            return self.env["zivo.shopify.market"]
        for market in instance.market_ids:
            if country_code in (market.country_codes or "").split(","):
                return market
        return self.env["zivo.shopify.market"]

    @api.model
    def _zivo_create_order(self, instance, node):
        Partner = self.env["res.partner"].with_context(tracking_disable=True)
        customer, invoice, delivery = Partner._zivo_order_partners(instance, node)
        currency = self._zivo_currency(instance, node)
        shipping = node.get("shippingAddress") or {}
        market = self._zivo_market(instance, shipping.get("countryCodeV2"))
        pricelist = market.pricelist_id if market.pricelist_id.currency_id == currency else self._zivo_get_pricelist(instance, currency)
        included = node.get("taxesIncluded")
        refunded = self._zivo_refunded_quantities(node)

        lines = []
        demand = {}
        for line in (node.get("lineItems") or {}).get("nodes") or []:
            quantity = self._zivo_line_quantity(line, refunded)
            if quantity <= 0:
                continue
            vals = self._zivo_line_vals(instance, line, quantity, included)
            demand[vals["product_id"]] = demand.get(vals["product_id"], 0) + quantity
            lines.append(Command.create(vals))
        if not lines:
            raise UserError(self.env._("Shopify order %s has no line to import.", node["name"]))

        for shipping_line in (node.get("shippingLines") or {}).get("nodes") or []:
            if shipping_line.get("isRemoved"):
                continue
            price = self._zivo_money(instance, shipping_line.get("discountedPriceSet"))
            taxes = self._zivo_taxes(instance, shipping_line.get("taxLines"), included)
            lines.append(Command.create({
                "product_id": instance.shipping_product_id.id,
                "name": shipping_line.get("title") or instance.shipping_product_id.name,
                "product_uom_qty": 1,
                "price_unit": price,
                "tax_ids": [Command.set(taxes.ids)],
                "is_delivery": True,
            }))
        tip = self._zivo_money(instance, node.get("totalTipReceivedSet"))
        if tip:
            lines.append(Command.create({
                "product_id": instance.tip_product_id.id, "name": "Tip", "product_uom_qty": 1,
                "price_unit": tip, "tax_ids": [Command.clear()],
            }))

        warehouse, note = self.env["zivo.shopify.routing.rule"]._zivo_route_order(instance, market, shipping, demand)
        vals = {
            "partner_id": customer.id,
            "partner_invoice_id": invoice.id,
            "partner_shipping_id": delivery.id,
            "company_id": instance.company_id.id,
            "pricelist_id": pricelist.id,
            "currency_id": currency.id,
            "warehouse_id": warehouse.id,
            "team_id": instance.team_id.id,
            "user_id": instance.user_id.id,
            "date_order": parse_dt(node.get("processedAt")),
            "client_order_ref": node["name"],
            "fiscal_position_id": False,
            "note": node.get("note") or False,
            "order_line": lines,
            "zivo_instance_id": instance.id,
            "zivo_shopify_gid": node["id"],
            "zivo_shopify_name": node["name"],
            "zivo_routing_note": note,
            "zivo_market_id": market.id,
            **self._zivo_header_vals(node),
        }
        if instance.use_shopify_order_name:
            vals["name"] = f"{instance.order_prefix or ''}{node['name'].lstrip('#')}"
        order = self.with_company(instance.company_id).with_context(mail_create_nosubscribe=True).create(vals)
        order._zivo_check_total(node)
        if instance.routing_allow_split and "best partial" in (note or ""):
            Routing = self.env["zivo.shopify.routing.rule"]
            used = Routing._zivo_split_lines(instance, order, Routing._zivo_candidates(instance))
            if len(used) > 1:
                order.zivo_routing_note = f"Split: {', '.join(used)}"
        self.env["zivo.shopify.metafield.mapping"]._apply_import(
            instance, "order", order, (node.get("metafields") or {}).get("nodes"))
        return order

    def _zivo_header_vals(self, node):
        return {
            "zivo_financial_status": node.get("displayFinancialStatus"),
            "zivo_fulfillment_status": node.get("displayFulfillmentStatus"),
            "zivo_sales_channel": node.get("sourceName"),
            "zivo_gateways": ", ".join(node.get("paymentGatewayNames") or []),
            "zivo_tags": ", ".join(node.get("tags") or []),
            "zivo_updated_at": parse_dt(node.get("updatedAt")),
        }

    def _zivo_check_total(self, node):
        """Absorb rounding, and report anything larger than the store tolerance."""
        self.ensure_one()
        if node.get("refunds"):
            self.zivo_amount_mismatch = 0.0
            return
        instance = self.zivo_instance_id
        key = "currentTotalPriceSet" if node.get("edited") else "totalPriceSet"
        expected = self._zivo_money(instance, node.get(key))
        difference = self.currency_id.round(expected - self.amount_total)
        if self.currency_id.is_zero(difference):
            self.zivo_amount_mismatch = 0.0
            return
        if abs(difference) <= instance.rounding_tolerance:
            self.write({"order_line": [Command.create({
                "product_id": instance.adjustment_product_id.id,
                "name": "Shopify rounding adjustment",
                "product_uom_qty": 1,
                "price_unit": difference,
                "tax_ids": [Command.clear()],
            })]})
            self.zivo_amount_mismatch = 0.0
            return
        self.zivo_amount_mismatch = difference
        self.message_post(body=self.env._(
            "Odoo total differs from Shopify by %(difference)s %(currency)s. Please review.",
            difference=difference, currency=self.currency_id.name,
        ))

    # ------------------------------------------------------------------
    # AI risk
    # ------------------------------------------------------------------
    def _zivo_compute_risk(self, node):
        self.ensure_one()
        instance = self.zivo_instance_id
        assessments = (node.get("risk") or {}).get("assessments") or []
        levels = [a.get("riskLevel") for a in assessments]
        level = next((name for name in ("HIGH", "MEDIUM", "LOW") if name in levels), "NONE")
        email = node.get("email") or ""
        average = self._zivo_average_value(instance)
        billing = node.get("billingAddress") or {}
        shipping = node.get("shippingAddress") or {}
        recent_domain = [
            ("zivo_instance_id", "=", instance.id), ("id", "!=", self.id),
            ("create_date", ">=", fields.Datetime.now() - timedelta(hours=24)),
        ]
        recent = self.search_count(recent_domain + [("partner_id", "=", self.partner_id.id)])
        failed = sum(1 for t in node.get("transactions") or [] if t.get("status") in ("FAILURE", "ERROR"))
        score, reasons = ai_lib.score_order_risk({
            "shopify_level": level,
            "shopify_recommendation": (node.get("risk") or {}).get("recommendation"),
            "address_mismatch": not node.get("billingAddressMatchesShippingAddress", True) and bool(shipping),
            "country_mismatch": bool(billing and shipping and billing.get("countryCodeV2") != shipping.get("countryCodeV2")),
            "disposable_email": ai_lib.is_disposable_email(email),
            "no_contact": not email and not node.get("phone"),
            "customer_orders": int((node.get("customer") or {}).get("numberOfOrders") or 0),
            "value_ratio": (self.amount_total / average) if average else 0.0,
            "recent_orders_same_contact": recent,
            "failed_payments": failed,
        })
        self.write({
            "zivo_risk_score": score,
            "zivo_risk_level": "high" if score >= 70 else "medium" if score >= 35 else "low",
            "zivo_risk_reasons": "\n".join(reasons),
            "zivo_risk_hold": score >= instance.risk_hold_threshold,
        })
        if self.zivo_risk_hold:
            self.activity_schedule(
                "mail.mail_activity_data_todo",
                summary=self.env._("Review risky Shopify order"),
                note="<br/>".join(reasons),
                user_id=self._zivo_activity_user().id,
            )

    def _zivo_activity_user(self):
        self.ensure_one()
        return self.user_id or self.zivo_instance_id.user_id or self.env.ref("base.user_admin")

    @api.model
    def _zivo_average_value(self, instance):
        rows = self._read_group(
            [("zivo_instance_id", "=", instance.id), ("state", "=", "sale"),
             ("date_order", ">=", fields.Datetime.now() - timedelta(days=90))],
            [], ["amount_untaxed:avg"])
        return rows[0][0] or 0.0

    def action_zivo_release_hold(self):
        for order in self.filtered("zivo_risk_hold"):
            order.zivo_instance_id.check_access("read")
            order.zivo_risk_hold = False
            order.activity_unlink(["mail.mail_activity_data_todo"])
            order.zivo_instance_id._enqueue("import_order", f"Release hold {order.zivo_shopify_name}",
                                            {"gid": order.zivo_shopify_gid}, dedupe_key=f"order:{order.zivo_shopify_gid}", source="odoo", priority=4)
        return True

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def _prepare_confirmation_values(self):
        values = super()._prepare_confirmation_values()
        # Odoo stamps "now" at confirmation; a Shopify order keeps its real date for reports.
        if self.zivo_shopify_gid:
            values.pop("date_order", None)
        return values

    def _zivo_workflow(self, node):
        self.ensure_one()
        for workflow in self.zivo_instance_id.workflow_ids:
            if workflow._matches(node):
                return workflow
        return self.env["zivo.shopify.workflow"]

    def _zivo_run_workflow(self, node):
        self.ensure_one()
        if self.zivo_risk_hold or self.state == "cancel":
            return
        workflow = self._zivo_workflow(node)
        if not workflow:
            return
        if workflow.confirm_order and self.state in ("draft", "sent"):
            self.with_context(send_email=False).action_confirm()
        if self.state != "sale":
            return
        if workflow.validate_delivery:
            self._zivo_validate_open_deliveries()
        if workflow.create_invoice:
            self._zivo_create_invoice(workflow)
        if workflow.register_payment:
            self._zivo_register_payments(workflow, node)

    def _zivo_validate_open_deliveries(self):
        for picking in self.picking_ids.filtered(lambda p: p.state not in ("done", "cancel")):
            for move in picking.move_ids.filtered(lambda m: m.state not in ("done", "cancel")):
                move.quantity = move.product_uom_qty
                move.picked = True
            picking.with_context(zivo_no_fulfillment_export=False)._action_done()

    def _zivo_create_invoice(self, workflow):
        self.ensure_one()
        if self.invoice_status == "to invoice":
            invoices = self._create_invoices()
        else:
            invoices = self.env["account.move"]
        if workflow.post_invoice:
            (invoices | self.invoice_ids).filtered(lambda move: move.state == "draft").action_post()

    def _zivo_register_payments(self, workflow, node):
        """Create one Odoo payment per captured Shopify transaction, once."""
        self.ensure_one()
        Transaction = self.env["zivo.shopify.transaction"]
        for data in node.get("transactions") or []:
            if data.get("kind") not in PAYMENT_KINDS or data.get("status") != SUCCESS:
                continue
            record = self.zivo_transaction_ids.filtered(lambda t: t.shopify_gid == data["id"])
            if record.payment_id:
                continue
            if not record:
                record = Transaction.create(self._zivo_transaction_vals(data))
            invoices = self.invoice_ids.filtered(
                lambda m: m.state == "posted" and m.move_type == "out_invoice" and m.payment_state in ("not_paid", "partial", "in_payment"))
            lines = invoices.line_ids.filtered(lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled)
            if not lines:
                continue
            amount = min(record.amount, sum(invoices.mapped("amount_residual")))
            if float_is_zero(amount, precision_rounding=self.currency_id.rounding):
                continue
            wizard = self.env["account.payment.register"].with_context(
                active_model="account.move.line", active_ids=lines.ids,
            ).create({
                "journal_id": workflow.payment_journal_id.id,
                "amount": amount,
                "currency_id": self.currency_id.id,
                "payment_date": (parse_dt(data.get("processedAt")) or fields.Datetime.now()).date(),
                "communication": f"{self.zivo_shopify_name} {data.get('gateway') or ''}".strip(),
                "group_payment": True,
                "payment_difference_handling": "open",
            })
            payments = wizard._create_payments()
            record.payment_id = payments[:1]

    def _zivo_transaction_vals(self, data):
        instance = self.zivo_instance_id
        return {
            "order_id": self.id,
            "shopify_gid": data["id"],
            "kind": data.get("kind"),
            "status": data.get("status"),
            "gateway": data.get("gateway"),
            "amount": self._zivo_money(instance, data.get("amountSet")),
            "fee": sum(float((fee.get("amount") or {}).get("amount") or 0) for fee in data.get("fees") or []),
        }

    # ------------------------------------------------------------------
    # Updates from Shopify
    # ------------------------------------------------------------------
    def _zivo_refresh(self, node):
        self.ensure_one()
        self.write(self._zivo_header_vals(node))
        if node.get("cancelledAt"):
            self._zivo_apply_cancel(node)
            return
        self._zivo_apply_edits(node)
        self._zivo_update_shipping_address(node)
        self._zivo_compute_risk_if_changed(node)
        self._zivo_run_workflow(node)
        self._zivo_sync_children(node)

    def _zivo_compute_risk_if_changed(self, node):
        if self.state in ("draft", "sent"):
            self._zivo_compute_risk(node)

    def _zivo_sync_children(self, node):
        """Everything that hangs under an order. Each step is idempotent."""
        self.ensure_one()
        for data in node.get("transactions") or []:
            if data.get("kind") in PAYMENT_KINDS and data.get("status") == SUCCESS and not self.zivo_transaction_ids.filtered(lambda t: t.shopify_gid == data["id"]):
                self.env["zivo.shopify.transaction"].create(self._zivo_transaction_vals(data))
        self._zivo_apply_fulfillments(node)
        self._zivo_apply_refunds(node)
        self._zivo_apply_returns(node)

    def _zivo_apply_cancel(self, node):
        self.ensure_one()
        if self.state == "cancel":
            self.zivo_cancelled_on_shopify = True
            return
        self.zivo_cancelled_on_shopify = True
        blocked = self.picking_ids.filtered(lambda p: p.state == "done") or self.invoice_ids.filtered(lambda m: m.state == "posted")
        if blocked:
            self.activity_schedule(
                "mail.mail_activity_data_todo",
                summary=self.env._("Order cancelled on Shopify"),
                note=self.env._("Deliveries or invoices are already validated: reverse them manually."),
                user_id=self._zivo_activity_user().id,
            )
            self.message_post(body=self.env._("Cancelled on Shopify (%s). Manual reversal needed.", node.get("cancelReason") or ""))
            return
        self.with_context(disable_cancel_warning=True, zivo_from_shopify=True)._action_cancel()
        self.message_post(body=self.env._("Cancelled on Shopify (%s).", node.get("cancelReason") or ""))

    def _zivo_apply_edits(self, node):
        """Mirror added lines and changed quantities made in Shopify."""
        self.ensure_one()
        if self.state == "cancel" or self.locked:
            return
        instance = self.zivo_instance_id
        refunded = self._zivo_refunded_quantities(node)
        included = node.get("taxesIncluded")
        changes = []
        for line in (node.get("lineItems") or {}).get("nodes") or []:
            quantity = self._zivo_line_quantity(line, refunded)
            existing = self.order_line.filtered(lambda l: l.zivo_line_gid == line["id"])[:1]
            if existing:
                floor = max(existing.qty_delivered, existing.qty_invoiced)
                target = max(quantity, floor)
                if float_compare(existing.product_uom_qty, target, precision_digits=3):
                    changes.append(Command.update(existing.id, {"product_uom_qty": target}))
            elif quantity > 0:
                changes.append(Command.create(self._zivo_line_vals(instance, line, quantity, included)))
        if changes:
            self.write({"order_line": changes})
            self.message_post(body=self.env._("Order lines updated from a Shopify edit."))

    def _zivo_update_shipping_address(self, node):
        self.ensure_one()
        shipping = node.get("shippingAddress")
        if not shipping or self.picking_ids.filtered(lambda p: p.state == "done"):
            return
        delivery = self.env["res.partner"]._zivo_child_address(self.partner_id, shipping, "delivery")
        if delivery != self.partner_shipping_id:
            self.partner_shipping_id = delivery
            self.picking_ids.filtered(lambda p: p.state not in ("done", "cancel")).write({"partner_id": delivery.id})

    # ------------------------------------------------------------------
    # Pushing changes to Shopify
    # ------------------------------------------------------------------
    def action_zivo_push_edits(self):
        """Apply Odoo quantity changes to the Shopify order with an order edit."""
        for order in self.filtered("zivo_shopify_gid"):
            order._zivo_push_edits()
        return True

    def _zivo_push_edits(self):
        self.ensure_one()
        client = self.zivo_instance_id._client()
        begin = client.mutate(Q.ORDER_EDIT_BEGIN, {"id": self.zivo_shopify_gid}, "orderEditBegin")
        calculated = begin["calculatedOrder"]
        remote = {line["id"].rsplit("/", 1)[-1]: line for line in calculated["lineItems"]["nodes"]}
        changed = False
        Variant = self.env["zivo.shopify.variant"]
        for line in self.order_line.filtered(lambda l: not l.is_delivery and not l.display_type and l.product_id != self.zivo_instance_id.adjustment_product_id):
            if line.zivo_line_gid:
                remote_line = remote.get(line.zivo_line_gid.rsplit("/", 1)[-1])
                if remote_line and remote_line["quantity"] != int(line.product_uom_qty):
                    client.mutate(Q.ORDER_EDIT_SET_QTY, {
                        "id": calculated["id"], "lineItemId": remote_line["id"], "quantity": int(line.product_uom_qty),
                    }, "orderEditSetQuantity")
                    changed = True
            else:
                variant = Variant.search([("instance_id", "=", self.zivo_instance_id.id), ("product_id", "=", line.product_id.id)], limit=1)
                if not variant:
                    raise UserError(self.env._("%s is not published on this Shopify store.", line.product_id.display_name))
                client.mutate(Q.ORDER_EDIT_ADD_VARIANT, {
                    "id": calculated["id"], "variantId": variant.shopify_gid, "quantity": int(line.product_uom_qty),
                }, "orderEditAddVariant")
                changed = True
        if changed:
            client.mutate(Q.ORDER_EDIT_COMMIT, {"id": calculated["id"], "notifyCustomer": False, "staffNote": "Edited from Odoo"}, "orderEditCommit")
            self.message_post(body=self.env._("Quantities pushed to Shopify."))
        return changed

    def action_zivo_refresh(self):
        for order in self.filtered("zivo_shopify_gid"):
            order.zivo_instance_id._enqueue("import_order", f"Refresh {order.zivo_shopify_name}", {"gid": order.zivo_shopify_gid},
                                            dedupe_key=f"order:{order.zivo_shopify_gid}", priority=4)
        return True

    def action_zivo_open_cancel(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window", "res_model": "zivo.shopify.cancel.wizard", "view_mode": "form", "target": "new",
            "context": {"default_order_id": self.id},
        }

    def action_zivo_open_export(self):
        return {
            "type": "ir.actions.act_window", "res_model": "zivo.shopify.order.export.wizard", "view_mode": "form", "target": "new",
            "context": {"default_order_id": self.id},
        }

    def _zivo_export_to_shopify(self, instance):
        """Create on Shopify an order that was sold in Odoo."""
        self.ensure_one()
        if self.zivo_shopify_gid:
            raise UserError(self.env._("This order already exists on Shopify."))
        Variant = self.env["zivo.shopify.variant"]
        lines = []
        for line in self.order_line.filtered(lambda l: not l.display_type and not l.is_delivery):
            variant = Variant.search([("instance_id", "=", instance.id), ("product_id", "=", line.product_id.id)], limit=1)
            vals = {"quantity": int(line.product_uom_qty), "title": line.name[:255],
                    "priceSet": {"shopMoney": {"amount": f"{line.price_reduce_taxexcl:.2f}", "currencyCode": self.currency_id.name}}}
            if variant:
                vals["variantId"] = variant.shopify_gid
            lines.append(vals)
        partner = self.partner_id
        order = {"lineItems": lines, "email": partner.email or None, "currency": self.currency_id.name,
                 "note": f"Created from Odoo {self.name}", "financialStatus": "PENDING"}
        delivery = self.partner_shipping_id
        if delivery.street and delivery.country_id:
            parts = (delivery.name or "").split(" ", 1)
            order["shippingAddress"] = {
                "firstName": parts[0], "lastName": parts[1] if len(parts) > 1 else parts[0], "address1": delivery.street,
                "city": delivery.city or "", "zip": delivery.zip or "", "countryCode": delivery.country_id.code,
            }
        payload = instance._client().mutate(Q.ORDER_CREATE, {"order": order, "options": {"inventoryBehaviour": "BYPASS"}}, "orderCreate")
        self.write({
            "zivo_instance_id": instance.id,
            "zivo_shopify_gid": payload["order"]["id"],
            "zivo_shopify_name": payload["order"]["name"],
        })
        return payload["order"]["id"]


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    zivo_line_gid = fields.Char(string="Shopify Line", index=True, copy=False)


class ZivoShopifyInstanceOrders(models.Model):
    _inherit = "zivo.shopify.instance"

    def action_import_orders(self):
        self.ensure_one()
        self._enqueue("import_orders", "Import orders", {"query": self._orders_query()}, dedupe_key="import_orders")
        return self._notify(self.env._("Order import queued."), "success")

    def _fetch_order(self, gid, client=None):
        """Assemble a complete order from several documents that each fit the cost limit."""
        self.ensure_one()
        client = client or self._client()
        node = client.execute(Q.ORDER, {"id": gid}, expected_cost=500).get("order")
        if not node:
            return None
        node["lineItems"] = {"nodes": list(client.iter_connection(Q.ORDER_LINES, {"id": gid}, ["order", "lineItems"], expected_cost=700))}
        fulfillments = [
            client.node_with_pages(Q.FULFILLMENT_NODE, item["id"], "fulfillmentLineItems", expected_cost=250)
            for item in node.get("fulfillments") or []
        ]
        node["fulfillments"] = [item for item in fulfillments if item]
        refunds = [
            client.node_with_pages(Q.REFUND_NODE, item["id"], "refundLineItems", expected_cost=600)
            for item in node.get("refunds") or []
        ]
        node["refunds"] = [item for item in refunds if item]
        returns = []
        if node.get("returnStatus") != "NO_RETURN":
            returns = list(client.iter_connection(Q.ORDER_RETURNS, {"id": gid}, ["order", "returns"], expected_cost=500))
        node["returns"] = {"nodes": returns}
        return node

    def _job_import_orders(self, job):
        payload = job.payload or {}
        data = self._client().execute(Q.ORDERS, {"query": payload.get("query"), "cursor": payload.get("cursor")}, expected_cost=150)["orders"]
        for node in data["nodes"]:
            # One job per order: a bad order must never block the rest of the page.
            self._enqueue("import_order", f"Import {node['name']}", {"gid": node["id"]},
                          dedupe_key=f"order:{node['id']}", source=job.source, external_ref=node["id"], priority=8)
        info = data["pageInfo"]
        if info["hasNextPage"]:
            self._enqueue("import_orders", "Import orders (next page)", {**payload, "cursor": info["endCursor"]}, source=job.source)
        elif payload.get("stamp"):
            self.last_order_sync = fields.Datetime.now()
        return f"{len(data['nodes'])} orders queued"

    def _job_import_order(self, job):
        node = self._fetch_order(job.payload["gid"])
        if not node:
            return "Order no longer exists on Shopify"
        order, status = self.env["sale.order"].with_company(self.company_id)._zivo_import_order(self, node)
        return f"{order.name or node['name']}: {status}"
