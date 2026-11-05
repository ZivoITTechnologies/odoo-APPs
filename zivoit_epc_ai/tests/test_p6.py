import base64
from datetime import date

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.zivoit_epc_ai.wizard.epc_p6_import import parse_xer

from .common import EpcCommon

XER = "\r\n".join([
    "ERMHDR\t19.12\t2026-01-01\tProject\tadmin\tadmin\tdbx\tProject Management\tINR",
    "%T\tPROJWBS",
    "%F\twbs_id\tproj_id\twbs_short_name\twbs_name",
    "%R\t10\t1\tCIV\tCivil works",
    "%T\tTASK",
    "%F\ttask_id\tproj_id\twbs_id\ttask_code\ttask_name\ttask_type\ttarget_drtn_hr_cnt\ttarget_start_date"
    "\ttarget_end_date\tact_start_date\tact_end_date\tphys_complete_pct",
    "%R\t1001\t1\t10\tP-100\tPiling\tTT_Task\t80\t2026-02-01 08:00\t2026-02-10 17:00\t2026-02-02 08:00\t\t40",
    "%R\t1002\t1\t10\tP-200\tPile caps\tTT_Task\t44\t2026-02-11 08:00\t2026-02-16 17:00\t\t\t",
    "%R\t1003\t1\t10\tP-900\tCivil complete\tTT_FinMile\t0\t2026-02-16 17:00\t2026-02-16 17:00\t\t\t",
    "%R\t1004\t1\t10\tA\tFoundations (updated)\tTT_Task\t96\t2026-01-01 08:00\t2026-01-12 17:00\t\t\t",
    "%T\tTASKPRED",
    "%F\ttask_pred_id\ttask_id\tpred_task_id\tproj_id\tpred_proj_id\tpred_type\tlag_hr_cnt",
    "%R\t1\t1002\t1001\t1\t1\tPR_FS\t16",
    "%R\t2\t1003\t1002\t1\t1\tPR_FF\t0",
    "%E",
]) + "\r\n"


@tagged("post_install", "-at_install", "zivoit_epc")
class TestP6(EpcCommon):

    def test_parse(self):
        tables = parse_xer(XER)
        self.assertEqual(len(tables["TASK"]), 4)
        self.assertEqual(tables["TASK"][0]["task_code"], "P-100")
        self.assertEqual(tables["TASKPRED"][1]["pred_type"], "PR_FF")

    def test_import(self):
        wizard = self.env["epc.p6.import"].create({
            "project_id": self.project.id, "xer_file": base64.b64encode(XER.encode()), "xer_filename": "x.xer"})
        wizard.action_import()
        self.assertIn("3 activities created, 1 updated", wizard.result)
        acts = {a.code: a for a in self.project.epc_activity_ids}
        piling = acts["P-100"]
        self.assertEqual((piling.duration, piling.wbs), (10, "CIV"))
        self.assertEqual(piling.planned_start, date(2026, 2, 1))
        self.assertEqual(piling.baseline_finish, date(2026, 2, 10))
        self.assertEqual(piling.actual_start, date(2026, 2, 2))
        self.assertEqual((piling.progress_method, piling.progress), ("manual", 40.0))
        self.assertEqual(acts["P-200"].duration, 6)  # 44 h / 8 rounded up
        self.assertTrue(acts["P-900"].is_milestone)
        self.assertEqual(acts["A"].name, "Foundations (updated)")
        self.assertEqual(acts["A"].duration, 12)
        link = self.env["epc.activity.link"].search([("successor_id", "=", acts["P-200"].id)])
        self.assertEqual((link.predecessor_id, link.link_type, link.lag_days), (piling, "FS", 2))
        self.assertEqual(self.env["epc.activity.link"].search(
            [("successor_id", "=", acts["P-900"].id)]).link_type, "FF")
        # re-import is idempotent for links
        wizard.action_import()
        self.assertEqual(self.env["epc.activity.link"].search_count(
            [("successor_id", "in", [acts["P-200"].id, acts["P-900"].id])]), 2)

    def test_export_round_trip(self):
        wizard = self.env["epc.p6.import"].create({"project_id": self.project.id})
        wizard.action_export()
        self.assertTrue(wizard.export_filename.endswith(".xer"))
        content = base64.b64decode(wizard.export_file).decode()
        tables = parse_xer(content)
        codes = sorted(r["task_code"] for r in tables["TASK"])
        self.assertEqual(codes, ["A", "B", "C", "D"])
        self.assertEqual(len(tables["TASKPRED"]), 4)
        ss = [r for r in tables["TASKPRED"] if r["pred_type"] == "PR_SS"]
        self.assertEqual(ss[0]["lag_hr_cnt"], "16")
        # import into a fresh project reproduces the network
        target = self.env["project.project"].create({"name": "Copy", "is_epc": True, "date_start": date(2026, 1, 1)})
        importer = self.env["epc.p6.import"].create({
            "project_id": target.id, "xer_file": wizard.export_file, "xer_filename": wizard.export_filename})
        importer.action_import()
        self.assertEqual(len(target.epc_activity_ids), 4)
        target.action_epc_compute_schedule()
        copied_c = target.epc_activity_ids.filtered(lambda a: a.code == "C")
        self.assertEqual(copied_c.total_float, self.act_c.total_float)

    def test_project_export_action(self):
        action = self.project.action_epc_export_p6()
        self.assertEqual(action["type"], "ir.actions.act_url")
        self.assertIn("/web/content/epc.p6.import/", action["url"])

    def test_bad_file(self):
        wizard = self.env["epc.p6.import"].create({
            "project_id": self.project.id, "xer_file": base64.b64encode(b"hello world")})
        with self.assertRaises(UserError):
            wizard.action_import()
        with self.assertRaises(UserError):
            self.env["epc.p6.import"].create({"project_id": self.project.id}).action_import()
