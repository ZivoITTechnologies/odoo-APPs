from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import EpcCommon


@tagged("post_install", "-at_install", "zivoit_epc")
class TestDocuments(EpcCommon):

    def _doc(self, number, **vals):
        values = {"project_id": self.project.id, "doc_number": number, "title": "Doc %s" % number}
        values.update(vals)
        return self.env["epc.document"].create(values)

    def test_workflow_and_history(self):
        doc = self._doc("DWG-001")
        doc.action_issue_for_review()
        doc.action_issue_for_approval()
        doc.action_approve_afc()
        self.assertEqual(doc.status, "afc")
        self.assertEqual(doc.actual_afc_date, fields.Date.context_today(doc))
        doc.action_new_revision()
        self.assertEqual((doc.revision, doc.status), ("B", "draft"))
        self.assertFalse(doc.actual_afc_date)
        self.assertEqual(len(doc.revision_ids), 5)
        self.assertEqual(doc.display_name, "DWG-001 rev B — Doc DWG-001")

    def test_next_revision(self):
        nxt = self.env["epc.document"]._next_revision
        self.assertEqual(nxt("A"), "B")
        self.assertEqual(nxt("0"), "1")
        self.assertEqual(nxt("P2"), "P3")
        self.assertEqual(nxt(""), "A")

    def test_late_flag_and_search(self):
        today = fields.Date.context_today(self.project)
        late = self._doc("L-1", status="ifa", planned_afc_date=today - timedelta(days=3))
        on_time = self._doc("L-2", status="ifa", planned_afc_date=today + timedelta(days=3))
        approved = self._doc("L-3", status="afc", planned_afc_date=today - timedelta(days=3))
        self.assertTrue(late.is_late)
        self.assertEqual(late.days_late, 3)
        self.assertFalse(on_time.is_late or approved.is_late)
        Doc = self.env["epc.document"]
        self.assertEqual(Doc.search([("project_id", "=", self.project.id), ("is_late", "=", True)]), late)
        self.assertEqual(Doc.search([("project_id", "=", self.project.id), ("is_late", "=", False)]), on_time | approved)

    def test_transmittal(self):
        recipient = self.env["res.partner"].create({"name": "PMC Reviewer", "email": "pmc@example.com"})
        doc_a = self._doc("T-1")
        doc_b = self._doc("T-2", status="ifr")
        transmittal = self.env["epc.transmittal"].create({
            "project_id": self.project.id, "partner_id": recipient.id, "purpose": "approval",
            "document_ids": [(6, 0, (doc_a | doc_b).ids)]})
        self.assertTrue(transmittal.name.startswith("TRN/"))
        transmittal.action_ai_cover_note()
        self.assertIn("T-1", str(transmittal.cover_note))
        transmittal.action_send()
        self.assertEqual(transmittal.state, "sent")
        self.assertEqual((doc_a | doc_b).mapped("status"), ["ifa", "ifa"])
        self.assertIn(recipient, transmittal.message_ids[0].partner_ids)
        empty = self.env["epc.transmittal"].create({"project_id": self.project.id, "partner_id": recipient.id})
        with self.assertRaises(UserError):
            empty.action_send()

    def test_unique_number(self):
        self._doc("U-1")
        with self.assertRaises(Exception), mute_logger("odoo.sql_db"), self.cr.savepoint():
            self._doc("U-1")
