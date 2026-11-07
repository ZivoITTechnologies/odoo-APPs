# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    jw_challan_ids = fields.One2many('zivo.job.work.challan', 'picking_id', 'Job Work Challans',
                                     groups='zivo_job_work_360.group_job_work_viewer')
    jw_challan_count = fields.Integer(compute='_compute_jw_challan_count', compute_sudo=True)
    jw_is_issue = fields.Boolean(compute='_compute_jw_challan_count', compute_sudo=True)

    def _compute_jw_challan_count(self):
        for picking in self:
            picking.jw_challan_count = len(picking.jw_challan_ids)
            picking.jw_is_issue = bool(picking.jw_order_id) and any(
                m.jw_move_type == 'issue' for m in picking.move_ids)

    def action_create_jw_challan(self):
        self.ensure_one()
        if not self.jw_is_issue:
            raise UserError(_("Challans are created for dispatches of material to job workers."))
        challan = self.jw_challan_ids.filtered(lambda c: c.state != 'cancel')[:1] or \
            self.env['zivo.job.work.challan']._jw_create_from_picking(self, confirm=self.state == 'done')
        if not challan:
            raise UserError(_("There is no quantity to put on a challan."))
        return self.action_view_jw_challans()

    def action_view_jw_challans(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivo_job_work_360.action_zivo_job_work_challan')
        action['domain'] = [('picking_id', '=', self.id)]
        action['context'] = {'create': False}
        challans = self.jw_challan_ids
        if len(challans) == 1:
            action.update({'res_id': challans.id, 'view_mode': 'form', 'views': [(False, 'form')]})
        return action

    def _action_done(self):
        res = super()._action_done()
        Challan = self.env['zivo.job.work.challan'].sudo()
        for picking in self.sudo().filtered(lambda p: p.jw_order_id and p.state == 'done'):
            if not any(m.jw_move_type == 'issue' for m in picking.move_ids):
                continue
            drafts = picking.jw_challan_ids.filtered(lambda c: c.state == 'draft')
            if drafts:
                drafts._jw_sync_from_picking()
            elif not picking.jw_challan_ids.filtered(lambda c: c.state != 'cancel') \
                    and picking.company_id.jw_in_auto_challan and self.env['res.company']._jw_in_enabled():
                Challan._jw_create_from_picking(picking, confirm=True)
        return res
