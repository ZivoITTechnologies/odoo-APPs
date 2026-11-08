# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import QbCommon


@tagged("post_install", "-at_install", "qbo")
class TestExport(QbCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.seed_masters(cls)
        # map Odoo taxes / accounts like a real setup would (Smart Match or import)
        cls.env["qb.importer"]  # noqa
        cls.conn._enqueue("import", "TaxCode", qb_id=cls.qb_taxcode["Id"], payload=cls.qb_taxcode)._run_one()
        cls.qb_sale_tax = cls.Mapping._find_record(cls.conn, "TaxCode", cls.qb_taxcode["Id"])
        cls.qb_purchase_tax = cls.Mapping._find_record(cls.conn, "TaxCodePurchase", cls.qb_taxcode["Id"])
        cls.conn._enqueue("import", "Account", qb_id=cls.qb_bank["Id"], payload=cls.qb_bank)._run_one()
        cls.bank_journal = cls.env["account.journal"].search([
            ("default_account_id", "=", cls.Mapping._find_record(cls.conn, "Account", cls.qb_bank["Id"]).id)])
        cls.customer = cls.env["res.partner"].create({
            "name": "Odoo Customer Inc", "is_company": True, "email": "ap@odoocust.com", "phone": "+1 222",
            "street": "1 Main St", "city": "Austin", "zip": "73301", "country_id": cls.env.ref("base.us").id,
            "customer_rank": 1})
        cls.vendor = cls.env["res.partner"].create({"name": "Odoo Vendor LLC", "is_company": True,
                                                    "supplier_rank": 1, "ref": "ACC-9"})
        cls.product = cls.env["product.product"].create({
            "name": "Odoo Widget", "default_code": "OW-1", "lst_price": 40.0, "standard_price": 22.0,
            "taxes_id": [Command.set(cls.qb_sale_tax.ids)], "supplier_taxes_id": [Command.set(cls.qb_purchase_tax.ids)]})

    def export(self, entity, record):
        job = self.conn.with_context(qb_force=True)._enqueue("export", entity, record=record)
        job._run_one()
        return job

    def make_invoice(self, partner=None, qty=3, post=True, move_type="out_invoice", taxes=None):
        move = self.env["account.move"].create({
            "move_type": move_type, "partner_id": (partner or self.customer).id,
            "invoice_date": "2026-10-10",
            "invoice_line_ids": [Command.create({"product_id": self.product.id, "quantity": qty, "price_unit": 40.0,
                                                 "tax_ids": [Command.set((taxes if taxes is not None else
                                                                          (self.qb_sale_tax if move_type.startswith("out")
                                                                           else self.qb_purchase_tax)).ids)]}),
                                 Command.create({"display_type": "line_note", "name": "Handle with care"})]})
        if post:
            move.action_post()
        return move

    # ------------------------------------------------------------- masters
    def test_export_customer_payload(self):
        job = self.export("Customer", self.customer)
        self.assertJobDone(job)
        qb = self.fake.get("Customer", job.qb_id)
        self.assertEqual(qb["DisplayName"], "Odoo Customer Inc")
        self.assertEqual(qb["CompanyName"], "Odoo Customer Inc")
        self.assertEqual(qb["PrimaryEmailAddr"]["Address"], "ap@odoocust.com")
        self.assertEqual(qb["BillAddr"]["City"], "Austin")
        self.assertEqual(qb["BillAddr"]["Country"], "US")
        self.assertEqual(self.customer.qb_sync_state, "synced")
        self.assertEqual(self.customer.qb_id, job.qb_id)

    def test_export_update_uses_sync_token_and_skips_noop(self):
        job = self.export("Customer", self.customer)
        qb_id = job.qb_id
        self.customer.phone = "+1 333"
        self.assertEqual(self.customer.qb_sync_state, "dirty")
        job2 = self.conn._enqueue("export", "Customer", record=self.customer)
        job2._run_one()
        self.assertJobDone(job2)
        qb = self.fake.get("Customer", qb_id)
        self.assertEqual(qb["PrimaryPhone"]["FreeFormNumber"], "+1 333")
        self.assertEqual(qb["SyncToken"], "1")
        calls = len(self.fake.calls)
        job3 = self.conn._enqueue("export", "Customer", record=self.customer)
        job3._run_one()
        self.assertEqual(job3.state, "skipped", "unchanged payload must not call the API")
        self.assertEqual(len(self.fake.calls), calls)

    def test_export_vendor_suffix_when_also_customer(self):
        both = self.env["res.partner"].create({"name": "Dual Role", "is_company": True, "customer_rank": 1,
                                               "supplier_rank": 1})
        self.export("Customer", both)
        job = self.export("Vendor", both)
        self.assertJobDone(job)
        self.assertEqual(self.fake.get("Vendor", job.qb_id)["DisplayName"], "Dual Role (V)")

    def test_duplicate_name_links_existing(self):
        existing = self.fake.add("Customer", {"DisplayName": "Already There"})
        partner = self.env["res.partner"].create({"name": "Already There", "customer_rank": 1})
        self.conn.match_existing = False
        job = self.export("Customer", partner)
        self.conn.match_existing = True
        self.assertJobDone(job)
        self.assertEqual(job.qb_id, existing["Id"], "6240 duplicate -> linked instead of failing")
        self.assertEqual(len([c for c in self.fake.all("Customer") if c["DisplayName"] == "Already There"]), 1)

    def test_export_item(self):
        job = self.export("Item", self.product)
        self.assertJobDone(job)
        qb = self.fake.get("Item", job.qb_id)
        self.assertEqual(qb["Name"], "Odoo Widget")
        self.assertEqual(qb["Sku"], "OW-1")
        self.assertEqual(qb["UnitPrice"], 40.0)
        self.assertIn(qb["Type"], ("NonInventory", "Inventory"))
        self.assertEqual(qb["SalesTaxCodeRef"]["value"], self.qb_taxcode["Id"])
        self.assertTrue(qb.get("IncomeAccountRef"))

    def test_export_service_item(self):
        svc = self.env["product.product"].create({"name": "Consulting hour", "type": "service", "lst_price": 90})
        job = self.export("Item", svc)
        self.assertEqual(self.fake.get("Item", job.qb_id)["Type"], "Service")

    # ---------------------------------------------------------- invoices
    def test_export_invoice_with_dependencies(self):
        move = self.make_invoice()
        job = self.export("Invoice", move)
        self.assertJobDone(job)
        qb = self.fake.get("Invoice", job.qb_id)
        self.assertEqual(qb["DocNumber"], move.name[-21:])
        self.assertEqual(qb["CustomerRef"]["value"], self.customer.qb_id, "customer auto-exported first")
        self.assertTrue(self.product.qb_id, "item auto-exported first")
        sale_line = [l for l in qb["Line"] if l["DetailType"] == "SalesItemLineDetail"][0]
        self.assertEqual(sale_line["Amount"], 120.0)
        self.assertEqual(sale_line["SalesItemLineDetail"]["Qty"], 3)
        self.assertEqual(sale_line["SalesItemLineDetail"]["TaxCodeRef"]["value"], self.qb_taxcode["Id"])
        self.assertTrue(any(l["DetailType"] == "DescriptionOnly" for l in qb["Line"]))
        self.assertEqual(qb["GlobalTaxCalculation"], "TaxExcluded")
        self.assertEqual(move.qb_payment_link, qb["InvoiceLink"])

    def test_export_invoice_unmapped_tax_fails_clearly(self):
        other_tax = self.env["account.tax"].create({"name": "Unmapped 3%", "amount": 3, "type_tax_use": "sale"})
        move = self.make_invoice(taxes=other_tax)
        job = self.export("Invoice", move)
        self.assertEqual(job.state, "failed")
        self.assertIn("not mapped to a QuickBooks tax code", job.error)
        self.assertEqual(move.qb_sync_state, "not_synced")

    def test_us_invoice_tax_codes_and_total(self):
        self.conn.qb_country = "US"
        try:
            move = self.make_invoice()
            job = self.export("Invoice", move)
            self.assertJobDone(job)
            qb = self.fake.get("Invoice", job.qb_id)
            line = [l for l in qb["Line"] if l["DetailType"] == "SalesItemLineDetail"][0]
            self.assertEqual(line["SalesItemLineDetail"]["TaxCodeRef"]["value"], "TAX")
            self.assertEqual(qb["TxnTaxDetail"]["TotalTax"], round(move.amount_tax, 2))
            self.assertNotIn("GlobalTaxCalculation", qb)
        finally:
            self.conn.qb_country = "CA"

    def test_auto_export_on_post_queues_job(self):
        self.conn.auto_export_on_post = True
        move = self.make_invoice()
        job = self.Job.search([("res_model", "=", "account.move"), ("res_id", "=", move.id)])
        self.assertEqual(job.direction, "export")
        self.assertEqual(job.source, "auto")
        self.conn.auto_export_on_post = False
        self.run_queue()
        self.assertEqual(move.qb_sync_state, "synced")

    def test_reset_and_repost_updates_quickbooks(self):
        move = self.make_invoice()
        job = self.export("Invoice", move)
        move.button_draft()
        move.invoice_line_ids.filtered("product_id").quantity = 5
        move.action_post()
        self.assertEqual(move.qb_sync_state, "dirty")
        job2 = self.conn._enqueue("export", "Invoice", record=move)
        job2._run_one()
        self.assertJobDone(job2)
        qb = self.fake.get("Invoice", job.qb_id)
        self.assertEqual(qb["SyncToken"], "1")
        self.assertEqual([l for l in qb["Line"] if l["DetailType"] == "SalesItemLineDetail"][0]["Amount"], 200.0)

    def test_stale_conflict_policies(self):
        move = self.make_invoice()
        job = self.export("Invoice", move)
        qb = self.fake.get("Invoice", job.qb_id)
        qb["SyncToken"] = "5"  # someone edited it in QuickBooks
        move.button_draft()
        move.invoice_line_ids.filtered("product_id").quantity = 6
        move.action_post()
        # manual -> conflict
        self.conn.conflict_policy = "manual"
        j = self.conn._enqueue("export", "Invoice", record=move)
        j._run_one()
        self.assertEqual(j.state, "conflict")
        # resolve keeping Odoo
        j.action_force_odoo()
        self.assertJobDone(j)
        self.assertEqual(self.fake.get("Invoice", job.qb_id)["SyncToken"], "6")
        # qb wins -> import queued instead of overwrite
        self.conn.conflict_policy = "qb"
        self.fake.get("Invoice", job.qb_id)["SyncToken"] = "9"
        move.button_draft()
        move.invoice_line_ids.filtered("product_id").quantity = 7
        move.action_post()
        j2 = self.conn._enqueue("export", "Invoice", record=move)
        j2._run_one()
        self.assertEqual(j2.state, "skipped")
        self.assertTrue(self.Job.search([("direction", "=", "import"), ("qb_id", "=", job.qb_id),
                                         ("state", "=", "pending")]))
        self.conn.conflict_policy = "newest"

    def test_cancel_invoice_voids_in_quickbooks(self):
        move = self.make_invoice()
        job = self.export("Invoice", move)
        move.button_draft()
        move.button_cancel()
        void_job = self.Job.search([("direction", "=", "void"), ("qb_id", "=", job.qb_id)])
        self.assertTrue(void_job)
        void_job._run_one()
        self.assertJobDone(void_job)
        self.assertEqual(self.fake.get("Invoice", job.qb_id)["TotalAmt"], 0)
        self.assertEqual(self.fake.get("Invoice", job.qb_id)["PrivateNote"], "Voided")

    def test_delete_partner_deactivates_in_quickbooks(self):
        partner = self.env["res.partner"].create({"name": "Short Lived", "customer_rank": 1})
        job = self.export("Customer", partner)
        partner.unlink()
        del_job = self.Job.search([("direction", "=", "delete"), ("qb_id", "=", job.qb_id)])
        del_job._run_one()
        self.assertJobDone(del_job)
        self.assertFalse(self.fake.get("Customer", job.qb_id)["Active"])

    def test_bill_and_vendor_credit(self):
        bill = self.make_invoice(partner=self.vendor, move_type="in_invoice")
        bill.ref = "SUP-INV-1"
        job = self.export("Bill", bill)
        self.assertJobDone(job)
        qb = self.fake.get("Bill", job.qb_id)
        self.assertEqual(qb["VendorRef"]["value"], self.vendor.qb_id)
        self.assertEqual(qb["DocNumber"], "SUP-INV-1")
        line = qb["Line"][0]
        self.assertEqual(line["DetailType"], "ItemBasedExpenseLineDetail")
        self.assertEqual(line["ItemBasedExpenseLineDetail"]["TaxCodeRef"]["value"], self.qb_taxcode["Id"])
        vc = self.make_invoice(partner=self.vendor, move_type="in_refund")
        self.assertJobDone(self.export("VendorCredit", vc))

    def test_bill_account_line(self):
        self.conn._config("Account").direction = "both"
        bill = self.env["account.move"].create({
            "move_type": "in_invoice", "partner_id": self.vendor.id, "invoice_date": "2026-10-10",
            "invoice_line_ids": [Command.create({"name": "Rent", "price_unit": 900,
                                                 "account_id": self.company_data["default_account_expense"].id,
                                                 "tax_ids": [Command.clear()]})]})
        bill.action_post()
        job = self.export("Bill", bill)
        self.assertJobDone(job)
        line = self.fake.get("Bill", job.qb_id)["Line"][0]
        self.assertEqual(line["DetailType"], "AccountBasedExpenseLineDetail")
        acc_id = line["AccountBasedExpenseLineDetail"]["AccountRef"]["value"]
        qb_acc = self.fake.get("Account", acc_id)
        self.assertEqual(qb_acc["AccountType"], "Expense", "unmapped account exported as a dependency")
        self.assertEqual(qb_acc["AcctNum"], self.company_data["default_account_expense"].code)

    def test_unmapped_account_with_import_only_direction_fails_clearly(self):
        self.conn._config("Account").direction = "import"
        bill = self.env["account.move"].create({
            "move_type": "in_invoice", "partner_id": self.vendor.id, "invoice_date": "2026-10-10",
            "invoice_line_ids": [Command.create({"name": "Rent", "price_unit": 900, "tax_ids": [Command.clear()],
                                                 "account_id": self.company_data["default_account_expense"].id})]})
        bill.action_post()
        job = self.export("Bill", bill)
        self.assertEqual(job.state, "failed")
        self.assertIn("is not linked to QuickBooks", job.error)

    # ---------------------------------------------------------- payments
    def test_export_payment_linked_to_invoice(self):
        move = self.make_invoice()
        self.export("Invoice", move)
        payment = self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=move.ids).create({"journal_id": self.bank_journal.id}) \
            ._create_payments()
        job = self.export("Payment", payment)
        self.assertJobDone(job)
        qb = self.fake.get("Payment", job.qb_id)
        self.assertEqual(qb["TotalAmt"], round(move.amount_total, 2))
        self.assertEqual(qb["Line"][0]["LinkedTxn"][0]["TxnId"], move.qb_id)
        self.assertEqual(qb["DepositToAccountRef"]["value"], self.qb_bank["Id"])

    def test_export_bill_payment(self):
        bill = self.make_invoice(partner=self.vendor, move_type="in_invoice")
        payment = self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=bill.ids).create({"journal_id": self.bank_journal.id}) \
            ._create_payments()
        job = self.export("BillPayment", payment)
        self.assertJobDone(job)
        qb = self.fake.get("BillPayment", job.qb_id)
        self.assertEqual(qb["PayType"], "Check")
        self.assertEqual(qb["CheckPayment"]["BankAccountRef"]["value"], self.qb_bank["Id"])
        self.assertEqual(qb["Line"][0]["LinkedTxn"][0]["TxnType"], "Bill")

    def test_cancel_payment_voids(self):
        move = self.make_invoice()
        payment = self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=move.ids).create({"journal_id": self.bank_journal.id}) \
            ._create_payments()
        job = self.export("Payment", payment)
        payment.action_draft()
        payment.action_cancel()
        void = self.Job.search([("direction", "=", "void"), ("qb_id", "=", job.qb_id)])
        void._run_one()
        self.assertJobDone(void)
        self.assertEqual(self.fake.get("Payment", job.qb_id)["TotalAmt"], 0)

    # ------------------------------------------------------- other docs
    def test_export_sale_order_as_estimate(self):
        order = self.env["sale.order"].create({"partner_id": self.customer.id, "order_line": [
            Command.create({"product_id": self.product.id, "product_uom_qty": 2, "price_unit": 40,
                            "tax_ids": [Command.set(self.qb_sale_tax.ids)]})]})
        job = self.export("Estimate", order)
        self.assertJobDone(job)
        self.assertEqual(self.fake.get("Estimate", job.qb_id)["TxnStatus"], "Pending")
        order.action_confirm()
        job2 = self.conn._enqueue("export", "Estimate", record=order)
        job2._run_one()
        self.assertEqual(self.fake.get("Estimate", job.qb_id)["TxnStatus"], "Accepted")

    def test_export_purchase_order(self):
        po = self.env["purchase.order"].create({"partner_id": self.vendor.id, "order_line": [
            Command.create({"product_id": self.product.id, "product_qty": 4, "price_unit": 22,
                            "tax_ids": [Command.set(self.qb_purchase_tax.ids)]})]})
        po.button_confirm()
        job = self.export("PurchaseOrder", po)
        self.assertJobDone(job)
        qb = self.fake.get("PurchaseOrder", job.qb_id)
        self.assertEqual(qb["POStatus"], "Open")
        self.assertEqual(qb["Line"][0]["ItemBasedExpenseLineDetail"]["Qty"], 4)

    def test_export_journal_entry(self):
        self.conn._config("Account").direction = "both"
        je = self.env["account.move"].create({
            "move_type": "entry", "date": "2026-10-12", "journal_id": self.conn.misc_journal_id.id, "ref": "Accrual",
            "line_ids": [
                Command.create({"name": "Exp", "account_id": self.company_data["default_account_expense"].id,
                                "debit": 300}),
                Command.create({"name": "Liab", "account_id": self.company_data["default_account_payable"].id,
                                "credit": 300, "partner_id": self.vendor.id})]})
        je.action_post()
        job = self.export("JournalEntry", je)
        self.assertJobDone(job)
        qb = self.fake.get("JournalEntry", job.qb_id)
        types = sorted(l["JournalEntryLineDetail"]["PostingType"] for l in qb["Line"])
        self.assertEqual(types, ["Credit", "Debit"])
        ap_line = [l for l in qb["Line"] if l["JournalEntryLineDetail"]["PostingType"] == "Credit"][0]
        self.assertEqual(ap_line["JournalEntryLineDetail"]["Entity"]["Type"], "Vendor")

    def test_queue_pending_exports_respects_start_date_and_direction(self):
        self.conn.export_start_date = "2026-10-11"
        old = self.make_invoice()  # dated 2026-10-10
        self.conn._queue_pending_exports(entities=["Invoice"])
        self.assertFalse(self.Job.search([("res_model", "=", "account.move"), ("res_id", "=", old.id)]))
        self.conn._queue_pending_exports(entities=["Invoice"], force=True)
        self.assertTrue(self.Job.search([("res_model", "=", "account.move"), ("res_id", "=", old.id)]))
        self.conn.export_start_date = "2020-01-01"
        self.conn._config("Customer").direction = "import"
        n = self.conn._queue_pending_exports(entities=["Customer"])
        self.assertEqual(n, 0)
        self.conn._config("Customer").direction = "both"

    def test_send_to_quickbooks_button(self):
        partner = self.env["res.partner"].create({"name": "Button Co", "customer_rank": 1})
        partner.action_qb_export()
        self.assertEqual(partner.qb_sync_state, "synced")
        move = self.make_invoice(partner=partner)
        move.action_qb_export()
        self.assertTrue(move.qb_id)
        move.action_qb_send_email()
        self.assertEqual(self.fake.get("Invoice", move.qb_id)["EmailStatus"], "EmailSent")
        move.action_qb_fetch_pdf()
        self.assertTrue(self.env["ir.attachment"].search([("res_model", "=", "account.move"),
                                                          ("res_id", "=", move.id)]))

    def test_send_button_surface_error(self):
        other_tax = self.env["account.tax"].create({"name": "Unmapped 4%", "amount": 4, "type_tax_use": "sale"})
        move = self.make_invoice(taxes=other_tax)
        with self.assertRaises(UserError):
            move.action_qb_export()

    def test_search_on_qb_sync_state(self):
        move = self.make_invoice()
        self.export("Invoice", move)
        not_synced = self.make_invoice()
        found = self.env["account.move"].search([("qb_sync_state", "=", "synced"), ("id", "in", (move | not_synced).ids)])
        self.assertEqual(found, move)
        found = self.env["account.move"].search([("qb_sync_state", "=", "not_synced"),
                                                 ("id", "in", (move | not_synced).ids)])
        self.assertEqual(found, not_synced)
