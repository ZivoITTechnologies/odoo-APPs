# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare


class ZivoJobWorkLossWizard(models.TransientModel):
    _name = 'zivo.job.work.loss.wizard'
    _description = 'Record Material Loss'

    job_id = fields.Many2one('zivo.job.work', required=True, readonly=True)
    material_id = fields.Many2one('zivo.job.work.material', 'Material', required=True,
                                  domain="[('job_id', '=', job_id), ('balance_qty', '>', 0)]")
    product_uom_id = fields.Many2one(related='material_id.product_uom_id')
    qty_balance = fields.Float(related='material_id.balance_qty', string='At Job Worker')
    qty = fields.Float('Loss Quantity', digits='Product Unit', required=True)
    reason = fields.Selection([
        ('process', 'Process Loss / Waste'),
        ('damage', 'Damaged'),
        ('lost', 'Lost / Missing'),
        ('other', 'Other'),
    ], required=True, default='process')
    note = fields.Text('Explanation', required=True)
    recover_from_vendor = fields.Boolean('Recover From Job Worker')
    is_manager = fields.Boolean(compute='_compute_is_manager')
    approve_now = fields.Boolean('Approve Now', default=True)
    done = fields.Boolean(readonly=True)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        job = self.env['zivo.job.work'].browse(res.get('job_id') or self.env.context.get('default_job_id'))
        mats = job.material_ids.filtered(lambda m: m.balance_qty > 0)
        if len(mats) == 1:
            res['material_id'] = mats.id
            res['qty'] = mats.balance_qty
        return res

    def _compute_is_manager(self):
        is_manager = self.env.user.has_group('zivo_job_work_360.group_job_work_manager')
        for wiz in self:
            wiz.is_manager = is_manager

    @api.onchange('material_id')
    def _onchange_material_id(self):
        if self.material_id:
            self.qty = self.material_id.balance_qty

    def action_confirm(self):
        self.ensure_one()
        if self.done:
            raise UserError(_("This loss has already been registered."))
        self.done = True
        mat = self.material_id
        if self.qty <= 0 or float_compare(self.qty, mat.balance_qty,
                                          precision_rounding=mat.product_uom_id.rounding) > 0:
            raise UserError(_("The loss must be positive and cannot exceed the balance at the job worker (%s).",
                              mat.balance_qty))
        loss = self.env['zivo.job.work.loss'].create({
            'job_id': self.job_id.id,
            'material_id': mat.id,
            'qty': self.qty,
            'reason': self.reason,
            'note': self.note,
            'recover_from_vendor': self.recover_from_vendor,
        })
        if self.approve_now and self.is_manager:
            loss.action_approve()
        else:
            self.job_id._jw_schedule_activity(_("Approve material loss %s", loss.name), self.note)
        return {'type': 'ir.actions.act_window_close'}
