# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .job_work_material import jw_allowed_uoms


class ZivoJobWorkRequest(models.Model):
    _name = 'zivo.job.work.request'
    _description = 'Additional Material Request'
    _inherit = ['mail.thread']
    _order = 'id desc'
    _check_company_auto = True

    name = fields.Char('Request', required=True, readonly=True, copy=False, default=lambda self: _('New'))
    job_id = fields.Many2one('zivo.job.work', 'Job Work', required=True, ondelete='cascade', index=True,
                             check_company=True)
    company_id = fields.Many2one(related='job_id.company_id', store=True)
    partner_id = fields.Many2one(related='job_id.partner_id', store=True, string='Job Worker')
    material_id = fields.Many2one('zivo.job.work.material', 'Material Line',
                                  domain="[('job_id', '=', job_id)]")
    product_id = fields.Many2one('product.product', 'Material', required=True,
                                 domain="[('type', '=', 'consu')]")
    qty = fields.Float('Quantity', digits='Product Unit', required=True)
    allowed_uom_ids = fields.Many2many('uom.uom', compute='_compute_allowed_uom_ids')
    product_uom_id = fields.Many2one('uom.uom', 'Unit', required=True, compute='_compute_uom', store=True,
                                     readonly=False, precompute=True, domain="[('id', 'in', allowed_uom_ids)]")
    reason = fields.Text('Reason', required=True)
    state = fields.Selection([
        ('to_approve', 'To Approve'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ], default='to_approve', required=True, tracking=True, index=True)
    source = fields.Selection([('portal', 'Vendor Portal'), ('backend', 'Internal')], default='backend',
                              required=True)
    requested_by_id = fields.Many2one('res.users', 'Requested By', default=lambda self: self.env.user)
    decided_by_id = fields.Many2one('res.users', 'Decided By', readonly=True)
    date_decision = fields.Datetime('Decision Date', readonly=True)
    picking_id = fields.Many2one('stock.picking', 'Issue Transfer', readonly=True)

    @api.depends('product_id')
    def _compute_allowed_uom_ids(self):
        for req in self:
            req.allowed_uom_ids = jw_allowed_uoms(req.product_id)

    @api.constrains('product_id', 'product_uom_id', 'material_id')
    def _check_uom(self):
        for req in self:
            if req.product_id and req.product_uom_id not in jw_allowed_uoms(req.product_id):
                raise ValidationError(_("The unit %s cannot be used for this material.", req.product_uom_id.name))
            if req.material_id and req.material_id.product_id != req.product_id:
                raise ValidationError(_("The requested product must match the material line."))

    @api.depends('product_id', 'material_id')
    def _compute_uom(self):
        for req in self:
            req.product_uom_id = req.material_id.product_uom_id or req.product_id.uom_id

    @api.onchange('material_id')
    def _onchange_material_id(self):
        if self.material_id:
            self.product_id = self.material_id.product_id

    @api.constrains('qty')
    def _check_qty(self):
        for req in self:
            if req.qty <= 0:
                raise ValidationError(_("The requested quantity must be positive."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('zivo.job.work.request') or _('New')
        requests = super().create(vals_list)
        for req in requests:
            req.job_id._jw_set_exception(
                'extra_material', True,
                _("%(name)s: %(qty)s %(uom)s of %(product)s requested beyond plan.",
                  name=req.name, qty=req.qty, uom=req.product_uom_id.name, product=req.product_id.display_name),
                severity='medium')
            req.job_id.message_post(body=_("Additional material requested: %(qty)s %(uom)s of %(product)s. "
                                           "Reason: %(reason)s",
                                           qty=req.qty, uom=req.product_uom_id.name,
                                           product=req.product_id.display_name, reason=req.reason))
            manager_group = self.env.ref('zivo_job_work_360.group_job_work_manager')
            approver = req.job_id.user_id if req.job_id.user_id in manager_group.all_user_ids else False
            req.job_id._jw_schedule_activity(_("Approve material request %s", req.name), req.reason,
                                             user=approver or req.job_id.user_id)
        return requests

    def _check_manager(self):
        if not self.env.user.has_group('zivo_job_work_360.group_job_work_manager') and not self.env.su:
            raise AccessError(_("Only Job Work Administrators can approve or reject material requests."))

    def _refresh_exception(self):
        for job in self.job_id:
            pending = job.request_ids.filtered(lambda r: r.state == 'to_approve')
            job._jw_set_exception('extra_material', bool(pending),
                                  _("%s pending material request(s).", len(pending)), severity='medium')

    def action_approve(self):
        self._check_manager()
        for req in self:
            if req.state != 'to_approve':
                continue
            job = req.job_id
            if job.state in ('draft', 'closed', 'cancel', 'reconciled'):
                raise UserError(_("Material cannot be added to %s in its current status.", job.name))
            material = req.material_id or job.material_ids.filtered(lambda m: m.product_id == req.product_id)[:1]
            if material:
                qty = req.product_uom_id._compute_quantity(req.qty, material.product_uom_id)
                material.planned_qty += qty
            else:
                material = self.env['zivo.job.work.material'].create({
                    'job_id': job.id,
                    'product_id': req.product_id.id,
                    'product_uom_id': req.product_uom_id.id,
                    'planned_qty': req.qty,
                })
                qty = req.qty
            vals = {'state': 'approved', 'decided_by_id': self.env.user.id,
                    'date_decision': fields.Datetime.now(), 'material_id': material.id}
            if job.state not in ('planned', 'hold'):
                picking = job._jw_create_picking(
                    job._jw_issue_picking_type(), job.location_src_id, job._jw_get_vendor_location(),
                    [(material, material.product_id, qty, material.product_uom_id)], 'issue')
                vals['picking_id'] = picking.id
            req.write(vals)
            job.message_post(body=_("Material request %s approved.", req.name))
        self._refresh_exception()
        return True

    def action_reject(self):
        self._check_manager()
        for req in self.filtered(lambda r: r.state == 'to_approve'):
            req.write({'state': 'rejected', 'decided_by_id': self.env.user.id,
                       'date_decision': fields.Datetime.now()})
            req.job_id.message_post(body=_("Material request %s rejected.", req.name))
        self._refresh_exception()
        return True
