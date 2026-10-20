# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import Command, fields
from odoo.tests import Form, TransactionCase, new_test_user


class JobWorkCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True, mail_notrack=True))
        cls.company = cls.env.company
        cls.warehouse = cls.env['stock.warehouse'].search([('company_id', '=', cls.company.id)], limit=1)
        cls.stock_location = cls.warehouse.lot_stock_id
        cls.uom_unit = cls.env.ref('uom.product_uom_unit')
        cls.uom_kg = cls.env.ref('uom.product_uom_kgm')
        cls.raw = cls.env['product.product'].create({
            'name': 'Brass Rod', 'type': 'consu', 'is_storable': True, 'uom_id': cls.uom_kg.id,
            'standard_price': 10.0,
        })
        cls.output = cls.env['product.product'].create({
            'name': 'Brass Bush', 'type': 'consu', 'is_storable': True, 'uom_id': cls.uom_kg.id,
            'standard_price': 20.0,
        })
        cls.vendor = cls.env['res.partner'].create({'name': 'ABC Heat Treat', 'is_company': True,
                                                    'jw_is_job_worker': True, 'email': 'abc@example.com'})
        cls.vendor2 = cls.env['res.partner'].create({'name': 'XYZ Plating', 'is_company': True,
                                                     'jw_is_job_worker': True, 'email': 'xyz@example.com'})
        cls.job_type = cls.env.ref('zivo_job_work_360.job_type_heat_treatment')
        cls.job_type.write({'quality_required': True, 'expected_yield': 92.0, 'expected_scrap_pct': 8.0})
        cls.env['stock.quant']._update_available_quantity(cls.raw, cls.stock_location, 10000.0)
        cls.manager = new_test_user(
            cls.env, login='jw_manager', name='JW Manager',
            groups='zivo_job_work_360.group_job_work_manager,account.group_account_invoice,mrp.group_mrp_user')
        cls.user = new_test_user(
            cls.env, login='jw_user', name='JW User',
            groups='zivo_job_work_360.group_job_work_user,account.group_account_invoice')
        cls.viewer = new_test_user(cls.env, login='jw_viewer', name='JW Viewer',
                                   groups='base.group_user,zivo_job_work_360.group_job_work_viewer')

    def _create_job(self, qty=92.0, material_qty=100.0, partner=None, product=None, raw=None, rate=0.0,
                    job_type=None, user=None):
        env = self.env(user=user) if user else self.env
        return env['zivo.job.work'].create({
            'partner_id': (partner or self.vendor).id,
            'job_type_id': (job_type or self.job_type).id,
            'product_id': (product or self.output).id,
            'product_qty': qty,
            'rate': rate,
            'date_planned_start': fields.Datetime.now(),
            'date_expected': fields.Datetime.now() + timedelta(days=5),
            'material_ids': [Command.create({
                'product_id': (raw or self.raw).id,
                'planned_qty': material_qty,
            })],
        })

    def _issue_all(self, job):
        job.action_plan()
        job.action_issue_material()
        picking = job.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))
        self.assertTrue(picking)
        picking.button_validate()
        self.assertEqual(picking.state, 'done')
        return picking

    def _receive(self, job, qty, consumed=None, scrap=0.0, final=False, scrap_returned=False):
        wiz_form = Form(self.env['zivo.job.work.receive.wizard'].with_context(default_job_id=job.id))
        wiz_form.qty_received = qty
        wiz_form.is_final = final
        wizard = wiz_form.save()
        for line in wizard.line_ids:
            if consumed is not None:
                line.qty_consumed = consumed
            line.qty_scrap = scrap
            line.scrap_returned = scrap_returned
        return wizard.action_confirm()

    def _inspect(self, job, accepted=None, rejected=0.0, rework=0.0, scrap=0.0):
        qc = job.qc_ids.filtered(lambda q: q.state == 'draft')
        self.assertEqual(len(qc), 1)
        qc.write({
            'qty_accepted': qc.qty_inspected - rejected - rework - scrap if accepted is None else accepted,
            'qty_rejected': rejected, 'qty_rework': rework, 'qty_scrap': scrap,
        })
        qc.action_done()
        return qc

    def _qty_at(self, product, location):
        return self.env['stock.quant']._get_available_quantity(product, location, allow_negative=True)
