import base64
import json
from datetime import date
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.zivoit_epc_ai.wizard.epc_bim_import import parse_ifc, split_args

from .common import EpcCommon
from .test_ai_service import POST, openai_answer

BOM_TEXT = """GENERAL ARRANGEMENT OF TOWER TYPE DA+0
BILL OF MATERIALS
SL  DESCRIPTION                     QTY  UNIT
1   Angle ISA 75x75x6               1200 kg
2   Hex bolt M16x50 with nut        400 nos
3.  Stub angle 100x100x8            16.5 MT
NOTE: ALL DIMENSIONS IN MM
"""

IFC = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [CoordinationView]'),'2;1');
FILE_NAME('sub.ifc','2026-01-01T00:00:00',(''),(''),'','','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#1=IFCPROJECT('0YvctVUKr0kugbFTf53O9L',$,'Substation',$,$,$,$,$,$);
#10=IFCCOLUMN('1a',$,'Column-001',$,'UC 203x203x46',$,$,$,$);
#11=IFCCOLUMN('1b',$,'Column-002',$,'UC 203x203x46',$,$,$,$);
#12=IFCBEAM('1c',$,'Beam:01',$,$,$,$,$,$);
#13=IFCBEAM('1d',$,'Beam:02',$,$,$,$,$,$);
#14=IFCFOOTING('1e',$,'Pad footing F1',$,'F1',$,$,$,$);
#15=IFCSPACE('1f',$,'Room',$,$,$,$,$,$,$,$);
#20=IFCQUANTITYWEIGHT('GrossWeight',$,$,500.,$);
#21=IFCQUANTITYWEIGHT('NetWeight',$,$,460.,$);
#22=IFCELEMENTQUANTITY('2a',$,'Qto_ColumnBaseQuantities',$,$,(#20,#21));
#23=IFCRELDEFINESBYPROPERTIES('2b',$,$,$,(#10,#11),#22);
#30=IFCQUANTITYVOLUME('NetVolume',$,$,2.5,$);
#31=IFCELEMENTQUANTITY('3a',$,'Qto_FootingBaseQuantities',$,$,(#30));
#32=IFCRELDEFINESBYPROPERTIES('3b',$,$,$,(#14),#31);
ENDSEC;
END-ISO-10303-21;
"""


@tagged("post_install", "-at_install", "zivoit_epc")
class TestMto(EpcCommon):

    def _drawing(self, content, filename="GA-DA0.txt"):
        return self.env["epc.document"].create({
            "project_id": self.project.id, "doc_number": "DWG-%s" % filename, "title": "Tower GA",
            "drawing_file": base64.b64encode(content), "drawing_filename": filename})

    def test_fallback_bom_parser(self):
        doc = self._drawing(BOM_TEXT.encode())
        doc.action_ai_extract_mto()
        rows = [(l.item_code, l.quantity, l.uom_name.lower()) for l in doc.mto_line_ids]
        self.assertEqual(rows, [("1", 1200.0, "kg"), ("2", 400.0, "nos"), ("3", 16.5, "mt")])
        self.assertIn("Hex bolt M16x50", doc.mto_line_ids[1].description)
        self.assertTrue(all(l.source == "ai_drawing" for l in doc.mto_line_ids))
        # re-running replaces the draft lines instead of duplicating them
        doc.action_ai_extract_mto()
        self.assertEqual(doc.mto_count, 3)

    def test_unreadable_drawing(self):
        doc = self._drawing(b"nothing useful here")
        with self.assertRaises(UserError):
            doc.action_ai_extract_mto()
        image = self._drawing(b"\x89PNG fake", filename="scan.png")
        with self.assertRaises(UserError):  # image without a vision provider
            image.action_ai_extract_mto()

    def test_llm_extraction_maps_boq(self):
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("zivoit_epc_ai.provider", "openai")
        icp.set_param("zivoit_epc_ai.api_key", "sk-test")
        doc = self._drawing(BOM_TEXT.encode()).with_context(epc_ai_force_fallback=False)
        answer = json.dumps({"items": [
            {"item_code": "1", "description": "Angle ISA 75x75x6", "quantity": "1,200", "unit": "kg", "boq_code": "B2"},
            {"item_code": "x", "description": "bad", "quantity": 0},
        ]})
        with patch(POST, return_value=openai_answer(answer)):
            doc.action_ai_extract_mto()
        self.assertEqual(len(doc.mto_line_ids), 1)
        self.assertEqual((doc.mto_line_ids.quantity, doc.mto_line_ids.boq_line_id), (1200.0, self.boq_erc))

    def test_boq_design_variance_and_rfqs(self):
        vendor = self.env["res.partner"].create({"name": "Steel Mill"})
        Mto = self.env["epc.mto.line"]
        line1 = Mto.create({"project_id": self.project.id, "description": "Tower steel", "quantity": 55,
                            "uom_name": "MT", "boq_line_id": self.boq_erc.id, "activity_id": self.act_b.id,
                            "vendor_id": vendor.id, "estimated_rate": 900})
        line2 = Mto.create({"project_id": self.project.id, "description": "Bolts", "quantity": 4000,
                            "vendor_id": vendor.id, "estimated_rate": 2})
        self.assertAlmostEqual(self.boq_erc.mto_variance_pct, 10.0)
        self.assertEqual(line1.required_date, self.act_b.planned_start)
        with self.assertRaises(UserError):
            (line1 | line2).action_create_rfqs()  # still draft
        (line1 | line2).action_confirm()
        action = (line1 | line2).action_create_rfqs()
        order = self.env["purchase.order"].search(action["domain"])
        self.assertEqual(len(order), 1)
        self.assertEqual(order.partner_id, vendor)
        self.assertEqual(len(order.order_line), 2)
        self.assertEqual((line1.state, line1.purchase_line_id.epc_project_id), ("rfq", self.project))
        self.assertEqual(line1.purchase_line_id.epc_required_date, self.act_b.planned_start)
        self.assertIn(line1.purchase_line_id, self.act_b.purchase_line_ids)


@tagged("post_install", "-at_install", "zivoit_epc")
class TestBim(EpcCommon):

    def test_split_args(self):
        args = split_args("'a''b',$,(#1,#2),'x,y',1.5")
        self.assertEqual(args, [("str", "a'b"), "$", ["#1", "#2"], ("str", "x,y"), "1.5"])

    def test_parse_and_import(self):
        groups = parse_ifc(IFC)
        self.assertEqual(groups[("IFCCOLUMN", "UC 203x203x46")]["count"], 2)
        self.assertEqual(groups[("IFCCOLUMN", "UC 203x203x46")]["weight"], 920.0)  # net weight preferred
        self.assertEqual(groups[("IFCBEAM", "Beam")]["count"], 2)
        self.assertNotIn("IFCSPACE", {k[0] for k in groups})
        with self.assertRaises(ValueError):
            parse_ifc("hello")
        wizard = self.env["epc.bim.import"].with_context(active_model="project.project", active_id=self.project.id)\
            .create({"ifc_file": base64.b64encode(IFC.encode()), "ifc_filename": "sub.ifc"})
        self.assertEqual(wizard.project_id, self.project)
        wizard.action_import()
        lines = self.env["epc.mto.line"].search([("project_id", "=", self.project.id), ("source", "=", "bim")])
        result = {(l.item_code, l.quantity, l.uom_name) for l in lines}
        self.assertEqual(result, {("UC 203x203x46", 920.0, "kg"), ("Beam", 2, "Nos"), ("F1", 2.5, "m3")})
        wizard.action_import()  # idempotent
        self.assertEqual(self.env["epc.mto.line"].search_count([("project_id", "=", self.project.id)]), 3)
        self.assertEqual(self.project.epc_mto_count, 3)


@tagged("post_install", "-at_install", "zivoit_epc")
class TestChangeOrder(EpcCommon):

    def _change(self, description):
        return self.env["epc.change.order"].create({
            "project_id": self.project.id, "title": "Extra foundations at angle points",
            "description": description})

    def test_fallback_assessment_and_approval(self):
        change = self._change("Client added 10 foundations: B1 +10 Nos. Foundations A +3 days; C is unaffected.")
        self.assertTrue(change.name.startswith("CO/"))
        change.action_ai_assess()
        self.assertEqual(change.ai_source, "fallback")
        self.assertEqual([(l.boq_line_id, l.qty_delta, l.rate) for l in change.boq_change_ids],
                         [(self.boq_fdn, 10.0, 1000.0)])
        self.assertEqual([(l.activity_id, l.duration_delta) for l in change.activity_change_ids], [(self.act_a, 3)])
        self.assertEqual(change.cost_impact, 10000)
        self.assertEqual(change.time_impact_days, 3)
        with self.assertRaises(UserError):
            change.action_approve()  # not submitted yet
        change.action_submit()
        change.action_approve()
        self.assertEqual(change.state, "approved")
        self.assertEqual(self.boq_fdn.quantity, 110)
        self.assertEqual(self.act_a.duration, 13)
        self.assertEqual(self.act_d.planned_start, date(2026, 1, 19))
        self.assertEqual(self.project.epc_contract_value, 100010000.0)
        self.assertEqual(self.project.epc_approved_variations, 10000)

    def test_float_absorbs_delay_and_new_item(self):
        change = self._change("Survey rework")
        change.write({
            "activity_change_ids": [(0, 0, {"activity_id": self.act_c.id, "duration_delta": 5})],
            "boq_change_ids": [(0, 0, {"new_code": "B9", "new_name": "Soil investigation", "new_uom": "LS",
                                       "qty_delta": 1, "rate": 25000})],
            "affects_contract_value": False,
        })
        change.action_submit()
        self.assertEqual(change.time_impact_days, 0)  # C has 9 days of float
        change.action_approve()
        new_boq = self.env["epc.boq.line"].search([("project_id", "=", self.project.id), ("code", "=", "B9")])
        self.assertEqual((new_boq.quantity, new_boq.rate), (1, 25000))
        self.assertEqual(self.project.epc_contract_value, 100000000.0)

    def test_llm_assessment(self):
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("zivoit_epc_ai.provider", "openai")
        icp.set_param("zivoit_epc_ai.api_key", "sk-test")
        change = self._change("Line diversion near the river crossing").with_context(epc_ai_force_fallback=False)
        answer = json.dumps({
            "boq_changes": [{"boq_code": "B2", "qty_delta": 6, "rate": 2100}, {"boq_code": "NOPE", "qty_delta": 1}],
            "new_items": [{"code": "N1", "name": "River crossing tower", "unit": "Nos", "qty": 2, "rate": 50000}],
            "activity_changes": [{"activity_code": "B", "duration_delta": "2"}],
            "justification": "Two extra towers and heavier sections.",
        })
        with patch(POST, return_value=openai_answer(answer)):
            change.action_ai_assess()
        self.assertEqual(change.ai_source, "llm")
        self.assertEqual(len(change.boq_change_ids), 2)
        self.assertEqual(change.cost_impact, 6 * 2100 + 2 * 50000)
        self.assertEqual(change.time_impact_days, 2)

    def test_only_managers_approve(self):
        user = new_test_user(self.env, "epc_site_user", groups="zivoit_epc_ai.group_epc_user")
        change = self._change("A +1 days")
        change.action_ai_assess()
        change.action_submit()
        with self.assertRaises(UserError):
            change.with_user(user).action_approve()
        change.action_reject()
        self.assertEqual(change.state, "rejected")
        empty = self._change("nothing measurable")
        with self.assertRaises(UserError):
            empty.action_submit()
