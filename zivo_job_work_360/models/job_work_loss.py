# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_compare


class ZivoJobWorkLoss(models.Model):
    _name = 'zivo.job.work.loss'
    _description = 'Job Work Material Loss'
    _inherit = ['mail.thread']
    _order = 'id desc'
    _check_company_auto = True

    name = fields.Char('Reference', required=True, readonly=True, copy=False, default=lambda self: _('New'))
    job_id = fields.Many2one('zivo.job.work', 'Job Work', required=True, ondelete='cascade', index=True,
                             check_company=True)
    company_id = fields.Many2one(related='job_id.company_id', store=True)
    partner_id = fields.Many2one(related='job_id.partner_id', store=True, string='Job Worker')
    material_id = fields.Many2one('zivo.job.work.material', 'Material', required=True, ondelete='cascade',
                                  domain="[('job_id', '=', job_id)]")
    product_id = fields.Many2one(related='material_id.product_id', store=True, string='Product')
    product_uom_id = fields.Many2one(related='material_id.product_uom_id', string='Unit')
    qty = fields.Float('Quantity', digits='Product Unit', required=True)
    value = fields.Monetary('Value', compute='_compute_value', store=True, currency_field='company_currency_id')
    company_currency_id = fields.Many2one(related='company_id.currency_id', string='Company Currency')
    reason = fields.Selection([
        ('process', 'Process Loss / Waste'),
        ('damage', 'Damaged'),
        ('lost', 'Lost / Missing'),
        ('tolerance', 'Within Tolerance'),
        ('other', 'Other'),
    ], required=True, default='process')
    note = fields.Text('Explanation')
    recover_from_vendor = fields.Boolean('Recover From Job Worker',
                                         help="The value will be debited to the job worker.")
    state = fields.Selection([
        ('to_approve', 'To Approve'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ], default='to_approve', required=True, tracking=True, index=True)
    move_id = fields.Many2one('stock.move', 'Stock Move', readonly=True)
    approved_by_id = fields.Many2one('res.users', 'Approved By', readonly=True)

    @api.depends('qty', 'material_id.unit_cost')
    def _compute_value(self):
        for loss in self:
            loss.value = loss.qty * loss.material_id.unit_cost

    @api.constrains('qty')
    def _check_qty(self):
        for loss in self:
            if loss.qty <= 0:
                raise ValidationError(_("The loss quantity must be positive."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('zivo.job.work.loss') or _('New')
        return super().create(vals_list)

    def _jw_post(self):
        """Post the approved loss as a stock move from the job worker location."""
        for loss in self:
            job = loss.job_id
            mat = loss.material_id
            if float_compare(loss.qty, mat.balance_qty, precision_rounding=mat.product_uom_id.rounding) > 0:
                raise UserError(_("The loss (%(qty)s) exceeds the balance at the job worker (%(bal)s) for %(p)s.",
                                  qty=loss.qty, bal=mat.balance_qty, p=mat.product_id.display_name))
            move = job._jw_post_move(mat.product_id, loss.qty, mat.product_uom_id, job._jw_get_vendor_location(),
                                     job.company_id._jw_get_scrap_location(), 'loss', material=mat,
                                     description=_('%(job)s - approved loss (%(ref)s)', job=job.name, ref=loss.name))
            loss.write({'state': 'approved', 'move_id': move.id, 'approved_by_id': self.env.user.id})
            job.message_post(body=_("Approved loss %(ref)s: %(qty)s %(uom)s of %(p)s.", ref=loss.name,
                                    qty=loss.qty, uom=mat.product_uom_id.name, p=mat.product_id.display_name))

    def action_approve(self):
        if not self.env.user.has_group('zivo_job_work_360.group_job_work_manager'):
            raise AccessError(_("Only Job Work Administrators can approve material losses."))
        self.filtered(lambda l: l.state == 'to_approve')._jw_post()
        return True

    def action_reject(self):
        if not self.env.user.has_group('zivo_job_work_360.group_job_work_manager'):
            raise AccessError(_("Only Job Work Administrators can reject material losses."))
        self.filtered(lambda l: l.state == 'to_approve').write({'state': 'rejected'})
        return True
