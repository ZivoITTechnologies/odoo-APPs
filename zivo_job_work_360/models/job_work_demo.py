# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

from odoo import Command, api, fields, models

from .job_work_reason import EXCEPTION_REASON_TYPES

_logger = logging.getLogger(__name__)


class ZivoJobWorkDemo(models.Model):
    _inherit = 'zivo.job.work'

    @api.model
    def _jw_load_demo_scenario(self):
        """Blueprint demo story: heat treatment -> plating chain, plus a brass machining job."""
        ref = self.env.ref
        try:
            company = self.env.company
            warehouse = self.env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
            stock = warehouse.lot_stock_id
            machined = ref('zivo_job_work_360.demo_product_bracket_machined')
            hardened = ref('zivo_job_work_360.demo_product_bracket_hardened')
            plated = ref('zivo_job_work_360.demo_product_bracket_plated')
            brass = ref('zivo_job_work_360.demo_product_brass_rod')
            bush = ref('zivo_job_work_360.demo_product_brass_bush')
            vendor_a = ref('zivo_job_work_360.demo_partner_vendor_a')
            vendor_b = ref('zivo_job_work_360.demo_partner_vendor_b')
            vendor_c = ref('zivo_job_work_360.demo_partner_vendor_c')
            ht = ref('zivo_job_work_360.job_type_heat_treatment')
            plating = ref('zivo_job_work_360.job_type_plating')
            machining = ref('zivo_job_work_360.job_type_machining')
            machining.scrap_product_id = ref('zivo_job_work_360.demo_product_brass_scrap')
            Quant = self.env['stock.quant']
            Quant._update_available_quantity(machined, stock, 10000.0)
            Quant._update_available_quantity(brass, stock, 500.0)
            now = fields.Datetime.now()

            def receive(job, qty, consumed, scrap=0.0):
                wizard = self.env['zivo.job.work.receive.wizard'].with_context(default_job_id=job.id).create({
                    'qty_received': qty,
                })
                wizard.line_ids.write({'qty_consumed': consumed, 'qty_scrap': scrap})
                wizard.action_confirm()

            def issue(job, days_ago):
                job.action_plan()
                job.action_issue_material()
                for picking in job.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')):
                    job._jw_validate_picking(picking)
                    date = now - timedelta(days=days_ago)
                    picking.write({'date_done': date})
                    picking.move_ids.write({'date': date})
                    picking.move_ids.move_line_ids.write({'date': date})

            # 1. Vendor A heat treatment: 10,000 sent, 9,850 accepted, 100 rejected, 50 scrap
            job_ht = self.create({
                'partner_id': vendor_a.id, 'job_type_id': ht.id, 'product_id': hardened.id,
                'product_qty': 10000.0, 'rate': 12.0, 'client_ref': 'SO-AUTO-0425', 'priority': '1',
                'date_planned_start': now - timedelta(days=20), 'date_expected': now - timedelta(days=12),
                'material_ids': [Command.create({'product_id': machined.id, 'planned_qty': 10000.0})],
                'cost_line_ids': [
                    Command.create({'cost_type': 'freight', 'name': 'Truck to Apex and back',
                                    'expected_amount': 15000.0, 'actual_amount': 17000.0}),
                    Command.create({'cost_type': 'inspection', 'name': 'Hardness testing',
                                    'expected_amount': 5000.0, 'actual_amount': 5000.0}),
                ],
            })
            issue(job_ht, 19)
            job_ht.write({'date_sent': now - timedelta(days=20)})
            job_ht.action_vendor_accept()
            job_ht.action_vendor_confirm_receipt()
            job_ht.action_vendor_start()
            receive(job_ht, 10000.0, 10000.0)
            qc = job_ht.qc_ids[:1]
            qc.write({'qty_accepted': 9850.0, 'qty_rejected': 100.0, 'qty_scrap': 50.0})
            qc.action_done()
            job_ht.write({'date_completed': now - timedelta(days=13), 'vendor_accept_date': now - timedelta(days=19, hours=20)})
            job_ht.action_create_purchase_order()
            job_ht.purchase_order_ids.button_confirm()
            job_ht.action_reconcile()

            # 2. Vendor B plating: next stage, 2 days late, requests extra material
            job_pl = self.create({
                'parent_id': job_ht.id, 'partner_id': vendor_b.id, 'job_type_id': plating.id,
                'product_id': plated.id, 'product_qty': 9850.0, 'rate': 8.5, 'client_ref': 'SO-AUTO-0425',
                'priority': '2',
                'date_planned_start': now - timedelta(days=12), 'date_expected': now - timedelta(days=2),
                'material_ids': [Command.create({'product_id': hardened.id, 'planned_qty': 9850.0})],
            })
            issue(job_pl, 11)
            job_pl.write({'date_sent': now - timedelta(days=12)})
            job_pl.action_vendor_accept()
            job_pl.action_vendor_start()
            self.env['zivo.job.work.progress'].create({
                'job_id': job_pl.id, 'qty_completed': 6000.0, 'qty_scrap': 20.0, 'source': 'portal',
                'note': 'First batch plated, second batch in tank.',
            })
            self.env['zivo.job.work.request'].create({
                'job_id': job_pl.id, 'material_id': job_pl.material_ids.id, 'product_id': hardened.id,
                'qty': 40.0, 'reason': 'Rack damage on 40 parts during loading', 'source': 'portal',
            })
            job_pl._jw_run_checks()

            # 3. Vendor C machining brass: 100 kg -> 92 kg bushes + returnable scrap
            job_br = self.create({
                'partner_id': vendor_c.id, 'job_type_id': machining.id, 'product_id': bush.id,
                'product_qty': 92.0, 'rate': 45.0,
                'date_planned_start': now - timedelta(days=6), 'date_expected': now + timedelta(days=3),
                'material_ids': [Command.create({'product_id': brass.id, 'planned_qty': 100.0, 'scrap_pct': 8.0})],
            })
            issue(job_br, 5)
            job_br.action_vendor_accept()
            job_br.action_vendor_start()
            receive(job_br, 60.0, 60.0, 5.0)

            # 4. A draft job ready to plan
            self.create({
                'partner_id': vendor_a.id, 'job_type_id': ht.id, 'product_id': hardened.id,
                'product_qty': 5000.0, 'rate': 12.0, 'client_ref': 'SO-AUTO-0431',
                'date_planned_start': now + timedelta(days=2), 'date_expected': now + timedelta(days=9),
                'material_ids': [Command.create({'product_id': machined.id, 'planned_qty': 5000.0})],
            })
            self._jw_load_demo_v11(locals())
            self.env['zivo.job.work.vendor.score'].generate_scorecards(company)
        except Exception:  # demo data must never block the installation
            _logger.exception("Job Work 360: the demo scenario could not be generated")

    @api.model
    def _jw_load_demo_v11(self, ctx):
        """Version 1.1 stories: labour hours, customer-owned material held too long, approval queue."""
        company, stock, now = ctx['company'], ctx['stock'], ctx['now']
        issue, receive = ctx['issue'], ctx['receive']
        job_ht, job_pl = ctx['job_ht'], ctx['job_pl']
        brass, bush = ctx['brass'], ctx['bush']
        vendor_b, vendor_c, machining = ctx['vendor_b'], ctx['vendor_c'], ctx['machining']
        ref = self.env.ref

        # vendor labour hours on the plating job
        company.jw_labour_enabled = True
        vendor_b.jw_labour_rate = 250.0
        Labour = self.env['zivo.job.work.labour']
        for days, operation, worker, hours, state in (
                (10, 'Loading on racks', 'Line team A', 6.0, 'approved'),
                (9, 'Plating', 'Line team A', 14.0, 'approved'),
                (8, 'Plating', 'Line team B', 12.5, 'approved'),
                (1, 'Unloading and packing', 'Line team B', 5.0, 'submitted')):
            line = Labour.create({
                'job_id': job_pl.id, 'date': (now - timedelta(days=days)).date(), 'operation': operation,
                'worker_name': worker, 'hours': hours, 'source': 'portal', 'state': 'draft'})
            line.action_submit()
            if state == 'approved':
                line.action_approve()

        # customer-owned brass machined for a principal, partly returned and held for more than 90 days
        customer = self.env['res.partner'].create({
            'name': 'Orion Auto Components', 'is_company': True, 'city': 'Pune',
            'country_id': ref('base.in').id})
        self.env['stock.quant']._update_available_quantity(brass, stock, 300.0, owner_id=customer)
        job_co = self.create({
            'partner_id': vendor_c.id, 'principal_id': customer.id, 'job_type_id': machining.id,
            'product_id': bush.id, 'product_qty': 110.0, 'rate': 40.0, 'client_ref': 'ORION-PO-7781',
            'date_planned_start': now - timedelta(days=96), 'date_expected': now - timedelta(days=80),
            'material_ids': [Command.create({'product_id': brass.id, 'planned_qty': 120.0, 'scrap_pct': 8.0})],
        })
        issue(job_co, 95)
        job_co.write({'date_sent': now - timedelta(days=95)})
        job_co.action_vendor_accept()
        job_co.action_vendor_start()
        receive(job_co, 40.0, 43.0, 3.0)
        materials = (job_co | ctx['job_br'] | job_pl).material_ids
        self.env.add_to_compute(materials._fields['date_oldest_balance'], materials)
        materials.flush_recordset()
        self.env['zivo.job.work.material']._cron_refresh_aging()
        job_co._jw_run_checks()

        # a resolution waiting for the manager
        exception = (job_ht | job_pl).exception_ids.filtered(lambda e: e.state == 'open')[:1]
        reason = self.env['zivo.job.work.reason'].search([
            ('reason_type', 'in', EXCEPTION_REASON_TYPES.get(exception.exception_type, ['other']))], limit=1)
        if exception and reason:
            exception.write({
                'state': 'to_approve', 'reason_id': reason.id,
                'resolution': 'Explained by the job worker, evidence attached on the portal.',
                'requested_by_id': self.env.user.id, 'pending_action': 'resolve'})
