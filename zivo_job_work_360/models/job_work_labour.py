# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class ZivoJobWorkLabour(models.Model):
    _name = 'zivo.job.work.labour'
    _description = 'Job Worker Labour Hours'
    _inherit = ['mail.thread']
    _order = 'date desc, id desc'
    _check_company_auto = True

    job_id = fields.Many2one('zivo.job.work', 'Job Work', required=True, ondelete='cascade', index=True,
                             check_company=True)
    company_id = fields.Many2one(related='job_id.company_id', store=True, index=True)
    company_currency_id = fields.Many2one(related='company_id.currency_id', string='Company Currency')
    partner_id = fields.Many2one(related='job_id.partner_id', store=True, string='Job Worker', index=True)
    product_id = fields.Many2one(related='job_id.product_id', store=True, string='Product')
    job_type_id = fields.Many2one(related='job_id.job_type_id', store=True)
    date = fields.Date('Date', required=True, default=fields.Date.context_today)
    operation = fields.Char('Operation', required=True, help="e.g. Loading, Plating, Unloading, Polishing")
    worker_name = fields.Char('Worker / Team')
    hours = fields.Float('Hours', required=True, digits=(16, 2))
    hourly_rate = fields.Float('Hourly Rate', digits='Product Price', compute='_compute_hourly_rate', store=True,
                               readonly=False, precompute=True)
    cost = fields.Monetary('Labour Cost', compute='_compute_cost', store=True, currency_field='company_currency_id')
    note = fields.Text('Comment')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'To Approve'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ], default='draft', required=True, tracking=True, index=True)
    source = fields.Selection([('portal', 'Vendor Portal'), ('backend', 'Internal')], default='backend',
                              required=True)
    submitted_by_id = fields.Many2one('res.users', 'Reported By', default=lambda self: self.env.user, readonly=True)
    approved_by_id = fields.Many2one('res.users', 'Approved By', readonly=True)
    reject_reason = fields.Char('Rejection Reason', readonly=True)

    @api.depends('job_id.partner_id')
    def _compute_hourly_rate(self):
        for line in self:
            if not line.hourly_rate:
                partner = line.job_id.partner_id.commercial_partner_id
                line.hourly_rate = partner.jw_labour_rate or line.job_id.company_id.jw_default_labour_rate

    @api.depends('hours', 'hourly_rate')
    def _compute_cost(self):
        for line in self:
            line.cost = line.hours * line.hourly_rate

    @api.constrains('hours', 'date', 'hourly_rate')
    def _check_values(self):
        today = fields.Date.context_today(self)
        for line in self:
            if line.hours <= 0 or line.hours > 1000:
                raise ValidationError(_("Labour hours must be greater than 0 and at most 1000 per entry."))
            if line.hourly_rate < 0:
                raise ValidationError(_("The hourly rate cannot be negative."))
            if line.date and line.date > today:
                raise ValidationError(_("Labour cannot be reported for a future date."))

    def write(self, vals):
        protected = {'hours', 'hourly_rate', 'date', 'operation', 'job_id'}
        if protected & set(vals) and any(l.state == 'approved' for l in self) and not self.env.su:
            raise UserError(_("Approved labour entries cannot be modified."))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_approved(self):
        if any(l.state == 'approved' for l in self):
            raise UserError(_("Approved labour entries cannot be deleted."))

    def _check_manager(self):
        if not self.env.su and not self.env.user.has_group('zivo_job_work_360.group_job_work_manager'):
            raise AccessError(_("Only Job Work Administrators can approve or reject labour hours."))

    def action_submit(self):
        for line in self.filtered(lambda l: l.state in ('draft', 'rejected')):
            line.write({'state': 'submitted', 'reject_reason': False})
        return True

    def action_approve(self):
        self._check_manager()
        for line in self.filtered(lambda l: l.state == 'submitted'):
            line.write({'state': 'approved', 'approved_by_id': self.env.user.id})
        return True

    def action_reject(self, reason=None):
        self._check_manager()
        for line in self.filtered(lambda l: l.state in ('submitted', 'draft')):
            line.write({'state': 'rejected', 'reject_reason': reason or _('Rejected')})
            line.job_id.message_post(body=_("Labour entry of %(date)s (%(hours)s h, %(op)s) rejected.",
                                            date=line.date, hours=line.hours, op=line.operation))
        return True

    def action_reset_draft(self):
        self.filtered(lambda l: l.state == 'rejected').write({'state': 'draft', 'reject_reason': False})
        return True
