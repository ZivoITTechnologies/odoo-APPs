# -*- coding: utf-8 -*-
import base64
import io
import zipfile

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EduagentCase

MANIFEST = """<?xml version="1.0"?>
<manifest identifier="m" xmlns="http://www.imsproject.org/xsd/imscp_rootv1p1p2">
 <metadata><schemaversion>1.2</schemaversion></metadata>
 <organizations default="o"><organization identifier="o"><title>Safety Induction</title>
  <item identifier="i1" identifierref="r1"><title>Module 1</title></item>
  <item identifier="i2" identifierref="r2"><title>Module 2</title></item>
 </organization></organizations>
 <resources><resource identifier="r1" type="webcontent" href="m1/index.html"/>
  <resource identifier="r2" type="webcontent" href="m2/index.html"/></resources>
</manifest>"""


@tagged("post_install", "-at_install", "eduagent")
class TestEduagentBusiness(EduagentCase):

    def test_risk_scoring_and_nudge(self):
        self.enrolment._eduagent_compute_risk()
        self.assertEqual(self.enrolment.eduagent_risk_level, "low")
        self.env.cr.execute("UPDATE slide_channel_partner SET create_date = now() - interval '40 days' "
                            "WHERE id = %s", [self.enrolment.id])
        self.enrolment.invalidate_recordset()
        self.enrolment._eduagent_compute_risk()
        self.assertEqual(self.enrolment.eduagent_risk_level, "high")
        self.assertIn("Inactive", self.enrolment.eduagent_risk_reasons)
        self.enrolment._eduagent_send_nudge(force=True)
        nudge = self.env["eduagent.nudge"].search([("enrollment_id", "=", self.enrolment.id)])
        self.assertTrue(nudge.mail_id)
        self.assertEqual(self.enrolment.eduagent_nudge_count, 1)
        # rate limited: a second automatic nudge within 5 days is skipped
        self.enrolment._eduagent_send_nudge()
        self.assertEqual(self.enrolment.eduagent_nudge_count, 1)
        self.enrolment._eduagent_escalate()
        self.assertTrue(self.course.activity_ids)
        self.env["slide.channel.partner"]._cron_eduagent_retention()

    def _order(self, amount=60000.0):
        product = self.env["product.product"].create({"name": "Program", "list_price": amount, "taxes_id": False})
        return self.env["sale.order"].create({
            "partner_id": self.learner.id,
            "order_line": [(0, 0, {"product_id": product.id, "product_uom_qty": 1, "price_unit": amount,
                                   "tax_ids": False})]})

    def test_emi_schedule_and_payment_tracking(self):
        order = self._order()
        with self.assertRaises(UserError):
            order.action_eduagent_generate_schedule()
        order.eduagent_emi_plan_id = self.env.ref("zivoit_eduagent_ai.emi_plan_6")
        order.action_eduagent_generate_schedule()
        inst = order.eduagent_installment_ids.sorted("sequence")
        self.assertEqual(len(inst), 7)
        self.assertAlmostEqual(sum(inst.mapped("amount")), order.amount_total, places=2)
        self.assertAlmostEqual(inst[0].amount, 12000.0, places=2)
        order.action_confirm()
        invoice = order._create_invoices()
        invoice.action_post()
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=invoice.ids).create({"amount": 12000.0})._create_payments()
        order.invalidate_recordset()
        self.assertEqual(inst[0].state, "paid")
        self.assertNotEqual(inst[1].state, "paid")
        self.env["eduagent.installment"]._cron_eduagent_fees()
        self.assertIn(order.eduagent_refund_risk, ("low", "medium", "high"))

    def test_submission_review_needs_human(self):
        rubric = self.env["eduagent.rubric"].create({
            "name": "Capstone", "channel_id": self.course.id,
            "criterion_ids": [(0, 0, {"name": "Correctness"}), (0, 0, {"name": "Clarity"})]})
        sub = self.env["eduagent.submission"].create({
            "name": "EDA", "partner_id": self.learner.id, "channel_id": self.course.id, "rubric_id": rubric.id,
            "content": "df.groupby('region').sum()"})
        sub.action_ai_review()
        self.assertEqual(sub.state, "ai_reviewed")
        self.assertGreater(sub.ai_score, 0)
        self.assertFalse(sub.passed, "AI review alone never passes a learner")
        log = self.env["eduagent.usage.log"].search([("purpose", "=", "Project review")], limit=1)
        self.assertTrue(log.high_risk)
        sub.action_approve()
        self.assertTrue(sub.passed)
        self.assertEqual(sub.reviewer_id, self.env.user)

    def test_credential_gating_and_signature(self):
        self.env.company.eduagent_require_oral_check = True
        self.enrolment.write({"member_status": "completed", "completion": 100})
        self.assertFalse(self.env["eduagent.credential"]._issue_for_completed(partners=self.learner))
        check = self.env["eduagent.oral.check"].create({"partner_id": self.learner.id,
                                                        "channel_id": self.course.id})
        check.action_issue_question()
        self.assertTrue(check.question)
        check.action_submit_answer("x" * 400, pasted=True)
        self.assertEqual(check.state, "pending_review")
        self.assertIn("pasted", check.flag_ids)
        check.action_pass()
        cred = self.env["eduagent.credential"].search([("partner_id", "=", self.learner.id)])
        self.assertEqual(len(cred), 1)
        self.assertTrue(cred.is_authentic())
        self.assertIn("/eduagent/verify/%s" % cred.code, cred.verify_url)
        # idempotent
        self.assertFalse(self.env["eduagent.credential"]._issue_for_completed(partners=self.learner))
        cred.signature = "tampered"
        self.assertFalse(cred.is_authentic())
        cred.signature = cred._sign()
        cred.action_revoke()
        self.assertFalse(cred.is_authentic())

    def test_course_builder_creates_draft(self):
        wiz = self.env["eduagent.course.builder.wizard"].create({
            "topic": "GST basics", "section_count": 2, "lessons_per_section": 2,
            "source_file": base64.b64encode(b"GST is a destination based tax."), "source_filename": "notes.txt"})
        wiz.action_generate()
        course = wiz.result_channel_id
        self.assertFalse(course.is_published, "AI courses are drafts until reviewed")
        lessons = course.slide_ids.filtered(lambda s: not s.is_category)
        self.assertEqual(len(lessons), 4)
        self.assertTrue(lessons.mapped("question_ids"))
        self.assertTrue(all(q.answer_ids.filtered("is_correct") for q in lessons.mapped("question_ids")))
        self.assertGreater(course.eduagent_chunk_count, 0)

    def _scorm_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("imsmanifest.xml", MANIFEST)
            zf.writestr("m1/index.html", "<html>one</html>")
            zf.writestr("m2/index.html", "<html>two</html>")
        return buf.getvalue()

    def test_scorm_import_and_safe_paths(self):
        wiz = self.env["eduagent.scorm.import.wizard"].create({
            "package_file": base64.b64encode(self._scorm_zip()), "filename": "s.zip"})
        wiz.action_import()
        pkg = self.env["eduagent.scorm.package"].search([("name", "=", "Safety Induction")])
        self.assertEqual(pkg.version, "1.2")
        self.assertEqual(pkg.item_ids.mapped("launch_path"), ["m1/index.html", "m2/index.html"])
        self.assertTrue(all(pkg.item_ids.mapped("slide_id")))
        self.assertEqual(pkg._read_file("m1/index.html"), b"<html>one</html>")
        self.assertIsNone(pkg._read_file("../../etc/passwd"))
        self.assertIsNone(pkg._read_file("missing.html"))

    def test_scorm_rejects_bad_zip(self):
        with self.assertRaises(UserError):
            self.env["eduagent.scorm.package"].parse_manifest(b"not a zip")

    def test_insights_kpis(self):
        kpis = self.env["eduagent.insights.wizard"]._collect_kpis(30)
        for key in ("new_enrolments", "active_learners", "risk_distribution", "revenue", "ai_tokens"):
            self.assertIn(key, kpis)
        wiz = self.env["eduagent.insights.wizard"].create({"question": "How are we doing?"})
        wiz.action_ask()
        self.assertTrue(wiz.answer)

    def test_job_skill_extraction_and_index_cron(self):
        job = self.env["eduagent.job.opening"].create({"name": "Data Analyst", "description": "<p>SQL, pandas</p>"})
        job.action_extract_skills()
        self.assertTrue(job.skill_ids)
        self.env["slide.channel"]._cron_eduagent_reindex()
        self.env["eduagent.credential"]._cron_issue_credentials()
