# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_compare

from ..models.job_work_reason import EXCEPTION_REASON_TYPES

LOSS_REASON_BY_TYPE = {'shortage': 'lost', 'loss': 'process', 'scrap': 'process', 'quality': 'damage',
                       'other': 'other'}


class ZivoJobWorkExceptionResolveWizard(models.TransientModel):
    _name = 'zivo.job.work.exception.resolve.wizard'
    _description = 'Resolve Job Work Exception'

    exception_id = fields.Many2one('zivo.job.work.exception', required=True, readonly=True)
    mode = fields.Selection([('resolve', 'Resolve'), ('waive', 'Waive')], required=True, default='resolve')
    exception_type = fields.Selection(related='exception_id.exception_type', string='Exception Type')
    severity = fields.Selection(related='exception_id.severity')
    description = fields.Text(related='exception_id.description')
    allowed_reason_ids = fields.Many2many('zivo.job.work.reason', compute='_compute_allowed_reason_ids')
    reason_id = fields.Many2one('zivo.job.work.reason', 'Reason Code', required=True,
                                domain="[('id', 'in', allowed_reason_ids)]")
    resolution = fields.Text('Explanation', required=True)
    can_write_off = fields.Boolean(related='exception_id.can_write_off')
    write_off = fields.Boolean('Write Off the Balance as a Loss',
                               help="Post the unaccounted quantity at the job worker as an approved loss.")
    material_id = fields.Many2one(related='exception_id.material_id')
    product_uom_id = fields.Many2one(related='exception_id.material_id.product_uom_id')
    qty_balance = fields.Float(related='exception_id.material_id.balance_qty', string='At Job Worker')
    write_off_qty = fields.Float('Quantity to Write Off', digits='Product Unit')
    recover_from_vendor = fields.Boolean('Recover From Job Worker')
    needs_approval = fields.Boolean(compute='_compute_needs_approval')
    done = fields.Boolean(readonly=True)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        exc = self.env['zivo.job.work.exception'].browse(
            res.get('exception_id') or self.env.context.get('default_exception_id'))
        if exc.material_id:
            res['write_off_qty'] = exc.material_id.balance_qty
        return res

    @api.depends('exception_id')
    def _compute_allowed_reason_ids(self):
        Reason = self.env['zivo.job.work.reason']
        for wiz in self:
            types = EXCEPTION_REASON_TYPES.get(wiz.exception_id.exception_type)
            domain = [('company_id', 'in', (wiz.exception_id.company_id.id, False))]
            if types:
                domain.append(('reason_type', 'in', types))
            wiz.allowed_reason_ids = Reason.search(domain)

    @api.depends('reason_id', 'mode', 'exception_id')
    def _compute_needs_approval(self):
        is_manager = self.env.user.has_group('zivo_job_work_360.group_job_work_manager')
        for wiz in self:
            needs = wiz.mode == 'waive' or wiz.reason_id.requires_approval or (
                wiz.exception_id.severity == 'high' and wiz.exception_id.company_id.jw_resolution_approval)
            wiz.needs_approval = bool(needs and not is_manager)

    @api.onchange('reason_id')
    def _onchange_reason_id(self):
        if self.reason_id:
            self.recover_from_vendor = self.reason_id.recover_from_vendor

    def action_confirm(self):
        self.ensure_one()
        if self.done:
            raise UserError(_("This resolution has already been submitted."))
        self.done = True
        exc = self.exception_id
        if exc.state != 'open':
            raise UserError(_("The exception %s is no longer open.", exc.name))
        if self.reason_id not in self.allowed_reason_ids:
            raise UserError(_("The reason %s does not apply to this exception.", self.reason_id.display_name))
        is_manager = self.env.user.has_group('zivo_job_work_360.group_job_work_manager')
        if self.mode == 'waive' and not is_manager:
            raise AccessError(_("Only Job Work Administrators can waive exceptions."))
        if self.write_off and self.mode == 'resolve':
            mat = exc.material_id
            rounding = mat.product_uom_id.rounding
            if not exc.can_write_off:
                raise UserError(_("Nothing can be written off for this exception."))
            if float_compare(self.write_off_qty, 0.0, precision_rounding=rounding) <= 0 or \
                    float_compare(self.write_off_qty, mat.balance_qty, precision_rounding=rounding) > 0:
                raise UserError(_("The write-off must be positive and cannot exceed the balance at the job "
                                  "worker (%s).", mat.balance_qty))
            loss = self.env['zivo.job.work.loss'].create({
                'job_id': exc.job_id.id,
                'material_id': mat.id,
                'qty': self.write_off_qty,
                'reason': LOSS_REASON_BY_TYPE.get(self.reason_id.reason_type, 'other'),
                'reason_id': self.reason_id.id,
                'note': self.resolution,
                'recover_from_vendor': self.recover_from_vendor,
                'exception_id': exc.id,
            })
            if self.needs_approval:
                exc.write({'state': 'to_approve', 'reason_id': self.reason_id.id, 'resolution': self.resolution,
                           'requested_by_id': self.env.user.id, 'pending_action': 'resolve', 'loss_id': loss.id})
                exc.job_id._jw_schedule_activity(_("Approve write-off %s", loss.name), self.resolution)
            else:
                exc.loss_id = loss
                exc.write({'reason_id': self.reason_id.id, 'resolution': self.resolution})
                loss.sudo()._jw_post()
        else:
            exc._jw_submit_resolution(self.mode, self.reason_id, self.resolution)
        return {'type': 'ir.actions.act_window_close'}
