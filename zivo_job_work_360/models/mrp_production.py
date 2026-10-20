# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    jw_order_ids = fields.One2many('zivo.job.work', 'mrp_production_id', 'Job Work Orders',
                                   groups='zivo_job_work_360.group_job_work_viewer')
    jw_order_count = fields.Integer(compute='_compute_jw_order_count', compute_sudo=True)

    @api.depends('jw_order_ids')
    def _compute_jw_order_count(self):
        for production in self:
            production.jw_order_count = len(production.jw_order_ids)

    def action_create_job_work(self):
        """Send the produced (or to-be-produced) product to a job worker for an external operation."""
        self.ensure_one()
        if self.state == 'cancel':
            raise UserError(_("A cancelled manufacturing order cannot be sent for job work."))
        qty = self.qty_produced if self.state == 'done' else self.product_qty
        warehouse = self.picking_type_id.warehouse_id or self.env['stock.warehouse'].search(
            [('company_id', '=', self.company_id.id)], limit=1)
        return {
            'type': 'ir.actions.act_window',
            'name': _('New Job Work'),
            'res_model': 'zivo.job.work',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'current',
            'context': {
                'default_mrp_production_id': self.id,
                'default_company_id': self.company_id.id,
                'default_client_ref': self.origin or self.name,
                'default_product_id': self.product_id.id,
                'default_product_qty': qty,
                'default_warehouse_id': warehouse.id,
                'default_material_ids': [(0, 0, {
                    'product_id': self.product_id.id,
                    'product_uom_id': self.product_uom_id.id,
                    'planned_qty': qty,
                })],
            },
        }

    def action_view_jw_orders(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivo_job_work_360.action_zivo_job_work')
        action['domain'] = [('mrp_production_id', '=', self.id)]
        action['context'] = {'default_mrp_production_id': self.id}
        return action
