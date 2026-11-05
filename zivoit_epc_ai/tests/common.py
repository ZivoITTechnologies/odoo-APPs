from datetime import date

from odoo.tests.common import TransactionCase


class EpcCommon(TransactionCase):
    """Small T&D project with a known network.

    A(10d) -FS-> B(5d) -FS-> D(0d milestone)
    A(10d) -SS+2-> C(4d) -FS-> D
    Project start 2026-01-01.
    CPM: A 0-10, B 10-15, C 2-6 (float 9), D 15.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True, epc_ai_force_fallback=True))
        cls.env["ir.config_parameter"].sudo().set_param("zivoit_epc_ai.provider", "none")
        cls.client = cls.env["res.partner"].create({"name": "Test Utility", "is_company": True})
        cls.admin = cls.env.ref("base.user_admin")
        cls.project = cls.env["project.project"].create({
            "name": "Test 220 kV Line",
            "is_epc": True,
            "partner_id": cls.client.id,
            "date_start": date(2026, 1, 1),
            "date": date(2026, 12, 31),
            "epc_contract_value": 100000000.0,
            "epc_ld_rate_pct": 0.5,
            "epc_ld_cap_pct": 10.0,
            "epc_retention_pct": 5.0,
            "user_id": cls.admin.id,
        })
        Boq = cls.env["epc.boq.line"]
        cls.boq_fdn = Boq.create({"project_id": cls.project.id, "code": "B1", "name": "Foundations",
                                  "uom_name": "Nos", "quantity": 100, "rate": 1000})
        cls.boq_erc = Boq.create({"project_id": cls.project.id, "code": "B2", "name": "Erection",
                                  "uom_name": "MT", "quantity": 50, "rate": 2000})
        Activity = cls.env["epc.activity"]
        cls.act_a = Activity.create({"project_id": cls.project.id, "code": "A", "name": "Foundations",
                                     "discipline": "civil", "duration": 10, "budget": 100000, "responsible_id": cls.admin.id,
                                     "boq_line_id": cls.boq_fdn.id, "planned_qty": 100})
        cls.act_b = Activity.create({"project_id": cls.project.id, "code": "B", "name": "Tower erection",
                                     "discipline": "structural", "duration": 5, "budget": 100000,
                                     "boq_line_id": cls.boq_erc.id, "planned_qty": 50})
        cls.act_c = Activity.create({"project_id": cls.project.id, "code": "C", "name": "Survey",
                                     "discipline": "engineering", "duration": 4, "budget": 20000,
                                     "progress_method": "manual"})
        cls.act_d = Activity.create({"project_id": cls.project.id, "code": "D", "name": "Completion",
                                     "duration": 0, "is_milestone": True})
        Link = cls.env["epc.activity.link"]
        Link.create({"predecessor_id": cls.act_a.id, "successor_id": cls.act_b.id, "link_type": "FS"})
        Link.create({"predecessor_id": cls.act_a.id, "successor_id": cls.act_c.id, "link_type": "SS", "lag_days": 2})
        Link.create({"predecessor_id": cls.act_b.id, "successor_id": cls.act_d.id, "link_type": "FS"})
        Link.create({"predecessor_id": cls.act_c.id, "successor_id": cls.act_d.id, "link_type": "FS"})
        cls.project.action_epc_compute_schedule()
        cls.project.action_epc_set_baseline()

    @classmethod
    def make_dpr(cls, day, lines, approve=True, **extra):
        vals = {
            "project_id": cls.project.id,
            "date": day,
            "line_ids": [(0, 0, {"activity_id": act.id, "quantity": qty}) for act, qty in lines],
        }
        vals.update(extra)
        dpr = cls.env["epc.dpr"].create(vals)
        if approve:
            dpr.action_approve()
        return dpr
