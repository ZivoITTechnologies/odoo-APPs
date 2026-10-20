# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare


class ZivoJobWorkReturnWizard(models.TransientModel):
    _name = 'zivo.job.work.return.wizard'
    _description = 'Return Unprocessed Material'

    job_id = fields.Many2one('zivo.job.work', required=True, readonly=True)
    location_dest_id = fields.Many2one('stock.location', 'Return To', required=True,
                                       domain="[('usage', '=', 'internal')]")
    reason = fields.Char('Reason')
    line_ids = fields.One2many('zivo.job.work.return.wizard.line', 'wizard_id', 'Materials')
    done = fields.Boolean(readonly=True)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        job = self.env['zivo.job.work'].browse(res.get('job_id') or self.env.context.get('default_job_id'))
        if job:
            res['location_dest_id'] = job.location_src_id.id
            res['line_ids'] = [(0, 0, {'material_id': mat.id, 'qty': 0.0})
                               for mat in job.material_ids.filtered(lambda m: m.balance_qty > 0)]
        return res

    def action_confirm(self):
        self.ensure_one()
        if self.done:
            raise UserError(_("This return has already been registered."))
        self.done = True
        job = self.job_id
        lines = []
        for line in self.line_ids.filtered(lambda l: l.qty > 0):
            mat = line.material_id
            if float_compare(line.qty, mat.balance_qty, precision_rounding=mat.product_uom_id.rounding) > 0:
                raise UserError(_("%(p)s: you cannot return more than the balance at the job worker (%(bal)s).",
                                  p=mat.product_id.display_name, bal=mat.balance_qty))
            lines.append((mat, mat.product_id, line.qty, mat.product_uom_id))
        if not lines:
            raise UserError(_("Enter the quantities returned by the job worker."))
        picking = job._jw_create_picking(job._jw_receipt_picking_type(), job._jw_get_vendor_location(),
                                         self.location_dest_id, lines, 'return')
        job._jw_validate_picking(picking)
        job.message_post(body=_("Unprocessed material returned (%(picking)s). %(reason)s",
                                picking=picking._get_html_link(), reason=self.reason or ''))
        return {'type': 'ir.actions.act_window_close'}


class ZivoJobWorkReturnWizardLine(models.TransientModel):
    _name = 'zivo.job.work.return.wizard.line'
    _description = 'Return Unprocessed Material - Line'

    wizard_id = fields.Many2one('zivo.job.work.return.wizard', required=True, ondelete='cascade')
    material_id = fields.Many2one('zivo.job.work.material', required=True)
    product_id = fields.Many2one(related='material_id.product_id', string='Product')
    product_uom_id = fields.Many2one(related='material_id.product_uom_id')
    qty_balance = fields.Float(related='material_id.balance_qty', string='At Job Worker')
    qty = fields.Float('Returned', digits='Product Unit')
