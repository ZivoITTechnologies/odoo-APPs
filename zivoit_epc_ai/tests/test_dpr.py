from datetime import date

from psycopg2 import IntegrityError

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import EpcCommon


@tagged("post_install", "-at_install", "zivoit_epc")
class TestDpr(EpcCommon):

    def test_approval_updates_progress_and_boq(self):
        dpr = self.make_dpr(date(2026, 1, 3), [(self.act_a, 30), (self.act_b, 10)], approve=False)
        self.assertEqual(self.boq_fdn.executed_qty, 0.0, "draft DPRs do not count")
        dpr.action_submit()
        self.assertEqual(dpr.state, "submitted")
        dpr.action_approve()
        self.assertEqual(dpr.state, "approved")
        self.assertEqual(self.boq_fdn.executed_qty, 30.0)
        self.assertAlmostEqual(self.boq_fdn.progress, 30.0)
        self.assertEqual(self.boq_erc.executed_qty, 10.0)
        self.assertEqual(self.act_a.actual_start, date(2026, 1, 3))
        self.assertEqual(self.act_a.state, "in_progress")
        self.assertEqual(dpr.line_ids[0].boq_line_id, self.boq_fdn)

    def test_reset_to_draft_removes_quantities(self):
        dpr = self.make_dpr(date(2026, 1, 3), [(self.act_a, 30)])
        dpr.action_reset_draft()
        self.assertEqual(self.boq_fdn.executed_qty, 0.0)
        self.assertEqual(self.act_a.progress, 0.0)

    def test_earlier_dpr_moves_actual_start(self):
        self.make_dpr(date(2026, 1, 5), [(self.act_a, 10)])
        self.make_dpr(date(2026, 1, 2), [(self.act_a, 10)])
        self.assertEqual(self.act_a.actual_start, date(2026, 1, 2))

    def test_guards(self):
        dpr = self.make_dpr(date(2026, 1, 3), [(self.act_a, 5)])
        with self.assertRaises(UserError):
            dpr.unlink()
        empty = self.env["epc.dpr"].create({"project_id": self.project.id, "date": date(2026, 1, 4)})
        with self.assertRaises(UserError):
            empty.action_submit()
        with self.assertRaises(ValidationError):
            self.make_dpr(date(2026, 1, 6), [(self.act_a, -1)], approve=False)
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"), self.cr.savepoint():
            self.make_dpr(date(2026, 1, 3), [(self.act_a, 1)], approve=False)

    def test_dpr_reference_number(self):
        dpr = self.make_dpr(date(2026, 1, 7), [(self.act_a, 1)], approve=False)
        self.assertTrue(dpr.name.startswith("DPR/"))

    def test_ai_draft_fallback_parser(self):
        dpr = self.env["epc.dpr"].create({
            "project_id": self.project.id,
            "date": date(2026, 1, 8),
            "notes_raw": (
                "Foundations completed 6 nos at loc 45, 46.\n"
                "B: tower erection 4.5 MT today; heavy rain stopped work for 2 hrs.\n"
                "Survey crew idle - drawing revision awaited, 5 hours lost.\n"
                "10 riggers, 22 helpers and 3 welders deployed."
            ),
        })
        dpr.action_ai_draft()
        quantities = {l.activity_id.code: l.quantity for l in dpr.line_ids}
        self.assertEqual(quantities, {"A": 6.0, "B": 4.5})
        manpower = {m.trade: m.count for m in dpr.manpower_ids}
        self.assertEqual(manpower, {"rigger": 10, "helper": 22, "welder": 3})
        types = sorted(dpr.hindrance_ids.mapped("hindrance_type"))
        self.assertEqual(types, ["drawing", "weather"])
        self.assertEqual(dpr.hours_lost_total, 7.0)
        self.assertEqual(dpr.weather, "heavy_rain")
        self.assertEqual(dpr.ai_source, "fallback")
        # running it again does not duplicate lines
        dpr.action_ai_draft()
        self.assertEqual(len(dpr.line_ids), 2)
        self.assertEqual(len(dpr.hindrance_ids), 2)

    def test_ai_draft_requires_notes_and_draft(self):
        dpr = self.env["epc.dpr"].create({"project_id": self.project.id, "date": date(2026, 1, 9)})
        with self.assertRaises(UserError):
            dpr.action_ai_draft()
        approved = self.make_dpr(date(2026, 1, 10), [(self.act_a, 1)], notes_raw="A 1 nos")
        with self.assertRaises(UserError):
            approved.action_ai_draft()

    def test_activity_must_match_project(self):
        other = self.env["project.project"].create({"name": "Other EPC", "is_epc": True})
        foreign = self.env["epc.activity"].create({"project_id": other.id, "code": "Z", "name": "Z"})
        with self.assertRaises(ValidationError):
            self.make_dpr(date(2026, 1, 11), [(foreign, 1)], approve=False)
