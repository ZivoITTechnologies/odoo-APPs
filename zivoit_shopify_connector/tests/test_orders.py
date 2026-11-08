from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import ZivoShopifyCommon
from .shopify_mock import address, make_line, make_order, make_transaction, money, nodes


@tagged("post_install", "-at_install")
class TestOrders(ZivoShopifyCommon):

    def setUp(self):
        super().setUp()
        self.product = self.stock_product()

    def test_order_totals_match_shopify(self):
        order, status = self.import_order(make_order())
        self.assertEqual(status, "created")
        self.assertEqual(order.name, "1001")
        self.assertEqual(order.client_order_ref, "#1001")
        self.assertAlmostEqual(order.amount_untaxed, 200.0)
        self.assertAlmostEqual(order.amount_tax, 36.0)
        self.assertAlmostEqual(order.amount_total, 236.0)
        self.assertEqual(order.zivo_amount_mismatch, 0)
        self.assertEqual(order.state, "sale", "default workflow confirms")

    def test_order_keeps_shopify_date(self):
        order, _status = self.import_order(make_order())
        self.assertEqual(order.state, "sale")
        self.assertEqual(str(order.date_order), "2026-11-05 10:00:00")

    def test_tax_included_order(self):
        node = make_order(taxesIncluded=True, lines=[make_line("1", "11", qty=1, price=118.0, tax=18.0)], total=118.0)
        order, _status = self.import_order(node)
        self.assertAlmostEqual(order.amount_total, 118.0)
        self.assertAlmostEqual(order.amount_untaxed, 100.0)
        self.assertTrue(order.order_line.tax_ids.price_include)

    def test_multiple_taxes_per_line(self):
        line = make_line("1", "11", qty=1, price=100.0, tax_rate=None)
        line["taxLines"] = [
            {"title": "CGST", "rate": 0.09, "priceSet": money(9)},
            {"title": "SGST", "rate": 0.09, "priceSet": money(9)},
        ]
        order, _status = self.import_order(make_order(lines=[line], total=118.0))
        self.assertAlmostEqual(order.amount_total, 118.0)
        self.assertEqual(len(order.order_line.tax_ids), 2)
        self.assertEqual(sorted(order.order_line.tax_ids.mapped("amount")), [9.0, 9.0])

    def test_discount_and_shipping(self):
        line = make_line("1", "11", qty=2, price=100.0, discount=20.0, tax=32.4)
        shipping = {"title": "Express", "code": "x", "isRemoved": False, "originalPriceSet": money(30), "discountedPriceSet": money(30), "taxLines": [{"title": "GST", "rate": 0.18, "priceSet": money(5.4)}]}
        order, _status = self.import_order(make_order(lines=[line], total=247.8, shippingLines=nodes([shipping]), transactions=[make_transaction(amount=247.8)]))
        self.assertAlmostEqual(order.amount_total, 247.8)
        self.assertAlmostEqual(order.order_line[0].discount, 10.0)
        delivery = order.order_line.filtered("is_delivery")
        self.assertEqual(delivery.name, "Express")
        self.assertEqual(order.zivo_amount_mismatch, 0)

    def test_small_difference_is_absorbed(self):
        order, _status = self.import_order(make_order(total=236.03))
        self.assertAlmostEqual(order.amount_total, 236.03)
        self.assertTrue(order.order_line.filtered(lambda l: l.product_id == self.instance.adjustment_product_id))

    def test_large_difference_is_reported(self):
        order, _status = self.import_order(make_order(total=300.0))
        self.assertAlmostEqual(order.zivo_amount_mismatch, 64.0)

    def test_tip_line(self):
        order, _status = self.import_order(make_order(total=246.0, totalTipReceivedSet=money(10)))
        self.assertAlmostEqual(order.amount_total, 246.0)

    def test_custom_item_without_variant(self):
        line = make_line("1", None, qty=1, price=50.0, tax_rate=None, name="Engraving")
        order, _status = self.import_order(make_order(lines=[line], total=50.0))
        self.assertEqual(order.order_line.product_id, self.instance.custom_item_product_id)
        self.assertEqual(order.order_line.name, "Engraving")

    def test_reimport_is_idempotent(self):
        order, _ = self.import_order(make_order())
        again, status = self.import_order(make_order())
        self.assertEqual(status, "updated")
        self.assertEqual(again, order)
        self.assertEqual(len(order.order_line), 1)

    def test_test_orders_skipped(self):
        order, reason = self.import_order(make_order(test=True))
        self.assertFalse(order)
        self.assertIn("Test", reason)

    def test_financial_filter(self):
        self.instance.order_status_filter = "paid"
        order, reason = self.import_order(make_order(displayFinancialStatus="PENDING"))
        self.assertFalse(order)
        order, _ = self.import_order(make_order(gid="1002", displayFinancialStatus="PAID"))
        self.assertTrue(order)

    def test_import_after_date(self):
        self.instance.import_orders_after = "2026-12-01 00:00:00"
        order, reason = self.import_order(make_order())
        self.assertFalse(order)
        self.assertIn("Older", reason)

    def test_pos_filter(self):
        self.instance.import_pos_orders = False
        order, _reason = self.import_order(make_order(sourceName="pos"))
        self.assertFalse(order)

    def test_presentment_currency(self):
        self.instance.use_presentment_currency = True
        eur = self.env["res.currency"].with_context(active_test=False).search([("name", "=", "EUR")])
        eur.active = True
        line = make_line("1", "11", qty=1, tax_rate=None)
        line["originalUnitPriceSet"] = money(100, presentment=90, presentment_code="EUR")
        order, _ = self.import_order(make_order(lines=[line], presentmentCurrencyCode="EUR", totalPriceSet=money(100, presentment=90, presentment_code="EUR"), currentTotalPriceSet=money(100, presentment=90, presentment_code="EUR")))
        self.assertEqual(order.currency_id, eur)
        self.assertAlmostEqual(order.amount_total, 90.0)

    def test_prefix_and_channel(self):
        self.instance.order_prefix = "SHP-"
        order, _ = self.import_order(make_order(sourceName="pos"))
        self.assertEqual(order.name, "SHP-1001")
        self.assertEqual(order.zivo_sales_channel, "pos")

    def test_unmapped_variant_fails_job_with_diagnosis(self):
        node = make_order(lines=[make_line("1", "999", sku="NOPE")])
        self.shop.orders[node["id"]] = node
        job = self.instance._enqueue("import_order", "x", {"gid": node["id"]})
        with mute_logger("odoo.addons.zivoit_shopify_connector.models.job"):
            self.run_queue()
        self.assertEqual(job.state, "failed")
        self.assertIn("not linked", job.ai_root_cause)
        self.assertTrue(job.ai_fix)
        self.assertFalse(self.env["sale.order"].search([("zivo_shopify_gid", "=", node["id"])]), "savepoint rolls back")

    def test_unknown_variant_triggers_product_import(self):
        from .shopify_mock import make_product, make_variant
        self.shop.products["gid://shopify/Product/70"] = make_product("70", "Fresh", [make_variant("71", "FRESH-1")])
        order, _ = self.import_order(make_order(lines=[make_line("1", "71", sku="FRESH-1", qty=1, tax_rate=None)], total=100))
        self.assertEqual(order.order_line.product_id.default_code, "FRESH-1")

    def test_partner_reused_and_addresses(self):
        first, _ = self.import_order(make_order())
        ship = address(city="Pune", zip_code="411001", province="MH", line="9 Hill")
        second, _ = self.import_order(make_order(gid="1002", shippingAddress=ship, billingAddressMatchesShippingAddress=False))
        self.assertEqual(first.partner_id, second.partner_id)
        self.assertEqual(second.partner_shipping_id.type, "delivery")
        self.assertEqual(second.partner_shipping_id.city, "Pune")
        self.assertEqual(first.partner_id.country_id.code, "IN")

    def test_guest_checkout(self):
        order, _ = self.import_order(make_order(customer=None, email="guest@example.com"))
        self.assertEqual(order.partner_id.email, "guest@example.com")

    def test_full_workflow_invoices_and_pays(self):
        self.enable_full_workflow()
        order, _ = self.import_order(make_order())
        invoice = order.invoice_ids
        self.assertEqual(invoice.state, "posted")
        self.assertIn(invoice.payment_state, ("in_payment", "paid"))
        self.assertTrue(order.zivo_transaction_ids.payment_id)
        self.assertAlmostEqual(order.zivo_fees, 7.0)

    def test_payment_not_duplicated_on_refresh(self):
        self.enable_full_workflow()
        order, _ = self.import_order(make_order())
        payments = order.zivo_transaction_ids.payment_id
        self.import_order(make_order())
        self.assertEqual(order.zivo_transaction_ids.payment_id, payments)
        self.assertEqual(self.env["account.payment"].search_count([("id", "in", payments.ids)]), 1)

    def test_partial_captures_become_two_payments(self):
        self.enable_full_workflow()
        order, _ = self.import_order(make_order(transactions=[make_transaction("1", 100.0, "CAPTURE")], displayFinancialStatus="PARTIALLY_PAID"))
        self.assertEqual(order.invoice_ids.payment_state, "partial")
        self.import_order(make_order(transactions=[make_transaction("1", 100.0, "CAPTURE"), make_transaction("2", 136.0, "CAPTURE")]))
        self.assertEqual(len(order.zivo_transaction_ids.payment_id), 2)
        self.assertIn(order.invoice_ids.payment_state, ("in_payment", "paid"))

    def test_gateway_workflow_selection(self):
        cod = self.env["zivo.shopify.workflow"].create({"instance_id": self.instance.id, "name": "COD", "sequence": 1, "gateways": "Cash on Delivery (COD)", "confirm_order": False})
        order, _ = self.import_order(make_order(paymentGatewayNames=["Cash on Delivery (COD)"]))
        self.assertEqual(order.state, "draft")
        self.assertEqual(order._zivo_workflow(make_order(paymentGatewayNames=["Cash on Delivery (COD)"])), cod)

    def test_paid_only_workflow_runs_on_later_payment(self):
        self.instance.workflow_ids.required_status = "paid"
        order, _ = self.import_order(make_order(displayFinancialStatus="PENDING", transactions=[]))
        self.assertEqual(order.state, "draft")
        self.import_order(make_order(displayFinancialStatus="PAID"))
        self.assertEqual(order.state, "sale")

    def test_cancel_from_shopify(self):
        order, _ = self.import_order(make_order())
        self.import_order(make_order(cancelledAt="2026-11-06T00:00:00Z", cancelReason="CUSTOMER"))
        self.assertEqual(order.state, "cancel")
        self.assertTrue(order.zivo_cancelled_on_shopify)

    def test_cancel_after_delivery_needs_human(self):
        order, _ = self.import_order(make_order())
        order._zivo_validate_open_deliveries()
        self.import_order(make_order(cancelledAt="2026-11-06T00:00:00Z", cancelReason="CUSTOMER"))
        self.assertEqual(order.state, "sale")
        self.assertTrue(order.activity_ids)

    def test_cancel_to_shopify(self):
        order, _ = self.import_order(make_order())
        self.env["zivo.shopify.cancel.wizard"].create({"order_id": order.id, "reason": "FRAUD"}).action_confirm()
        self.assertEqual(self.shop.ops("ZivoOrderCancel")[0][1]["reason"], "FRAUD")
        self.assertEqual(order.state, "cancel")

    def test_shopify_edit_adds_and_changes_lines(self):
        order, _ = self.import_order(make_order())
        lines = [make_line("1", "11", qty=2, current=1), make_line("2", "11", qty=3, name="Added")]
        self.import_order(make_order(lines=lines, edited=True))
        first = order.order_line.filtered(lambda l: l.zivo_line_gid.endswith("/1"))
        self.assertEqual(first.product_uom_qty, 1)
        self.assertEqual(len(order.order_line), 2)

    def test_edit_cannot_go_below_delivered(self):
        order, _ = self.import_order(make_order())
        order._zivo_validate_open_deliveries()
        self.import_order(make_order(lines=[make_line("1", "11", qty=2, current=1)], edited=True))
        self.assertEqual(order.order_line.product_uom_qty, 2)

    def test_push_edits_to_shopify(self):
        order, _ = self.import_order(make_order())
        order.order_line.product_uom_qty = 5
        self.assertTrue(order._zivo_push_edits())
        self.assertEqual(self.shop.ops("ZivoOrderEditSetQty")[0][1]["quantity"], 5)
        self.assertTrue(self.shop.ops("ZivoOrderEditCommit"))

    def test_export_order_to_shopify(self):
        partner = self.env["res.partner"].create({"name": "Offline Buyer", "email": "o@x.test"})
        order = self.env["sale.order"].create({"partner_id": partner.id, "order_line": [Command.create({"product_id": self.product.id, "product_uom_qty": 2})]})
        order._zivo_export_to_shopify(self.instance)
        self.assertEqual(order.zivo_shopify_name, "#9001")
        sent = self.shop.ops("ZivoOrderCreate")[0][1]
        self.assertEqual(sent["order"]["lineItems"][0]["variantId"], "gid://shopify/ProductVariant/11")

    def test_shipping_address_update(self):
        order, _ = self.import_order(make_order())
        self.import_order(make_order(shippingAddress=address(city="Chennai", zip_code="600001", province="TN", line="3 Bay")))
        self.assertEqual(order.partner_shipping_id.city, "Chennai")
        self.assertEqual(order.picking_ids[:1].partner_id.city, "Chennai")

    def test_tax_not_created_when_disabled(self):
        self.instance.auto_create_taxes = False
        with self.assertRaises(UserError):
            self.env["sale.order"]._zivo_get_tax(self.instance, 0.237, "Odd", False)

    def test_order_page_job_queues_one_job_per_order(self):
        self.shop.orders["a"] = make_order(gid="1")
        self.shop.orders["b"] = make_order(gid="2")
        job = self.instance._enqueue("import_orders", "page", {})
        job._run()
        self.assertEqual(job.state, "done", job.error_message)
        self.assertEqual(len(self.jobs().filtered(lambda j: j.job_type == "import_order")), 2)

    def test_b2b_company(self):
        self.instance.company_mode = "b2b"
        order, _ = self.import_order(make_order(billingAddress=address(company="Zen Pvt Ltd")))
        self.assertEqual(order.partner_id.parent_id.name, "Zen Pvt Ltd")
        self.assertTrue(order.partner_id.parent_id.is_company)

    def test_market_routes_warehouse(self):
        second = self.env["stock.warehouse"].create({"name": "EU", "code": "EUW", "company_id": self.env.company.id})
        market = self.env["zivo.shopify.market"].create({"instance_id": self.instance.id, "name": "EU", "shopify_gid": "gid://shopify/Market/1", "country_codes": "DE,FR", "warehouse_id": second.id})
        self.env["stock.quant"].with_context(inventory_mode=True).create({"product_id": self.product.id, "location_id": second.lot_stock_id.id, "inventory_quantity": 5}).action_apply_inventory()
        order, _ = self.import_order(make_order(shippingAddress=address(country="DE", province=None, zip_code="10115", city="Berlin")))
        self.assertEqual(order.zivo_market_id, market)


@tagged("post_install", "-at_install")
class TestRouting(ZivoShopifyCommon):

    def setUp(self):
        super().setUp()
        self.product = self.stock_product(qty=1)
        self.south = self.env["stock.warehouse"].create({"name": "South", "code": "STH", "company_id": self.env.company.id})
        self.env["stock.quant"].with_context(inventory_mode=True).create({"product_id": self.product.id, "location_id": self.south.lot_stock_id.id, "inventory_quantity": 10}).action_apply_inventory()
        self.Rule = self.env["zivo.shopify.routing.rule"]

    def rule(self, **vals):
        return self.Rule.create({"instance_id": self.instance.id, "name": "R", "warehouse_id": self.south.id, **vals})

    def test_zip_prefix_rule_wins(self):
        self.rule(zip_prefixes="500,501")
        order, _ = self.import_order(make_order())
        self.assertEqual(order.warehouse_id, self.south)
        self.assertIn("South", order.zivo_routing_note)

    def test_rule_not_matching_keeps_default(self):
        self.rule(zip_prefixes="110")
        warehouse, _note = self.Rule._zivo_route_order(self.instance, self.env["zivo.shopify.market"], address(), {self.product.id: 1})
        self.assertEqual(warehouse, self.warehouse)

    def test_zip_range(self):
        rule = self.rule(zip_from="400000", zip_to="599999")
        self.assertTrue(rule._matches("IN", "TS", "500049"))
        self.assertFalse(rule._matches("IN", "TS", "700001"))

    def test_stock_overrides_geography(self):
        north = self.env["stock.warehouse"].create({"name": "North", "code": "NTH", "company_id": self.env.company.id})
        self.rule(zip_prefixes="500", warehouse_id=north.id, sequence=1)
        self.rule(zip_prefixes="500", sequence=2)
        warehouse, note = self.Rule._zivo_route_order(self.instance, self.env["zivo.shopify.market"], address(), {self.product.id: 5})
        self.assertEqual(warehouse, self.south, "north has none, south has full stock")
        self.assertIn("full stock", note)

    def test_split_across_warehouses(self):
        self.stock_product(sku="SKU-2", name="Only North", qty=3, gid="12")
        self.env["stock.quant"].with_context(inventory_mode=True).search([("product_id", "=", self.product.id), ("location_id", "=", self.warehouse.lot_stock_id.id)]).write({"inventory_quantity": 0})
        self.env["stock.quant"].search([("product_id", "=", self.product.id), ("location_id", "=", self.warehouse.lot_stock_id.id)]).action_apply_inventory()
        self.instance.routing_allow_split = True
        self.rule(zip_prefixes="500")
        lines = [make_line("1", "11", qty=2, tax_rate=None), make_line("2", "12", sku="SKU-2", qty=1, tax_rate=None, name="Other")]
        order, _ = self.import_order(make_order(lines=lines, total=300))
        self.assertIn("Split", order.zivo_routing_note)
        self.assertEqual(order.state, "sale")
        self.assertEqual(len(order.picking_ids), 2)
        self.assertEqual(order.picking_ids.picking_type_id.warehouse_id, self.warehouse | self.south)

    def test_partial_stock_picks_best(self):
        self.rule(zip_prefixes="500")
        warehouse, note = self.Rule._zivo_route_order(self.instance, self.env["zivo.shopify.market"], address(), {self.product.id: 20})
        self.assertEqual(warehouse, self.south)
        self.assertIn("50%", note)
