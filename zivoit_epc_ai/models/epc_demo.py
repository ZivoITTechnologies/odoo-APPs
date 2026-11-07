"""Demo data generator.

Dates are generated relative to the install date so the demo always shows a
live project with realistic slippage, a completed project the engine learns
from, a late conductor supplier and an overdue stringing drawing.
"""
import base64
from datetime import datetime, time, timedelta

from odoo import api, fields, models

DEMO_CONTRACT = """GENERAL CONDITIONS OF CONTRACT (DEMO, FICTIONAL)

1. Liquidated Damages. If the Contractor fails to complete the Works by the Time for Completion, the Contractor shall pay liquidated damages at the rate of 0.5% of the Contract Price per week of delay, subject to a maximum of 10% of the Contract Price.

2. Payment. The Employer shall pay each certified running account bill within 45 days of receipt of the invoice.

3. Retention. Retention money of 5% shall be deducted from each running account bill and released after the Defect Liability Period.

4. Performance Security. The Contractor shall furnish a performance bank guarantee of 10% of the Contract Price.

5. Defect Liability Period. The defect liability period shall be 24 months from the date of taking over.

6. Termination. The Employer may terminate the Contract for convenience by giving 30 days notice.

7. Indemnity. The Contractor shall indemnify the Employer against any and all claims arising out of the execution of the Works.

8. Force Majeure. Neither party shall be liable for failure to perform due to force majeure events.

9. Disputes. Any dispute shall be settled by arbitration under the Arbitration and Conciliation Act, 1996.

10. Prices. The Contract Price is firm and no price variation or escalation shall be payable.
"""


class EpcDemo(models.AbstractModel):
    _name = "epc.demo"
    _description = "EPC Demo Data Loader"

    # ------------------------------------------------------------------
    @api.model
    def _partner(self, name, **vals):
        partner = self.env["res.partner"].search([("name", "=", name)], limit=1)
        return partner or self.env["res.partner"].create(dict({"name": name, "is_company": True}, **vals))

    @api.model
    def _product(self, name):
        product = self.env["product.product"].search([("name", "=", name)], limit=1)
        return product or self.env["product.product"].create({
            "name": name, "type": "consu", "purchase_ok": True, "sale_ok": False,
        })

    @api.model
    def _purchase(self, vendor, project, lines, confirm=True):
        order = self.env["purchase.order"].create({
            "partner_id": vendor.id,
            "order_line": [(0, 0, {
                "product_id": product.id,
                "name": product.name,
                "product_qty": qty,
                "price_unit": price,
                "date_planned": datetime.combine(planned, time(9, 0)),
                "epc_project_id": project.id if project else False,
            }) for product, qty, price, planned in lines],
        })
        if confirm:
            order.button_confirm()
            # keep the promised dates set on the lines
            for line, (_product, _qty, _price, planned) in zip(order.order_line, lines):
                line.date_planned = datetime.combine(planned, time(9, 0))
        return order

    @api.model
    def _receive(self, line, qty, when):
        picking = line.order_id.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))[:1]
        if not picking:
            return
        picking.action_assign()
        move = picking.move_ids.filtered(lambda m: m.purchase_line_id == line)[:1]
        if not move:
            return
        move.quantity = qty
        move.picked = True
        picking.with_context(cancel_backorder=False)._action_done()
        when_dt = datetime.combine(when, time(12, 0))
        move.write({"date": when_dt})
        move.move_line_ids.write({"date": when_dt})
        picking.write({"date_done": when_dt})

    @api.model
    def _activities(self, project, rows, links):
        Activity = self.env["epc.activity"]
        acts = {}
        for seq, row in enumerate(rows, start=1):
            code, name, discipline, duration, budget, boq, qty = row[:7]
            vals = {
                "project_id": project.id,
                "sequence": seq * 10,
                "code": code,
                "name": name,
                "discipline": discipline,
                "duration": duration,
                "is_milestone": duration == 0,
                "budget": budget,
                "boq_line_id": boq.id if boq else False,
                "planned_qty": qty,
                "progress_method": "quantity" if boq else "manual",
                "responsible_id": project.user_id.id,
            }
            acts[code] = Activity.create(vals)
        for pred, succ, link_type, lag in links:
            self.env["epc.activity.link"].create({
                "predecessor_id": acts[pred].id,
                "successor_id": acts[succ].id,
                "link_type": link_type,
                "lag_days": lag,
            })
        return acts

    # ------------------------------------------------------------------
    @api.model
    def _load_demo(self):
        if self.env["project.project"].search_count([("epc_contract_ref", "like", "DEMO-")]):
            return True
        today = fields.Date.context_today(self)
        user = self.env.ref("base.user_admin", raise_if_not_found=False) or self.env.user
        client = self._partner("Demo State Transmission Utility", email="projects@example.com")
        steel_vendor = self._partner("Demo Tower Steel Works", email="sales@steel.example.com", supplier_rank=1)
        conductor_vendor = self._partner("Demo Conductor Mills", email="orders@conductor.example.com", supplier_rank=1)
        insulator_vendor = self._partner("Demo Insulator Company", email="sales@insulator.example.com", supplier_rank=1)
        steel = self._product("Galvanised tower steel (MT)")
        conductor = self._product("ACSR Moose conductor (km)")
        insulator = self._product("Composite long-rod insulator")

        self._load_history_project(client, today, user)
        self._vendor_history(steel_vendor, conductor_vendor, steel, conductor, today)

        project = self.env["project.project"].create({
            "name": "400 kV D/C Transmission Line — Demo",
            "is_epc": True,
            "epc_segment": "td",
            "epc_contract_ref": "DEMO-TL-400-01",
            "partner_id": client.id,
            "user_id": user.id,
            "date_start": today - timedelta(days=90),
            "date": today + timedelta(days=330),
            "epc_contract_value": 850000000.0,
            "epc_ld_rate_pct": 0.5,
            "epc_ld_cap_pct": 10.0,
            "epc_retention_pct": 5.0,
            "epc_advance_recovery_pct": 5.0,
        })
        Boq = self.env["epc.boq.line"]
        boq_fdn = Boq.create({"project_id": project.id, "sequence": 10, "code": "B-01", "name": "Tower foundations (all types)",
                              "uom_name": "Nos", "quantity": 120, "rate": 350000})
        boq_erc = Boq.create({"project_id": project.id, "sequence": 20, "code": "B-02", "name": "Tower erection incl. bolts & nuts",
                              "uom_name": "MT", "quantity": 3000, "rate": 45000})
        boq_str = Boq.create({"project_id": project.id, "sequence": 30, "code": "B-03", "name": "Stringing of twin Moose conductor",
                              "uom_name": "ckm", "quantity": 60, "rate": 900000})
        Boq.create({"project_id": project.id, "sequence": 40, "code": "B-04", "name": "Testing & commissioning",
                    "uom_name": "Job", "quantity": 1, "rate": 15000000})

        rows = [
            ("M0", "Notice to proceed", "general", 0, 0, None, 0),
            ("ENG1", "Detailed route survey & tower spotting", "engineering", 20, 6000000, None, 0),
            ("ENG2", "Tower foundation drawings", "engineering", 25, 4000000, None, 0),
            ("ENG3", "Stringing charts & sag-tension calculations", "engineering", 30, 3000000, None, 0),
            ("PRC1", "Tower steel fabrication & supply", "procurement", 60, 20000000, None, 0),
            ("FDN1", "Tower foundations section A", "civil", 45, 21000000, boq_fdn, 60),
            ("FDN2", "Tower foundations section B", "civil", 45, 21000000, boq_fdn, 60),
            ("ERC1", "Tower erection section A", "structural", 40, 67500000, boq_erc, 1500),
            ("ERC2", "Tower erection section B", "structural", 40, 67500000, boq_erc, 1500),
            ("STR1", "Stringing section A", "stringing", 30, 27000000, boq_str, 30),
            ("STR2", "Stringing section B", "stringing", 30, 27000000, boq_str, 30),
            ("TC1", "Testing & commissioning", "commissioning", 15, 15000000, None, 0),
            ("M1", "Line charged — completion", "general", 0, 0, None, 0),
        ]
        links = [
            ("M0", "ENG1", "FS", 0), ("ENG1", "ENG2", "FS", 0), ("ENG1", "ENG3", "FS", 10),
            ("ENG2", "FDN1", "FS", 0), ("ENG2", "PRC1", "SS", 10), ("FDN1", "FDN2", "SS", 15),
            ("FDN1", "ERC1", "SS", 20), ("PRC1", "ERC1", "SS", 25), ("FDN2", "ERC2", "SS", 20),
            ("ERC1", "STR1", "SS", 20), ("ENG3", "STR1", "FS", 0), ("ERC2", "STR2", "SS", 20),
            ("STR1", "STR2", "SS", 10), ("STR1", "TC1", "FS", 0), ("STR2", "TC1", "FS", 0),
            ("TC1", "M1", "FS", 0),
        ]
        acts = self._activities(project, rows, links)
        project.action_epc_compute_schedule()
        project.action_epc_set_baseline()

        # Actual progress so far
        start = project.date_start
        acts["M0"].write({"actual_start": start, "actual_finish": start})
        for code in ("ENG1", "ENG2"):
            act = acts[code]
            act.write({"actual_start": act.planned_start, "actual_finish": act.planned_finish + timedelta(days=3)})
        eng3 = acts["ENG3"]
        eng3.write({"actual_start": eng3.planned_start + timedelta(days=5), "manual_progress": 55})
        prc1 = acts["PRC1"]
        prc1.write({"actual_start": prc1.planned_start, "manual_progress": 70})

        # Daily progress reports (weekly) — foundations A complete, B and erection behind plan
        plan = []
        for offset in range(70, 0, -7):
            day = today - timedelta(days=offset)
            lines = []
            if acts["FDN1"].planned_start <= day:
                lines.append(("FDN1", 8))
            if acts["FDN2"].planned_start <= day:
                lines.append(("FDN2", 4))
            if acts["ERC1"].planned_start <= day:
                lines.append(("ERC1", 110))
            plan.append((day, lines))
        executed = {"FDN1": 0.0, "FDN2": 0.0, "ERC1": 0.0}
        Dpr = self.env["epc.dpr"]
        for index, (day, lines) in enumerate(plan):
            commands = []
            for code, qty in lines:
                remaining = acts[code].planned_qty - executed[code]
                qty = min(qty, remaining)
                if qty <= 0:
                    continue
                executed[code] += qty
                commands.append((0, 0, {"activity_id": acts[code].id, "quantity": qty,
                                        "remarks": "Weekly measured quantity"}))
            if not commands:
                continue
            hindrances = []
            if index == len(plan) - 1:
                hindrances = [
                    (0, 0, {"hindrance_type": "right_of_way", "activity_id": acts["ERC1"].id, "hours_lost": 16,
                            "description": "Erection stopped at loc 47-49: landowner compensation dispute"}),
                    (0, 0, {"hindrance_type": "weather", "activity_id": acts["FDN2"].id, "hours_lost": 6,
                            "description": "Heavy rain, excavation pits flooded"}),
                ]
            dpr = Dpr.create({
                "project_id": project.id,
                "date": day,
                "supervisor_id": user.id,
                "weather": "rain" if hindrances else "clear",
                "line_ids": commands,
                "manpower_ids": [(0, 0, {"trade": "Foundation gang", "count": 42}),
                                 (0, 0, {"trade": "Erection gang", "count": 30})],
                "hindrance_ids": hindrances,
            })
            dpr.action_approve()

        # Today's draft DPR with raw notes for the AI assistant
        Dpr.create({
            "project_id": project.id,
            "date": today,
            "supervisor_id": user.id,
            "notes_raw": (
                "FDN2 concreting done for 3 nos foundations at loc 61, 62, 63.\n"
                "Tower erection section A: 85 MT erected today.\n"
                "Crane breakdown at loc 52, 3 hrs lost.\n"
                "12 riggers, 25 helpers and 4 welders on site."
            ),
        })

        # Engineering documents
        Doc = self.env["epc.document"]
        Doc.create({"project_id": project.id, "doc_number": "TL400-DWG-FDN-001", "title": "Typical tower foundation drawings",
                    "discipline": "engineering", "doc_type": "drawing", "revision": "B", "status": "afc",
                    "planned_afc_date": acts["ENG2"].baseline_finish})
        Doc.create({"project_id": project.id, "doc_number": "TL400-DWG-TWR-001", "title": "Tower structural drawings (DD type)",
                    "discipline": "engineering", "doc_type": "drawing", "revision": "A", "status": "afc",
                    "planned_afc_date": acts["ENG2"].baseline_finish})
        chart = Doc.create({"project_id": project.id, "doc_number": "TL400-CAL-STR-001",
                            "title": "Sag-tension calculation & stringing chart",
                            "discipline": "engineering", "doc_type": "calculation", "revision": "A",
                            "status": "ifa", "planned_afc_date": today - timedelta(days=10)})
        acts["STR1"].document_ids = [(4, chart.id)]
        acts["STR2"].document_ids = [(4, chart.id)]

        # Live purchase orders
        steel_po = self._purchase(steel_vendor, project, [
            (steel, 3000, 62000, acts["ERC1"].baseline_start - timedelta(days=10)),
        ])
        self._receive(steel_po.order_line[0], 1200, today - timedelta(days=20))
        acts["ERC2"].purchase_line_ids = [(4, steel_po.order_line[0].id)]
        conductor_po = self._purchase(conductor_vendor, project, [
            (conductor, 380, 410000, acts["STR1"].baseline_start - timedelta(days=7)),
        ])
        acts["STR1"].purchase_line_ids = [(4, conductor_po.order_line[0].id)]
        conductor_po.order_line[0].epc_required_date = acts["STR1"].baseline_start
        insulator_po = self._purchase(insulator_vendor, project, [
            (insulator, 4200, 3100, acts["STR1"].baseline_start - timedelta(days=20)),
        ])
        acts["STR1"].purchase_line_ids = [(4, insulator_po.order_line[0].id)]

        # Contract review
        review = self.env["epc.contract.review"].create({
            "name": "Demo GCC review — 400 kV line",
            "project_id": project.id,
            "contract_text": DEMO_CONTRACT,
        })
        review.with_context(epc_ai_force_fallback=True).action_analyse()
        review.action_apply_to_project()

        # First RA bill
        bill = self.env["epc.ra.bill"].create({"project_id": project.id, "date": today - timedelta(days=5)})
        bill.action_generate_lines()
        bill.action_submit()
        bill.action_certify()

        self._load_demo_extended(project, acts, user, today, steel_vendor, conductor_vendor, insulator_vendor)

        # Predictions
        engine = self.env["epc.prediction.engine"]
        engine.learn_from_history()
        engine.run_for_projects(project)
        project.with_context(epc_ai_force_fallback=True).action_epc_generate_report()
        return True

    @api.model
    def _load_demo_extended(self, project, acts, user, today, steel_vendor, conductor_vendor, insulator_vendor):
        """Site operations, design (MTO / change orders), bid evaluation and ERP-overlay demo records."""
        env = self.env(context=dict(self.env.context, epc_ai_force_fallback=True))
        project.write({"epc_external_system": "sap", "epc_external_ref": "DEMO-PS-400-01"})
        acts["ERC1"].external_ref = "A2010"
        # crash data for the schedule optimiser (shortest duration, cost per day saved)
        for code, shortest, cost in (("ERC1", 32, 450000), ("ERC2", 32, 450000), ("STR1", 24, 300000),
                                     ("STR2", 24, 300000), ("TC1", 12, 200000), ("FDN2", 38, 350000)):
            acts[code].write({"crash_duration": shortest, "crash_cost_per_day": cost})
        acts["STR2"].external_ref = "A3020"
        now = fields.Datetime.now().replace(minute=0, second=0, microsecond=0)

        # Site, HSE & quality
        permit = env["epc.hse.permit"].create({
            "project_id": project.id, "permit_type": "height", "location": "Tower loc 53 (DD+3)",
            "work_description": "Top hamper and cross-arm erection", "receiver": "Ramesh (erection foreman)",
            "activity_id": acts["ERC1"].id, "valid_from": now, "valid_to": now + timedelta(hours=10)})
        permit.action_ai_precautions()
        permit.action_approve()
        env["epc.hse.incident"].create({
            "project_id": project.id, "incident_type": "near_miss", "location": "Tower loc 49",
            "date": now - timedelta(days=6),
            "description": "Spanner dropped from 30 m during bolt tightening; no one below, area was not barricaded."})
        env["epc.hse.observation"].create({
            "name": "Rigger without hard hat at stub setting", "project_id": project.id,
            "observation_type": "ppe", "severity": "medium", "date": today - timedelta(days=2)})
        passed = env["epc.qa.inspection"].create({"project_id": project.id, "activity_id": acts["FDN1"].id,
                                                  "description": "Foundation loc 12 reinforcement before concreting"})
        passed.action_pass()
        failed = env["epc.qa.inspection"].create({"project_id": project.id, "activity_id": acts["FDN2"].id,
                                                  "description": "Foundation loc 61 chimney after de-shuttering",
                                                  "remarks": "Honeycombing on chimney face, cover less than 50 mm"})
        failed.action_fail()
        failed.ncr_ids.action_ai_suggest()
        env["epc.punch.item"].create({"project_id": project.id, "name": "Earthing strip missing at loc 18",
                                      "category": "A", "discipline": "electrical"})
        env["epc.punch.item"].create({"project_id": project.id, "name": "Danger plate missing at loc 22",
                                      "category": "B"})
        crane = env["epc.plant"].create({"name": "40 T hydraulic crane", "code": "CR-07", "plant_type": "crane",
                                         "ownership": "hired"})
        draft_dpr = env["epc.dpr"].search([("project_id", "=", project.id), ("state", "=", "draft")], limit=1)
        if draft_dpr:
            draft_dpr.plant_ids = [(0, 0, {"plant_id": crane.id, "activity_id": acts["ERC1"].id, "hours_worked": 5,
                                           "hours_idle": 1, "hours_breakdown": 3})]

        # Design: drawing with bill of materials -> MTO, change order
        bom = (
            "TOWER TYPE DD+3  BILL OF MATERIALS\n"
            "1  Galvanised angle ISA 100x100x8   42.5 MT\n"
            "2  Galvanised angle ISA 75x75x6     18 MT\n"
            "3  Hex bolt M16x60 with nut         3600 nos\n"
            "4  Step bolt M16x175                220 nos\n"
        )
        drawing = env["epc.document"].create({
            "project_id": project.id, "doc_number": "TL400-DWG-TWR-DD3", "title": "Tower DD+3 structural drawing",
            "discipline": "engineering", "doc_type": "drawing", "revision": "0", "status": "afc",
            "planned_afc_date": today - timedelta(days=30),
            "drawing_file": base64.b64encode(bom.encode()), "drawing_filename": "TL400-DWG-TWR-DD3_BOM.txt"})
        drawing.action_ai_extract_mto()
        boq_erc = acts["ERC1"].boq_line_id
        for line in drawing.mto_line_ids:
            line.write({"activity_id": acts["ERC2"].id, "vendor_id": steel_vendor.id,
                        "boq_line_id": boq_erc.id if line.uom_name.lower() == "mt" else False,
                        "estimated_rate": 62000 if line.uom_name.lower() == "mt" else 45})
        drawing.mto_line_ids.action_confirm()
        change = env["epc.change.order"].create({
            "project_id": project.id, "title": "Four additional angle towers for river crossing diversion",
            "origin": "client_instruction", "document_id": drawing.id,
            "description": "Client instructed a route diversion near the river crossing. B-01 +4 Nos foundations, "
                           "B-02 +180 MT tower steel. FDN2 +6 days; ERC2 +5 days; STR2 +4 days for the extra spans."})
        change.action_ai_assess()

        # Bid evaluation for the bolts from the MTO
        bolts = drawing.mto_line_ids.filtered(lambda l: l.uom_name.lower() == "nos")
        package = env["epc.bid.package"].browse(bolts.action_create_bid_package()["res_id"])
        package.write({"title": "Bolts and step bolts for DD+3 towers", "required_date": today + timedelta(days=45)})
        bolt_vendor = self._partner("Demo Fasteners Ltd", email="sales@fasteners.example.com", supplier_rank=1)
        offers = [
            (bolt_vendor, "1  Hex bolt M16x60 with nut  3600 nos  @ 42.00  151,200.00\n"
                          "2  Step bolt M16x175  220 nos  @ 95.00  20,900.00\n"
                          "Grand Total 172,100.00\nDelivery: 3 weeks from PO\nPayment terms: 30 days credit\n"
                          "No deviations. Offer validity 45 days."),
            (steel_vendor, "1  Hex bolt M16x60 with nut  3600 nos  @ 39.50  142,200.00\n"
                           "2  Step bolt M16x175  220 nos  @ 99.00  21,780.00\n"
                           "TOTAL 163,980.00\nDelivery 8 weeks\n"
                           "Deviation: hot-dip galvanising to 70 micron instead of 86 micron\n"
                           "Payment terms: 20% advance, balance against delivery"),
        ]
        for vendor, text in offers:
            bid = env["epc.bid"].create({"package_id": package.id, "vendor_id": vendor.id, "offer_text": text})
            bid.action_ai_extract()
        package.action_evaluate()

        # Materials bought in SAP and mirrored here
        Material = env["epc.external.material"]
        Material.create({
            "project_id": project.id, "source_system": "sap", "external_ref": "4500098765/10",
            "description": "OPGW cable 24F", "vendor_id": conductor_vendor.id, "quantity": 62, "uom_name": "km",
            "promised_date": acts["STR2"].baseline_start + timedelta(days=12), "stage": "manufacturing",
            "activity_ids": [(6, 0, acts["STR2"].ids)], "last_sync": fields.Datetime.now()})
        Material.create({
            "project_id": project.id, "source_system": "sap", "external_ref": "4500098766/10",
            "description": "Vibration dampers", "vendor_id": insulator_vendor.id, "quantity": 1800,
            "delivered_qty": 600, "promised_date": acts["STR1"].baseline_start - timedelta(days=15),
            "stage": "shipped", "activity_ids": [(6, 0, acts["STR1"].ids)], "last_sync": fields.Datetime.now()})
        return True

    @api.model
    def _load_history_project(self, client, today, user):
        """A completed project: the engine learns duration overruns from it."""
        project = self.env["project.project"].create({
            "name": "132 kV S/C Line — Phase 1 (Demo, completed)",
            "is_epc": True,
            "epc_segment": "td",
            "epc_contract_ref": "DEMO-TL-132-01",
            "partner_id": client.id,
            "user_id": user.id,
            "date_start": today - timedelta(days=520),
            "date": today - timedelta(days=200),
            "epc_contract_value": 260000000.0,
        })
        rows = [
            ("H-ENG1", "Route survey", "engineering", 20, 2000000, None, 0, 22),
            ("H-ENG2", "Foundation drawings", "engineering", 20, 1500000, None, 0, 24),
            ("H-FDN1", "Foundations package 1", "civil", 40, 9000000, None, 0, 52),
            ("H-FDN2", "Foundations package 2", "civil", 40, 9000000, None, 0, 49),
            ("H-FDN3", "Foundations package 3", "civil", 30, 7000000, None, 0, 38),
            ("H-ERC1", "Erection package 1", "structural", 35, 20000000, None, 0, 41),
            ("H-ERC2", "Erection package 2", "structural", 35, 20000000, None, 0, 39),
            ("H-STR1", "Stringing package 1", "stringing", 25, 12000000, None, 0, 31),
            ("H-STR2", "Stringing package 2", "stringing", 25, 12000000, None, 0, 28),
            ("H-TC1", "Testing & commissioning", "commissioning", 10, 4000000, None, 0, 11),
        ]
        links = [
            ("H-ENG1", "H-ENG2", "FS", 0), ("H-ENG2", "H-FDN1", "FS", 0), ("H-FDN1", "H-FDN2", "SS", 10),
            ("H-FDN2", "H-FDN3", "SS", 10), ("H-FDN1", "H-ERC1", "SS", 25), ("H-FDN2", "H-ERC2", "SS", 25),
            ("H-ERC1", "H-STR1", "SS", 20), ("H-ERC2", "H-STR2", "SS", 20), ("H-STR1", "H-TC1", "FS", 0),
            ("H-STR2", "H-TC1", "FS", 0),
        ]
        acts = self._activities(project, [r[:7] for r in rows], links)
        project.action_epc_compute_schedule()
        project.action_epc_set_baseline()
        actual_days = {r[0]: r[7] for r in rows}
        for code, act in acts.items():
            start = act.planned_start + timedelta(days=3)
            act.write({
                "actual_start": start,
                "actual_finish": start + timedelta(days=actual_days[code] - 1),
                "progress_method": "manual",
                "manual_progress": 100,
                "actual_cost": act.budget * 1.08,
            })
        return project

    @api.model
    def _vendor_history(self, steel_vendor, conductor_vendor, steel, conductor, today):
        """Past deliveries: steel on time, conductor consistently late."""
        for promised_offset, delay in ((200, 0), (150, 2), (110, 0)):
            order = self._purchase(steel_vendor, None, [(steel, 100, 60000, today - timedelta(days=promised_offset))])
            self._receive(order.order_line[0], 100, today - timedelta(days=promised_offset - delay))
        for promised_offset, delay in ((210, 18), (160, 25), (120, 14)):
            order = self._purchase(conductor_vendor, None, [(conductor, 50, 400000, today - timedelta(days=promised_offset))])
            self._receive(order.order_line[0], 50, today - timedelta(days=promised_offset - delay))
        return True
