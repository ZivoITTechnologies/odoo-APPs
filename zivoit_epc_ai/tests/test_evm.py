from datetime import date

from odoo.tests import tagged

from .common import EpcCommon


@tagged("post_install", "-at_install", "zivoit_epc")
class TestEvm(EpcCommon):

    def test_planned_value(self):
        self.project.epc_status_date = date(2026, 1, 5)
        # A: 5 of 10 days -> 50% of 100000; C: 3 of 4 days (Jan 3-6) -> 75% of 20000; B not started
        self.assertAlmostEqual(self.project.epc_pv, 50000 + 15000)
        self.assertAlmostEqual(self.project.epc_bac, 220000)

    def test_earned_value_and_indices(self):
        self.project.epc_status_date = date(2026, 1, 5)
        self.make_dpr(date(2026, 1, 4), [(self.act_a, 40)])
        self.act_c.manual_progress = 50
        self.act_a.actual_cost = 50000
        self.act_c.actual_cost = 5000
        self.assertAlmostEqual(self.act_a.progress, 40.0)
        self.assertAlmostEqual(self.project.epc_ev, 40000 + 10000)
        self.assertAlmostEqual(self.project.epc_ac, 55000)
        self.assertAlmostEqual(self.project.epc_spi, 50000 / 65000, places=2)
        self.assertAlmostEqual(self.project.epc_cpi, 50000 / 55000, places=2)
        self.assertAlmostEqual(self.project.epc_actual_progress, 50000 / 220000 * 100, places=1)

    def test_progress_capped_and_actual_finish(self):
        self.make_dpr(date(2026, 1, 4), [(self.act_a, 120)])
        self.assertEqual(self.act_a.progress, 100.0)
        self.assertEqual(self.act_a.state, "done")
        self.assertEqual(self.act_a.actual_finish, date(2026, 1, 4))

    def test_manual_progress_bounds(self):
        with self.assertRaises(Exception):
            self.act_c.manual_progress = 120
            self.act_c.flush_recordset()
