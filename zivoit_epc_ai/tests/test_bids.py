import base64
import json
from datetime import date
from unittest.mock import patch

from freezegun import freeze_time

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from .common import EpcCommon
from .test_ai_service import POST, openai_answer

OFFER_A = """QUOTATION Q-881 from Alpha Towers
Item  Description                 Qty      Rate       Amount
1     Tower steel (galvanised)    50 MT    68,000.00  3,400,000.00
2     Bolts and nuts              4000 Nos 25.00      100,000.00
Grand Total                                           3,500,000.00
Delivery: 45 days from PO
Payment terms: 10% advance, 90% against dispatch
Offer validity: 60 days
Deviation: galvanising thickness 70 micron instead of 86 micron
"""

OFFER_B = """Beta Structurals offer
1. Tower steel galvanised 50 MT @ 66,000.00 = 3,300,000.00
2. Bolts and nuts 4000 Nos @ 30.00 = 120,000.00
TOTAL 3,420,000.00
Delivery 10 weeks ex-works. No deviations.
"""


@tagged("post_install", "-at_install", "zivoit_epc")
@freeze_time("2026-02-01")
class TestBidEvaluation(EpcCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.alpha = cls.env["res.partner"].create({"name": "Alpha Towers", "is_company": True})
        cls.beta = cls.env["res.partner"].create({"name": "Beta Structurals", "is_company": True})
        cls.package = cls.env["epc.bid.package"].create({
            "title": "Tower steel", "project_id": cls.project.id, "required_date": date(2026, 3, 20),
            "line_ids": [(0, 0, {"item_no": "1", "description": "Tower steel (galvanised)", "quantity": 50,
                                 "uom_name": "MT", "activity_id": cls.act_b.id}),
                         (0, 0, {"item_no": "2", "description": "Bolts and nuts", "quantity": 4000})],
        })

    def _bid(self, vendor, offer):
        return self.env["epc.bid"].create({"package_id": self.package.id, "vendor_id": vendor.id,
                                           "offer_file": base64.b64encode(offer.encode()),
                                           "offer_filename": "offer.txt"})

    def test_fallback_extraction_and_scoring(self):
        self.assertTrue(self.package.name.startswith("BID/"))
        alpha, beta = self._bid(self.alpha, OFFER_A), self._bid(self.beta, OFFER_B)
        alpha.action_ai_extract()
        beta.action_ai_extract()
        self.assertEqual(sorted(alpha.line_ids.mapped("unit_price")), [25.0, 68000.0])
        self.assertEqual((alpha.total_price, alpha.delivery_days, alpha.validity_days), (3500000.0, 45, 60))
        self.assertEqual(alpha.technical_compliance, 90.0)
        self.assertIn("galvanising", alpha.deviations)
        self.assertIn("advance", alpha.payment_terms)
        self.assertEqual(sorted(beta.line_ids.mapped("unit_price")), [30.0, 66000.0])
        self.assertEqual((beta.total_price, beta.delivery_days, beta.technical_compliance), (3420000.0, 70, 100.0))
        self.package.action_evaluate()
        self.assertEqual(self.package.state, "evaluated")
        # Beta is cheaper and compliant but 10 weeks (12 April) misses the 20 March need date by 23 days
        self.assertEqual(alpha.delivery_score, 100.0)
        self.assertEqual(beta.delivery_score, 0.0)
        self.assertEqual(alpha.rank, 1)
        self.assertEqual(beta.rank, 2)
        self.assertIn("Alpha Towers", str(self.package.recommendation))
        self.assertEqual(self.package.recommendation_source, "fallback")
        action = alpha.action_award()
        order = self.env["purchase.order"].browse(action["res_id"])
        self.assertEqual(order.partner_id, self.alpha)
        self.assertEqual(order.amount_untaxed, 3500000.0)
        self.assertEqual(self.package.state, "awarded")
        self.assertIn(order.order_line[0], self.act_b.purchase_line_ids)
        self.assertEqual(order.order_line[0].epc_required_date, date(2026, 3, 20))

    def test_technical_gate_and_rules(self):
        alpha = self._bid(self.alpha, OFFER_A)
        alpha.write({"total_price": 100, "technically_acceptable": False})
        beta = self._bid(self.beta, OFFER_B)
        beta.write({"total_price": 200, "technical_compliance": 50})
        self.package.action_evaluate()
        self.assertTrue(alpha.disqualified and beta.disqualified)
        self.assertIn("No bid passed", str(self.package.recommendation))
        with self.assertRaises(UserError):
            alpha.action_award()
        with self.assertRaises(ValidationError):
            self.package.weight_price = 70
        with self.assertRaises(ValidationError):
            beta.technical_compliance = 120

    def test_llm_extraction(self):
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("zivoit_epc_ai.provider", "openai")
        icp.set_param("zivoit_epc_ai.api_key", "sk-test")
        bid = self._bid(self.alpha, OFFER_A).with_context(epc_ai_force_fallback=False)
        answer = json.dumps({"items": [{"item_no": "1", "unit_price": "68,000"},
                                       {"item_no": "", "description": "Bolts and nuts", "unit_price": 25}],
                             "total_price": None, "delivery_days": 45, "technical_compliance": 85,
                             "deviations": "Galvanising 70 micron", "payment_terms": "10/90", "validity_days": 60})
        with patch(POST, return_value=openai_answer(answer)):
            bid.action_ai_extract()
        self.assertEqual(bid.ai_source, "llm")
        self.assertEqual(bid.total_price, 3500000.0)
        self.assertEqual(bid.technical_compliance, 85.0)

    def test_package_from_mto(self):
        Mto = self.env["epc.mto.line"]
        lines = Mto.create([{"project_id": self.project.id, "description": "Earthwire", "quantity": 12,
                             "uom_name": "km", "activity_id": self.act_b.id},
                            {"project_id": self.project.id, "description": "Insulators", "quantity": 300}])
        lines.action_confirm()
        action = lines.action_create_bid_package()
        package = self.env["epc.bid.package"].browse(action["res_id"])
        self.assertEqual(len(package.line_ids), 2)
        self.assertEqual(package.required_date, self.act_b.planned_start)
        bid = self.env["epc.bid"].create({"package_id": package.id, "vendor_id": self.beta.id,
                                          "delivery_days": 20,
                                          "line_ids": [(0, 0, {"package_line_id": l.id, "unit_price": 10})
                                                       for l in package.line_ids]})
        package.action_evaluate()
        self.assertEqual(bid.total_price, 3120)
        bid.action_award()
        self.assertEqual(set(lines.mapped("state")), {"rfq"})
        self.assertEqual(lines[0].vendor_id, self.beta)
