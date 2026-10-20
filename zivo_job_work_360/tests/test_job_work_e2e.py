# -*- coding: utf-8 -*-
"""End-to-end, smoke and edge-case tests.

* browser tours driving the complete lifecycle as a non-admin Job Work administrator and the vendor portal
* every window action / view / report / menu of the Job Work modules opened with each role
* edge cases: units of measure, serial numbers, multi-step warehouses, double submission, chains, volume
"""
import time
from datetime import timedelta

from lxml import etree

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, HttpCase, new_test_user, tagged
from odoo.tools import mute_logger
from odoo.tools.safe_eval import safe_eval

from .common import JobWorkCommon

JW_MODULES = ('zivo_job_work_360', 'zivo_job_work_360_india')


def _spec_from_arch(model, arch):
    """Build a web_read specification from a view arch, like the web client does."""
    doc = etree.fromstring(arch)
    spec = {}
    for node in doc.iter('field'):
        # skip fields of embedded x2many sub-views: they belong to another model
        parent = node.getparent()
        nested = False
        while parent is not None and parent is not doc:
            if parent.tag == 'field':
                nested = True
                break
            parent = parent.getparent()
        if nested:
            continue
        name = node.get('name')
        field = model._fields.get(name)
        if not field:
            continue
        if field.type in ('many2one', 'one2many', 'many2many'):
            spec[name] = {'fields': {'display_name': {}}}
        else:
            spec[name] = {}
    return spec


@tagged('post_install', '-at_install', 'job_work', 'job_work_e2e')
class TestJobWorkE2EBrowser(HttpCase, JobWorkCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.e2e_vendor = cls.env['res.partner'].create({'name': 'E2E Forge', 'is_company': True,
                                                        'jw_is_job_worker': True})
        cls.e2e_type = cls.env['zivo.job.work.type'].create({
            'name': 'E2E Coating', 'code': 'E2E', 'quality_required': True, 'expected_yield': 98.0,
            'expected_scrap_pct': 0.0, 'lead_days': 4})
        cls.e2e_raw = cls.env['product.product'].create({'name': 'E2E Raw', 'type': 'consu', 'is_storable': True,
                                                         'standard_price': 3.0})
        cls.e2e_out = cls.env['product.product'].create({'name': 'E2E Output', 'type': 'consu',
                                                         'is_storable': True, 'standard_price': 9.0})
        cls.env['stock.quant']._update_available_quantity(cls.e2e_raw, cls.stock_location, 500.0)
        cls.manager.write({'password': 'jw_manager_pwd'})

    def test_01_full_lifecycle_in_browser(self):
        """A non-admin Job Work administrator runs the complete lifecycle through the web client."""
        self.start_tour('/odoo/action-zivo_job_work_360.action_zivo_job_work', 'zivo_e2e_create_issue',
                        login='jw_manager', password='jw_manager_pwd')
        job = self.env['zivo.job.work'].search([('client_ref', '=', 'E2E-REF')])
        self.assertEqual(len(job), 1)
        self.assertEqual(job.state, 'issued')
        self.assertEqual(job.material_ids.qty_issued, 50.0)
        self.assertEqual(job.user_id, self.manager)

        self.start_tour('/odoo/action-zivo_job_work_360.action_zivo_job_work/%s' % job.id,
                        'zivo_e2e_receive_inspect', login='jw_manager', password='jw_manager_pwd')
        self.assertEqual(job.qty_accepted, 49.0)
        self.assertEqual(job.qty_rejected, 1.0)
        self.assertEqual(job.production_ids.vendor_challan_ref, 'DC-77')
        self.assertEqual(job.state, 'completed')
        self.assertEqual(job.material_ids.balance_qty, 0.0)

        self.start_tour('/odoo/action-zivo_job_work_360.action_zivo_job_work/%s' % job.id,
                        'zivo_e2e_reconcile_close', login='jw_manager', password='jw_manager_pwd')
        self.assertEqual(job.state, 'closed')
        self.assertEqual(job.reconciliation_state, 'balanced')

    def test_02_vendor_portal_in_browser(self):
        portal = new_test_user(self.env, login='e2e_portal', password='e2e_portal_pwd',
                               groups='base.group_portal', name='E2E Portal')
        portal.partner_id.parent_id = self.e2e_vendor
        job = self._create_job(qty=40.0, material_qty=40.0, partner=self.e2e_vendor, product=self.e2e_out,
                               raw=self.e2e_raw, job_type=self.e2e_type)
        self._issue_all(job)
        self.start_tour('/my/job-works/%s' % job.id, 'zivo_e2e_portal', login='e2e_portal',
                        password='e2e_portal_pwd')
        self.assertEqual(job.vendor_state, 'processing')
        self.assertEqual(job.vendor_reported_qty, 30.0)
        self.assertEqual(job.progress_ids.note, 'First batch')


@tagged('post_install', '-at_install', 'job_work', 'job_work_e2e')
class TestJobWorkSmoke(JobWorkCommon):
    """Open every screen, report and menu of Job Work with every role."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.job_type.quality_required = True
        # a fully processed job feeding every model with data
        job = cls.env['zivo.job.work'].create({
            'partner_id': cls.vendor.id, 'job_type_id': cls.job_type.id, 'product_id': cls.output.id,
            'product_qty': 92.0, 'rate': 2.0,
            'material_ids': [Command.create({'product_id': cls.raw.id, 'planned_qty': 100.0})],
            'cost_line_ids': [Command.create({'cost_type': 'freight', 'name': 'Truck', 'expected_amount': 5,
                                              'actual_amount': 6})],
        })
        job.action_plan()
        job.action_issue_material()
        job._jw_validate_picking(job.picking_ids)
        wiz = cls.env['zivo.job.work.receive.wizard'].with_context(default_job_id=job.id).create(
            {'qty_received': 92.0})
        wiz.line_ids.write({'qty_consumed': 92.0, 'qty_scrap': 5.0})
        wiz.action_confirm()
        job.qc_ids.write({'qty_accepted': 88.0, 'qty_rejected': 2.0, 'qty_rework': 2.0})
        job.qc_ids.action_done()
        cls.env['zivo.job.work.request'].create({'job_id': job.id, 'material_id': job.material_ids.id,
                                                 'product_id': cls.raw.id, 'qty': 1.0, 'reason': 'x'})
        cls.env['zivo.job.work.loss'].create({'job_id': job.id, 'material_id': job.material_ids.id,
                                              'qty': 1.0, 'note': 'x'})
        cls.env['zivo.job.work.progress'].create({'job_id': job.id, 'qty_completed': 10})
        job.action_reconcile()
        cls.env['zivo.job.work.vendor.score'].generate_scorecards(cls.company)
        cls.job = job

    def _jw_actions(self):
        xmlids = self.env['ir.model.data'].search([
            ('module', 'in', JW_MODULES), ('model', 'in', ('ir.actions.act_window', 'ir.actions.server',
                                                           'ir.actions.client'))])
        return [self.env[x.model].browse(x.res_id) for x in xmlids]

    def _open_action(self, user, action):
        if action._name == 'ir.actions.server':
            result = action.with_user(user).run()
            if isinstance(result, dict) and result.get('res_model'):
                self.env[result['res_model']].with_user(user).search(result.get('domain') or [], limit=5)
            return
        if action._name == 'ir.actions.client':
            if action.tag == 'zivo_job_work_dashboard':
                data = self.env['zivo.job.work'].with_user(user).get_jw_dashboard_data()
                self.assertIn('kpis', data)
            return
        Model = self.env[action.res_model].with_user(user)
        if action.res_model.startswith('res.config') or action.target == 'new':
            Model.get_views([(False, 'form')])
            return
        view_types = [v.strip() for v in action.view_mode.split(',')]
        views = Model.get_views([(False, vt) for vt in view_types])
        domain = safe_eval(action.domain or '[]', {'uid': user.id, 'context_today': fields.Date.today})
        records = Model.search(domain, limit=10)
        for vt in ('list', 'kanban'):
            if vt in views['views']:
                spec = _spec_from_arch(Model, views['views'][vt]['arch'])
                Model.web_search_read([('id', 'in', records.ids)], spec)
        if 'form' in views['views'] and records:
            spec = _spec_from_arch(Model, views['views']['form']['arch'])
            records[:3].web_read(spec)
        for vt in ('pivot', 'graph'):
            if vt in views['views']:
                Model.read_group([], ['__count'], [])

    def _menu_actions(self, user):
        menus = self.env['ir.ui.menu'].with_user(user).load_web_menus(False)
        refs = set()
        for menu in menus.values():
            if isinstance(menu, dict) and menu.get('actionModel') and menu.get('actionID'):
                refs.add((menu['actionModel'], menu['actionID']))
        return refs

    def test_01_every_action_view_with_every_role(self):
        actions = [a for a in self._jw_actions()
                   if not (a._name == 'ir.actions.server' and self.env['ir.cron'].search_count(
                       [('ir_actions_server_id', '=', a.id)]))]
        self.assertGreater(len(actions), 15)
        for user in (self.viewer, self.user, self.manager):
            reachable = self._menu_actions(user)
            for action in actions:
                in_menu = any((a._name, a.id) == ref for a in [action] for ref in reachable)
                linked_to_menu = self.env['ir.ui.menu'].sudo().search_count(
                    [('action', '=', '%s,%s' % (action._name, action.id))])
                if linked_to_menu and not in_menu:
                    continue  # the role does not see this menu
                if not linked_to_menu and user == self.viewer and action._name != 'ir.actions.act_window':
                    continue
                with self.subTest(user=user.login, action=action.name):
                    self._open_action(user, action)

    def test_02_menus_per_role(self):
        def xmlids(user):
            menus = self.env['ir.ui.menu'].with_user(user).load_web_menus(False)
            return {m.get('xmlid') for m in menus.values() if isinstance(m, dict)}
        config = 'zivo_job_work_360.menu_job_work_config'
        for user in (self.viewer, self.user, self.manager):
            self.assertIn('zivo_job_work_360.menu_job_work_root', xmlids(user))
            self.assertIn('zivo_job_work_360.menu_job_work_orders', xmlids(user))
        self.assertNotIn(config, xmlids(self.viewer))
        self.assertNotIn(config, xmlids(self.user))
        self.assertIn(config, xmlids(self.manager))
        outsider = new_test_user(self.env, login='menu_outsider', groups='base.group_user')
        self.assertNotIn('zivo_job_work_360.menu_job_work_root', xmlids(outsider))

    def test_03_every_report_renders(self):
        reports = self.env['ir.actions.report'].search([('report_name', 'like', 'zivo_job_work_360')])
        self.assertGreaterEqual(len(reports), 2)
        for report in reports:
            records = self.env[report.model].search([], limit=2)
            if not records:
                continue
            for user in (self.user, self.viewer):
                html = self.env['ir.actions.report'].with_user(user)._render_qweb_html(report.id, records.ids)[0]
                self.assertTrue(html)

    def test_04_related_forms_per_role(self):
        """Partner, transfer, purchase order, MO forms keep working for each role."""
        po_action = self.job.action_create_purchase_order() if not self.job.purchase_order_ids else None
        records = [self.vendor, self.job.picking_ids[:1]]
        if po_action:
            records.append(self.env['purchase.order'].browse(po_action['res_id']))
        for user in (self.user, self.manager):
            for record in records:
                Model = self.env[record._name].with_user(user)
                arch = Model.get_views([(False, 'form')])['views']['form']['arch']
                record.with_user(user).web_read(_spec_from_arch(Model, arch))

    def test_05_mail_template_and_send(self):
        job = self._create_job(qty=5.0, material_qty=5.0)
        job.action_plan()
        action = job.action_send_to_vendor()
        composer = Form(self.env['mail.compose.message'].with_context(action['context']))
        self.assertIn(job.name, composer.subject)
        composer.save().action_send_mail()
        self.assertTrue(job.date_sent)
        message = job.message_ids.filtered(lambda m: m.attachment_ids)[:1]
        # (reports are rendered as HTML instead of PDF in test mode)
        self.assertTrue(message.attachment_ids.filtered(lambda a: a.name.startswith('Job Work')),
                        "the job sheet is attached")

    def test_06_scheduled_actions_run(self):
        crons = self.env['ir.cron'].search([('model_id.model', 'in', (
            'zivo.job.work', 'zivo.job.work.vendor.score'))])
        self.assertGreaterEqual(len(crons), 2)
        for cron in crons:
            cron.method_direct_trigger()

    def test_07_settings_save(self):
        settings = Form(self.env['res.config.settings'])
        settings.jw_scrap_tolerance = 3.5
        settings.jw_weight_quality = 30.0
        settings.save().execute()
        self.assertEqual(self.company.jw_scrap_tolerance, 3.5)
        self.assertEqual(self.company.jw_weight_quality, 30.0)

    def test_08_access_matrix(self):
        job = self.job
        # viewer: read only
        job.with_user(self.viewer).read(['name', 'state', 'cost_act_total'])
        for method in ('action_plan', 'action_issue_material', 'action_reconcile', 'action_close'):
            with self.assertRaises(UserError):  # AccessError is a UserError
                getattr(job.with_user(self.viewer), method)()
        with self.assertRaises(AccessError):
            self.env['zivo.job.work'].with_user(self.viewer).create({
                'partner_id': self.vendor.id, 'job_type_id': self.job_type.id, 'product_id': self.output.id})
        # user cannot configure, cannot approve
        with self.assertRaises(AccessError):
            self.job_type.with_user(self.user).write({'name': 'x'})
        with self.assertRaises(AccessError):
            job.loss_ids.filtered(lambda l: l.state == 'to_approve').with_user(self.user).action_approve()
        with self.assertRaises(AccessError):
            job.exception_ids[:1].with_user(self.user).action_waive()
        # an internal user without Job Work rights sees nothing
        outsider = new_test_user(self.env, login='outsider', groups='base.group_user')
        with self.assertRaises(AccessError):
            self.env['zivo.job.work'].with_user(outsider).search([])
        # portal users cannot write even their own jobs through the ORM
        portal = new_test_user(self.env, login='portal_x', groups='base.group_portal')
        portal.partner_id.parent_id = self.vendor
        self.assertIn(job, self.env['zivo.job.work'].with_user(portal).search([]))
        with self.assertRaises(AccessError):
            job.with_user(portal).write({'vendor_state': 'completed'})
        with self.assertRaises(AccessError):
            self.env['zivo.job.work.exception'].with_user(portal).search([])


@tagged('post_install', '-at_install', 'job_work', 'job_work_e2e')
class TestJobWorkEdgeCases(JobWorkCommon):

    def test_01_material_in_other_unit(self):
        """Material planned in grams for a product stocked in kg."""
        gram = self.env.ref('uom.product_uom_gram')
        self.job_type.quality_required = False
        job = self._create_job(qty=10.0, material_qty=1.0)
        job.material_ids.write({'product_uom_id': gram.id, 'planned_qty': 5000.0, 'scrap_pct': 0.0})
        self._issue_all(job)
        self.assertEqual(job.material_ids.qty_issued, 5000.0)
        self.assertAlmostEqual(self._qty_at(self.raw, job.vendor_location_id), 5.0)
        self._receive(job, 10.0, consumed=4950.0, scrap=50.0)
        self.assertEqual(job.material_ids.balance_qty, 0.0)
        self.assertAlmostEqual(self._qty_at(self.raw, job.vendor_location_id), 0.0)
        job.action_reconcile()
        self.assertEqual(job.state, 'reconciled', job.exception_ids.mapped('description'))
        # an incompatible unit is refused
        with self.assertRaises(ValidationError):
            job.material_ids.copy({'job_id': job.id, 'product_uom_id': self.uom_unit.id})

    def test_02_serial_numbers_on_output(self):
        serial_out = self.env['product.product'].create({
            'name': 'Valve Body', 'type': 'consu', 'is_storable': True, 'tracking': 'serial'})
        self.job_type.quality_required = True
        job = self._create_job(qty=3.0, material_qty=3.0, product=serial_out)
        job.material_ids.scrap_pct = 0.0
        self._issue_all(job)
        action = self._receive(job, 3.0, consumed=3.0)
        picking = self.env['stock.picking'].browse(action['res_id'])
        move = picking.move_ids
        move.move_line_ids.unlink()
        for i in range(3):
            self.env['stock.move.line'].create({
                'move_id': move.id, 'picking_id': picking.id, 'product_id': serial_out.id,
                'location_id': move.location_id.id, 'location_dest_id': move.location_dest_id.id,
                'lot_name': 'SN-%03d' % i, 'quantity': 1.0, 'product_uom_id': move.product_uom.id})
        move.picked = True
        picking.button_validate()
        self.assertEqual(picking.state, 'done')
        self.assertEqual(job.state, 'qc_pending')
        self.assertEqual(len(self.env['stock.lot'].search([('product_id', '=', serial_out.id)])), 3)
        self._inspect(job, rejected=1.0)
        self.assertEqual(job.qty_accepted, 2.0)
        self.assertEqual(self._qty_at(serial_out, self.stock_location), 2.0)

    def test_03_multi_step_warehouse(self):
        self.warehouse.write({'reception_steps': 'three_steps', 'delivery_steps': 'pick_pack_ship'})
        self.job_type.quality_required = False
        job = self._create_job(qty=20.0, material_qty=20.0)
        job.material_ids.scrap_pct = 0.0
        self._issue_all(job)
        self.assertEqual(job.material_ids.qty_issued, 20.0)
        self._receive(job, 20.0, consumed=20.0)
        self.assertEqual(job.state, 'completed')
        self.assertEqual(self._qty_at(self.output, self.stock_location), 20.0,
                         "output lands in stock, not in the input buffer")
        job.action_reconcile()
        self.assertEqual(job.state, 'reconciled')

    def test_04_double_submit_is_rejected(self):
        self.job_type.quality_required = False
        job = self._create_job(qty=10.0, material_qty=10.0)
        job.material_ids.scrap_pct = 0.0
        self._issue_all(job)
        wizard = Form(self.env['zivo.job.work.receive.wizard'].with_context(default_job_id=job.id)).save()
        wizard.qty_received = 4.0
        wizard.line_ids.qty_consumed = 4.0
        wizard.action_confirm()
        with self.assertRaises(UserError):
            wizard.action_confirm()
        self.assertEqual(job.qty_received, 4.0)

    def test_05_request_before_dispatch_and_chain(self):
        self.job_type.quality_required = False
        job = self._create_job(qty=10.0, material_qty=10.0)
        job.material_ids.scrap_pct = 0.0
        job.action_plan()
        req = self.env['zivo.job.work.request'].create({
            'job_id': job.id, 'material_id': job.material_ids.id, 'product_id': self.raw.id, 'qty': 2.0,
            'reason': 'allowance'})
        req.with_user(self.manager).action_approve()
        self.assertFalse(req.picking_id, "no separate dispatch before the job is issued")
        self.assertEqual(job.material_ids.planned_qty, 12.0)
        self._issue_all_from_planned(job)
        self.assertEqual(job.material_ids.qty_issued, 12.0)
        self._receive(job, 10.0, consumed=10.0)
        wiz = Form(self.env['zivo.job.work.return.wizard'].with_context(default_job_id=job.id)).save()
        wiz.line_ids.qty = 2.0
        wiz.action_confirm()
        with self.assertRaises(UserError):
            wiz.action_confirm()
        job.action_reconcile()
        self.assertEqual(job.state, 'reconciled')
        # stage 2 consumes stage 1 output from stock
        ctx = job.action_create_next_stage()['context']
        stage2 = self.env['zivo.job.work'].with_context(**ctx).create({
            'partner_id': self.vendor2.id, 'job_type_id': self.env.ref('zivo_job_work_360.job_type_plating').id})
        stage2.action_plan()
        stage2.action_issue_material()
        stage2._jw_validate_picking(stage2.picking_ids)
        self.assertEqual(stage2.material_ids.qty_issued, 10.0)
        self.assertEqual(self._qty_at(self.output, self.stock_location), 0.0)
        self.assertEqual(stage2.root_id, job)

    def _issue_all_from_planned(self, job):
        job.action_issue_material()
        job._jw_validate_picking(job.picking_ids.filtered(lambda p: p.state != 'done'))

    def test_06_lot_tracked_material_return(self):
        lot_raw = self.env['product.product'].create({
            'name': 'Copper Coil', 'type': 'consu', 'is_storable': True, 'tracking': 'lot'})
        lots = self.env['stock.lot'].create([{'name': 'CU-%s' % i, 'product_id': lot_raw.id} for i in (1, 2)])
        for lot in lots:
            self.env['stock.quant']._update_available_quantity(lot_raw, self.stock_location, 10.0, lot_id=lot)
        self.job_type.quality_required = False
        job = self._create_job(qty=15.0, material_qty=15.0, raw=lot_raw)
        job.material_ids.scrap_pct = 0.0
        self._issue_all(job)
        self.assertEqual(job.material_ids.qty_issued, 15.0)
        self._receive(job, 12.0, consumed=12.0)
        wiz = Form(self.env['zivo.job.work.return.wizard'].with_context(default_job_id=job.id)).save()
        wiz.line_ids.qty = 3.0
        wiz.action_confirm()
        self.assertEqual(job.material_ids.balance_qty, 0.0)
        self.assertEqual(self._qty_at(lot_raw, self.stock_location), 20.0 - 15.0 + 3.0)

    def test_07_negative_and_invalid_inputs(self):
        job = self._create_job(qty=10.0, material_qty=10.0)
        with self.assertRaises(ValidationError):
            job.material_ids.scrap_pct = 120.0
        with self.assertRaises(Exception), mute_logger('odoo.sql_db'), self.env.cr.savepoint():
            job.product_qty = -5.0
            job.flush_recordset()
        job.invalidate_recordset()
        self._issue_all(job)
        wizard = Form(self.env['zivo.job.work.receive.wizard'].with_context(default_job_id=job.id)).save()
        wizard.qty_received = -1.0
        with self.assertRaises(UserError):
            wizard.action_confirm()
        with self.assertRaises(ValidationError):
            self.env['zivo.job.work.progress'].create({'job_id': job.id, 'qty_completed': -1})
        with self.assertRaises(ValidationError):
            self.env['zivo.job.work.request'].create({
                'job_id': job.id, 'product_id': self.raw.id, 'qty': 0.0, 'reason': 'x'})

    def test_08_multi_company_user_dashboard(self):
        company_b = self.env['res.company'].create({'name': 'JW Company B'})
        wh_b = self.env['stock.warehouse'].search([('company_id', '=', company_b.id)], limit=1)
        self.manager.write({'company_ids': [Command.link(company_b.id)]})
        job_b = self.env['zivo.job.work'].with_company(company_b).create({
            'company_id': company_b.id, 'warehouse_id': wh_b.id, 'partner_id': self.vendor.id,
            'job_type_id': self.job_type.id, 'product_id': self.output.id, 'product_qty': 1.0,
            'material_ids': [Command.create({'product_id': self.raw.id, 'planned_qty': 1.0})]})
        job_b.action_plan()
        job_a = self._create_job(qty=1.0, material_qty=1.0)
        job_a.action_plan()
        both = self.env['zivo.job.work'].with_user(self.manager).with_context(
            allowed_company_ids=[self.company.id, company_b.id])
        data = both.get_jw_dashboard_data()
        self.assertGreaterEqual(data['kpis']['open_jobs'], 2)
        only_a = self.env['zivo.job.work'].with_user(self.manager).with_context(allowed_company_ids=[self.company.id])
        self.assertNotIn(job_b, only_a.search([]))

    def test_09_volume_performance(self):
        """Dashboard and lists stay fast with hundreds of jobs."""
        self.job_type.quality_required = False
        Job = self.env['zivo.job.work']
        vals = [{
            'partner_id': (self.vendor if i % 2 else self.vendor2).id, 'job_type_id': self.job_type.id,
            'product_id': self.output.id, 'product_qty': 10.0,
            'material_ids': [Command.create({'product_id': self.raw.id, 'planned_qty': 10.0})],
        } for i in range(300)]
        jobs = Job.create(vals)
        jobs.action_plan()
        for job in jobs[:60]:
            job.action_issue_material()
            job._jw_validate_picking(job.picking_ids)
        for job in jobs[:30]:
            self._receive(job, 10.0, consumed=9.2)
        self.env.flush_all()
        self.env.invalidate_all()
        start = time.time()
        data = Job.with_user(self.manager).get_jw_dashboard_data()
        dashboard_time = time.time() - start
        self.assertGreaterEqual(data['kpis']['open_jobs'], 300)
        start = time.time()
        list_view = Job.with_user(self.manager).get_views([(False, 'list')])['views']['list']['arch']
        Job.with_user(self.manager).web_search_read([], _spec_from_arch(Job, list_view), limit=80)
        list_time = time.time() - start
        self.assertLess(dashboard_time, 5.0, "dashboard took %.2fs" % dashboard_time)
        self.assertLess(list_time, 3.0, "list took %.2fs" % list_time)
        self.env['zivo.job.work.vendor.score'].generate_scorecards(self.company)

    def test_10_native_return_of_output_is_blocked(self):
        self.job_type.quality_required = False
        job = self._create_job(qty=5.0, material_qty=5.0)
        job.material_ids.scrap_pct = 0.0
        self._issue_all(job)
        self._receive(job, 5.0, consumed=5.0)
        receipt = job.production_ids.picking_id
        wizard = Form(self.env['stock.return.picking'].with_context(
            active_id=receipt.id, active_ids=receipt.ids, active_model='stock.picking')).save()
        wizard.product_return_moves.quantity = 1.0
        with self.assertRaises(UserError):
            wizard.action_create_returns()
        self.assertEqual(job.qty_accepted, 5.0)
