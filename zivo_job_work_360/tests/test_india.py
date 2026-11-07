# -*- coding: utf-8 -*-
import io
import json
from datetime import timedelta

import openpyxl

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import Form, tagged

from odoo.addons.zivo_job_work_360.tests.common import JobWorkCommon


@tagged('post_install', '-at_install', 'job_work')
class TestJobWorkIndia(JobWorkCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["res.company"]._jw_in_enable()
        india = cls.env.ref('base.in')
        ka = cls.env['res.country.state'].search([('country_id', '=', india.id), ('code', '=', 'KA')], limit=1)
        mh = cls.env['res.country.state'].search([('country_id', '=', india.id), ('code', '=', 'MH')], limit=1)
        cls.company.partner_id.write({'country_id': india.id, 'state_id': ka.id, 'zip': '560001',
                                      'street': '1 MG Road', 'city': 'Bengaluru'})
        cls.company.vat = '29AABCU9603R1ZM'
        cls.vendor.write({'country_id': india.id, 'state_id': mh.id, 'zip': '411001', 'street': 'MIDC',
                          'city': 'Pune', 'vat': '27AAPFU0939F1ZV'})
        cls.raw.product_tmpl_id.write({'jw_hsn_code': '74072110', 'supplier_taxes_id': [(5, 0, 0)]})
        cls.job_type.quality_required = False

    def test_01_challan_on_dispatch_and_ewaybill(self):
        job = self._create_job(qty=92.0, material_qty=100.0)
        self._issue_all(job)
        challan = job.challan_ids
        self.assertEqual(len(challan), 1)
        self.assertEqual(challan.state, 'confirmed')
        self.assertEqual(challan.principal_gstin, '29AABCU9603R1ZM')
        self.assertEqual(challan.job_worker_gstin, '27AAPFU0939F1ZV')
        self.assertEqual((challan.from_state_code, challan.to_state_code), (29, 27))
        self.assertTrue(challan.is_interstate)
        line = challan.line_ids
        self.assertEqual(line.qty, 100.0)
        self.assertEqual(line.hsn_code, '74072110')
        self.assertEqual(line.uqc, 'KGS')
        self.assertEqual(line.value, 1000.0)
        self.assertEqual(line.gst_rate, 18.0)
        self.assertTrue(challan.qr_image)
        self.assertIn(challan.name, challan.qr_value)

        with self.assertRaises(UserError):
            challan.action_export_ewaybill_json()  # vehicle missing
        challan.vehicle_no = 'MH 12 AB 1234'
        challan.distance_km = 840
        action = challan.action_export_ewaybill_json()
        att = self.env['ir.attachment'].browse(int(action['url'].split('/web/content/')[1].split('?')[0]))
        payload = json.loads(att.raw)
        bill = payload['billLists'][0]
        self.assertEqual(bill['docType'], 'CHL')
        self.assertEqual(bill['subSupplyType'], 4)
        self.assertEqual(bill['fromStateCode'], 29)
        self.assertEqual(bill['toStateCode'], 27)
        self.assertEqual(bill['vehicleNo'], 'MH12AB1234')
        self.assertEqual(bill['itemList'][0]['igstRate'], 18.0)
        self.assertEqual(bill['itemList'][0]['hsnCode'], 74072110)
        self.assertEqual(bill['totalValue'], 1000.0)

        html = self.env['ir.actions.report']._render_qweb_html(
            'zivo_job_work_360.action_report_job_work_challan', challan.ids)[0]
        self.assertIn(challan.name.encode(), html)
        with self.assertRaises(UserError):
            challan.unlink()

    def test_02_draft_challan_synced_on_partial_dispatch(self):
        job = self._create_job(qty=10.0, material_qty=100.0)
        job.action_plan()
        job.action_issue_material()
        picking = job.picking_ids
        picking.action_create_jw_challan()
        challan = picking.jw_challan_ids
        self.assertEqual(challan.state, 'draft')
        self.assertEqual(challan.line_ids.qty, 100.0)
        picking.move_ids.quantity = 40.0
        picking.move_ids.picked = True
        Form.from_action(self.env, picking.button_validate()).save().process()
        self.assertEqual(challan.state, 'confirmed')
        self.assertEqual(challan.line_ids.qty, 40.0, "challan reflects the dispatched quantity")
        backorder = job.picking_ids - picking
        backorder.button_validate()
        self.assertEqual(len(job.challan_ids), 2)
        self.assertEqual(sum(job.challan_ids.line_ids.mapped('qty')), 100.0)

    def test_03_itc04_and_alerts(self):
        job = self._create_job(qty=92.0, material_qty=100.0)
        self._issue_all(job)
        self._receive(job, 92.0, consumed=92.0, scrap=5.0)
        wiz = Form(self.env['zivo.job.work.loss.wizard'].with_context(default_job_id=job.id))
        wiz.qty = 3.0
        wiz.note = 'Process loss'
        wiz.save().action_confirm()
        itc = self.env['zivo.job.work.itc04.wizard'].create({
            'date_from': fields.Date.today() - timedelta(days=1), 'date_to': fields.Date.today()})
        t4, t5, warnings = itc._jw_itc04_rows()
        # the database may already contain other job work of today (demo data): keep this job's rows only
        t4 = [r for r in t4 if r['challan'] in job.challan_ids.mapped('name')]
        t5 = [r for r in t5 if r['job'] == job.name]
        self.assertEqual(len(t4), 1)
        self.assertEqual(t4[0]['qty'], 100.0)
        self.assertEqual(t4[0]['igst'], 18.0)
        self.assertEqual(sum(r['qty'] for r in t5), 92.0)
        self.assertEqual(sum(r['loss'] for r in t5), 8.0)
        self.assertGreaterEqual(itc.table4_count, 1)
        action = itc.action_export_xlsx()
        att = self.env['ir.attachment'].browse(int(action['url'].split('/web/content/')[1].split('?')[0]))
        wb = openpyxl.load_workbook(io.BytesIO(att.raw))
        self.assertEqual(len(wb.sheetnames), 3)
        self.assertIn(job.challan_ids.name, [row[3] for row in wb.worksheets[0].iter_rows(min_row=2, values_only=True)])

        # return-period alerts
        job2 = self._create_job(qty=10.0, material_qty=10.0)
        self._issue_all(job2)
        self.company.write({'jw_in_return_days_inputs': 10, 'jw_in_alert_days': 2})
        job2.material_ids.move_ids.write({'date': fields.Datetime.now() - timedelta(days=9)})
        self.assertEqual(job2.material_ids.jw_in_due_date, fields.Date.today() + timedelta(days=1))
        self.env['zivo.job.work']._cron_jw_in_return_alerts()
        exc = job2.exception_ids.filtered(lambda e: e.exception_type == 'statutory')
        self.assertEqual(exc.state, 'open')
        self.assertEqual(exc.severity, 'medium')
        data = self.env['zivo.job.work'].get_jw_dashboard_data()
        self.assertIn('compliance', data)
        self.assertEqual(len(data['compliance']['kpis']), 6)

    def test_04_plain_stock_user_opens_and_validates_pickings(self):
        """Inventory users without Job Work rights must be able to use any transfer form."""
        from odoo.tests import new_test_user
        stock_user = new_test_user(self.env, login='plain_stock_in', groups='stock.group_stock_user')
        job = self._create_job(qty=10.0, material_qty=10.0)
        job.action_plan()
        job.action_issue_material()
        picking = job.picking_ids.with_user(stock_user)
        Picking = self.env['stock.picking'].with_user(stock_user)
        form_fields = Picking.get_views([(False, 'form')])['models']['stock.picking']['fields']
        picking.web_read({name: {} for name in form_fields if name in Picking._fields
                          and not Picking._fields[name].groups and Picking._fields[name].type not in
                          ('one2many', 'many2many', 'many2one')})
        picking.read(['jw_challan_count', 'jw_is_issue', 'jw_order_id'])
        picking.button_validate()
        self.assertEqual(picking.state, 'done')
        self.assertEqual(len(job.challan_ids), 1)

    def test_10_india_switch_off(self):
        """Without the India switch no challan is created and the dashboard has no compliance panel."""
        group = self.env.ref('zivo_job_work_360.group_jw_india')
        self.env.ref('base.group_user')._remove_group(group)
        self.assertFalse(self.env['res.company']._jw_in_enabled())
        job = self._create_job()
        self._issue_all(job)
        self.assertFalse(job.challan_ids)
        self.assertNotIn('compliance', self.env['zivo.job.work'].get_jw_dashboard_data())
        settings = self.env['res.config.settings'].create({})
        self.assertFalse(settings.group_jw_india)
        settings.group_jw_india = True
        settings.execute()
        self.assertTrue(self.env['res.company']._jw_in_enabled())
        job2 = self._create_job()
        self._issue_all(job2)
        self.assertTrue(job2.challan_ids)
