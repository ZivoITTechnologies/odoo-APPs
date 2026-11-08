from odoo.tests import tagged

from .common import ZivoShopifyCommon
from .shopify_mock import make_customer, make_line, make_order, make_transaction, money, nodes


def ship_gid(number="9"):
    return f"gid://shopify/Fulfillment/{number}"


def refund(gid="1", lines=None, total=118.0, transactions=None, shipping=None, note=None):
    return {
        "id": f"gid://shopify/Refund/{gid}", "note": note, "createdAt": "2026-11-07T00:00:00Z",
        "totalRefundedSet": money(total),
        "refundLineItems": nodes(lines if lines is not None else []),
        "refundShippingLines": nodes(shipping or []),
        "orderAdjustments": nodes([]),
        "transactions": nodes(transactions if transactions is not None else [
            {"id": "gid://shopify/OrderTransaction/90", "kind": "REFUND", "status": "SUCCESS", "gateway": "shopify_payments", "amountSet": money(total)},
        ]),
    }


def refund_line(line_gid="1", qty=1, restock="NO_RESTOCK", restocked=False):
    return {
        "quantity": qty, "restockType": restock, "restocked": restocked, "lineItem": {"id": f"gid://shopify/LineItem/{line_gid}"},
        "location": {"id": "gid://shopify/Location/1"}, "subtotalSet": money(100.0), "totalTaxSet": money(18.0),
    }


@tagged("post_install", "-at_install")
class TestFulfillment(ZivoShopifyCommon):

    def setUp(self):
        super().setUp()
        self.product = self.stock_product()
        self.order, _ = self.import_order(make_order())
        self.picking = self.order.picking_ids

    def validate(self, quantity=None):
        for move in self.picking.move_ids:
            move.quantity = quantity if quantity is not None else move.product_uom_qty
            move.picked = True
        self.picking._action_done()

    def test_delivery_exports_fulfillment_with_tracking(self):
        carrier = self.env["delivery.carrier"].create({"name": "DTDC", "product_id": self.instance.shipping_product_id.id})
        self.picking.write({"carrier_id": carrier.id, "carrier_tracking_ref": "TRK123"})
        self.validate()
        self.assertEqual(self.picking.zivo_export_state, "todo")
        self.run_queue()
        self.assertFalse(self.jobs("failed"), self.jobs("failed").mapped("error_message"))
        sent = self.shop.ops("ZivoFulfillmentCreate")[0][1]["fulfillment"]
        self.assertEqual(sent["trackingInfo"], {"number": "TRK123", "company": "DTDC"})
        self.assertEqual(sent["lineItemsByFulfillmentOrder"][0]["fulfillmentOrderLineItems"][0]["quantity"], 2)
        self.assertEqual(self.picking.zivo_export_state, "done")
        self.assertEqual(self.order.zivo_fulfillment_ids.origin, "odoo")

    def test_partial_delivery(self):
        self.validate(quantity=1)
        self.run_queue()
        sent = self.shop.ops("ZivoFulfillmentCreate")[0][1]["fulfillment"]
        self.assertEqual(sent["lineItemsByFulfillmentOrder"][0]["fulfillmentOrderLineItems"][0]["quantity"], 1)

    def test_export_only_once(self):
        self.validate()
        self.run_queue()
        job = self.instance._enqueue("export_fulfillment", "again", {"picking": self.picking.id})
        self.run_queue()
        self.assertEqual(len(self.shop.ops("ZivoFulfillmentCreate")), 1)
        self.assertIn("Already", job.result)

    def test_auto_export_can_be_disabled(self):
        self.instance.auto_export_fulfillment = False
        self.validate()
        self.assertFalse(self.jobs().filtered(lambda j: j.job_type == "export_fulfillment"))

    def test_multi_location_fulfillment_orders(self):
        lines = [make_line("1", "11", qty=2), make_line("2", "11", qty=1, name="Second")]
        node = make_order(gid="2000", lines=lines, total=354.0)
        order, _ = self.import_order(node)
        self.shop.fulfillment_orders[node["id"]] = [
            {"id": "gid://shopify/FulfillmentOrder/A", "status": "OPEN", "supportedActions": [{"action": "CREATE_FULFILLMENT"}],
             "assignedLocation": {"location": {"id": "gid://shopify/Location/1"}},
             "lineItems": nodes([{"id": "gid://shopify/FulfillmentOrderLineItem/1", "remainingQuantity": 2, "totalQuantity": 2, "lineItem": {"id": "gid://shopify/LineItem/1"}}])},
            {"id": "gid://shopify/FulfillmentOrder/B", "status": "OPEN", "supportedActions": [{"action": "CREATE_FULFILLMENT"}],
             "assignedLocation": {"location": {"id": "gid://shopify/Location/2"}},
             "lineItems": nodes([{"id": "gid://shopify/FulfillmentOrderLineItem/2", "remainingQuantity": 1, "totalQuantity": 1, "lineItem": {"id": "gid://shopify/LineItem/2"}}])},
        ]
        picking = order.picking_ids
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        picking._action_done()
        self.run_queue()
        self.assertEqual(len(self.shop.ops("ZivoFulfillmentCreate")), 2)

    def shopify_fulfillment(self, quantity=2, number="9"):
        return {
            "id": ship_gid(number), "name": "#1", "status": "SUCCESS", "createdAt": "2026-11-06T00:00:00Z", "location": {"id": "gid://shopify/Location/1"},
            "trackingInfo": [{"company": "FedEx", "number": "FX1", "url": "https://t.test/FX1"}],
            "fulfillmentLineItems": nodes([{"id": "gid://shopify/FulfillmentLineItem/1", "quantity": quantity, "lineItem": {"id": "gid://shopify/LineItem/1"}}]),
        }

    def test_fulfillment_made_on_shopify_validates_delivery(self):
        self.import_order(make_order(fulfillments=[self.shopify_fulfillment()], displayFulfillmentStatus="FULFILLED"))
        self.assertEqual(self.picking.state, "done")
        self.assertEqual(self.picking.carrier_tracking_ref, "FX1")
        self.assertEqual(self.order.zivo_fulfillment_ids.origin, "shopify")
        self.assertFalse(self.jobs().filtered(lambda j: j.job_type == "export_fulfillment"), "must not echo back")

    def test_partial_fulfillment_on_shopify_leaves_backorder(self):
        self.import_order(make_order(fulfillments=[self.shopify_fulfillment(quantity=1)]))
        self.assertEqual(self.picking.state, "done")
        backorder = self.order.picking_ids - self.picking
        self.assertEqual(backorder.move_ids.product_uom_qty, 1)

    def test_shopify_fulfillment_applied_once(self):
        self.import_order(make_order(fulfillments=[self.shopify_fulfillment()]))
        self.import_order(make_order(fulfillments=[self.shopify_fulfillment()]))
        self.assertEqual(len(self.order.zivo_fulfillment_ids), 1)

    def test_invoice_after_delivery_policy(self):
        product = self.stock_product(sku="DEL-1", gid="55")
        product.invoice_policy = "delivery"
        self.enable_full_workflow()
        node = make_order(gid="3000", lines=[make_line("1", "55", sku="DEL-1", qty=1, tax_rate=None)], total=100.0, transactions=[make_transaction(amount=100.0)])
        order, _ = self.import_order(node)
        self.assertFalse(order.invoice_ids, "nothing to invoice before shipping")
        picking = order.picking_ids
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        picking._action_done()
        self.run_queue()
        self.assertFalse(self.jobs("failed"), self.jobs("failed").mapped("error_message"))
        self.assertEqual(order.invoice_ids.state, "posted")


@tagged("post_install", "-at_install")
class TestRefunds(ZivoShopifyCommon):

    def setUp(self):
        super().setUp()
        self.product = self.stock_product()
        self.enable_full_workflow()
        self.node = make_order()
        self.order, _ = self.import_order(self.node)

    def deliver(self):
        for picking in self.order.picking_ids:
            for move in picking.move_ids:
                move.quantity = move.product_uom_qty
                move.picked = True
            picking.with_context(zivo_no_fulfillment_export=True)._action_done()

    def credit_notes(self):
        return self.order.zivo_refund_ids.move_id

    def test_line_refund_creates_credit_note(self):
        self.import_order(make_order(refunds=[refund(lines=[refund_line()])], displayFinancialStatus="PARTIALLY_REFUNDED"))
        move = self.credit_notes()
        self.assertEqual(move.move_type, "out_refund")
        self.assertEqual(move.state, "posted")
        self.assertAlmostEqual(move.amount_total, 118.0)
        self.assertIn(move.payment_state, ("in_payment", "paid"), "refund is paid back")

    def test_refund_imported_once(self):
        data = refund(lines=[refund_line()])
        self.import_order(make_order(refunds=[data]))
        self.import_order(make_order(refunds=[data]))
        self.assertEqual(len(self.order.zivo_refund_ids), 1)

    def test_goodwill_refund_without_lines(self):
        self.import_order(make_order(refunds=[refund(total=20.0, lines=[], note="Sorry")]))
        move = self.credit_notes()
        self.assertAlmostEqual(move.amount_total, 20.0)
        self.assertEqual(move.invoice_line_ids.product_id, self.instance.adjustment_product_id)

    def test_partial_amount_is_adjusted_to_shopify_total(self):
        self.import_order(make_order(refunds=[refund(total=50.0, lines=[refund_line()])]))
        self.assertAlmostEqual(self.credit_notes().amount_total, 50.0)

    def test_shipping_refund(self):
        shipping_line = {"subtotalAmountSet": money(30), "taxAmountSet": money(0)}
        self.import_order(make_order(refunds=[refund(total=30.0, lines=[], shipping=[shipping_line])]))
        self.assertAlmostEqual(self.credit_notes().amount_total, 30.0)

    def test_return_restock_creates_return_picking(self):
        self.deliver()
        before = self.product.with_context(warehouse_id=self.warehouse.id).qty_available
        self.import_order(make_order(refunds=[refund(lines=[refund_line(restock="RETURN", restocked=True)])]))
        record = self.order.zivo_refund_ids
        self.assertEqual(record.return_picking_id.state, "done")
        self.env.invalidate_all()
        self.assertEqual(self.product.with_context(warehouse_id=self.warehouse.id).qty_available, before + 1)
        line = self.order.order_line
        self.assertEqual(line.qty_delivered, 1)

    def test_pure_cancel_lowers_order_quantity(self):
        data = refund(lines=[refund_line(restock="CANCEL", restocked=True)])
        self.import_order(make_order(lines=[make_line("1", "11", qty=2, current=1)], refunds=[data]))
        self.assertEqual(self.order.order_line.product_uom_qty, 1)
        self.assertEqual(self.credit_notes().state, "posted")
        self.assertEqual(self.order.picking_ids.move_ids.product_uom_qty, 1)

    def test_pure_cancel_before_import_is_absorbed(self):
        data = refund(gid="8", lines=[refund_line(restock="CANCEL", restocked=True)])
        order, _ = self.import_order(make_order(gid="4000", lines=[make_line("1", "11", qty=2, current=1)], refunds=[data], total=118.0, transactions=[make_transaction(amount=118.0)]))
        self.assertEqual(order.order_line.product_uom_qty, 1)
        self.assertEqual(order.zivo_refund_ids.state, "absorbed")
        self.assertFalse(order.zivo_refund_ids.move_id)

    def test_export_refund_from_credit_note(self):
        invoice = self.order.invoice_ids
        wizard = self.env["account.move.reversal"].with_context(active_model="account.move", active_ids=invoice.ids).create({"journal_id": invoice.journal_id.id, "date": invoice.date})
        result = wizard.refund_moves()
        move = self.env["account.move"].browse(result["res_id"])
        move.invoice_line_ids.quantity = 1
        move.action_post()
        self.assertEqual(move.zivo_order_id, self.order)
        move.action_zivo_refund_on_shopify()
        self.run_queue()
        self.assertFalse(self.jobs("failed"), self.jobs("failed").mapped("error_message"))
        sent = self.shop.ops("ZivoRefundCreate")[0][1]
        self.assertEqual(sent["input"]["refundLineItems"][0]["quantity"], 1)
        self.assertEqual(sent["input"]["transactions"][0]["amount"], "118.00")
        self.assertTrue(sent["key"])
        self.assertTrue(move.zivo_refund_gid)
        # The webhook echo for this refund must not create a second credit note.
        self.import_order(make_order(refunds=[refund(gid=move.zivo_refund_gid.rsplit("/", 1)[-1], lines=[refund_line()])]))
        self.assertEqual(len(self.order.zivo_refund_ids), 1)

    def test_return_import_then_picking(self):
        self.deliver()
        returned = {"id": "gid://shopify/Return/1", "name": "#1-R1", "status": "OPEN", "returnLineItems": nodes([{
            "__typename": "ReturnLineItem", "id": "gid://shopify/ReturnLineItem/1", "quantity": 1, "customerNote": "Too big", "returnReasonDefinition": {"name": "Size"},
            "fulfillmentLineItem": {"lineItem": {"id": "gid://shopify/LineItem/1"}},
        }])}
        self.import_order(make_order(returns=nodes([returned])))
        record = self.order.zivo_return_ids
        self.assertEqual(record.status, "OPEN")
        self.assertIn("Size", record.summary)
        record.action_create_picking()
        self.assertEqual(record.picking_id.state, "done")

    def test_return_export(self):
        self.deliver()
        self.node["fulfillments"] = [{
            "id": ship_gid("7"), "name": "#1", "status": "SUCCESS", "createdAt": "2026-11-06T00:00:00Z", "location": {"id": "gid://shopify/Location/1"}, "trackingInfo": [],
            "fulfillmentLineItems": nodes([{"id": "gid://shopify/FulfillmentLineItem/5", "quantity": 2, "lineItem": {"id": "gid://shopify/LineItem/1"}}]),
        }]
        self.shop.orders[self.node["id"]] = self.node
        delivery = self.order.picking_ids
        wizard = self.env["stock.return.picking"].with_context(active_id=delivery.id, active_model="stock.picking").create({"picking_id": delivery.id})
        wizard.product_return_moves.quantity = 1
        back = wizard._create_return()
        gid = back._zivo_create_shopify_return()
        self.assertTrue(gid)
        sent = self.shop.ops("ZivoReturnCreate")[0][1]["input"]
        self.assertEqual(sent["returnLineItems"][0]["fulfillmentLineItemId"], "gid://shopify/FulfillmentLineItem/5")


@tagged("post_install", "-at_install")
class TestPayouts(ZivoShopifyCommon):

    def setUp(self):
        super().setUp()
        self.stock_product()
        self.enable_full_workflow()
        self.order, _ = self.import_order(make_order())
        self.shop.payouts = [{
            "id": "gid://shopify/ShopifyPaymentsPayout/1", "status": "PAID", "issuedAt": "2026-11-07T00:00:00Z", "transactionType": "DEPOSIT",
            "net": {"amount": "229.00", "currencyCode": "USD"},
            "summary": {key: {"amount": "0.00", "currencyCode": "USD"} for key in (
                "chargesGross", "chargesFee", "refundsFeeGross", "refundsFee", "adjustmentsGross", "adjustmentsFee",
                "reservedFundsGross", "reservedFundsFee", "retriedPayoutsGross", "retriedPayoutsFee")},
        }]
        self.shop.balance = [self.balance_line()]

    def balance_line(self, gid="1", amount=236.0, fee=7.0, order="1001", kind="CHARGE", payout="1"):
        return {
            "id": f"gid://shopify/ShopifyPaymentsBalanceTransaction/{gid}", "type": kind, "sourceType": kind, "sourceOrderTransactionId": "1",
            "transactionDate": "2026-11-06T00:00:00Z", "test": False,
            "amount": {"amount": f"{amount:.2f}", "currencyCode": "USD"}, "fee": {"amount": f"{fee:.2f}", "currencyCode": "USD"}, "net": {"amount": f"{amount - fee:.2f}", "currencyCode": "USD"},
            "associatedOrder": {"id": f"gid://shopify/Order/{order}", "name": f"#{order}"} if order else None,
            "associatedPayout": {"id": f"gid://shopify/ShopifyPaymentsPayout/{payout}", "status": "PAID"},
        }

    def payout(self):
        self.instance.action_import_payouts()
        self.run_queue()
        self.assertFalse(self.jobs("failed"), self.jobs("failed").mapped("error_message"))
        return self.env["zivo.shopify.payout"].search([("instance_id", "=", self.instance.id)])

    def test_import_links_orders(self):
        payout = self.payout()
        self.assertEqual(payout.line_ids.order_id, self.order)
        self.assertAlmostEqual(payout.gross_amount, 236.0)
        self.assertAlmostEqual(payout.fee_amount, 7.0)
        self.assertAlmostEqual(payout.effective_fee_percent, 7.0 / 236.0 * 100, places=3)
        self.assertEqual(payout.anomaly_count, 0)

    def test_import_is_idempotent(self):
        self.payout()
        self.assertEqual(len(self.payout()), 1)

    def test_entry_balances_and_reconciles(self):
        payout = self.payout()
        payout.action_process()
        self.assertEqual(payout.state, "reconciled")
        move = payout.move_id
        self.assertEqual(move.state, "posted")
        self.assertAlmostEqual(sum(move.line_ids.mapped("debit")), sum(move.line_ids.mapped("credit")))
        fees = move.line_ids.filtered(lambda l: l.account_id == self.instance.fee_account_id)
        self.assertAlmostEqual(fees.debit, 7.0)
        self.assertIn(self.order.invoice_ids.payment_state, ("paid", "in_payment"))
        self.assertTrue(self.order.zivo_transaction_ids.payment_id.move_id.line_ids.filtered(lambda l: l.account_id == self.instance._clearing_account()).reconciled)

    def test_unmatched_charge_is_flagged(self):
        self.shop.balance.append(self.balance_line("2", amount=0.0, fee=0.0, order="7777"))
        self.assertGreaterEqual(self.payout().anomaly_count, 1)

    def test_net_mismatch_is_flagged(self):
        self.shop.payouts[0]["net"]["amount"] = "200.00"
        payout = self.payout()
        self.assertIn("net to", payout.anomaly_note)

    def test_processing_requires_accounts(self):
        payout = self.payout()
        self.instance.fee_account_id = False
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            payout.action_process()

    def test_no_shopify_payments(self):
        self.shop.op_ZivoPayouts = lambda _v: {"shopifyPaymentsAccount": None}
        job = self.instance._enqueue("import_payouts", "p", {})
        self.run_queue()
        self.assertIn("not active", job.result)


@tagged("post_install", "-at_install")
class TestCustomersCommerce(ZivoShopifyCommon):

    def upsert(self, node):
        return self.env["res.partner"]._zivo_upsert_customer(self.instance, node)

    def test_new_customer(self):
        partner = self.upsert(make_customer())
        self.assertEqual(partner.name, "Asha Rao")
        self.assertEqual(partner.email, "asha@example.com")
        self.assertEqual(partner.state_id.code, "TS")

    def test_match_by_email(self):
        existing = self.env["res.partner"].create({"name": "A Rao", "email": "ASHA@example.com"})
        self.assertEqual(self.upsert(make_customer()), existing)

    def test_match_by_phone(self):
        existing = self.env["res.partner"].create({"name": "Someone", "phone": "98765 43210"})
        self.assertEqual(self.upsert(make_customer(email=None)), existing)
        self.assertEqual(self.env["zivo.shopify.customer"].search([("partner_id", "=", existing.id)]).match_method, "phone")

    def test_shared_phone_with_other_email_is_a_new_contact(self):
        existing = self.env["res.partner"].create({"name": "Someone", "email": "other@example.com", "phone": "98765 43210"})
        self.assertNotEqual(self.upsert(make_customer()), existing)

    def test_delivery_address_is_never_a_match(self):
        parent = self.env["res.partner"].create({"name": "Parent"})
        address = self.env["res.partner"].create({"name": "Courier Desk", "parent_id": parent.id, "type": "delivery", "phone": "98765 43210"})
        self.assertNotEqual(self.upsert(make_customer(email=None)), address)

    def test_fuzzy_name_and_zip(self):
        existing = self.env["res.partner"].create({"name": "Asha Rao", "zip": "500049"})
        partner = self.upsert(make_customer(email=None, phone=None))
        self.assertEqual(partner, existing)
        self.assertEqual(self.env["zivo.shopify.customer"].search([("partner_id", "=", existing.id)]).match_method, "fuzzy")

    def test_different_person_is_not_merged(self):
        existing = self.env["res.partner"].create({"name": "Ravi Kumar", "zip": "500049"})
        self.assertNotEqual(self.upsert(make_customer(email=None, phone=None)), existing)

    def test_second_import_same_partner(self):
        self.assertEqual(self.upsert(make_customer()), self.upsert(make_customer()))

    def test_export_customer(self):
        partner = self.env["res.partner"].create({"name": "Veer Singh", "email": "v@x.test"})
        job = self.instance._enqueue("export_customer", "e", {"partner": partner.id})
        self.run_queue()
        self.assertEqual(job.state, "done", job.error_message)
        sent = self.shop.ops("ZivoCustomerSet")[0][1]
        self.assertEqual(sent["identifier"], {"email": "v@x.test"})
        self.assertEqual(sent["input"]["lastName"], "Singh")

    def test_customer_page(self):
        self.shop.customers["a"] = make_customer("9", "z@x.test")
        self.instance.action_import_customers()
        self.run_queue()
        self.assertTrue(self.env["res.partner"].search([("email", "=", "z@x.test")]))

    def test_segments_tag_partners(self):
        partner = self.upsert(make_customer())
        self.shop.segments = [{"id": "gid://shopify/Segment/1", "name": "VIP", "query": "x"}]
        self.shop.segment_members["gid://shopify/Segment/1"] = ["gid://shopify/CustomerSegmentMember/1"]
        self.instance.action_import_segments()
        self.run_queue()
        self.assertIn("Shopify: VIP", partner.category_id.mapped("name"))

    def checkout(self, gid="1", completed=None):
        return {
            "id": f"gid://shopify/AbandonedCheckout/{gid}", "name": f"#C{gid}", "abandonedCheckoutUrl": "https://acme.test/recover", "createdAt": "2026-11-05T00:00:00Z", "updatedAt": "2026-11-05T00:00:00Z",
            "completedAt": completed, "note": "", "totalPriceSet": money(99.0),
            "customer": {"id": "gid://shopify/Customer/1", "firstName": "Asha", "lastName": "Rao", "defaultEmailAddress": {"emailAddress": "asha@example.com"}, "defaultPhoneNumber": None},
            "billingAddress": None,
            "lineItems": nodes([{"id": "gid://shopify/AbandonedCheckoutLineItem/1", "title": "Blue Tee", "variantTitle": "M", "quantity": 2, "sku": "S", "variant": None, "discountedTotalPriceSet": money(99.0)}]),
        }

    def test_checkout_becomes_lead_then_won(self):
        self.stock_product()
        self.shop.checkouts = [self.checkout()]
        self.instance.action_import_checkouts()
        self.run_queue()
        lead = self.env["crm.lead"].search([("zivo_checkout_gid", "!=", False), ("zivo_instance_id", "=", self.instance.id)])
        self.assertEqual(lead.expected_revenue, 99.0)
        self.assertIn("2 x Blue Tee (M)", lead.zivo_cart_summary)
        self.import_order(make_order())
        self.assertEqual(lead.won_status, "won")

    def test_completed_checkout_ignored(self):
        self.shop.checkouts = [self.checkout(completed="2026-11-05T00:00:00Z")]
        self.instance.action_import_checkouts()
        self.run_queue()
        self.assertFalse(self.env["crm.lead"].search([("zivo_instance_id", "=", self.instance.id)]))

    def test_recovery_email_template_escapes(self):
        self.shop.checkouts = [self.checkout()]
        self.instance.action_import_checkouts()
        self.run_queue()
        lead = self.env["crm.lead"].search([("zivo_instance_id", "=", self.instance.id)])
        lead.zivo_cart_summary = "<script>x</script>"
        lead.action_zivo_generate_email()
        self.assertNotIn("<script>", str(lead.zivo_recovery_email))
        self.assertIn("https://acme.test/recover", str(lead.zivo_recovery_email))

    def market_node(self):
        return {
            "id": "gid://shopify/Market/1", "name": "Europe", "handle": "eu", "status": "ACTIVE", "type": "REGION",
            "conditions": {"regionsCondition": {"regions": nodes([{"__typename": "MarketRegionCountry", "code": "DE", "name": "Germany"}, {"__typename": "MarketRegionCountry", "code": "FR", "name": "France"}])}},
            "catalogs": nodes([{"id": "gid://shopify/MarketCatalog/1", "title": "EU", "status": "ACTIVE", "priceList": {"id": "gid://shopify/PriceList/1", "name": "EU", "currency": "EUR"}}]),
        }

    def test_markets_and_price_export(self):
        self.stock_product()
        self.shop.markets = [self.market_node()]
        self.instance.action_import_markets()
        self.run_queue()
        market = self.instance.market_ids
        self.assertEqual(market.country_codes, "DE,FR")
        eur = self.env["res.currency"].with_context(active_test=False).search([("name", "=", "EUR")])
        eur.active = True
        market.pricelist_id = self.env["product.pricelist"].create({"name": "EU", "currency_id": eur.id})
        market.action_export_prices()
        self.run_queue()
        self.assertFalse(self.jobs("failed"), self.jobs("failed").mapped("error_message"))
        self.assertEqual(self.shop.ops("ZivoPriceListAdd")[0][1]["prices"][0]["price"]["currencyCode"], "EUR")
