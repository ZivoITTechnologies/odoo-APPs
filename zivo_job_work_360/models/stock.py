# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError

MOVE_TYPES = [
    ('issue', 'Issue to Job Worker'),
    ('consume', 'Consumption'),
    ('scrap', 'Process Scrap'),
    ('return', 'Unprocessed Return'),
    ('loss', 'Approved Loss'),
    ('output', 'Output Receipt'),
    ('scrap_return', 'Returned Scrap'),
    ('reject', 'Rejected Output'),
    ('qc_scrap', 'Scrapped Output'),
]
REVERSE_TYPES = {'issue': 'return', 'return': 'issue'}


class StockMove(models.Model):
    _inherit = 'stock.move'

    jw_material_id = fields.Many2one('zivo.job.work.material', 'Job Work Material', index='btree_not_null',
                                     ondelete='set null')
    jw_production_id = fields.Many2one('zivo.job.work.production', 'Job Work Receipt', index='btree_not_null',
                                       ondelete='set null')
    jw_move_type = fields.Selection(MOVE_TYPES, 'Job Work Movement')
    jw_order_id = fields.Many2one('zivo.job.work', 'Job Work Order', compute='_compute_jw_order_id',
                                  store=True, index='btree_not_null')

    @api.depends('jw_material_id.job_id', 'jw_production_id.job_id', 'picking_id.jw_order_id')
    def _compute_jw_order_id(self):
        for move in self:
            move.jw_order_id = (move.jw_material_id.job_id or move.jw_production_id.job_id
                                or move.picking_id.jw_order_id)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            origin_id = vals.get('origin_returned_move_id')
            if origin_id:
                origin = self.browse(origin_id)
                if origin.jw_move_type in REVERSE_TYPES:
                    vals['jw_move_type'] = REVERSE_TYPES[origin.jw_move_type]
                    vals['jw_material_id'] = origin.jw_material_id.id
                elif origin.jw_move_type == 'output':
                    raise UserError(_(
                        "Job work output cannot be returned with a standard return. Use the inspection "
                        "(reject / rework) of job work %s instead.", origin.jw_production_id.job_id.name))
                elif origin.jw_move_type:
                    # returning other job work postings (scrap by-product...) is not tracked as custody
                    vals['jw_move_type'] = False
                    vals['jw_production_id'] = False
        return super().create(vals_list)

    def _action_done(self, cancel_backorder=False):
        moves = super()._action_done(cancel_backorder=cancel_backorder)
        # sudo: inventory users validating transfers do not need Job Work access rights
        done = moves.sudo().filtered(lambda m: m.state == 'done')
        issue_jobs = done.filtered(lambda m: m.jw_move_type == 'issue').jw_material_id.job_id
        if issue_jobs:
            issue_jobs.sudo()._jw_on_issue_done()
        productions = done.filtered(lambda m: m.jw_move_type == 'output').jw_production_id
        if productions:
            productions.sudo()._jw_on_received()
        return moves


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    # copied on purpose: backorders and returns of job work transfers stay linked to the job
    jw_order_id = fields.Many2one('zivo.job.work', 'Job Work Order', index='btree_not_null', readonly=True)

    def action_view_jw_order(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'zivo.job.work',
            'res_id': self.jw_order_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }

    def _action_done(self):
        # Output receipts: the quantity validated must match the receipt registered on the job
        for picking in self.filtered('jw_order_id'):
            for move in picking.move_ids.filtered(lambda m: m.jw_move_type == 'output' and m.state != 'cancel'):
                if move.quantity and move.product_uom.compare(move.quantity, move.product_uom_qty) != 0:
                    raise UserError(_(
                        "Job work output receipt %(picking)s must be validated for the full registered quantity "
                        "(%(qty)s %(uom)s). Register a new receipt on the job for a different quantity.",
                        picking=picking.name, qty=move.product_uom_qty, uom=move.product_uom.name))
        return super()._action_done()


class StockQuant(models.Model):
    _inherit = 'stock.quant'

    @api.model
    def action_view_jw_subcontractor_stock(self):
        companies = self.env.companies
        locations = companies.subcontracting_location_id
        return {
            'type': 'ir.actions.act_window',
            'name': _('Stock at Job Workers'),
            'res_model': 'stock.quant',
            'view_mode': 'list,pivot',
            'views': [(self.env.ref('stock.view_stock_quant_tree').id, 'list'),
                      (self.env.ref('stock.view_stock_quant_pivot').id, 'pivot')],
            'domain': [('location_id', 'child_of', locations.ids)],
            'context': {'search_default_locationgroup': 1, 'create': False, 'inventory_report_mode': True},
        }
