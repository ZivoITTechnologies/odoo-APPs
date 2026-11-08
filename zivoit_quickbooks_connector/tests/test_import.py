# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
from odoo import fields
from odoo.tests import tagged

from .common import QbCommon


@tagged("post_install", "-at_install", "qbo")
class TestImport(QbCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.seed_masters(cls)

    # ------------------------------------------------------------- masters
    def test_import_accounts_create_and_match(self):
        existing = self.company_data["default_account_expense"]
        self.fake.add("Account", {"Id": "7001", "Name": existing.name, "AccountType": "Expense"})
        job = self.import_obj("Account", self.fake.get("Account", "7001"))
        self.assertJobDone(job)
        self.assertEqual(self.mapped("Account", "7001"), existing, "matched by name, no duplicate")
        job = self.import_obj("Account", self.qb_bank)
        acc = self.mapped("Account", self.qb_bank["Id"])
        self.assertEqual(acc.account_type, "asset_cash")
        self.assertEqual(acc.code, "1010")
        journal = self.env["account.journal"].search([("default_account_id", "=", acc.id)])
        self.assertEqual(journal.type, "bank", "a bank journal is created for QuickBooks bank accounts")

    def test_import_taxcode_non_us(self):
        self.assertJobDone(self.import_obj("TaxCode", self.qb_taxcode))
        sale = self.mapped("TaxCode", self.qb_taxcode["Id"])
        purchase = self.mapped("TaxCodePurchase", self.qb_taxcode["Id"])
        self.assertEqual(sale.type_tax_use, "sale")
        self.assertAlmostEqual(sale.amount, 12.0)
        self.assertEqual(purchase.type_tax_use, "purchase")
        self.assertAlmostEqual(purchase.amount, 5.0)

    def test_import_term_and_analytics(self):
        self.import_obj("Term", self.qb_term)
        term = self.mapped("Term", self.qb_term["Id"])
        self.assertEqual(term.line_ids.nb_days, 30)
        self.import_obj("Class", self.qb_class)
        self.import_obj("Department", self.qb_dept)
        self.assertEqual(self.mapped("Class", self.qb_class["Id"]).plan_id, self.conn.class_plan_id)
        self.assertEqual(self.mapped("Department", self.qb_dept["Id"]).plan_id, self.conn.location_plan_id)

    def test_import_customer_with_dependencies(self):
        job = self.import_obj("Customer", self.qb_customer)
        self.assertJobDone(job)
        partner = self.mapped("Customer", self.qb_customer["Id"])
        self.assertEqual(partner.name, "Amy's Bird Sanctuary")
        self.assertEqual(partner.email, "birds@example.com")
        self.assertEqual(partner.city, "Bayshore")
        self.assertEqual(partner.country_id.code, "US")
        self.assertEqual(partner.state_id.code, "CA")
        self.assertGreater(partner.customer_rank, 0)
        self.assertEqual(partner.property_payment_term_id, self.mapped("Term", self.qb_term["Id"]),
                         "payment term auto-imported as dependency")

    def test_import_vendor_strips_suffix_and_matches_email(self):
        partner = self.env["res.partner"].create({"name": "Bessie Old Name", "email": "bessie@example.com"})
        vend = dict(self.qb_vendor, DisplayName="Books by Bessie (V)")
        self.fake.db["Vendor"][vend["Id"]] = vend
        self.import_obj("Vendor", vend)
        self.assertEqual(self.mapped("Vendor", vend["Id"]), partner, "matched by e-mail")
        self.assertEqual(partner.name, "Books by Bessie")
        self.assertEqual(partner.ref, "V-77")
        self.assertGreater(partner.supplier_rank, 0)

    def test_import_item_with_accounts_taxes_and_category(self):
        cat = self.fake.add("Item", {"Name": "ZQT Landscaping", "Type": "Category"})
        item = self.fake.add("Item", {"Name": "ZQT Pump", "Sku": "ZQT-PUMP-1", "Type": "Inventory", "UnitPrice": 15,
                                      "PurchaseCost": 10, "QtyOnHand": 25, "SubItem": True,
                                      "ParentRef": {"value": cat["Id"]},
                                      "IncomeAccountRef": {"value": self.qb_income["Id"]},
                                      "ExpenseAccountRef": {"value": self.qb_cogs["Id"]},
                                      "SalesTaxCodeRef": {"value": self.qb_taxcode["Id"]}})
        self.assertJobDone(self.import_obj("Item", item))
        product = self.mapped("Item", item["Id"])
        self.assertEqual(product.default_code, "ZQT-PUMP-1")
        self.assertEqual(product.lst_price, 15)
        self.assertEqual(product.categ_id.name, "ZQT Landscaping")
        self.assertEqual(product.property_account_income_id, self.mapped("Account", self.qb_income["Id"]))
        self.assertEqual(product.taxes_id, self.mapped("TaxCode", self.qb_taxcode["Id"]))
        if "is_storable" in product._fields:
            self.assertTrue(product.is_storable)
            self.assertEqual(product.qty_available, 25, "opening stock imported once")
        self.assertEqual(self.mapped("ItemCategory", cat["Id"]), product.categ_id)

    def test_import_item_matches_existing_sku(self):
        prod = self.env["product.product"].create({"name": "Old rock", "default_code": "ZQT-ROCK-01"})
        self.import_obj("Item", self.qb_item)
        self.assertEqual(self.mapped("Item", self.qb_item["Id"]), prod)

    # ---------------------------------------------------------- invoices
    def test_import_invoice_full(self):
        data = self.qb_invoice(Line=self.qb_invoice()["Line"] + [
            {"DetailType": "DescriptionOnly", "Description": "Thank you!"},
            {"DetailType": "DiscountLineDetail", "Amount": 10.0,
             "DiscountLineDetail": {"PercentBased": False, "DiscountAccountRef": {"value": self.qb_income["Id"]}}},
            {"DetailType": "GroupLineDetail", "GroupLineDetail": {"Line": [
                {"DetailType": "SalesItemLineDetail", "Amount": 30.0,
                 "SalesItemLineDetail": {"ItemRef": {"value": self.qb_service["Id"]}, "Qty": 1, "UnitPrice": 30}}]}},
        ], DepartmentRef={"value": self.qb_dept["Id"]})
        job = self.import_obj("Invoice", data)
        self.assertJobDone(job)
        move = self.mapped("Invoice", job.qb_id)
        self.assertEqual(move.move_type, "out_invoice")
        self.assertEqual(move.state, "posted")
        self.assertEqual(move.name, "1037", "QuickBooks number kept")
        self.assertEqual(move.partner_id, self.mapped("Customer", self.qb_customer["Id"]))
        self.assertEqual(move.invoice_date, fields.Date.to_date("2026-10-01"))
        product_lines = move.invoice_line_ids.filtered(lambda l: l.display_type == "product")
        self.assertEqual(len(product_lines), 4, "2 items + discount + grouped line")
        rock = product_lines.filtered(lambda l: l.product_id == self.mapped("Item", self.qb_item["Id"]))
        self.assertEqual(rock.quantity, 4)
        self.assertEqual(rock.price_unit, 25)
        self.assertEqual(rock.tax_ids, self.mapped("TaxCode", self.qb_taxcode["Id"]))
        cls_acc = self.mapped("Class", self.qb_class["Id"])
        dept_acc = self.mapped("Department", self.qb_dept["Id"])
        self.assertIn(str(cls_acc.id), rock.analytic_distribution)
        self.assertIn(str(dept_acc.id), rock.analytic_distribution)
        self.assertEqual(move.amount_untaxed, 270.0)
        self.assertTrue(move.invoice_line_ids.filtered(lambda l: l.display_type == "line_note"))

    def test_import_invoice_update_reposts_and_keeps_payment(self):
        job = self.import_obj("Invoice", self.qb_invoice(DocNumber="2001"))
        move = self.mapped("Invoice", job.qb_id)
        self.env["account.payment.register"].with_context(active_model="account.move", active_ids=move.ids).create(
            {"amount": 50.0})._create_payments()
        self.assertEqual(move.payment_state, "partial")
        qb = self.fake.get("Invoice", job.qb_id)
        qb["Line"][0]["Amount"] = 200.0
        qb["Line"][0]["SalesItemLineDetail"]["Qty"] = 8
        qb["SyncToken"] = "1"
        job2 = self.import_obj("Invoice", qb)
        self.assertJobDone(job2)
        self.assertEqual(move.state, "posted")
        self.assertEqual(move.amount_untaxed, 350.0)
        self.assertEqual(move.payment_state, "partial", "existing payment re-reconciled after update")

    def test_same_sync_token_is_skipped(self):
        job = self.import_obj("Invoice", self.qb_invoice(DocNumber="2002"))
        again = self.conn._enqueue("import", "Invoice", qb_id=job.qb_id, payload=self.fake.get("Invoice", job.qb_id))
        again._run_one()
        self.assertEqual(again.state, "skipped")
        self.assertIn("up to date", again.error)

    def test_import_start_date_filter(self):
        job = self.import_obj("Invoice", self.qb_invoice(DocNumber="OLD", TxnDate="2019-05-05"))
        self.assertEqual(job.state, "skipped")

    def test_us_sales_tax_line(self):
        self.conn.qb_country = "US"
        try:
            data = self.qb_invoice(DocNumber="US-1", TxnTaxDetail={"TotalTax": 18.38})
            job = self.import_obj("Invoice", data)
            self.assertJobDone(job)
            move = self.mapped("Invoice", job.qb_id)
            tax_line = move.invoice_line_ids.filtered(lambda l: "Sales Tax" in (l.name or ""))
            self.assertEqual(tax_line.price_unit, 18.38)
            self.assertEqual(tax_line.account_id.account_type, "liability_current")
            self.assertAlmostEqual(move.amount_total, 268.38)
            self.assertFalse(move.invoice_line_ids.tax_ids, "no Odoo taxes on US invoices: QBO AST is the source")
        finally:
            self.conn.qb_country = "CA"

    def test_import_credit_memo_and_bill(self):
        cm = self.import_obj("CreditMemo", self.qb_invoice(DocNumber="CM-1"))
        self.assertEqual(self.mapped("CreditMemo", cm.qb_id).move_type, "out_refund")
        bill_data = {
            "DocNumber": "B-77", "TxnDate": "2026-10-03", "VendorRef": {"value": self.qb_vendor["Id"]},
            "Line": [
                {"DetailType": "AccountBasedExpenseLineDetail", "Amount": 120.0, "Description": "Paper",
                 "AccountBasedExpenseLineDetail": {"AccountRef": {"value": self.qb_expense["Id"]},
                                                   "TaxCodeRef": {"value": self.qb_taxcode["Id"]},
                                                   "ClassRef": {"value": self.qb_class["Id"]}}},
                {"DetailType": "ItemBasedExpenseLineDetail", "Amount": 50.0,
                 "ItemBasedExpenseLineDetail": {"ItemRef": {"value": self.qb_item["Id"]}, "Qty": 5,
                                                "UnitPrice": 10}}]}
        job = self.import_obj("Bill", bill_data)
        self.assertJobDone(job)
        bill = self.mapped("Bill", job.qb_id)
        self.assertEqual(bill.move_type, "in_invoice")
        self.assertEqual(bill.ref, "B-77")
        acc_line = bill.invoice_line_ids.filtered(lambda l: l.name == "Paper")
        self.assertEqual(acc_line.account_id, self.mapped("Account", self.qb_expense["Id"]))
        self.assertEqual(acc_line.tax_ids, self.mapped("TaxCodePurchase", self.qb_taxcode["Id"]))
        self.assertEqual(bill.amount_untaxed, 170.0)
        vc = self.import_obj("VendorCredit", dict(bill_data, DocNumber="VC-1"))
        self.assertEqual(self.mapped("VendorCredit", vc.qb_id).move_type, "in_refund")

    # ---------------------------------------------------------- payments
    def test_import_payment_reconciles_invoice(self):
        inv_job = self.import_obj("Invoice", self.qb_invoice(DocNumber="P-1"))
        move = self.mapped("Invoice", inv_job.qb_id)
        total = move.amount_total
        pay = {"TxnDate": "2026-10-15", "TotalAmt": total, "CustomerRef": {"value": self.qb_customer["Id"]},
               "DepositToAccountRef": {"value": self.qb_bank["Id"]}, "PaymentRefNum": "CHK-55",
               "Line": [{"Amount": total, "LinkedTxn": [{"TxnId": inv_job.qb_id, "TxnType": "Invoice"}]}]}
        job = self.import_obj("Payment", pay)
        self.assertJobDone(job)
        payment = self.mapped("Payment", job.qb_id)
        self.assertEqual(payment.amount, total)
        self.assertEqual(payment.memo, "CHK-55")
        self.assertIn(move.payment_state, ("paid", "in_payment"))
        bank_account = self.mapped("Account", self.qb_bank["Id"])
        self.assertEqual(payment.outstanding_account_id, bank_account, "posted straight to the QB deposit account")

    def test_import_partial_payment_then_change(self):
        inv_job = self.import_obj("Invoice", self.qb_invoice(DocNumber="P-2"))
        move = self.mapped("Invoice", inv_job.qb_id)
        pay = self.fake.add("Payment", {"TxnDate": "2026-10-15", "TotalAmt": 100.0,
                                        "CustomerRef": {"value": self.qb_customer["Id"]},
                                        "Line": [{"Amount": 100.0, "LinkedTxn": [{"TxnId": inv_job.qb_id,
                                                                                  "TxnType": "Invoice"}]}]})
        self.import_obj("Payment", pay)
        self.assertEqual(move.payment_state, "partial")
        self.assertAlmostEqual(move.amount_residual, move.amount_total - 100)
        pay.update(TotalAmt=move.amount_total, SyncToken="1")
        pay["Line"][0]["Amount"] = move.amount_total
        self.import_obj("Payment", pay)
        self.assertIn(move.payment_state, ("paid", "in_payment"))
        old = self.env["account.payment"].search([("partner_id", "=", move.partner_id.id), ("amount", "=", 100.0)])
        self.assertEqual(old.state, "canceled", "modified payment replaced, old one cancelled")

    def test_import_bill_payment(self):
        bill_job = self.import_obj("Bill", {"DocNumber": "B-9", "TxnDate": "2026-10-03",
                                            "VendorRef": {"value": self.qb_vendor["Id"]},
                                            "Line": [{"DetailType": "AccountBasedExpenseLineDetail", "Amount": 80.0,
                                                      "AccountBasedExpenseLineDetail": {
                                                          "AccountRef": {"value": self.qb_expense["Id"]}}}]})
        bill = self.mapped("Bill", bill_job.qb_id)
        bp = {"TxnDate": "2026-10-20", "TotalAmt": 80.0, "VendorRef": {"value": self.qb_vendor["Id"]},
              "PayType": "Check", "CheckPayment": {"BankAccountRef": {"value": self.qb_bank["Id"]}},
              "Line": [{"Amount": 80.0, "LinkedTxn": [{"TxnId": bill_job.qb_id, "TxnType": "Bill"}]}]}
        job = self.import_obj("BillPayment", bp)
        self.assertJobDone(job)
        payment = self.mapped("BillPayment", job.qb_id)
        self.assertEqual(payment.payment_type, "outbound")
        self.assertIn(bill.payment_state, ("paid", "in_payment"))

    def test_import_sales_receipt_is_paid(self):
        data = self.qb_invoice(DocNumber="SR-1", DepositToAccountRef={"value": self.qb_bank["Id"]})
        job = self.import_obj("SalesReceipt", data)
        self.assertJobDone(job)
        move = self.mapped("SalesReceipt", job.qb_id)
        self.assertIn(move.payment_state, ("paid", "in_payment"))

    def test_import_journal_entry(self):
        je = {"DocNumber": "JE-1", "TxnDate": "2026-10-05", "Line": [
            {"DetailType": "JournalEntryLineDetail", "Amount": 500, "Description": "Accrual",
             "JournalEntryLineDetail": {"PostingType": "Debit", "AccountRef": {"value": self.qb_expense["Id"]},
                                        "ClassRef": {"value": self.qb_class["Id"]}}},
            {"DetailType": "JournalEntryLineDetail", "Amount": 500,
             "JournalEntryLineDetail": {"PostingType": "Credit", "AccountRef": {"value": self.qb_bank["Id"]}}}]}
        job = self.import_obj("JournalEntry", je)
        self.assertJobDone(job)
        move = self.mapped("JournalEntry", job.qb_id)
        self.assertEqual(move.state, "posted")
        self.assertEqual(move.ref, "JE-1")
        debit = move.line_ids.filtered(lambda l: l.debit)
        self.assertEqual(debit.account_id, self.mapped("Account", self.qb_expense["Id"]))
        self.assertEqual(debit.debit, 500)

    # ------------------------------------------------------------ orders
    def test_import_estimate_accepted_confirms_quotation(self):
        data = self.qb_invoice(DocNumber="E-1", TxnStatus="Pending", ExpirationDate="2026-12-01")
        job = self.import_obj("Estimate", data)
        self.assertJobDone(job)
        order = self.mapped("Estimate", job.qb_id)
        self.assertEqual(order.state, "draft")
        self.assertEqual(len(order.order_line), 2)
        qb = self.fake.get("Estimate", job.qb_id)
        qb.update(TxnStatus="Accepted", SyncToken="1")
        self.import_obj("Estimate", qb)
        self.assertEqual(order.state, "sale")

    def test_import_purchase_order(self):
        data = {"DocNumber": "PO-5", "TxnDate": "2026-10-02", "VendorRef": {"value": self.qb_vendor["Id"]},
                "POStatus": "Open", "Line": [
                    {"DetailType": "ItemBasedExpenseLineDetail", "Amount": 100.0,
                     "ItemBasedExpenseLineDetail": {"ItemRef": {"value": self.qb_item["Id"]}, "Qty": 10,
                                                    "UnitPrice": 10}}]}
        job = self.import_obj("PurchaseOrder", data)
        self.assertJobDone(job)
        po = self.mapped("PurchaseOrder", job.qb_id)
        self.assertEqual(po.state, "purchase")
        self.assertEqual(po.order_line.product_qty, 10)
        self.assertEqual(po.partner_ref, "PO-5")

    # ------------------------------------------------- deletes / voids / merge
    def test_cdc_deletion_cancels_invoice(self):
        job = self.import_obj("Invoice", self.qb_invoice(DocNumber="D-1"))
        move = self.mapped("Invoice", job.qb_id)
        del self.fake.db["Invoice"][job.qb_id]
        self.fake.cdc_feed = {"Invoice": [{"Id": job.qb_id, "status": "Deleted"}]}
        self.conn._incremental_sync()
        self.run_queue()
        self.assertEqual(move.state, "cancel")
        self.assertEqual(self.Mapping._for_record(self.conn, move).state, "deleted_qb")

    def test_deletion_flag_only_policy(self):
        self.conn.deletion_policy = "flag"
        job = self.import_obj("Invoice", self.qb_invoice(DocNumber="D-2"))
        move = self.mapped("Invoice", job.qb_id)
        del_job = self.conn._enqueue("import", "Invoice", qb_id=job.qb_id, operation="Delete")
        del_job._run_one()
        self.assertEqual(move.state, "posted")
        self.assertEqual(self.Mapping._for_record(self.conn, move).state, "deleted_qb")
        self.conn.deletion_policy = "mirror"

    def test_void_in_quickbooks_cancels(self):
        job = self.import_obj("Invoice", self.qb_invoice(DocNumber="V-1"))
        move = self.mapped("Invoice", job.qb_id)
        qb = self.fake.get("Invoice", job.qb_id)
        qb.update(TotalAmt=0, Balance=0, PrivateNote="Voided", SyncToken="1")
        for line in qb["Line"]:
            line["Amount"] = 0
        void_job = self.conn._enqueue("import", "Invoice", qb_id=job.qb_id, operation="Void", source="webhook")
        void_job._run_one()
        self.assertEqual(move.state, "cancel")
        self.assertEqual(self.Mapping._for_record(self.conn, move).state, "voided_qb")

    def test_deleted_customer_archived(self):
        cust = self.fake.add("Customer", {"DisplayName": "To Delete"})
        self.import_obj("Customer", cust)
        partner = self.mapped("Customer", cust["Id"])
        self.conn._enqueue("import", "Customer", qb_id=cust["Id"], operation="Delete")._run_one()
        self.assertFalse(partner.active)

    def test_merge_repoints_mapping(self):
        cust = self.fake.add("Customer", {"DisplayName": "Merge Old"})
        self.import_obj("Customer", cust)
        partner = self.mapped("Customer", cust["Id"])
        job = self.conn._enqueue("import", "Customer", qb_id=cust["Id"], operation="Merge",
                                 payload={"merged_into": "88888"})
        job._run_one()
        self.assertEqual(self.mapped("Customer", "88888"), partner)

    # -------------------------------------------------------- conflicts
    def _dirty_customer(self):
        cust = self.fake.add("Customer", {"DisplayName": "Conflicted Co", "Notes": "qb"})
        self.import_obj("Customer", cust)
        partner = self.mapped("Customer", cust["Id"])
        partner.phone = "+1 555 0000"  # local change -> dirty
        self.assertEqual(self.Mapping._for_record(self.conn, partner).state, "dirty")
        cust.update(SyncToken="1", PrimaryPhone={"FreeFormNumber": "999"})
        return cust, partner

    def test_conflict_manual_policy(self):
        self.conn.conflict_policy = "manual"
        cust, partner = self._dirty_customer()
        job = self.conn._enqueue("import", "Customer", qb_id=cust["Id"], payload=cust)
        job._run_one()
        self.assertEqual(job.state, "conflict")
        self.assertEqual(partner.phone, "+1 555 0000")
        job.action_force_qb()
        self.assertEqual(partner.phone, "999")
        self.conn.conflict_policy = "newest"

    def test_conflict_odoo_wins_policy(self):
        self.conn.conflict_policy = "odoo"
        cust, partner = self._dirty_customer()
        job = self.conn._enqueue("import", "Customer", qb_id=cust["Id"], payload=cust)
        job._run_one()
        self.assertEqual(job.state, "skipped")
        self.assertEqual(partner.phone, "+1 555 0000")
        self.assertTrue(self.Job.search([("direction", "=", "export"), ("res_id", "=", partner.id),
                                         ("res_model", "=", "res.partner")]))
        self.conn.conflict_policy = "newest"

    def test_conflict_qb_wins_policy(self):
        self.conn.conflict_policy = "qb"
        cust, partner = self._dirty_customer()
        self.conn._enqueue("import", "Customer", qb_id=cust["Id"], payload=cust)._run_one()
        self.assertEqual(partner.phone, "999")
        self.conn.conflict_policy = "newest"

    # ------------------------------------------------------- full import
    def test_full_import_in_dependency_order(self):
        self.fake.add("Invoice", self.qb_invoice(DocNumber="FULL-1"))
        queued = self.conn._full_import(entities=["Account", "Customer", "Item", "Invoice"])
        self.assertGreater(queued, 5)
        jobs = self.Job.search([("state", "=", "pending"), ("source", "=", "manual")], order="priority, id")
        prios = [j.priority for j in jobs]
        self.assertEqual(prios, sorted(prios))
        self.run_queue()
        self.assertFalse(self.Job.search([("state", "=", "failed")]), self.Job.search([("state", "=", "failed")]).mapped("error"))
        self.assertTrue(self.env["account.move"].search([("name", "=", "FULL-1")]))

    def test_import_wizard_select(self):
        wiz = self.env["qb.import.wizard"].create({
            "connection_id": self.conn.id, "mode": "select", "run_now": True,
            "entity_ids": [(6, 0, self.conn.entity_config_ids.filtered(lambda c: c.entity == "Term").ids)]})
        wiz.action_import()
        self.assertTrue(self.mapped("Term", self.qb_term["Id"]))

    def test_pdf_attached(self):
        self.conn.fetch_pdf = True
        job = self.import_obj("Invoice", self.qb_invoice(DocNumber="PDF-1"))
        move = self.mapped("Invoice", job.qb_id)
        att = self.env["ir.attachment"].search([("res_model", "=", "account.move"), ("res_id", "=", move.id)])
        self.assertTrue(att.filtered(lambda a: a.mimetype == "application/pdf"))
        self.conn.fetch_pdf = False
