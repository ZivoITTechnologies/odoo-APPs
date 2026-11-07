# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

EXCEPTION_TYPES = [
    ('shortage', 'Material Shortage'),
    ('excess_scrap', 'Excess Scrap'),
    ('overdue', 'Overdue Return'),
    ('low_yield', 'Low Yield'),
    ('bill_variance', 'Invoice Variance'),
    ('extra_material', 'Unapproved Extra Material'),
    ('material_variance', 'Over-consumption'),
    ('quality', 'Quality Failure'),
    ('cost_variance', 'Cost Variance'),
    ('statutory', 'Statutory Return Due'),
    ('aging', 'Material Aging'),
    ('other', 'Other'),
]


class ZivoJobWorkException(models.Model):
    _name = 'zivo.job.work.exception'
    _description = 'Job Work Exception'
    _inherit = ['mail.thread']
    _order = 'state, severity_rank desc, id desc'
    _check_company_auto = True

    name = fields.Char(compute='_compute_name', store=True)
    job_id = fields.Many2one('zivo.job.work', 'Job Work', required=True, ondelete='cascade', index=True,
                             check_company=True)
    company_id = fields.Many2one(related='job_id.company_id', store=True, index=True)
    partner_id = fields.Many2one(related='job_id.partner_id', store=True, string='Job Worker')
    user_id = fields.Many2one(related='job_id.user_id', store=True, string='Responsible')
    material_id = fields.Many2one('zivo.job.work.material', 'Material', ondelete='cascade')
    ncr_id = fields.Many2one('zivo.job.work.ncr', 'NCR')
    exception_type = fields.Selection(EXCEPTION_TYPES, 'Exception', required=True, index=True)
    severity = fields.Selection([('low', 'Low'), ('medium', 'Medium'), ('high', 'High')],
                                default='high', required=True)
    severity_rank = fields.Integer(compute='_compute_severity_rank', store=True)
    description = fields.Text('Details')
    state = fields.Selection([
        ('open', 'Open'),
        ('to_approve', 'Resolution To Approve'),
        ('resolved', 'Resolved'),
        ('waived', 'Waived'),
    ], default='open', required=True, tracking=True, index=True)
    reason_id = fields.Many2one('zivo.job.work.reason', 'Reason Code', tracking=True)
    resolution = fields.Text('Resolution')
    requested_by_id = fields.Many2one('res.users', 'Resolution Requested By', readonly=True)
    pending_action = fields.Selection([('resolve', 'Resolve'), ('waive', 'Waive')], readonly=True)
    loss_id = fields.Many2one('zivo.job.work.loss', 'Write-off', readonly=True)
    resolved_by_id = fields.Many2one('res.users', 'Resolved By', readonly=True)
    date_resolved = fields.Datetime('Resolved On', readonly=True)
    can_write_off = fields.Boolean(compute='_compute_can_write_off')

    @api.depends('exception_type', 'job_id.name')
    def _compute_name(self):
        labels = dict(EXCEPTION_TYPES)
        for exc in self:
            exc.name = '%s - %s' % (exc.job_id.name or '', labels.get(exc.exception_type, ''))

    @api.depends('severity')
    def _compute_severity_rank(self):
        ranks = {'low': 1, 'medium': 2, 'high': 3}
        for exc in self:
            exc.severity_rank = ranks.get(exc.severity, 0)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for exc in records:
            exc.job_id.message_post(body=_("Exception raised: %(name)s. %(details)s",
                                           name=exc.name, details=exc.description or ''))
        return records

    @api.depends('exception_type', 'material_id.balance_qty', 'state')
    def _compute_can_write_off(self):
        for exc in self:
            exc.can_write_off = bool(exc.state == 'open' and exc.material_id and exc.material_id.balance_qty > 0
                                     and exc.exception_type in ('shortage', 'aging', 'statutory'))

    def _jw_is_manager(self):
        return self.env.su or self.env.user.has_group('zivo_job_work_360.group_job_work_manager')

    def _jw_open_wizard(self, mode):
        self.ensure_one()
        if self.state != 'open':
            raise UserError(_("Only open exceptions can be resolved or waived."))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Waive Exception') if mode == 'waive' else _('Resolve Exception'),
            'res_model': 'zivo.job.work.exception.resolve.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_exception_id': self.id, 'default_mode': mode},
        }

    def action_resolve(self):
        """Open the resolution wizard (a reason code and an explanation are mandatory)."""
        return self._jw_open_wizard('resolve')

    def action_waive(self):
        if not self._jw_is_manager():
            raise AccessError(_("Only Job Work Administrators can waive exceptions."))
        return self._jw_open_wizard('waive')

    def _jw_mark_done(self, state, reason=None, resolution=None):
        for exc in self:
            exc.write({
                'state': state,
                'reason_id': (reason or exc.reason_id).id,
                'resolution': resolution or exc.resolution,
                'resolved_by_id': self.env.user.id,
                'date_resolved': fields.Datetime.now(),
                'pending_action': False,
            })
            label = _('waived') if state == 'waived' else _('resolved')
            exc.job_id.message_post(body=_("Exception %(name)s %(label)s. Reason: %(reason)s. %(why)s",
                                           name=exc.name, label=label,
                                           reason=exc.reason_id.display_name or '-', why=exc.resolution or ''))

    def _jw_submit_resolution(self, mode, reason, resolution):
        """Resolve directly or send for approval depending on the reason code and the user's rights."""
        self.ensure_one()
        needs_approval = mode == 'waive' or (reason and reason.requires_approval) or (
            self.severity == 'high' and self.company_id.jw_resolution_approval)
        if needs_approval and not self._jw_is_manager():
            if mode == 'waive':
                raise AccessError(_("Only Job Work Administrators can waive exceptions."))
            self.write({'state': 'to_approve', 'reason_id': reason.id, 'resolution': resolution,
                        'requested_by_id': self.env.user.id, 'pending_action': mode})
            self.job_id.message_post(body=_("Resolution of %(name)s submitted for approval. Reason: %(reason)s.",
                                            name=self.name, reason=reason.display_name))
            # the requester may not read other users' companies: find approvers as superuser
            managers = self.env.ref('zivo_job_work_360.group_job_work_manager').sudo().all_user_ids.filtered(
                lambda u: self.company_id in u.company_ids and not u.share)
            responsible = self.job_id.sudo().user_id
            approver = responsible if responsible in managers else managers[:1]
            approver = approver.with_env(self.env)
            self.job_id._jw_schedule_activity(_("Approve resolution of %s", self.name),
                                              '%s: %s' % (reason.display_name, resolution), user=approver or None)
        else:
            self._jw_mark_done('waived' if mode == 'waive' else 'resolved', reason, resolution)

    def action_approve_resolution(self):
        if not self._jw_is_manager():
            raise AccessError(_("Only Job Work Administrators can approve resolutions."))
        for exc in self.filtered(lambda e: e.state == 'to_approve'):
            if exc.loss_id and exc.loss_id.state == 'to_approve':
                exc.loss_id.action_approve()  # resolves the exception through the write-off
                continue
            exc._jw_mark_done('waived' if exc.pending_action == 'waive' else 'resolved')
        return True

    def action_reject_resolution(self):
        if not self._jw_is_manager():
            raise AccessError(_("Only Job Work Administrators can reject resolutions."))
        for exc in self.filtered(lambda e: e.state == 'to_approve'):
            if exc.loss_id and exc.loss_id.state == 'to_approve':
                exc.loss_id.write({'state': 'rejected'})
            exc.write({'state': 'open', 'pending_action': False, 'loss_id': False})
            exc.job_id.message_post(body=_("Resolution of %s rejected; the exception is open again.", exc.name))
        return True

    def action_reopen(self):
        if not self._jw_is_manager():
            raise AccessError(_("Only Job Work Administrators can re-open exceptions."))
        self.write({'state': 'open', 'resolved_by_id': False, 'date_resolved': False, 'pending_action': False})
        return True

    def action_open_job(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'zivo.job.work', 'res_id': self.job_id.id,
                'view_mode': 'form', 'views': [(False, 'form')]}
