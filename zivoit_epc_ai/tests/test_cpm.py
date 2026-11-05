from datetime import date

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import EpcCommon


@tagged("post_install", "-at_install", "zivoit_epc")
class TestCpm(EpcCommon):

    def test_forward_backward_pass(self):
        a, b, c, d = self.act_a, self.act_b, self.act_c, self.act_d
        self.assertEqual((a.early_start, a.early_finish), (0, 10))
        self.assertEqual((b.early_start, b.early_finish), (10, 15))
        self.assertEqual((c.early_start, c.early_finish), (2, 6))
        self.assertEqual((d.early_start, d.early_finish), (15, 15))
        self.assertEqual(c.total_float, 9)
        self.assertTrue(a.is_critical and b.is_critical and d.is_critical)
        self.assertFalse(c.is_critical)

    def test_dates(self):
        self.assertEqual(self.act_a.planned_start, date(2026, 1, 1))
        self.assertEqual(self.act_a.planned_finish, date(2026, 1, 10))
        self.assertEqual(self.act_b.planned_start, date(2026, 1, 11))
        self.assertEqual(self.act_b.planned_finish, date(2026, 1, 15))
        self.assertEqual(self.act_c.planned_start, date(2026, 1, 3))
        self.assertEqual(self.act_d.planned_start, date(2026, 1, 16))
        self.assertEqual(self.project.epc_planned_finish, date(2026, 1, 16))
        self.assertEqual(self.act_a.baseline_finish, date(2026, 1, 10))

    def test_ff_and_sf_links_with_lag(self):
        Activity = self.env["epc.activity"]
        e = Activity.create({"project_id": self.project.id, "code": "E", "name": "FF test", "duration": 3})
        f = Activity.create({"project_id": self.project.id, "code": "F", "name": "SF test", "duration": 4})
        Link = self.env["epc.activity.link"]
        Link.create({"predecessor_id": self.act_b.id, "successor_id": e.id, "link_type": "FF", "lag_days": 2})
        Link.create({"predecessor_id": self.act_a.id, "successor_id": f.id, "link_type": "SF", "lag_days": 10})
        self.project.action_epc_compute_schedule()
        # FF: EF_e >= EF_b + 2 = 17 -> ES = 14
        self.assertEqual((e.early_start, e.early_finish), (14, 17))
        # SF: EF_f >= ES_a + 10 = 10 -> ES = 6
        self.assertEqual((f.early_start, f.early_finish), (6, 10))
        # E now drives the project finish (17)
        self.assertTrue(e.is_critical)
        self.assertEqual(self.act_b.total_float, 0)

    def test_constraint_date(self):
        self.act_c.constraint_date = date(2026, 1, 20)
        self.project.action_epc_compute_schedule()
        self.assertEqual(self.act_c.planned_start, date(2026, 1, 20))
        self.assertEqual(self.act_d.planned_start, date(2026, 1, 24))
        self.assertTrue(self.act_c.is_critical)
        self.assertFalse(self.act_b.is_critical)

    def test_cycle_detected(self):
        self.env["epc.activity.link"].create({"predecessor_id": self.act_d.id, "successor_id": self.act_a.id})
        with self.assertRaisesRegex(UserError, "loop"):
            self.project.action_epc_compute_schedule()

    def test_link_constraints(self):
        with self.assertRaises(Exception), mute_logger("odoo.sql_db"), self.cr.savepoint():
            self.env["epc.activity.link"].create({"predecessor_id": self.act_a.id, "successor_id": self.act_a.id})
        other = self.env["project.project"].create({"name": "Other", "is_epc": True})
        foreign = self.env["epc.activity"].create({"project_id": other.id, "code": "X", "name": "X"})
        with self.assertRaises(ValidationError):
            self.env["epc.activity.link"].create({"predecessor_id": foreign.id, "successor_id": self.act_a.id})

    def test_non_epc_project_refused(self):
        plain = self.env["project.project"].create({"name": "Plain"})
        with self.assertRaises(UserError):
            plain.action_epc_compute_schedule()
