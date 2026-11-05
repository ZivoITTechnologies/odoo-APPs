import json
from datetime import date, datetime, timedelta

from odoo.exceptions import AccessError, MissingError, ValidationError
from odoo.tests import HttpCase, new_test_user, tagged

from .common import EpcCommon

PREFIX = "/api/zivoit_epc/v1"


@tagged("post_install", "-at_install", "zivoit_epc")
class TestIntegrationApi(EpcCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.project.write({"epc_external_system": "sap", "epc_external_ref": "P-220KV"})
        cls.act_a.external_ref = "A1000"
        cls.site_user = new_test_user(cls.env, "epc_api_user", groups="zivoit_epc_ai.group_epc_user")
        cls.manager = new_test_user(cls.env, "epc_api_manager", groups="zivoit_epc_ai.group_epc_manager")
        cls.outsider = new_test_user(cls.env, "epc_api_outsider", groups="base.group_user")

    def api(self, user):
        return self.env["epc.api"].with_user(user)

    def test_read_endpoints(self):
        data = self.api(self.site_user).list_projects({"external_ref": "P-220KV"})
        self.assertEqual([p["id"] for p in data["projects"]], [self.project.id])
        self.assertEqual(data["projects"][0]["external_system"], "sap")
        self.assertEqual(data["projects"][0]["planned_finish"], str(self.project.epc_planned_finish))
        acts = {a["code"]: a for a in self.api(self.site_user).get_activities(self.project.id)["activities"]}
        self.assertEqual(acts["A"]["external_ref"], "A1000")
        self.assertTrue(acts["B"]["critical"])
        self.assertEqual(self.api(self.site_user).get_risks(self.project.id), {"risks": []})
        with self.assertRaises(AccessError):
            self.api(self.outsider).list_projects()
        with self.assertRaises(MissingError):
            self.api(self.site_user).get_project(999999)

    def test_push_materials_upsert(self):
        payload = {"materials": [
            {"source_system": "sap", "external_ref": "4500001/10", "description": "Conductor", "quantity": 10,
             "uom": "km", "promised_date": "2026-01-08", "stage": "manufacturing", "activity_codes": ["B"]},
            {"source_system": "sap", "external_ref": "4500001/20", "description": "Insulators",
             "activity_codes": ["A1000", "ZZZ"]},
            {"source_system": "mainframe", "external_ref": "X"},
            {"description": "no ref"},
        ]}
        result = self.api(self.site_user).push_materials(self.project.id, payload)
        self.assertEqual((result["created"], result["updated"]), (2, 0))
        self.assertEqual(sorted(e["index"] for e in result["errors"]), [1, 2, 3])
        material = self.env["epc.external.material"].search([("external_ref", "=", "4500001/10")])
        self.assertEqual((material.uom_name, material.activity_ids, material.stage), ("km", self.act_b, "manufacturing"))
        insulators = self.env["epc.external.material"].search([("external_ref", "=", "4500001/20")])
        self.assertEqual(insulators.activity_ids, self.act_a)
        again = self.api(self.site_user).push_materials(self.project.id, {"materials": [
            {"source_system": "sap", "external_ref": "4500001/10", "stage": "shipped", "delivered_qty": 4}]})
        self.assertEqual((again["created"], again["updated"]), (0, 1))
        self.assertEqual((material.stage, material.delivered_qty), ("shipped", 4))
        self.assertTrue(material.last_sync)
        with self.assertRaises(ValidationError):
            self.api(self.site_user).push_materials(self.project.id, {})

    def test_push_progress(self):
        result = self.api(self.site_user).push_progress(self.project.id, {
            "date": "2026-01-03", "lines": [{"activity_code": "A1000", "quantity": 12}],
            "manpower": [{"trade": "Civil gang", "count": 30}], "submit": True,
            "activities": [{"code": "A", "actual_start": "2026-01-01"}, {"code": "C", "progress": 50}],
        })
        dpr = self.env["epc.dpr"].browse(result["dpr"]["id"])
        self.assertEqual((dpr.state, dpr.manpower_total, dpr.line_ids.quantity), ("submitted", 30, 12))
        self.assertEqual(result["activities_updated"], 2)
        self.assertEqual(self.act_a.actual_start, date(2026, 1, 1))
        self.assertEqual(self.act_c.progress, 50)
        with self.assertRaises(ValidationError):  # quantity-measured activity cannot take a % directly
            self.api(self.site_user).push_progress(self.project.id, {"activities": [{"code": "B", "progress": 10}]})
        with self.assertRaises(ValidationError):
            self.api(self.site_user).push_progress(self.project.id, {"lines": [{"activity_code": "NOPE", "quantity": 1}]})

    def test_prediction_and_portfolio_snapshot(self):
        with self.assertRaises(AccessError):
            self.api(self.site_user).run_prediction(self.project.id)
        self.project.epc_status_date = date(2026, 1, 6)
        self.make_dpr(date(2026, 1, 5), [(self.act_a, 20)])
        summary = self.api(self.manager).run_prediction(self.project.id)
        self.assertTrue(summary["last_prediction"])
        self.assertAlmostEqual(self.project.epc_snap_spi, self.project.epc_spi, places=2)
        self.assertAlmostEqual(self.project.epc_snap_progress, self.project.epc_actual_progress, places=1)
        groups = self.env["project.project"]._read_group(
            [("id", "=", self.project.id)], ["epc_segment"], ["epc_contract_value:sum", "epc_snap_spi:avg"])
        self.assertEqual(groups[0][1], 100000000.0)
        # pivot / graph measures need an aggregator (monetary fields need a stored currency)
        info = self.env["project.project"].fields_get(["epc_contract_value", "epc_ld_exposure", "epc_eac_p80"],
                                                      ["aggregator"])
        self.assertEqual({v.get("aggregator") for v in info.values()}, {"sum"})
        no_company = self.env["project.project"].create({"name": "No company", "is_epc": True, "company_id": False})
        self.assertEqual(no_company.epc_currency_id, self.env.company.currency_id)

    def test_import_templates(self):
        for model in ("epc.boq.line", "epc.activity", "epc.mto.line", "epc.external.material"):
            template = self.env[model].get_import_templates()[0]["template"]
            self.assertTrue(template.startswith("/zivoit_epc_ai/static/templates/"))


@tagged("post_install", "-at_install", "zivoit_epc")
class TestIntegrationHttp(HttpCase):

    def setUp(self):
        super().setUp()
        self.user = new_test_user(self.env, "epc_http_user", groups="zivoit_epc_ai.group_epc_user")
        self.project = self.env["project.project"].create({"name": "HTTP EPC", "is_epc": True})
        self.env["epc.activity"].create({"project_id": self.project.id, "code": "X1", "name": "Work", "duration": 3})
        self.key = self.env["res.users.apikeys"].with_user(self.user)._generate(None, "integration", datetime.now() + timedelta(days=1))

    def _get(self, path, key=None):
        headers = {"Authorization": "Bearer %s" % key} if key else {}
        return self.url_open(PREFIX + path, headers=headers)

    def test_auth_and_endpoints(self):
        self.assertEqual(self._get("/projects").status_code, 401)
        self.assertEqual(self._get("/projects", key="wrong").status_code, 401)
        response = self._get("/projects", key=self.key)
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.project.id, [p["id"] for p in response.json()["projects"]])
        self.assertEqual(self._get("/projects/%s/activities" % self.project.id, key=self.key).json()
                         ["activities"][0]["code"], "X1")
        self.assertEqual(self._get("/projects/99999999", key=self.key).status_code, 404)
        post = self.url_open(PREFIX + "/projects/%s/materials" % self.project.id,
                             data=json.dumps({"materials": [{"source_system": "oracle", "external_ref": "PO-1",
                                                             "description": "Valves", "activity_codes": ["X1"]}]}),
                             headers={"Authorization": "Bearer %s" % self.key, "Content-Type": "application/json"})
        self.assertEqual(post.status_code, 200)
        self.assertEqual(post.json()["created"], 1)
        bad = self.url_open(PREFIX + "/projects/%s/progress" % self.project.id, data="not json",
                            headers={"Authorization": "Bearer %s" % self.key})
        self.assertEqual(bad.status_code, 400)
        forbidden = self.url_open(PREFIX + "/projects/%s/predict" % self.project.id, data="{}",
                                  headers={"Authorization": "Bearer %s" % self.key})
        self.assertEqual(forbidden.status_code, 403)
