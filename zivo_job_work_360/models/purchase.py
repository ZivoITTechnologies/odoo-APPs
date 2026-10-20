# -*- coding: utf-8 -*-
from odoo import api, fields, models


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    jw_order_id = fields.Many2one('zivo.job.work', 'Job Work Order', index='btree_not_null', copy=False,
                                  ondelete='set null')


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    jw_order_ids = fields.Many2many('zivo.job.work', compute='_compute_jw_order_ids', string='Job Work Orders',
                                    compute_sudo=True, groups='zivo_job_work_360.group_job_work_viewer')
    jw_order_count = fields.Integer(compute='_compute_jw_order_ids', compute_sudo=True)

    @api.depends('order_line.jw_order_id')
    def _compute_jw_order_ids(self):
        for order in self:
            order.jw_order_ids = order.order_line.jw_order_id
            order.jw_order_count = len(order.jw_order_ids)

    def button_confirm(self):
        res = super().button_confirm()
        self.sudo().order_line.jw_order_id._jw_update_po_received()
        return res

    def action_view_jw_orders(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivo_job_work_360.action_zivo_job_work')
        action['domain'] = [('id', 'in', self.jw_order_ids.ids)]
        action['context'] = {'create': False}
        if len(self.jw_order_ids) == 1:
            action.update({'res_id': self.jw_order_ids.id, 'view_mode': 'form', 'views': [(False, 'form')]})
        return action
