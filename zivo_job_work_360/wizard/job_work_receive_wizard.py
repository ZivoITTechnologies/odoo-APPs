# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_is_zero


class ZivoJobWorkReceiveWizard(models.TransientModel):
    _name = 'zivo.job.work.receive.wizard'
    _description = 'Receive Job Work Output'

    job_id = fields.Many2one('zivo.job.work', required=True, readonly=True)
    product_id = fields.Many2one(related='job_id.product_id')
    product_uom_id = fields.Many2one(related='job_id.product_uom_id')
    product_tracking = fields.Selection(related='job_id.product_id.tracking')
    qty_remaining = fields.Float(related='job_id.qty_remaining')
    vendor_reported_qty = fields.Float(related='job_id.vendor_reported_qty')
    date = fields.Datetime('Received On', default=fields.Datetime.now, required=True)
    qty_received = fields.Float('Output Received', digits='Product Unit')
    is_final = fields.Boolean('Final Receipt', help="No more output will be delivered for this job.")
    vendor_challan_ref = fields.Char('Job Worker Delivery Ref.')
    vendor_challan_date = fields.Date('Job Worker Delivery Date')
    note = fields.Text('Notes')
    line_ids = fields.One2many('zivo.job.work.receive.wizard.line', 'wizard_id', 'Material Consumption')
    done = fields.Boolean(readonly=True)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        job = self.env['zivo.job.work'].browse(res.get('job_id') or self.env.context.get('default_job_id'))
        if job:
            reported_open = max(job.vendor_reported_qty - job.qty_received, 0.0)
            qty = reported_open or job.qty_remaining
            res['qty_received'] = qty
            lines = []
            for mat in job.material_ids.filtered(lambda m: m.balance_qty > 0):
                expected = min(mat.std_qty_per_unit * qty, mat.balance_qty)
                lines.append((0, 0, {
                    'material_id': mat.id,
                    'qty_consumed': expected,
                    'qty_scrap': 0.0,
                }))
            res['line_ids'] = lines
        return res

    @api.onchange('qty_received')
    def _onchange_qty_received(self):
        for line in self.line_ids:
            line.qty_consumed = min(line.material_id.std_qty_per_unit * self.qty_received, line.qty_balance)

    def action_confirm(self):
        self.ensure_one()
        if self.done:
            raise UserError(_("This receipt has already been registered."))
        self.done = True
        job = self.job_id
        job._jw_ensure_state(('issued', 'at_vendor', 'processing', 'qc_pending', 'completed'), _("Receiving output"))
        rounding = job.product_uom_id.rounding
        if float_compare(self.qty_received, 0.0, precision_rounding=rounding) < 0:
            raise UserError(_("The received quantity cannot be negative."))
        has_material = any(l.qty_consumed > 0 or l.qty_scrap > 0 for l in self.line_ids)
        if float_is_zero(self.qty_received, precision_rounding=rounding) and not has_material and not self.is_final:
            raise UserError(_("Enter a received quantity, a consumption or scrap quantity."))
        for line in self.line_ids:
            mat_round = line.material_id.product_uom_id.rounding
            if line.qty_consumed < 0 or line.qty_scrap < 0:
                raise UserError(_("Consumed and scrap quantities cannot be negative."))
            if float_compare(line.qty_consumed + line.qty_scrap, line.material_id.balance_qty,
                             precision_rounding=mat_round) > 0:
                raise UserError(_(
                    "%(product)s: consumed + scrap (%(qty)s) exceeds the balance at the job worker (%(bal)s). "
                    "Issue more material or correct the quantities.",
                    product=line.material_id.product_id.display_name, qty=line.qty_consumed + line.qty_scrap,
                    bal=line.material_id.balance_qty))

        production = self.env['zivo.job.work.production']
        if not float_is_zero(self.qty_received, precision_rounding=rounding):
            production = production.create({
                'job_id': job.id,
                'date': self.date,
                'qty_received': self.qty_received,
                'is_final': self.is_final,
                'vendor_challan_ref': self.vendor_challan_ref,
                'vendor_challan_date': self.vendor_challan_date,
                'note': self.note,
            })
        vendor_loc = job._jw_get_vendor_location()
        processing_loc = job.company_id._jw_get_processing_location()
        scrap_loc = job.company_id._jw_get_scrap_location()
        for line in self.line_ids:
            mat = line.material_id
            job._jw_post_move(mat.product_id, line.qty_consumed, mat.product_uom_id, vendor_loc, processing_loc,
                              'consume', material=mat, production=production,
                              description=_('%s - consumption', job.name))
            if line.qty_scrap > 0:
                if line.scrap_returned and mat.scrap_product_id:
                    job._jw_post_move(mat.product_id, line.qty_scrap, mat.product_uom_id, vendor_loc,
                                      processing_loc, 'scrap', material=mat, production=production,
                                      description=_('%s - process scrap', job.name))
                    scrap_product = mat.scrap_product_id
                    try:
                        scrap_qty = mat.product_uom_id._compute_quantity(line.qty_scrap, scrap_product.uom_id)
                    except UserError:
                        scrap_qty = line.qty_scrap
                    job._jw_post_move(scrap_product, scrap_qty, scrap_product.uom_id, processing_loc,
                                      job.location_dest_id, 'scrap_return', material=mat, production=production,
                                      description=_('%s - returned scrap', job.name))
                else:
                    job._jw_post_move(mat.product_id, line.qty_scrap, mat.product_uom_id, vendor_loc, scrap_loc,
                                      'scrap', material=mat, production=production,
                                      description=_('%s - process scrap', job.name))
        if not production:
            job.message_post(body=_("Material consumption/scrap recorded without output."))
            if self.is_final and job.state in ('issued', 'at_vendor', 'processing'):
                job.action_mark_completed()
            return {'type': 'ir.actions.act_window_close'}

        picking = job._jw_create_picking(
            job._jw_receipt_picking_type(), processing_loc, job.location_dest_id,
            [(False, job.product_id, self.qty_received, job.product_uom_id)], 'output')
        picking.move_ids.write({'jw_production_id': production.id})
        production.picking_id = picking
        job.message_post(body=_("Received %(qty)s %(uom)s of %(product)s (%(ref)s).",
                                qty=self.qty_received, uom=job.product_uom_id.name,
                                product=job.product_id.display_name, ref=production.name))
        if job.product_id.tracking == 'none':
            job._jw_validate_picking(picking)
            return {'type': 'ir.actions.act_window_close'}
        # Tracked output: let the user register lots / serial numbers on the receipt
        return job._jw_action_open_picking(picking)


class ZivoJobWorkReceiveWizardLine(models.TransientModel):
    _name = 'zivo.job.work.receive.wizard.line'
    _description = 'Receive Job Work Output - Material'

    wizard_id = fields.Many2one('zivo.job.work.receive.wizard', required=True, ondelete='cascade')
    material_id = fields.Many2one('zivo.job.work.material', required=True)
    product_id = fields.Many2one(related='material_id.product_id', string='Product')
    product_uom_id = fields.Many2one(related='material_id.product_uom_id')
    qty_balance = fields.Float(related='material_id.balance_qty', string='At Job Worker')
    qty_expected = fields.Float('Std. Consumption', compute='_compute_expected', digits='Product Unit')
    qty_consumed = fields.Float('Consumed', digits='Product Unit')
    qty_scrap = fields.Float('Scrap', digits='Product Unit')
    scrap_product_id = fields.Many2one(related='material_id.scrap_product_id')
    scrap_returned = fields.Boolean('Scrap Returned',
                                    help="The job worker returns the scrap; it is received as the scrap product.")

    @api.depends('material_id', 'wizard_id.qty_received')
    def _compute_expected(self):
        for line in self:
            line.qty_expected = line.material_id.std_qty_per_unit * line.wizard_id.qty_received
