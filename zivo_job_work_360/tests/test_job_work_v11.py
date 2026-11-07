# -*- coding: utf-8 -*-
"""Version 1.1: reason codes & approvals, by-products, aging, customer-owned material, chains, labour."""
import re
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, HttpCase, new_test_user, tagged

from .common import JobWorkCommon


@tagged('post_install', '-at_install', 'job_work', 'job_work_v11')
class TestJobWorkV11(JobWorkCommon):

    def _shortage_job(self):
        """Completed job with 5 kg unaccounted at the job worker (above tolerance)."""
        self.job_type.quality_required = False
        job = self._create_job(qty=10.0, material_qty=100.0)
        job.material_ids.scrap_pct = 0.0
        self._issue_all(job)
        self._receive(job, 10.0, consumed=95.0)
        job.action_reconcile()
        shortage = job.exception_ids.filtered(lambda e: e.exception_type == 'shortage' and e.state == 'open')
        self.assertTrue(shortage)
        return job, shortage

    # ------------------------------------------------------------------ reason codes & approvals
    def test_01_write_off_with_approval(self):
        job, shortage = self._shortage_job()
        self.assertTrue(shortage.can_write_off)
        self._resolve(shortage, 'zivo_job_work_360.reason_lost_at_vendor', user=self.user, write_off=True)
        self.assertEqual(shortage.state, 'to_approve')
        loss = shortage.loss_id
        self.assertEqual(loss.state, 'to_approve')
        self.assertEqual(loss.qty, 5.0)
        self.assertTrue(loss.recover_from_vendor, "defaults from the reason code")
        self.assertEqual(loss.reason_id, self.env.ref('zivo_job_work_360.reason_lost_at_vendor'))
        job.action_reconcile()
        self.assertEqual(job.state, 'completed', "a resolution awaiting approval still blocks")
        with self.assertRaises(AccessError):
            shortage.with_user(self.user).action_approve_resolution()
        shortage.with_user(self.manager).action_approve_resolution()
        self.assertEqual(shortage.state, 'resolved')
        self.assertEqual(loss.state, 'approved')
        self.assertEqual(job.material_ids.balance_qty, 0.0)
        job.action_reconcile()
        self.assertEqual(job.state, 'reconciled')

    def test_02_reject_resolution_reopens(self):
        job, shortage = self._shortage_job()
        self._resolve(shortage, 'zivo_job_work_360.reason_lost_at_vendor', user=self.user, write_off=True, qty=2.0)
        loss = shortage.loss_id
        shortage.with_user(self.manager).action_reject_resolution()
        self.assertEqual(shortage.state, 'open')
        self.assertEqual(loss.state, 'rejected')
        self.assertEqual(job.material_ids.balance_qty, 5.0)
        # rejecting the loss itself also re-opens
        self._resolve(shortage, 'zivo_job_work_360.reason_weighing_difference', user=self.user, write_off=True)
        shortage.loss_id.with_user(self.manager).action_reject()
        self.assertEqual(shortage.state, 'open')

    def test_03_direct_resolution_rules(self):
        job, shortage = self._shortage_job()
        # a manager writes off directly
        self._resolve(shortage, 'zivo_job_work_360.reason_weighing_difference', user=self.manager, write_off=True)
        self.assertEqual(shortage.state, 'resolved')
        self.assertEqual(shortage.loss_id.state, 'approved')
        self.assertEqual(shortage.resolved_by_id, self.manager)
        # medium exception + reason without approval: a user resolves directly
        exc = self.env['zivo.job.work.exception'].create({
            'job_id': job.id, 'exception_type': 'material_variance', 'severity': 'medium', 'description': 'x'})
        self._resolve(exc, 'zivo_job_work_360.reason_setup_scrap', user=self.user)
        self.assertEqual(exc.state, 'resolved')
        self.assertEqual(exc.reason_id.code, 'SETUP')
        # high severity: approval required by company setting, even for a reason without approval
        # (an administrator of another company the requester cannot read must not break the submission)
        other_company = self.env['res.company'].create({'name': 'Other Plant'})
        new_test_user(self.env, login='jw_other_mgr', company_id=other_company.id,
                      company_ids=[Command.set(other_company.ids)],
                      groups='zivo_job_work_360.group_job_work_manager')
        high = self.env['zivo.job.work.exception'].create({
            'job_id': job.id, 'exception_type': 'excess_scrap', 'severity': 'high', 'description': 'x'})
        self._resolve(high, 'zivo_job_work_360.reason_setup_scrap', user=self.user)
        self.assertEqual(high.state, 'to_approve')
        high.with_user(self.manager).action_approve_resolution()
        self.assertEqual(high.state, 'resolved')
        self.company.jw_resolution_approval = False
        high2 = self.env['zivo.job.work.exception'].create({
            'job_id': job.id, 'exception_type': 'excess_scrap', 'severity': 'high', 'description': 'x'})
        self._resolve(high2, 'zivo_job_work_360.reason_setup_scrap', user=self.user)
        self.assertEqual(high2.state, 'resolved')
        # reason codes must match the exception type
        exc3 = self.env['zivo.job.work.exception'].create({
            'job_id': job.id, 'exception_type': 'quality', 'severity': 'medium', 'description': 'x'})
        wizard = self.env['zivo.job.work.exception.resolve.wizard'].with_user(self.user).create({
            'exception_id': exc3.id, 'reason_id': self.env.ref('zivo_job_work_360.reason_setup_scrap').id,
            'resolution': 'x'})
        with self.assertRaises(UserError):
            wizard.action_confirm()
        # users cannot waive
        with self.assertRaises(AccessError):
            exc3.with_user(self.user).action_waive()
        self._resolve(exc3, 'zivo_job_work_360.reason_material_defect', user=self.manager, mode='waive')
        self.assertEqual(exc3.state, 'waived')
        with self.assertRaises(AccessError):
            exc3.with_user(self.user).action_reopen()

    def test_04_write_off_limits(self):
        job, shortage = self._shortage_job()
        action = shortage.action_resolve()
        wiz = Form(self.env['zivo.job.work.exception.resolve.wizard'].with_context(action['context']))
        wiz.reason_id = self.env.ref('zivo_job_work_360.reason_weighing_difference')
        wiz.resolution = 'too much'
        wiz.write_off = True
        wiz.write_off_qty = 50.0
        wizard = wiz.save()
        with self.assertRaises(UserError):
            wizard.action_confirm()
        self.assertEqual(shortage.state, 'open')

    # ------------------------------------------------------------------ by-products
    def test_05_byproducts(self):
        offcut = self.env['product.product'].create({'name': 'Brass Offcut', 'type': 'consu', 'is_storable': True,
                                                     'uom_id': self.uom_kg.id})
        tracked = self.env['product.product'].create({'name': 'Tracked Bin', 'type': 'consu', 'is_storable': True,
                                                      'tracking': 'lot'})
        self.job_type.write({'quality_required': False, 'byproduct_product_ids': [Command.set(offcut.ids)]})
        job = self._create_job(qty=92.0, material_qty=100.0)
        self._issue_all(job)
        wizard = Form(self.env['zivo.job.work.receive.wizard'].with_context(default_job_id=job.id)).save()
        self.assertEqual(wizard.byproduct_line_ids.product_id, offcut)
        wizard.line_ids.write({'qty_consumed': 92.0, 'qty_scrap': 8.0})
        wizard.byproduct_line_ids.qty = 6.5
        wizard.action_confirm()
        self.assertEqual(self._qty_at(offcut, self.stock_location), 6.5)
        self.assertEqual(job.byproduct_count, 1)
        self.assertEqual(job.byproduct_move_ids.jw_move_type, 'byproduct')
        self.assertEqual(job.state, 'completed')
        # tracked by-products are refused
        job2 = self._create_job(qty=10.0, material_qty=10.0)
        self._issue_all(job2)
        wiz2 = Form(self.env['zivo.job.work.receive.wizard'].with_context(default_job_id=job2.id)).save()
        wiz2.byproduct_line_ids = [Command.create({'product_id': tracked.id, 'qty': 1.0})]
        with self.assertRaises(UserError):
            wiz2.action_confirm()

    # ------------------------------------------------------------------ aging
    def test_06_fifo_aging_and_alerts(self):
        self.job_type.quality_required = False
        job = self._create_job(qty=10.0, material_qty=100.0)
        job.material_ids.scrap_pct = 0.0
        self._issue_all(job)
        mat = job.material_ids
        mat.move_ids.filtered(lambda m: m.jw_move_type == 'issue').write(
            {'date': fields.Datetime.now() - timedelta(days=100)})
        # second dispatch, recent
        req = self.env['zivo.job.work.request'].create({'job_id': job.id, 'material_id': mat.id,
                                                        'product_id': self.raw.id, 'qty': 50.0, 'reason': 'more'})
        req.with_user(self.manager).action_approve()
        req.picking_id.button_validate()
        recent = req.picking_id.move_ids
        recent.write({'date': fields.Datetime.now() - timedelta(days=10)})
        self.assertEqual(mat.qty_issued, 150.0)
        self.assertEqual(mat.age_days, 100)
        self.assertEqual(mat.aging_bucket, '4. > 90 days')
        self.env['zivo.job.work.material']._cron_refresh_aging()
        aging_exc = job.exception_ids.filtered(lambda e: e.exception_type == 'aging' and e.state == 'open')
        self.assertTrue(aging_exc)
        # consuming 120 kg (FIFO) leaves only part of the recent dispatch
        self._receive(job, 5.0, consumed=120.0)
        self.assertEqual(mat.balance_qty, 30.0)
        self.assertEqual(mat.age_days, 10)
        self.assertEqual(mat.aging_bucket, '1. 0-30 days')
        self.env['zivo.job.work.material']._cron_refresh_aging()
        self.assertEqual(aging_exc.state, 'resolved')
        data = self.env['zivo.job.work'].get_jw_dashboard_data()
        self.assertEqual(len(data['aging']), 4)
        self.assertAlmostEqual(sum(r['value'] for r in data['aging']), data['kpis']['value_at_vendors_raw'])
        with self.assertRaises(ValidationError):
            self.company.write({'jw_aging_days_1': 70, 'jw_aging_days_2': 60})

    # ------------------------------------------------------------------ customer-owned material
    def test_07_customer_owned_material(self):
        customer = self.env['res.partner'].create({'name': 'Auto OEM Ltd', 'is_company': True})
        self.env['stock.quant']._update_available_quantity(self.raw, self.stock_location, 80.0, owner_id=customer)
        company_stock_before = self._qty_at(self.raw, self.stock_location, owner=self.env['res.partner'])
        self.job_type.quality_required = True
        job = self._create_job(qty=40.0, material_qty=50.0)
        job.write({'principal_id': customer.id})
        job.material_ids.scrap_pct = 0.0
        self.assertTrue(job.is_customer_owned)
        self.assertEqual(job.jw_owner_id, customer)
        self._issue_all(job)
        vendor_loc = job.vendor_location_id
        self.assertEqual(self._qty_at(self.raw, vendor_loc, owner=customer), 50.0)
        self.assertEqual(self._qty_at(self.raw, self.stock_location, owner=customer), 30.0)
        self.assertEqual(self._qty_at(self.raw, self.stock_location, owner=self.env['res.partner']),
                         company_stock_before, "company-owned stock is untouched")
        with self.assertRaises(UserError):
            job.write({'principal_id': self.company.partner_id.id})
        self._receive(job, 40.0, consumed=45.0, scrap=1.0)
        self.assertEqual(self._qty_at(self.output, self.stock_location, owner=customer), 40.0)
        self._inspect(job, rejected=2.0)
        self.assertEqual(self._qty_at(self.output, self.stock_location, owner=customer), 38.0)
        wiz = Form(self.env['zivo.job.work.return.wizard'].with_context(default_job_id=job.id)).save()
        wiz.line_ids.qty = 4.0
        wiz.action_confirm()
        self.assertEqual(self._qty_at(self.raw, self.stock_location, owner=customer), 34.0)
        self.assertEqual(self._qty_at(self.raw, vendor_loc, owner=customer), 0.0)
        self.assertEqual(job.cost_act_material, 0.0, "customer material is not part of our cost")
        self.assertEqual(job.cost_exp_material, 0.0)
        self.assertGreater(job.material_balance_value + job.material_issued_value, 0.0,
                           "custody value is still reported")
        mat_custody = self.env['zivo.job.work.material'].search([('is_customer_owned', '=', True)])
        self.assertIn(job.material_ids, mat_custody)

    def test_08_customer_owned_insufficient_stock(self):
        customer = self.env['res.partner'].create({'name': 'Owner Two', 'is_company': True})
        self.env['stock.quant']._update_available_quantity(self.raw, self.stock_location, 5.0, owner_id=customer)
        job = self._create_job(qty=10.0, material_qty=10.0)
        job.principal_id = customer
        job.action_plan()
        job.action_issue_material()
        picking = job.picking_ids
        self.assertEqual(picking.move_ids.quantity, 5.0, "only the customer's own stock is reserved")
        picking.action_assign()
        self.assertEqual(picking.move_ids.quantity, 5.0)

    # ------------------------------------------------------------------ chain traceability
    def test_09_chain_traceability(self):
        self.job_type.quality_required = True
        stage1 = self._create_job(qty=20.0, material_qty=20.0)
        stage1.material_ids.scrap_pct = 0.0
        self._issue_all(stage1)
        self._receive(stage1, 20.0, consumed=20.0)
        self._inspect(stage1, rejected=1.0, rework=1.0)
        ctx = stage1.action_create_next_stage()['context']
        stage2 = self.env['zivo.job.work'].with_context(**ctx).create({
            'partner_id': self.vendor2.id, 'job_type_id': self.env.ref('zivo_job_work_360.job_type_plating').id,
            'rate': 2.0})
        stage2.job_type_id.quality_required = False
        stage2.material_ids.scrap_pct = 0.0
        self._issue_all(stage2)
        self._receive(stage2, 18.0, consumed=18.0)
        self.assertEqual(stage2.state, 'completed')
        for job in (stage1, stage2):
            self.assertEqual(job.chain_count, 3, "stage 1, rework job and stage 2")
        self.assertEqual(stage2.chain_final_qty, 18.0)
        self.assertEqual(stage1.chain_rejected_qty, 1.0)
        self.assertAlmostEqual(stage2.chain_cost, sum(stage2.chain_job_ids.mapped('cost_act_total')))
        self.assertTrue(stage2.chain_cost_per_unit)
        html = self.env['ir.actions.report']._render_qweb_html(
            'zivo_job_work_360.action_report_job_work_chain', stage2.ids)[0]
        self.assertIn(stage1.name.encode(), html)
        self.assertIn(stage2.name.encode(), html)

    # ------------------------------------------------------------------ labour
    def test_10_labour_hours(self):
        self.company.write({'jw_labour_enabled': True, 'jw_default_labour_rate': 100.0})
        self.vendor.jw_labour_rate = 150.0
        self.job_type.quality_required = False
        job = self._create_job(qty=10.0, material_qty=10.0, rate=60.0)
        job.material_ids.scrap_pct = 0.0
        self._issue_all(job)
        lab = self.env['zivo.job.work.labour'].with_user(self.user).create({
            'job_id': job.id, 'operation': 'Plating', 'hours': 3.0})
        self.assertEqual(lab.hourly_rate, 150.0, "job worker rate first")
        self.assertEqual(lab.cost, 450.0)
        lab.action_submit()
        self.assertEqual(job.labour_hours_pending, 3.0)
        with self.assertRaises(AccessError):
            lab.with_user(self.user).action_approve()
        lab.with_user(self.manager).action_approve()
        self._receive(job, 10.0, consumed=10.0)
        self.assertEqual(job.labour_hours, 3.0)
        self.assertAlmostEqual(job.labour_cost_per_unit, 45.0)
        self.assertAlmostEqual(job.labour_hours_per_unit, 0.3)
        self.assertAlmostEqual(job.labour_margin_pct, 25.0)
        with self.assertRaises(UserError):
            lab.write({'hours': 10.0})
        with self.assertRaises(UserError):
            lab.unlink()
        with self.assertRaises(ValidationError):
            self.env['zivo.job.work.labour'].create({'job_id': job.id, 'operation': 'x', 'hours': 0})
        with self.assertRaises(ValidationError):
            self.env['zivo.job.work.labour'].create({
                'job_id': job.id, 'operation': 'x', 'hours': 1,
                'date': fields.Date.today() + timedelta(days=3)})
        rejected = self.env['zivo.job.work.labour'].create({'job_id': job.id, 'operation': 'Polish', 'hours': 2})
        rejected.action_submit()
        rejected.with_user(self.manager).action_reject('Not in scope')
        self.assertEqual(rejected.state, 'rejected')
        self.assertEqual(job.labour_hours, 3.0)
        data = self.env['zivo.job.work'].get_jw_dashboard_data()
        self.assertIn('Labour hours', [a['label'] for a in data['approvals']])


@tagged('post_install', '-at_install', 'job_work', 'job_work_v11')
class TestJobWorkV11Portal(HttpCase, JobWorkCommon):

    def test_01_portal_labour(self):
        portal = new_test_user(self.env, login='lab_portal', password='lab_portal_pwd',
                               groups='base.group_portal', name='Lab Portal')
        portal.partner_id.parent_id = self.vendor
        self.company.jw_labour_enabled = False  # demo data enables it
        job = self._create_job(qty=10.0, material_qty=10.0)
        self._issue_all(job)
        url = '/my/job-works/%s' % job.id
        self.authenticate('lab_portal', 'lab_portal_pwd')
        res = self.url_open(url)
        self.assertNotIn('Log Hours', res.text, "hidden while labour reporting is disabled")
        token = re.search(r'name="csrf_token" value="([^"]+)"', res.text).group(1)
        res = self.url_open(url + '/labour', data={'csrf_token': token, 'hours': '2', 'operation': 'Load'})
        self.assertIn('not enabled', res.text)
        self.company.jw_labour_enabled = True
        res = self.url_open(url)
        self.assertIn('Log Hours', res.text)
        self.url_open(url + '/labour', data={'csrf_token': token, 'hours': '2.5', 'operation': 'Loading',
                                             'worker_name': 'Team A', 'date': fields.Date.today().isoformat()})
        lab = job.labour_ids
        self.assertEqual(lab.hours, 2.5)
        self.assertEqual(lab.state, 'submitted')
        self.assertEqual(lab.source, 'portal')
        res = self.url_open(url + '/labour', data={'csrf_token': token, 'hours': '-1', 'operation': 'x'})
        self.assertIn('cannot be negative', res.text)
        res = self.url_open(url + '/labour', data={'csrf_token': token, 'hours': '1', 'operation': ''})
        self.assertIn('Enter the operation', res.text)
        res = self.url_open(url + '/labour', data={'csrf_token': token, 'hours': '1', 'operation': 'x',
                                                   'date': 'not-a-date'})
        self.assertIn('valid date', res.text)
        self.assertEqual(len(job.labour_ids), 1)
        res = self.url_open(url)
        self.assertIn('Your labour hours', res.text)
        self.assertIn('Loading', res.text)
