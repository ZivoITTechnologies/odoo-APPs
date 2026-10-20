# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError

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
        ('resolved', 'Resolved'),
        ('waived', 'Waived'),
    ], default='open', required=True, tracking=True, index=True)
    resolution = fields.Text('Resolution')
    resolved_by_id = fields.Many2one('res.users', 'Resolved By', readonly=True)
    date_resolved = fields.Datetime('Resolved On', readonly=True)

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

    def action_resolve(self):
        for exc in self.filtered(lambda e: e.state == 'open'):
            exc.write({'state': 'resolved', 'resolved_by_id': self.env.user.id,
                       'date_resolved': fields.Datetime.now()})
        return True

    def action_waive(self):
        if not self.env.user.has_group('zivo_job_work_360.group_job_work_manager'):
            raise AccessError(_("Only Job Work Administrators can waive exceptions."))
        for exc in self.filtered(lambda e: e.state == 'open'):
            exc.write({'state': 'waived', 'resolved_by_id': self.env.user.id,
                       'date_resolved': fields.Datetime.now()})
            exc.job_id.message_post(body=_("Exception waived: %(name)s. %(why)s",
                                           name=exc.name, why=exc.resolution or ''))
        return True

    def action_reopen(self):
        self.write({'state': 'open', 'resolved_by_id': False, 'date_resolved': False})
        return True

    def action_open_job(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'zivo.job.work', 'res_id': self.job_id.id,
                'view_mode': 'form', 'views': [(False, 'form')]}
