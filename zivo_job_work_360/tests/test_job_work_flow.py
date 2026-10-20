# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, tagged

from .common import JobWorkCommon


@tagged('post_install', '-at_install', 'job_work')
class TestJobWorkFlow(JobWorkCommon):

    def test_01_full_lifecycle(self):
        """Issue -> receive -> inspect -> rework -> reconcile -> close, brass example of the blueprint."""
        job = self._create_job(qty=92.0, material_qty=100.0)
        self.assertTrue(job.name.startswith('JW/'))
        mat = job.material_ids
        self.assertEqual(mat.scrap_pct, 8.0, "scrap % defaults from the job type")
        self.assertAlmostEqual(mat.std_qty_per_unit, 1.0)
        self.assertEqual(mat.unit_cost, 10.0)

        self._issue_all(job)
        vendor_loc = job.vendor_location_id
        self.assertTrue(vendor_loc.is_subcontract(), "custody location is a subcontracting location")
        self.assertEqual(vendor_loc.location_id, self.company.subcontracting_location_id)
        self.assertEqual(job.state, 'issued')
        self.assertEqual(mat.qty_issued, 100.0)
        self.assertEqual(mat.balance_qty, 100.0)
        self.assertEqual(self._qty_at(self.raw, vendor_loc), 100.0)
        self.assertEqual(job.material_balance_value, 1000.0)

        job.action_vendor_confirm_receipt()
        self.assertEqual(job.state, 'at_vendor')
        job.action_vendor_start()
        self.assertEqual(job.state, 'processing')

        # Partial receipt: 50 kg output, 50 consumed, 4 scrap
        self._receive(job, 50.0, consumed=50.0, scrap=4.0)
        self.assertEqual(job.state, 'qc_pending')
        self.assertEqual(job.qty_received, 50.0)
        self.assertEqual(job.qty_pending_qc, 50.0)
        self.assertEqual(mat.qty_consumed, 50.0)
        self.assertEqual(mat.qty_scrap, 4.0)
        self.assertEqual(mat.balance_qty, 46.0)
        self.assertEqual(self._qty_at(self.raw, vendor_loc), 46.0)
        self.assertEqual(self._qty_at(self.output, self.stock_location), 50.0)

        # Inspection: 48 accepted, 1 rejected, 1 rework -> 96% < 98% tolerance
        qc = self._inspect(job, rejected=1.0, rework=1.0)
        self.assertEqual(qc.result, 'fail')
        self.assertTrue(qc.ncr_id, "an NCR is opened automatically")
        self.assertTrue(qc.rework_job_id)
        rework = qc.rework_job_id
        self.assertEqual(rework.parent_id, job)
        self.assertEqual(rework.state, 'planned')
        self.assertEqual(rework.material_ids.product_id, self.output)
        self.assertEqual(job.qty_accepted, 48.0)
        self.assertEqual(job.state, 'processing')
        self.assertEqual(self._qty_at(self.output, self.stock_location), 49.0, "rejected output is scrapped")
        quality_exc = job.exception_ids.filtered(lambda e: e.exception_type == 'quality')
        self.assertEqual(quality_exc.state, 'open')

        # Final receipt: 42 kg, 42 consumed, 3 scrap, all accepted
        self._receive(job, 42.0, consumed=42.0, scrap=3.0)
        self._inspect(job)
        self.assertEqual(job.qty_accepted, 90.0)
        self.assertEqual(job.state, 'completed', "accepted + rejected + rework covers the planned output")
        self.assertTrue(job.date_completed)
        self.assertEqual(mat.balance_qty, 1.0)

        # Yield: 90 accepted out of 99 kg used (std 1 kg/unit) => 90.9%
        self.assertAlmostEqual(job.actual_yield, 90 / 99 * 100, places=2)

        # Reconciliation is blocked: 1 kg unaccounted (> 0.5% tolerance) and the quality exception
        result = job.action_reconcile()
        self.assertEqual(result['tag'], 'display_notification')
        self.assertEqual(job.state, 'completed')
        self.assertEqual(job.reconciliation_state, 'exception')
        shortage = job.exception_ids.filtered(lambda e: e.exception_type == 'shortage' and e.state == 'open')
        self.assertTrue(shortage)

        # Job worker returns the 1 kg, NCR is closed
        wiz = Form(self.env['zivo.job.work.return.wizard'].with_context(default_job_id=job.id)).save()
        wiz.line_ids.qty = 1.0
        wiz.action_confirm()
        self.assertEqual(mat.qty_returned, 1.0)
        self.assertEqual(mat.balance_qty, 0.0)
        self.assertEqual(self._qty_at(self.raw, vendor_loc), 0.0)
        qc.ncr_id.corrective_action = 'Furnace recalibrated'
        qc.ncr_id.action_close()
        self.assertEqual(quality_exc.state, 'resolved')

        job.action_reconcile()
        self.assertEqual(job.state, 'reconciled')
        self.assertEqual(job.reconciliation_state, 'balanced')
        self.assertEqual(shortage.state, 'resolved')
        self.assertTrue(job.cost_snapshot_actual)
        job.with_user(self.manager).action_close()
        self.assertEqual(job.state, 'closed')

        # Stock integrity
        self.assertEqual(self._qty_at(self.raw, self.stock_location), 10000.0 - 100.0 + 1.0)
        self.assertEqual(self._qty_at(self.output, self.stock_location), 91.0)

        # Costing: actual material = 92 kg consumed * 10, scrap impact = 7 kg * 10
        self.assertAlmostEqual(job.cost_act_material, 920.0)
        self.assertAlmostEqual(job.cost_act_scrap, 70.0)
        # expected for 92 received: 100*(1-0.08)*10 = 920 and scrap 100*0.08*10 = 80
        self.assertAlmostEqual(job.cost_exp_material, 920.0)
        self.assertAlmostEqual(job.cost_exp_scrap, 80.0)

        # Next stage uses the accepted output as material
        action = job.action_create_next_stage()
        self.assertEqual(action['context']['default_product_qty'], 90.0)
        stage2 = self.env['zivo.job.work'].with_context(**action['context']).create({
            'partner_id': self.vendor2.id,
            'job_type_id': self.env.ref('zivo_job_work_360.job_type_plating').id,
        })
        self.assertEqual(stage2.parent_id, job)
        self.assertEqual(stage2.root_id, job)
        self.assertEqual(stage2.stage_sequence, 2)
        self.assertEqual(stage2.material_ids.product_id, self.output)

    def test_02_tolerance_write_off_and_scrap_exception(self):
        job = self._create_job(qty=100.0, material_qty=1000.0)
        job.material_ids.scrap_pct = 0.0
        job.job_type_id.quality_required = False
        self._issue_all(job)
        # 100 output requiring 10 kg each; vendor consumed 960, scrapped 38 -> 2 kg left (0.2%)
        self._receive(job, 100.0, consumed=960.0, scrap=38.0)
        self.assertEqual(job.state, 'completed', "no inspection required: received output is accepted")
        self.assertEqual(job.qty_accepted, 100.0)
        mat = job.material_ids
        self.assertEqual(mat.balance_qty, 2.0)
        job.action_reconcile()
        excess = job.exception_ids.filtered(lambda e: e.exception_type == 'excess_scrap')
        self.assertTrue(excess, "3.8% scrap with 0% standard and 2% tolerance is excess scrap")
        self.assertEqual(mat.qty_loss, 2.0, "within shortage tolerance (0.5%) -> written off")
        self.assertEqual(mat.balance_qty, 0.0)
        self.assertEqual(job.state, 'completed', "excess scrap is high severity and blocks")
        excess.with_user(self.manager).action_waive()
        job.action_reconcile()
        self.assertEqual(job.state, 'reconciled')

    def test_03_loss_approval_and_rights(self):
        job = self._create_job(qty=10.0, material_qty=10.0)
        self._issue_all(job)
        wiz_form = Form(self.env['zivo.job.work.loss.wizard'].with_user(self.user).with_context(
            default_job_id=job.id))
        wiz_form.qty = 3.0
        wiz_form.note = 'Damaged in transit'
        wiz = wiz_form.save()
        wiz.action_confirm()
        loss = job.loss_ids
        self.assertEqual(loss.state, 'to_approve', "non-managers only request losses")
        with self.assertRaises(AccessError):
            loss.with_user(self.user).action_approve()
        loss.with_user(self.manager).action_approve()
        self.assertEqual(loss.state, 'approved')
        self.assertEqual(job.material_ids.qty_loss, 3.0)
        self.assertEqual(job.material_ids.balance_qty, 7.0)
        self.assertEqual(loss.value, 30.0)
        with self.assertRaises(UserError):
            self.env['zivo.job.work.loss'].create({
                'job_id': job.id, 'material_id': job.material_ids.id, 'qty': 50.0, 'note': 'x',
            })._jw_post()

    def test_04_material_request(self):
        job = self._create_job(qty=10.0, material_qty=10.0)
        self._issue_all(job)
        req = self.env['zivo.job.work.request'].create({
            'job_id': job.id, 'material_id': job.material_ids.id, 'product_id': self.raw.id,
            'qty': 2.0, 'reason': 'Setup scrap higher than planned',
        })
        exc = job.exception_ids.filtered(lambda e: e.exception_type == 'extra_material' and e.state == 'open')
        self.assertTrue(exc)
        with self.assertRaises(AccessError):
            req.with_user(self.user).action_approve()
        req.with_user(self.manager).action_approve()
        self.assertEqual(req.state, 'approved')
        self.assertEqual(job.material_ids.planned_qty, 12.0)
        self.assertTrue(req.picking_id)
        self.assertEqual(req.picking_id.move_ids.product_uom_qty, 2.0)
        self.assertEqual(exc.state, 'resolved')
        req.picking_id.button_validate()
        self.assertEqual(job.material_ids.qty_issued, 12.0)

    def test_05_guards(self):
        job = self._create_job()
        with self.assertRaises(UserError):
            job.action_issue_material()  # not planned
        job.action_plan()
        with self.assertRaises(UserError):
            job.action_receive()  # nothing issued yet
        self._issue_all_after_plan(job)
        with self.assertRaises(UserError):
            job.action_cancel()  # material at vendor
        with self.assertRaises(UserError):
            job.write({'partner_id': self.vendor2.id})
        with self.assertRaises(UserError):
            job.unlink()
        with self.assertRaises(UserError):
            job.material_ids.unlink()
        # cannot consume more than at vendor
        with self.assertRaises(UserError):
            self._receive(job, 10.0, consumed=500.0)
        with self.assertRaises(ValidationError):
            self._create_job(qty=10).write({'date_expected': fields.Datetime.now() - timedelta(days=30)})
        draft = self._create_job()
        draft.action_cancel()
        self.assertEqual(draft.state, 'cancel')
        draft.action_draft()
        draft.unlink()

    def _issue_all_after_plan(self, job):
        job.action_issue_material()
        job.picking_ids.button_validate()

    def test_06_partial_issue_backorder_and_native_return(self):
        job = self._create_job(qty=10.0, material_qty=100.0)
        job.action_plan()
        job.action_issue_material()
        self.assertEqual(job.state, 'ready')
        picking = job.picking_ids
        picking.move_ids.quantity = 60.0
        picking.move_ids.picked = True
        Form.from_action(self.env, picking.button_validate()).save().process()
        self.assertEqual(job.material_ids.qty_issued, 60.0)
        self.assertEqual(job.state, 'issued')
        backorder = job.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))
        self.assertTrue(backorder)
        self.assertEqual(backorder.move_ids.jw_move_type, 'issue')
        self.assertEqual(job.material_ids.qty_in_transit, 40.0)
        # native return of 10 kg from the issue transfer counts as returned material
        return_wiz = Form(self.env['stock.return.picking'].with_context(
            active_id=picking.id, active_ids=picking.ids, active_model='stock.picking')).save()
        return_wiz.product_return_moves.quantity = 10.0
        res = return_wiz.action_create_returns()
        ret = self.env['stock.picking'].browse(res['res_id'])
        self.assertEqual(ret.move_ids.jw_move_type, 'return')
        ret.button_validate()
        self.assertEqual(job.material_ids.qty_returned, 10.0)
        self.assertEqual(job.material_ids.balance_qty, 50.0)

    def test_07_lot_tracking(self):
        raw_lot = self.env['product.product'].create({
            'name': 'Steel Shaft', 'type': 'consu', 'is_storable': True, 'tracking': 'lot', 'standard_price': 5,
        })
        out_lot = self.env['product.product'].create({
            'name': 'Hardened Shaft', 'type': 'consu', 'is_storable': True, 'tracking': 'lot', 'standard_price': 9,
        })
        lot = self.env['stock.lot'].create({'name': 'HEAT-001', 'product_id': raw_lot.id})
        self.env['stock.quant']._update_available_quantity(raw_lot, self.stock_location, 20.0, lot_id=lot)
        self.job_type.quality_required = False
        job = self._create_job(qty=20.0, material_qty=20.0, product=out_lot, raw=raw_lot)
        job.material_ids.scrap_pct = 0.0
        self._issue_all(job)
        self.assertEqual(job.material_ids.qty_issued, 20.0)
        action = self._receive(job, 20.0, consumed=20.0)
        self.assertEqual(action['res_model'], 'stock.picking', "tracked output opens the receipt")
        picking = self.env['stock.picking'].browse(action['res_id'])
        production = job.production_ids
        self.assertEqual(production.state, 'waiting')
        consume = job.material_ids.move_ids.filtered(lambda m: m.jw_move_type == 'consume')
        self.assertEqual(consume.move_line_ids.lot_id, lot, "consumption uses the lot at the job worker")
        picking.move_ids.move_line_ids.write({'lot_name': 'HT-OUT-1', 'quantity': 20.0})
        picking.move_ids.picked = True
        picking.button_validate()
        self.assertEqual(production.state, 'done')
        self.assertEqual(job.qty_accepted, 20.0)
        self.assertEqual(job.state, 'completed')

    def test_08_returned_scrap_by_product(self):
        scrap_product = self.env['product.product'].create({
            'name': 'Brass Turnings', 'type': 'consu', 'is_storable': True, 'uom_id': self.uom_kg.id,
        })
        self.job_type.write({'scrap_product_id': scrap_product.id, 'quality_required': False})
        job = self._create_job(qty=92.0, material_qty=100.0)
        self.assertEqual(job.material_ids.scrap_product_id, scrap_product)
        self._issue_all(job)
        self._receive(job, 92.0, consumed=92.0, scrap=8.0, scrap_returned=True)
        self.assertEqual(self._qty_at(scrap_product, self.stock_location), 8.0)
        self.assertEqual(job.material_ids.balance_qty, 0.0)
        self.assertEqual(job.state, 'completed')
        job.action_reconcile()
        self.assertEqual(job.state, 'reconciled')

    def test_09_overdue_cron_and_scorecard(self):
        job = self._create_job(qty=10.0, material_qty=10.0)
        job.job_type_id.quality_required = False
        self._issue_all(job)
        job.write({'date_planned_start': fields.Datetime.now() - timedelta(days=10),
                   'date_expected': fields.Datetime.now() - timedelta(days=2)})
        self.assertTrue(job.is_overdue)
        self.assertIn(job, self.env['zivo.job.work'].search([('is_overdue', '=', True)]))
        self.env['zivo.job.work']._cron_check_exceptions()
        self.assertTrue(job.exception_ids.filtered(lambda e: e.exception_type == 'overdue' and e.state == 'open'))
        self._receive(job, 10.0, consumed=9.0)
        self.assertEqual(job.state, 'completed')
        self.env['zivo.job.work']._cron_check_exceptions()
        self.assertFalse(job.exception_ids.filtered(lambda e: e.exception_type == 'overdue' and e.state == 'open'),
                         "overdue exception resolves once the job is completed")
        scores = self.env['zivo.job.work.vendor.score'].generate_scorecards(self.company)
        score = scores.filtered(lambda s: s.partner_id == self.vendor)
        self.assertEqual(score.job_count, 1)
        self.assertEqual(score.on_time_rate, 0.0)
        self.assertEqual(score.quality_rate, 100.0)
        self.assertTrue(score.grade)
        self.assertEqual(self.vendor.jw_grade, score.grade)
        # regenerate keeps one latest record per vendor and date
        self.env['zivo.job.work.vendor.score'].generate_scorecards(self.company)
        self.assertEqual(self.env['zivo.job.work.vendor.score'].search_count(
            [('partner_id', '=', self.vendor.id), ('is_latest', '=', True)]), 1)
        data = self.env['zivo.job.work'].get_jw_dashboard_data()
        self.assertIn('kpis', data)
        self.assertGreaterEqual(data['kpis']['open_jobs'], 1)

    def test_10_hold_resume_and_short_close(self):
        job = self._create_job(qty=100.0, material_qty=100.0)
        job.job_type_id.quality_required = False
        self._issue_all(job)
        job.action_hold()
        self.assertEqual(job.state, 'hold')
        job.action_resume()
        self.assertEqual(job.state, 'issued')
        self._receive(job, 40.0, consumed=40.0)
        self.assertEqual(job.state, 'processing')
        job.action_mark_completed()
        self.assertEqual(job.state, 'completed')
        self.assertEqual(job.material_ids.balance_qty, 60.0)
        wiz = Form(self.env['zivo.job.work.return.wizard'].with_context(default_job_id=job.id)).save()
        wiz.line_ids.qty = job.material_ids.balance_qty
        wiz.action_confirm()
        job.action_reconcile()
        self.assertEqual(job.state, 'reconciled')

    def test_11_vendor_reject_and_viewer_access(self):
        job = self._create_job()
        job.action_plan()
        job.action_vendor_reject('No furnace capacity')
        self.assertEqual(job.vendor_state, 'rejected')
        with self.assertRaises(UserError):
            job.action_issue_material()
        job.action_reset_vendor_state()
        job.action_vendor_accept()
        self.assertEqual(job.vendor_state, 'accepted')
        # management viewer can read everything but not write
        job_v = job.with_user(self.viewer)
        job_v.read(list(job_v.fields_get()))
        job_v.material_ids.read()
        with self.assertRaises(AccessError):
            job_v.write({'client_ref': 'x'})
        self.env['zivo.job.work'].with_user(self.viewer).get_jw_dashboard_data()
        self.vendor.with_user(self.viewer).read(['jw_job_count', 'jw_grade'])

    def test_12_multi_company_isolation(self):
        company_b = self.env['res.company'].create({'name': 'Company B'})
        self.assertTrue(company_b.subcontracting_location_id)
        wh_b = self.env['stock.warehouse'].search([('company_id', '=', company_b.id)], limit=1)
        job_b = self.env['zivo.job.work'].with_company(company_b).create({
            'company_id': company_b.id,
            'warehouse_id': wh_b.id,
            'partner_id': self.vendor.id,
            'job_type_id': self.job_type.id,
            'product_id': self.output.id,
            'product_qty': 1.0,
            'material_ids': [Command.create({'product_id': self.raw.id, 'planned_qty': 1.0})],
        })
        job_b.with_company(company_b).action_plan()
        self.assertEqual(job_b.vendor_location_id.company_id, company_b)
        self.assertNotIn(job_b, self.env['zivo.job.work'].with_user(self.user).search([]))

    def test_13_mrp_link(self):
        bom_product = self.output
        mo_form = Form(self.env['mrp.production'])
        mo_form.product_id = bom_product
        mo_form.product_qty = 5.0
        mo = mo_form.save()
        mo.action_confirm()
        action = mo.action_create_job_work()
        job = self.env['zivo.job.work'].with_context(**action['context']).create({
            'partner_id': self.vendor.id, 'job_type_id': self.job_type.id,
        })
        self.assertEqual(job.mrp_production_id, mo)
        self.assertEqual(mo.jw_order_count, 1)
        self.assertEqual(job.material_ids.planned_qty, 5.0)

    def test_14_reports_render(self):
        job = self._create_job(qty=10.0, material_qty=10.0)
        job.cost_line_ids = [Command.create({'cost_type': 'freight', 'name': 'Truck', 'expected_amount': 10,
                                             'actual_amount': 12})]
        self._issue_all(job)
        Report = self.env['ir.actions.report']
        for xmlid in ('zivo_job_work_360.action_report_job_work',
                      'zivo_job_work_360.action_report_job_work_reconciliation'):
            html = Report._render_qweb_html(xmlid, job.ids)[0]
            self.assertIn(job.name.encode(), html)

    def test_15_hold_while_material_is_dispatched(self):
        job = self._create_job(qty=10.0, material_qty=10.0)
        job.job_type_id.quality_required = False
        job.action_plan()
        job.action_issue_material()
        job.action_hold()
        job.picking_ids.button_validate()
        self.assertEqual(job.state, 'hold')
        job.action_resume()
        self.assertEqual(job.state, 'issued', "dispatch validated during hold is taken into account")
        job.action_hold()
        with self.assertRaises(UserError):
            job.action_receive()
        job.action_resume()
        self._receive(job, 10.0, consumed=10.0)
        self.assertEqual(job.state, 'completed')
        self.assertEqual(job._jw_fmt_qty(9850.0), '9,850')
