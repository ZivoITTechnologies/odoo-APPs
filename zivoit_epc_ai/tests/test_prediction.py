from datetime import date, timedelta

from freezegun import freeze_time

from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import EpcCommon


@tagged("post_install", "-at_install", "zivoit_epc")
@freeze_time("2026-01-08")
class TestPrediction(EpcCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.engine = cls.env["epc.prediction.engine"]
        cls.project.epc_status_date = date(2026, 1, 8)
        # neutral learned factors, so results do not depend on other data in the database
        Model = cls.env["epc.prediction.model"]
        Model.search([]).unlink()
        for discipline in [False] + [d for d, _l in cls.env["epc.activity"]._fields["discipline"].selection]:
            Model.create({"discipline": discipline, "p50_factor": 1.0, "p80_factor": 1.25})

    def test_on_plan_project(self):
        # A planned Jan 1-10. 8 days elapsed, 80% done -> finishes Jan 10 as planned
        self.make_dpr(date(2026, 1, 1), [(self.act_a, 40)])
        self.make_dpr(date(2026, 1, 8), [(self.act_a, 40)])
        self.act_c.write({"actual_start": date(2026, 1, 3), "manual_progress": 100, "actual_finish": date(2026, 1, 6)})
        self.engine.run_for_projects(self.project)
        self.assertEqual(self.act_a.forecast_finish, date(2026, 1, 10))
        self.assertEqual(self.act_a.predicted_slip_days, 0)
        self.assertEqual(self.project.epc_predicted_delay_days, 0)
        self.assertEqual(self.project.epc_risk_level, "low")
        self.assertFalse(self.env["epc.risk"].search([("project_id", "=", self.project.id), ("source", "=", "engine")]))

    def test_slow_progress_propagates(self):
        # 8 days elapsed, only 20% done: rate 2.5%/day -> 32 more days after the status date
        self.make_dpr(date(2026, 1, 1), [(self.act_a, 20)])
        self.engine.run_for_projects(self.project)
        self.assertEqual(self.act_a.forecast_finish, date(2026, 1, 8) + timedelta(days=32))
        slip_a = self.act_a.predicted_slip_days
        self.assertEqual(slip_a, 30)
        self.assertIn("20% done", self.act_a.prediction_drivers)
        # B follows A (FS) and inherits the slip
        self.assertEqual(self.act_b.forecast_start, self.act_a.forecast_finish + timedelta(days=1))
        self.assertGreaterEqual(self.act_b.predicted_slip_days, slip_a)
        self.assertIn("predecessor A", self.act_b.prediction_drivers)
        self.assertEqual(self.act_a.risk_level, "critical")
        self.assertGreater(self.act_a.delay_probability, 90)
        self.assertGreater(self.project.epc_predicted_delay_days, 28)
        # LD: 0.5% per week, ceil(delay / 7) weeks, on 100M, capped at 10%
        weeks = -(-self.project.epc_predicted_delay_days // 7)
        self.assertAlmostEqual(self.project.epc_ld_exposure, 100000000 * min(weeks * 0.5, 10) / 100)
        risks = self.env["epc.risk"].search([("project_id", "=", self.project.id), ("source", "=", "engine")])
        self.assertIn("engine:activity:%s" % self.act_a.id, risks.mapped("engine_key"))
        self.assertIn("engine:project:%s" % self.project.id, risks.mapped("engine_key"))
        self.assertFalse(any(k == "engine:activity:%s" % self.act_d.id for k in risks.mapped("engine_key")),
                         "milestones are covered by the project-level alert")
        self.assertTrue(risks.activity_ids, "owners are notified with a to-do")
        self.assertIn("<li>", str(self.project.epc_prediction_drivers))

    def test_alerts_are_idempotent_and_auto_close(self):
        dpr = self.make_dpr(date(2026, 1, 1), [(self.act_a, 20)])
        self.engine.run_for_projects(self.project)
        self.engine.run_for_projects(self.project)
        key = "engine:activity:%s" % self.act_a.id
        risk = self.env["epc.risk"].search([("engine_key", "=", key)])
        self.assertEqual(len(risk), 1)
        # Recovery: progress catches up
        dpr.action_reset_draft()
        dpr.line_ids.quantity = 80
        dpr.action_approve()
        self.engine.run_for_projects(self.project)
        self.assertEqual(risk.state, "closed")

    def test_not_started_activity_waits_for_status_date(self):
        # Nothing reported: A should have started on Jan 1
        self.engine.run_for_projects(self.project)
        self.assertEqual(self.act_a.forecast_start, date(2026, 1, 8))
        self.assertIn("not started", self.act_a.prediction_drivers)
        self.assertEqual(self.act_a.predicted_slip_days, 7)

    def test_document_readiness_blocks_start(self):
        doc = self.env["epc.document"].create({
            "project_id": self.project.id, "doc_number": "DWG-1", "title": "Erection drawing",
            "status": "ifa", "planned_afc_date": date(2026, 1, 2),
        })
        self.act_b.document_ids = [(4, doc.id)]
        self.make_dpr(date(2026, 1, 1), [(self.act_a, 80)])
        self.engine.run_for_projects(self.project)
        # late document: expected status date + 14 days
        self.assertEqual(self.act_b.forecast_start, date(2026, 1, 23))
        self.assertIn("DWG-1", self.act_b.prediction_drivers)

    def test_learning_from_history(self):
        other = self.env["project.project"].create({"name": "History", "is_epc": True})
        Activity = self.env["epc.activity"]
        for i, actual in enumerate((12, 13, 15, 12, 14, 13)):
            Activity.create({
                "project_id": other.id, "code": "H%s" % i, "name": "Past civil", "discipline": "civil",
                "duration": 10, "actual_start": date(2025, 1, 1),
                "actual_finish": date(2025, 1, 1) + timedelta(days=actual - 1),
            })
        completed = Activity.search([
            ("discipline", "=", "civil"), ("actual_start", "!=", False), ("actual_finish", "!=", False),
            ("duration", ">", 0), ("is_milestone", "=", False)])
        ratios = sorted(((a.actual_finish - a.actual_start).days + 1) / a.duration for a in completed)
        n = len(ratios)
        mid = (n - 1) / 2.0
        median = (ratios[int(mid)] + ratios[int(mid + 0.5)]) / 2.0
        self.engine.learn_from_history()
        civil = self.env["epc.prediction.model"].search([("discipline", "=", "civil")])
        self.assertEqual(civil.sample_count, n)
        self.assertAlmostEqual(civil.p50_factor, (n * median + 5 * 1.0) / (n + 5), places=3)
        self.assertGreaterEqual(civil.p80_factor, civil.p50_factor)
        self.assertGreater(civil.p50_factor, 1.0)
        unused = self.env["epc.prediction.model"].search([("discipline", "=", "piping")])
        self.assertEqual((unused.sample_count, unused.p50_factor), (0, 1.0))

    def test_learned_factor_stretches_unstarted_work(self):
        self.env["epc.prediction.model"].search([]).unlink()
        self.env["epc.prediction.model"].create({"discipline": False, "sample_count": 0,
                                                 "p50_factor": 1.0, "p80_factor": 1.25})
        self.env["epc.prediction.model"].create({"discipline": "structural", "sample_count": 20,
                                                 "p50_factor": 1.4, "p80_factor": 1.8})
        self.make_dpr(date(2026, 1, 1), [(self.act_a, 80)])
        self.engine.run_for_projects(self.project)
        # B: 5 days * 1.4 -> 7 days
        self.assertEqual((self.act_b.forecast_finish - self.act_b.forecast_start).days + 1, 7)
        self.assertIn("40% longer", self.act_b.prediction_drivers)

    def test_eac(self):
        self.make_dpr(date(2026, 1, 1), [(self.act_a, 50)])
        self.act_a.actual_cost = 62500  # EV 50000 -> CPI 0.8
        self.engine.run_for_projects(self.project)
        bac, ev, ac = 220000, 50000, 62500
        self.assertAlmostEqual(self.project.epc_eac_p50, ac + (bac - ev) / 0.8, places=0)
        self.assertGreaterEqual(self.project.epc_eac_p80, self.project.epc_eac_p50)

    @mute_logger("odoo.addons.zivoit_epc_ai.models.epc_prediction")
    def test_cron_runs_and_survives_bad_project(self):
        broken = self.env["project.project"].create({"name": "Loop", "is_epc": True, "date_start": date(2026, 1, 1)})
        Activity = self.env["epc.activity"]
        x = Activity.create({"project_id": broken.id, "code": "X", "name": "X", "duration": 1,
                             "planned_start": date(2026, 1, 1), "planned_finish": date(2026, 1, 1)})
        y = Activity.create({"project_id": broken.id, "code": "Y", "name": "Y", "duration": 1,
                             "planned_start": date(2026, 1, 2), "planned_finish": date(2026, 1, 2)})
        self.env["epc.activity.link"].create({"predecessor_id": x.id, "successor_id": y.id})
        self.env["epc.activity.link"].create({"predecessor_id": y.id, "successor_id": x.id})
        self.make_dpr(date(2026, 1, 1), [(self.act_a, 20)])
        self.engine._cron_run_predictions()
        self.assertTrue(self.project.epc_prediction_date)
        self.assertFalse(broken.epc_prediction_date)
