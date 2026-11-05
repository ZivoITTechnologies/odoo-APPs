import base64

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.zivoit_epc_ai.models.epc_demo import DEMO_CONTRACT

from .common import EpcCommon


@tagged("post_install", "-at_install", "zivoit_epc")
class TestContractReview(EpcCommon):

    def _clauses(self, review):
        return {c.clause_type: c for c in review.clause_ids}

    def test_demo_contract(self):
        review = self.env["epc.contract.review"].create({
            "name": "GCC", "project_id": self.project.id, "contract_text": DEMO_CONTRACT})
        review.action_analyse()
        clauses = self._clauses(review)
        self.assertEqual(review.state, "analysed")
        self.assertEqual(review.ai_source, "fallback")
        self.assertAlmostEqual(review.ld_rate_pct_week, 0.5)
        self.assertAlmostEqual(review.ld_cap_pct, 10.0)
        self.assertAlmostEqual(review.retention_pct, 5.0)
        self.assertEqual(review.payment_days, 45)
        self.assertEqual(clauses["price_variation"].risk_level, "high")  # firm price
        self.assertEqual(clauses["termination"].risk_level, "high")      # for convenience
        self.assertEqual(clauses["indemnity"].risk_level, "high")        # "any and all"
        self.assertEqual(clauses["force_majeure"].risk_level, "low")
        self.assertEqual(clauses["limitation_of_liability"].value, "not found")
        self.assertEqual(clauses["defect_liability"].value, "24 months")
        self.assertEqual(review.overall_risk, "high")

        review.action_apply_to_project()
        risks = self.env["epc.risk"].search([("project_id", "=", self.project.id), ("source", "=", "contract")])
        self.assertEqual(len(risks), review.high_risk_count)
        review.action_apply_to_project()
        self.assertEqual(self.env["epc.risk"].search_count(
            [("project_id", "=", self.project.id), ("source", "=", "contract")]), len(risks), "no duplicates")

    def test_daily_rate_and_missing_cap(self):
        text = ("Liquidated damages shall be levied at 0.1% of the contract value per day of delay. "
                "Payment shall be made within 90 days of invoice.")
        review = self.env["epc.contract.review"].create({"name": "T", "contract_text": text})
        review.action_analyse()
        clauses = self._clauses(review)
        self.assertAlmostEqual(review.ld_rate_pct_week, 0.7)
        self.assertEqual(clauses["liquidated_damages"].risk_level, "high")
        self.assertIn("no cap", clauses["liquidated_damages"].value)
        self.assertEqual(clauses["payment"].risk_level, "high")
        self.assertEqual(clauses["force_majeure"].risk_level, "high")

    def test_text_file_upload_and_guards(self):
        review = self.env["epc.contract.review"].create({
            "name": "File", "contract_file": base64.b64encode(b"Force majeure applies. Retention of 10% applies."),
            "contract_filename": "gcc.txt"})
        review.action_analyse()
        self.assertIn("Force majeure", review.contract_text)
        self.assertEqual(self._clauses(review)["retention"].value, "10.0%")
        empty = self.env["epc.contract.review"].create({"name": "Empty"})
        with self.assertRaises(UserError):
            empty.action_analyse()
        with self.assertRaises(UserError):
            review.action_apply_to_project()  # no project linked
