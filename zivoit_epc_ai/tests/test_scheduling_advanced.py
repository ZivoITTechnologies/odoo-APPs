from datetime import date, timedelta

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.zivoit_epc_ai.models.epc_calendar import WorkCalendar

from .common import EpcCommon


@tagged("post_install", "-at_install", "zivoit_epc")
class TestWorkCalendar(EpcCommon):

    def test_calendar_arithmetic(self):
        # 2026-01-01 is a Thursday; Monday-Saturday week; 26 Jan is a holiday
        cal = WorkCalendar(date(2026, 1, 1), "6", [date(2026, 1, 26)])
        self.assertEqual(cal.shift(date(2026, 1, 3), 1), date(2026, 1, 5))   # Sat -> Mon
        self.assertEqual(cal.shift(date(2026, 1, 4), 0), date(2026, 1, 5))   # Sunday normalised
        self.assertEqual(cal.shift(date(2026, 1, 24), 1), date(2026, 1, 27))  # skips Sun + holiday
        self.assertEqual(cal.offset_of(date(2026, 1, 5)), 3)
        self.assertEqual(cal.date_at(3), date(2026, 1, 5))
        self.assertEqual(cal.workdays_between(date(2026, 1, 1), date(2026, 1, 10)), 9)
        self.assertEqual(cal.finish_from_start(date(2026, 1, 1), 10), date(2026, 1, 12))
        self.assertEqual(cal.shift(date(2026, 1, 5), -1), date(2026, 1, 3))
        five = WorkCalendar(date(2026, 1, 3), "5")  # Saturday start moves to Monday
        self.assertEqual(five.start, date(2026, 1, 5))

    def test_cpm_with_six_day_week_and_holiday(self):
        self.project.epc_work_week = "6"
        self.env["epc.holiday"].create({"name": "Pongal", "date": date(2026, 1, 14), "project_id": self.project.id})
        self.project.action_epc_compute_schedule()
        # A: 10 working days from Thu 1 Jan, Sundays off -> finishes Mon 12 Jan
        self.assertEqual(self.act_a.planned_finish, date(2026, 1, 12))
        # B: FS -> 13 Jan, 5 days skipping the 14 Jan holiday -> 13,15,16,17,19
        self.assertEqual(self.act_b.planned_start, date(2026, 1, 13))
        self.assertEqual(self.act_b.planned_finish, date(2026, 1, 19))
        self.assertEqual(self.act_c.total_float, 9)

    def test_company_holiday_applies(self):
        self.env["epc.holiday"].create({"name": "Company day", "date": date(2026, 1, 2)})
        self.project.action_epc_compute_schedule()
        self.assertEqual(self.act_a.planned_finish, date(2026, 1, 11))


@tagged("post_install", "-at_install", "zivoit_epc")
class TestMonteCarlo(EpcCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Model = cls.env["epc.prediction.model"]
        Model.search([]).unlink()
        for discipline in [False] + [d for d, _l in cls.env["epc.activity"]._fields["discipline"].selection]:
            Model.create({"discipline": discipline, "p50_factor": 1.0, "p80_factor": 1.25})
        cls.project.epc_status_date = date(2026, 1, 1)

    def test_simulation_outputs(self):
        self.env["ir.config_parameter"].sudo().set_param("zivoit_epc_ai.mc_iterations", "300")
        self.env["epc.prediction.engine"].run_for_projects(self.project)
        p = self.project
        self.assertEqual(p.epc_mc_iterations, 300)
        self.assertTrue(p.epc_mc_p50_finish <= p.epc_mc_p80_finish <= p.epc_mc_p90_finish)
        self.assertGreaterEqual(p.epc_mc_p50_finish, date(2026, 1, 13))
        self.assertTrue(0.0 <= p.epc_mc_on_time_probability < 100.0)
        # The A-B chain drives the finish far more often than the short C branch
        self.assertGreater(self.act_a.criticality_index, 60)  # early finishes let B wait for its planned start
        self.assertGreater(self.act_b.criticality_index, 90)
        self.assertLess(self.act_c.criticality_index, 10)

    def test_simulation_is_reproducible(self):
        engine = self.env["epc.prediction.engine"]
        engine.run_for_projects(self.project)
        first = (self.project.epc_mc_p80_finish, self.project.epc_mc_on_time_probability)
        engine.run_for_projects(self.project)
        self.assertEqual(first, (self.project.epc_mc_p80_finish, self.project.epc_mc_on_time_probability))


@tagged("post_install", "-at_install", "zivoit_epc")
class TestScheduleOptimizer(EpcCommon):

    def test_least_cost_compression(self):
        self.act_a.write({"crash_duration": 7, "crash_cost_per_day": 1000})
        self.act_b.write({"crash_duration": 4, "crash_cost_per_day": 500})
        action = self.project.action_epc_open_optimizer()
        wizard = self.env["epc.schedule.optimizer"].with_context(action["context"]).create(
            {"target_finish": date(2026, 1, 13)})
        self.assertEqual(wizard.project_id, self.project)
        wizard.action_optimise()
        self.assertTrue(wizard.target_met)
        self.assertEqual(wizard.current_finish, date(2026, 1, 15))
        self.assertEqual(wizard.achieved_finish, date(2026, 1, 13))
        proposals = {l.activity_id.code: (l.current_duration, l.proposed_duration) for l in wizard.line_ids}
        self.assertEqual(proposals, {"A": (10, 9), "B": (5, 4)})
        self.assertEqual(wizard.total_cost, 1500)
        self.assertIn("half done", str(wizard.fast_track_html))
        wizard.action_apply()
        self.assertEqual((self.act_a.duration, self.act_b.duration), (9, 4))
        self.assertEqual(self.act_b.planned_finish, date(2026, 1, 13))
        self.assertEqual(wizard.state, "applied")

    def test_target_not_reachable(self):
        wizard = self.env["epc.schedule.optimizer"].create({
            "project_id": self.project.id, "target_finish": date(2026, 1, 5)})
        wizard.action_optimise()
        self.assertFalse(wizard.target_met)
        self.assertFalse(wizard.line_ids)
        with self.assertRaises(UserError):
            wizard.action_apply()

    def test_does_not_pay_for_start_critical_activity(self):
        """X only drives Y through a start-to-start link: shortening X saves nothing, so it must not be crashed."""
        Activity = self.env["epc.activity"]
        x = Activity.create({"project_id": self.project.id, "code": "X", "name": "Erection", "duration": 10,
                             "crash_duration": 5, "crash_cost_per_day": 10})
        y = Activity.create({"project_id": self.project.id, "code": "Y", "name": "Stringing", "duration": 20,
                             "crash_duration": 15, "crash_cost_per_day": 1000})
        Link = self.env["epc.activity.link"]
        Link.create({"predecessor_id": x.id, "successor_id": y.id, "link_type": "SS", "lag_days": 2})
        Link.create({"predecessor_id": y.id, "successor_id": self.act_d.id, "link_type": "FS"})
        self.project.action_epc_compute_schedule()
        finish = self.act_d.planned_start
        wizard = self.env["epc.schedule.optimizer"].create({
            "project_id": self.project.id, "target_finish": finish - timedelta(days=3)})
        wizard.action_optimise()
        self.assertTrue(wizard.target_met)
        self.assertEqual({l.activity_id.code: l.days_saved for l in wizard.line_ids}, {"Y": 2})
        self.assertEqual(wizard.total_cost, 2000)
