# -*- coding: utf-8 -*-
from odoo import api, fields, models


class CpParcel(models.Model):
    _inherit = 'cp.parcel'

    picking_id = fields.Many2one('stock.picking', string='Delivery Order', index=True, copy=False)
    sale_order_id = fields.Many2one('sale.order', string='Sales Order', index=True, copy=False)

    def action_open_picking(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'stock.picking', 'res_id': self.picking_id.id,
                'view_mode': 'form'}


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    cp_parcel_ids = fields.One2many('cp.parcel', 'picking_id', string='Courier Parcels')
    cp_parcel_count = fields.Integer(compute='_compute_cp_parcel_count')

    @api.depends('cp_parcel_ids')
    def _compute_cp_parcel_count(self):
        for picking in self:
            picking.cp_parcel_count = len(picking.cp_parcel_ids)

    def action_cp_view_parcels(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('cargo_pilot_tms.action_cp_parcel')
        action['domain'] = [('picking_id', '=', self.id)]
        if self.cp_parcel_count == 1:
            action.update(res_id=self.cp_parcel_ids.id, view_mode='form', views=[(False, 'form')])
        return action
