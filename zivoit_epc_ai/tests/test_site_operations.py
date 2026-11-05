import base64
import json
from datetime import date, datetime
from unittest.mock import patch

from freezegun import freeze_time

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from .common import EpcCommon
from .test_ai_service import POST, openai_answer

# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")


@tagged("post_install", "-at_install", "zivoit_epc")
class TestHse(EpcCommon):

    def _permit(self, **vals):
        values = {
            "project_id": self.project.id, "permit_type": "height", "location": "Tower 45",
            "work_description": "Top hamper erection", "receiver": "Ravi (crew leader)",
            "valid_from": datetime(2026, 1, 5, 8, 0), "valid_to": datetime(2026, 1, 5, 18, 0),
        }
        values.update(vals)
        return self.env["epc.hse.permit"].create(values)

    def test_permit_lifecycle(self):
        permit = self._permit()
        self.assertTrue(permit.name.startswith("PTW/"))
        self.assertIn("harness", permit.precautions)
        permit.action_ai_precautions()
        self.assertIn("[ ]", permit.precautions)
        permit.action_approve()
        self.assertEqual(permit.state, "approved")
        with freeze_time("2026-01-06"):
            self.assertTrue(permit.is_expired)
            found = self.env["epc.hse.permit"].search([("project_id", "=", self.project.id), ("is_expired", "=", True)])
            self.assertEqual(found, permit)
        permit.action_close()
        self.assertEqual(permit.state, "closed")

    def test_permit_validity_rules(self):
        with self.assertRaises(ValidationError):
            self._permit(valid_to=datetime(2026, 1, 5, 7, 0))
        with self.assertRaises(ValidationError):
            self._permit(valid_to=datetime(2026, 1, 6, 12, 0))
        permit = self._permit(permit_type="general")
        permit.precautions = " "
        with self.assertRaises(UserError):
            permit.action_approve()

    def test_incident_root_cause_and_kpis(self):
        dpr = self.make_dpr(date(2026, 1, 3), [(self.act_a, 5)], approve=False,
                            manpower_ids=[(0, 0, {"trade": "Erection gang", "count": 50})])
        dpr.action_approve()
        self.assertEqual(self.project.epc_man_hours, 400)
        incident = self.env["epc.hse.incident"].create({
            "project_id": self.project.id, "incident_type": "lti", "location": "Tower 45",
            "date": datetime(2026, 1, 3, 11, 0), "lost_days": 4,
            "description": "Worker slipped from the cross-arm; he was not wearing his harness hooked to the lifeline.",
        })
        self.assertEqual(incident.severity, "high")
        self.assertEqual(incident.risk_id.category, "hse")
        with self.assertRaises(UserError):
            incident.action_close()
        incident.action_ai_root_cause()
        self.assertEqual(incident.root_cause_category, "people")
        self.assertEqual(incident.state, "investigating")
        self.assertTrue(incident.corrective_actions)
        incident.action_close()
        self.assertEqual(incident.state, "closed")
        self.project.invalidate_recordset()
        self.assertEqual(self.project.epc_lti_count, 1)
        self.assertAlmostEqual(self.project.epc_ltifr, 1000000.0 / 400)
        self.assertEqual(self.project.epc_safe_man_hours, 0)
        later = self.make_dpr(date(2026, 1, 6), [(self.act_a, 5)], approve=False,
                              manpower_ids=[(0, 0, {"trade": "Erection gang", "count": 10})])
        later.action_approve()
        self.project.invalidate_recordset()
        self.assertEqual(self.project.epc_safe_man_hours, 80)


@tagged("post_install", "-at_install", "zivoit_epc")
class TestPhotoReview(EpcCommon):

    def _dpr_with_photo(self):
        dpr = self.env["epc.dpr"].create({"project_id": self.project.id, "date": date(2026, 1, 8)})
        attachment = self.env["ir.attachment"].create({
            "name": "site.png", "datas": base64.b64encode(PNG), "mimetype": "image/png",
            "res_model": "epc.dpr", "res_id": dpr.id})
        dpr.photo_ids = [(4, attachment.id)]
        return dpr

    def test_requires_photos(self):
        dpr = self.env["epc.dpr"].create({"project_id": self.project.id, "date": date(2026, 1, 9)})
        with self.assertRaises(UserError):
            dpr.action_ai_photo_review()

    def test_without_vision_provider(self):
        dpr = self._dpr_with_photo()
        dpr.action_ai_photo_review()
        self.assertEqual(dpr.photo_review_source, "fallback")
        self.assertIn("manual review", str(dpr.photo_review))
        self.assertFalse(dpr.observation_ids)

    def test_with_vision_provider(self):
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("zivoit_epc_ai.provider", "openai")
        icp.set_param("zivoit_epc_ai.api_key", "sk-test")
        dpr = self._dpr_with_photo().with_context(epc_ai_force_fallback=False)
        answer = json.dumps({
            "summary": "Two towers under erection.",
            "activity_updates": [{"activity_code": "B", "observation": "Top section in progress",
                                  "estimated_progress_pct": 60}, {"activity_code": "ZZ", "observation": "x"}],
            "safety_findings": [
                {"observation_type": "ppe", "severity": "high", "description": "Worker at height without harness"},
                {"observation_type": "alien", "severity": "extreme", "description": "Loose material at base"},
            ],
        })
        with patch(POST, return_value=openai_answer(answer)) as post:
            dpr.action_ai_photo_review()
            dpr.action_ai_photo_review()  # second run must not duplicate observations
        content = post.call_args[1]["json"]["messages"][1]["content"]
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(dpr.photo_review_source, "llm")
        self.assertIn("Top section in progress", str(dpr.photo_review))
        self.assertEqual(len(dpr.observation_ids), 2)
        harness = dpr.observation_ids.filtered(lambda o: "harness" in o.description)
        self.assertEqual((harness.observation_type, harness.severity, harness.source), ("ppe", "high", "ai_photo"))
        other = dpr.observation_ids - harness
        self.assertEqual((other.observation_type, other.severity), ("unsafe_condition", "medium"))


@tagged("post_install", "-at_install", "zivoit_epc")
class TestQualityAndPlant(EpcCommon):

    def test_inspection_fail_raises_ncr_and_ncr_workflow(self):
        inspection = self.env["epc.qa.inspection"].create({
            "project_id": self.project.id, "activity_id": self.act_a.id,
            "description": "Foundation loc 45 before concreting", "remarks": "Honeycomb on pedestal face"})
        self.assertTrue(inspection.name.startswith("IR/"))
        inspection.action_fail()
        ncr = inspection.ncr_ids
        self.assertEqual(len(ncr), 1)
        self.assertTrue(ncr.name.startswith("NCR/"))
        with self.assertRaises(UserError):
            ncr.action_close()
        ncr.action_ai_suggest()
        self.assertEqual(ncr.disposition, "repair")
        ncr.action_start()
        ncr.action_verify()
        ncr.action_close()
        self.assertEqual(ncr.state, "closed")
        other = self.env["epc.qa.inspection"].create({"project_id": self.project.id, "description": "Bolt torque"})
        other.action_pass()
        self.project.invalidate_recordset()
        self.assertEqual(self.project.epc_inspection_pass_rate, 50.0)

    def test_open_major_ncr_feeds_prediction(self):
        self.env["epc.qa.ncr"].create({"project_id": self.project.id, "activity_id": self.act_b.id,
                                       "description": "Wrong grade of bolts supplied", "severity": "major"})
        self.project.epc_status_date = date(2026, 1, 2)
        self.env["epc.prediction.engine"].run_for_projects(self.project)
        self.assertIn("open major NCR", self.act_b.prediction_drivers)

    def test_punch_list(self):
        Punch = self.env["epc.punch.item"]
        a = Punch.create({"project_id": self.project.id, "name": "Earthing strip missing at T45", "category": "A"})
        Punch.create({"project_id": self.project.id, "name": "Paint touch-up", "category": "C"})
        self.project.invalidate_recordset()
        self.assertEqual((self.project.epc_open_punch_count, self.project.epc_open_punch_a_count), (2, 1))
        a.action_close()
        self.assertTrue(a.closed_date)
        self.project.invalidate_recordset()
        self.assertEqual(self.project.epc_open_punch_a_count, 0)

    def test_plant_logs_and_breakdown_hindrance(self):
        crane = self.env["epc.plant"].create({"name": "50 T crawler crane", "plant_type": "crane"})
        dpr = self.make_dpr(date(2026, 1, 4), [(self.act_b, 2)], approve=False, plant_ids=[
            (0, 0, {"plant_id": crane.id, "activity_id": self.act_b.id, "hours_worked": 6, "hours_idle": 1,
                    "hours_breakdown": 3})])
        dpr.action_approve()
        self.assertEqual(crane.utilisation, 60.0)
        self.assertEqual(crane.availability, 70.0)
        hindrance = dpr.hindrance_ids.filtered(lambda h: h.hindrance_type == "equipment")
        self.assertEqual((hindrance.hours_lost, hindrance.activity_id), (3.0, self.act_b))
        with self.assertRaises(ValidationError):
            self.make_dpr(date(2026, 1, 5), [(self.act_b, 1)], approve=False, plant_ids=[
                (0, 0, {"plant_id": crane.id, "hours_worked": 20, "hours_idle": 5})])
