# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ZivoJobWorkProduction(models.Model):
    _name = 'zivo.job.work.production'
    _description = 'Job Work Receipt (Actual Production)'
    _order = 'date desc, id desc'
    _check_company_auto = True

    name = fields.Char('Receipt', required=True, readonly=True, copy=False, default=lambda self: _('New'))
    job_id = fields.Many2one('zivo.job.work', 'Job Work', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='job_id.company_id', store=True, index=True)
    partner_id = fields.Many2one(related='job_id.partner_id', store=True, string='Job Worker')
    product_id = fields.Many2one(related='job_id.product_id', store=True, string='Output Product')
    product_uom_id = fields.Many2one(related='job_id.product_uom_id', string='Unit')
    date = fields.Datetime('Received On', default=fields.Datetime.now, required=True)
    qty_received = fields.Float('Received', digits='Product Unit', required=True)
    qty_accepted = fields.Float('Accepted', digits='Product Unit', readonly=True)
    qty_rejected = fields.Float('Rejected', digits='Product Unit', readonly=True)
    qty_rework = fields.Float('Rework', digits='Product Unit', readonly=True)
    qty_scrap = fields.Float('Output Scrap', digits='Product Unit', readonly=True)
    is_final = fields.Boolean('Final Receipt', help="The job worker will not deliver more output for this job.")
    picking_id = fields.Many2one('stock.picking', 'Receipt Transfer', readonly=True, check_company=True,
                                 groups='stock.group_stock_user')
    move_ids = fields.One2many('stock.move', 'jw_production_id', 'Stock Moves', groups='stock.group_stock_user')
    qc_ids = fields.One2many('zivo.job.work.qc', 'production_id', 'Inspections')
    state = fields.Selection([
        ('waiting', 'Waiting Validation'),
        ('qc_pending', 'QC Pending'),
        ('done', 'Done'),
        ('cancel', 'Cancelled'),
    ], default='waiting', required=True, readonly=True, index=True)
    vendor_challan_ref = fields.Char('Job Worker Delivery Ref.')
    vendor_challan_date = fields.Date('Job Worker Delivery Date')
    note = fields.Text('Notes')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('zivo.job.work.production') or _('New')
        return super().create(vals_list)

    def _jw_on_received(self):
        """Called once the output is physically received in stock."""
        QC = self.env['zivo.job.work.qc'].sudo()
        for prod in self.filtered(lambda p: p.state == 'waiting'):
            job = prod.job_id
            if prod.qty_received > 0 and job.job_type_id.quality_required:
                QC.create({
                    'job_id': job.id,
                    'production_id': prod.id,
                    'qty_inspected': prod.qty_received,
                    'qty_accepted': prod.qty_received,
                })
                prod.state = 'qc_pending'
            else:
                prod.write({'state': 'done', 'qty_accepted': prod.qty_received})
        jobs = self.job_id
        jobs._jw_update_po_received()
        jobs._jw_update_state()

    def action_open_picking(self):
        self.ensure_one()
        if not self.picking_id:
            raise UserError(_("This receipt has no transfer."))
        return self.job_id._jw_action_open_picking(self.picking_id)

    def action_open_qc(self):
        self.ensure_one()
        qc = self.qc_ids[:1]
        if not qc:
            raise UserError(_("No inspection for this receipt."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'zivo.job.work.qc',
            'res_id': qc.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }
