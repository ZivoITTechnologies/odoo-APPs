# -*- coding: utf-8 -*-
from odoo import _, api, fields, models


class ZivoJobWorkNCR(models.Model):
    _name = 'zivo.job.work.ncr'
    _description = 'Job Work Non-Conformance Report'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'
    _check_company_auto = True

    name = fields.Char('NCR', required=True, readonly=True, copy=False, default=lambda self: _('New'))
    job_id = fields.Many2one('zivo.job.work', 'Job Work', required=True, ondelete='cascade', index=True,
                             check_company=True)
    qc_id = fields.Many2one('zivo.job.work.qc', 'Inspection', domain="[('job_id', '=', job_id)]")
    exception_id = fields.Many2one('zivo.job.work.exception', 'Exception', readonly=True)
    company_id = fields.Many2one(related='job_id.company_id', store=True)
    partner_id = fields.Many2one(related='job_id.partner_id', store=True, string='Job Worker')
    product_id = fields.Many2one(related='job_id.product_id', store=True, string='Product')
    date = fields.Date(default=fields.Date.context_today)
    user_id = fields.Many2one('res.users', 'Responsible', default=lambda self: self.env.user, tracking=True)
    severity = fields.Selection([('minor', 'Minor'), ('major', 'Major'), ('critical', 'Critical')],
                                default='major', tracking=True)
    description = fields.Text('Non-Conformance', required=True)
    root_cause = fields.Text('Root Cause')
    corrective_action = fields.Text('Corrective Action')
    disposition = fields.Selection([
        ('rework', 'Rework'),
        ('scrap', 'Scrap'),
        ('return', 'Return to Job Worker'),
        ('deviation', 'Accept Under Deviation'),
    ], 'Disposition', tracking=True)
    state = fields.Selection([
        ('open', 'Open'),
        ('in_progress', 'In Progress'),
        ('closed', 'Closed'),
    ], default='open', required=True, tracking=True, index=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('zivo.job.work.ncr') or _('New')
        return super().create(vals_list)

    def action_start(self):
        self.filtered(lambda n: n.state == 'open').write({'state': 'in_progress'})
        return True

    def action_close(self):
        for ncr in self:
            ncr.state = 'closed'
            if ncr.exception_id and ncr.exception_id.state == 'open':
                ncr.exception_id.sudo().write({
                    'state': 'resolved',
                    'resolution': _("NCR %(ncr)s closed. Corrective action: %(action)s",
                                    ncr=ncr.name, action=ncr.corrective_action or '-'),
                    'resolved_by_id': self.env.user.id,
                    'date_resolved': fields.Datetime.now(),
                })
        return True

    def action_reopen(self):
        self.write({'state': 'open'})
        return True
