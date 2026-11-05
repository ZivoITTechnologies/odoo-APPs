from datetime import date

from freezegun import freeze_time

from odoo.tests import tagged

from .common import EpcCommon


@tagged("post_install", "-at_install", "zivoit_epc")
@freeze_time("2026-03-01")
class TestSupplyChain(EpcCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.demo = cls.env["epc.demo"]
        cls.late_vendor = cls.env["res.partner"].create({"name": "Slow Cables", "is_company": True})
        cls.good_vendor = cls.env["res.partner"].create({"name": "Prompt Steel", "is_company": True})
        cls.product = cls.env["product.product"].create({"name": "Conductor drum", "type": "consu"})
        for promised, delivered in ((date(2026, 1, 1), date(2026, 1, 21)), (date(2026, 1, 10), date(2026, 1, 30))):
            order = cls.demo._purchase(cls.late_vendor, None, [(cls.product, 10, 100, promised)])
            cls.demo._receive(order.order_line, 10, delivered)
        for promised in (date(2026, 1, 5), date(2026, 1, 15)):
            order = cls.demo._purchase(cls.good_vendor, None, [(cls.product, 10, 100, promised)])
            cls.demo._receive(order.order_line, 10, promised)

    def test_supplier_scores(self):
        engine = self.env["epc.prediction.engine"]
        engine._update_supplier_scores(self.late_vendor | self.good_vendor)
        self.assertEqual(self.late_vendor.epc_delivery_samples, 2)
        self.assertEqual(self.late_vendor.epc_on_time_rate, 0.0)
        self.assertAlmostEqual(self.late_vendor.epc_avg_delay_days, 20.0)
        self.assertEqual(self.good_vendor.epc_on_time_rate, 100.0)
        self.assertGreater(self.late_vendor.epc_supplier_risk_score, 50)
        self.assertLess(self.good_vendor.epc_supplier_risk_score, 20)

    def test_open_line_forecast_and_material_driver(self):
        order = self.demo._purchase(self.late_vendor, self.project, [(self.product, 5, 100, date(2026, 3, 10))])
        line = order.order_line
        line.epc_required_date = date(2026, 3, 12)
        self.act_b.purchase_line_ids = [(4, line.id)]
        self.project.epc_status_date = date(2026, 3, 1)
        self.env["epc.prediction.engine"].run_for_projects(self.project)
        # risky vendor: full average delay (20 days) added to the promised date
        self.assertEqual(line.epc_forecast_date, date(2026, 3, 30))
        self.assertEqual(line.epc_predicted_delay_days, 20)
        self.assertEqual(line.epc_expedite_status, "late")
        self.assertGreaterEqual(self.act_b.forecast_start, date(2026, 3, 31))
        self.assertIn("Slow Cables", self.act_b.prediction_drivers)
        risk = self.env["epc.risk"].search([("engine_key", "=", "engine:po_line:%s" % line.id)])
        self.assertEqual(risk.category, "procurement")

    def test_received_line(self):
        order = self.demo._purchase(self.good_vendor, self.project, [(self.product, 5, 100, date(2026, 2, 20))])
        self.demo._receive(order.order_line, 5, date(2026, 2, 22))
        self.env["epc.prediction.engine"]._forecast_purchase_lines(order.order_line)
        self.assertEqual(order.order_line.epc_expedite_status, "received")
        self.assertEqual(order.order_line.epc_predicted_delay_days, 2)

    def test_followup_draft(self):
        order = self.demo._purchase(self.late_vendor, self.project, [(self.product, 5, 100, date(2026, 2, 20))])
        messages_before = len(order.message_ids)
        result = order.order_line.action_epc_draft_followup()
        self.assertEqual(result["tag"], "display_notification")
        self.assertGreater(len(order.message_ids), messages_before)
        note = order.message_ids[0]
        self.assertIn("Slow Cables", note.body)
        self.assertEqual(order.order_line.epc_last_followup, date(2026, 3, 1))
        log = self.env["epc.ai.log"].search([("feature", "=", "expediting_followup")], limit=1)
        self.assertTrue(log.used_fallback)

    def test_purchase_user_without_epc_role(self):
        """Plain purchase users can still copy orders linked to EPC activities."""
        from odoo.tests import new_test_user
        buyer = new_test_user(self.env, login="buyer_only", groups="purchase.group_purchase_user")
        order = self.demo._purchase(self.good_vendor, self.project, [(self.product, 5, 100, date(2026, 3, 20))],
                                    confirm=False)
        self.act_b.purchase_line_ids = [(4, order.order_line.id)]
        copy = order.with_user(buyer).copy()
        self.assertTrue(copy.order_line)
        self.assertFalse(copy.order_line.sudo().epc_activity_ids)

    def test_external_erp_material_drives_forecast(self):
        material = self.env["epc.external.material"].create({
            "project_id": self.project.id, "source_system": "sap", "external_ref": "4500012345/10",
            "description": "ACSR Zebra conductor", "vendor_id": self.late_vendor.id, "quantity": 12,
            "promised_date": date(2026, 3, 10), "activity_ids": [(6, 0, self.act_b.ids)]})
        self.assertEqual(material.required_date, self.act_b.planned_start)
        self.project.epc_status_date = date(2026, 3, 1)
        self.env["epc.prediction.engine"].run_for_projects(self.project)
        self.assertEqual(material.forecast_date, date(2026, 3, 30))
        self.assertEqual(material.expedite_status, "late")
        self.assertGreaterEqual(self.act_b.forecast_start, date(2026, 3, 31))
        self.assertIn("4500012345/10", self.act_b.prediction_drivers)
        risk = self.env["epc.risk"].search([("engine_key", "=", "engine:ext_material:%s" % material.id)])
        self.assertEqual((risk.category, risk.state != "closed"), ("procurement", True))
        material.write({"stage": "delivered", "delivered_qty": 12})
        self.env["epc.prediction.engine"].run_for_projects(self.project)
        self.assertEqual(material.expedite_status, "received")
        self.assertNotIn("4500012345/10", self.act_b.prediction_drivers or "")
