from datetime import date

from odoo.exceptions import AccessError
from odoo.tests import new_test_user, tagged

from .common import EpcCommon


@tagged("post_install", "-at_install", "zivoit_epc")
class TestSecurity(EpcCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.site_user = new_test_user(cls.env, login="epc_site", groups="zivoit_epc_ai.group_epc_user")
        cls.manager = new_test_user(cls.env, login="epc_mgr", groups="zivoit_epc_ai.group_epc_manager")

    def test_site_user_can_file_dpr_but_not_change_boq_or_bills(self):
        env = self.env(user=self.site_user)
        dpr = env["epc.dpr"].create({
            "project_id": self.project.id, "date": date(2026, 1, 2),
            "line_ids": [(0, 0, {"activity_id": self.act_a.id, "quantity": 5})]})
        dpr.action_submit()
        self.assertEqual(dpr.state, "submitted")
        with self.assertRaises(AccessError):
            env["epc.boq.line"].browse(self.boq_fdn.id).write({"rate": 1})
        with self.assertRaises(AccessError):
            env["epc.ra.bill"].create({"project_id": self.project.id})
        with self.assertRaises(AccessError):
            env["epc.ai.log"].search([])
        risk = env["epc.risk"].create({"name": "Site risk", "project_id": self.project.id})
        self.assertEqual(risk.level, "medium")

    def test_manager_runs_engine_and_approves(self):
        env = self.env(user=self.manager)
        dpr = env["epc.dpr"].create({
            "project_id": self.project.id, "date": date(2026, 1, 2),
            "line_ids": [(0, 0, {"activity_id": self.act_a.id, "quantity": 5})]})
        dpr.action_approve()
        project = env["project.project"].browse(self.project.id)
        project.epc_status_date = date(2026, 1, 5)
        project.action_epc_run_prediction()
        self.assertTrue(project.epc_prediction_date)
        bill = env["epc.ra.bill"].create({"project_id": self.project.id})
        bill.action_generate_lines()
        self.assertEqual(bill.line_ids.claimed_qty, 5)

    def test_multi_company_isolation(self):
        other_company = self.env["res.company"].create({"name": "Other Co"})
        other_project = self.env["project.project"].create({
            "name": "Other company project", "is_epc": True, "company_id": other_company.id})
        self.env["epc.activity"].create({"project_id": other_project.id, "code": "OC", "name": "Other"})
        env = self.env(user=self.manager)
        visible = env["epc.activity"].search([("code", "=", "OC")])
        self.assertFalse(visible)
