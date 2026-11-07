# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
from odoo import Command
from odoo.tests import tagged

from .common import QbCommon


@tagged("post_install", "-at_install", "qbo")
class TestIntegrityAndSmartMatch(QbCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.seed_masters(cls)

    def test_integrity_detects_and_fixes(self):
        self.import_obj("Invoice", self.qb_invoice(DocNumber="I-OK"))
        bad = self.import_obj("Invoice", self.qb_invoice(DocNumber="I-BAD"))
        missing = self.fake.add("Invoice", self.qb_invoice(DocNumber="I-MISSING"))
        qb_bad = self.fake.get("Invoice", bad.qb_id)
        qb_bad.update(TotalAmt=999.0, SyncToken="1")
        wiz = self.env["qb.integrity.wizard"].create({"connection_id": self.conn.id, "date_from": "2026-01-01",
                                                      "date_to": "2026-12-31"})
        wiz.action_check()
        issues = {(l.qb_ref, l.issue) for l in wiz.line_ids}
        self.assertIn(("I-BAD", "amount"), issues)
        self.assertIn(("I-MISSING", "missing_odoo"), issues)
        self.assertNotIn("I-OK", {l.qb_ref for l in wiz.line_ids})
        self.assertGreaterEqual(wiz.checked_count, 3)
        wiz.line_ids.filtered(lambda l: l.issue == "missing_odoo").action_fix()
        self.assertTrue(self.mapped("Invoice", missing["Id"]))

    def test_integrity_balance_mismatch(self):
        job = self.import_obj("Invoice", self.qb_invoice(DocNumber="I-BAL"))
        move = self.mapped("Invoice", job.qb_id)
        self.fake.get("Invoice", job.qb_id)["Balance"] = 0  # paid in QB, not in Odoo
        self.fake.get("Invoice", job.qb_id)["TotalAmt"] = move.amount_total
        wiz = self.env["qb.integrity.wizard"].create({"connection_id": self.conn.id, "date_from": "2026-01-01",
                                                      "date_to": "2026-12-31"})
        wiz.action_check()
        self.assertIn(("I-BAL", "balance"), {(l.qb_ref, l.issue) for l in wiz.line_ids})

    def test_smart_match(self):
        acc = self.env["account.account"].create({"name": "Supplies", "code": "6100", "account_type": "expense"})
        journal = self.env["account.journal"].create({"name": "Visa Card", "type": "bank", "code": "VISA"})
        tax = self.env["account.tax"].create({"name": "GST/PST BC", "amount": 12.0, "type_tax_use": "sale"})
        self.fake.add("PaymentMethod", {"Name": "Visa"})
        self.fake.add("PaymentMethod", {"Name": "Zzz Unknown"})
        self.conn.action_smart_match()
        self.assertEqual(self.mapped("Account", self.qb_expense["Id"]), acc, "matched by account number")
        self.assertEqual(self.mapped("TaxCode", self.qb_taxcode["Id"]), tax, "matched by rate + name")
        visa = self.Mapping.search([("qb_entity", "=", "PaymentMethod"), ("qb_name", "=", "Visa")])
        self.assertEqual(visa.record_ref, journal)
        unknown = self.Mapping.search([("qb_entity", "=", "PaymentMethod"), ("qb_name", "=", "Zzz Unknown")])
        self.assertEqual(unknown.state, "unmapped")
        # the user maps it manually through the reference field
        unknown.record_ref = journal
        self.assertEqual(unknown.state, "synced")

    def test_payment_method_mapping_drives_journal(self):
        cash = self.env["account.journal"].create({"name": "Petty Cash", "type": "cash", "code": "PCSH"})
        pm = self.fake.add("PaymentMethod", {"Name": "Cash"})
        self.Mapping._link(self.conn, "PaymentMethod", pm, cash)
        inv = self.import_obj("Invoice", self.qb_invoice(DocNumber="PM-1"))
        move = self.mapped("Invoice", inv.qb_id)
        job = self.import_obj("Payment", {"TxnDate": "2026-10-15", "TotalAmt": move.amount_total,
                                          "CustomerRef": {"value": self.qb_customer["Id"]},
                                          "PaymentMethodRef": {"value": pm["Id"]},
                                          "Line": [{"Amount": move.amount_total,
                                                    "LinkedTxn": [{"TxnId": inv.qb_id, "TxnType": "Invoice"}]}]})
        self.assertJobDone(job)
        self.assertEqual(self.mapped("Payment", job.qb_id).journal_id, cash)

    def test_export_wizard(self):
        partner = self.env["res.partner"].create({"name": "Wizard Export Co", "customer_rank": 1})
        wiz = self.env["qb.export.wizard"].create({
            "connection_id": self.conn.id, "run_now": True,
            "entity_ids": [Command.set(self.conn.entity_config_ids.filtered(lambda c: c.entity == "Customer").ids)]})
        wiz.action_export()
        self.assertEqual(partner.qb_sync_state, "synced")
