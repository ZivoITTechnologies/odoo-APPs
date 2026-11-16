# -*- coding: utf-8 -*-
from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models

EXPIRING_WINDOW_DAYS = 30


class ZlmsCertificate(models.Model):
    _name = 'zlms.certificate'
    _description = 'Retail Learning Certificate'
    _inherit = ['mail.thread']
    _order = 'issue_date desc, id desc'

    name = fields.Char(string='Certificate No.', readonly=True, copy=False, default='/')
    learner_id = fields.Many2one('zlms.learner', required=True, ondelete='cascade', index=True)
    learner_user_id = fields.Many2one(related='learner_id.user_id', store=True)
    store_id = fields.Many2one(related='learner_id.store_id', store=True)
    region_id = fields.Many2one(related='learner_id.region_id', store=True)
    store_manager_user_id = fields.Many2one(related='learner_id.store_id.manager_user_id', store=True)
    role_id = fields.Many2one(related='learner_id.role_id', store=True)
    path_id = fields.Many2one('zlms.learning.path', required=True, ondelete='restrict')
    assignment_id = fields.Many2one('zlms.assignment', ondelete='set null')
    issue_date = fields.Date(default=fields.Date.context_today, required=True)
    expiry_date = fields.Date(compute='_compute_expiry_date', store=True, readonly=False)
    score = fields.Float(string='Score %')
    state = fields.Selection([('valid', 'Valid'), ('expiring', 'Expiring Soon'), ('expired', 'Expired'),
                              ('revoked', 'Revoked')], default='valid', required=True, tracking=True, index=True)
    company_id = fields.Many2one(related='learner_id.company_id', store=True)

    @api.depends('issue_date', 'path_id.certificate_validity_months')
    def _compute_expiry_date(self):
        for cert in self:
            months = cert.path_id.certificate_validity_months
            cert.expiry_date = cert.issue_date + relativedelta(months=months) if (months and cert.issue_date) else False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code('zlms.certificate') or '/'
        certs = super().create(vals_list)
        certs._refresh_state()
        return certs

    def _refresh_state(self):
        today = fields.Date.context_today(self)
        for cert in self.filtered(lambda c: c.state != 'revoked'):
            if cert.expiry_date and cert.expiry_date < today:
                state = 'expired'
            elif cert.expiry_date and cert.expiry_date <= today + timedelta(days=EXPIRING_WINDOW_DAYS):
                state = 'expiring'
            else:
                state = 'valid'
            if cert.state != state:
                cert.state = state

    def action_revoke(self):
        self.write({'state': 'revoked'})

    def action_print(self):
        return self.env.ref('zivoit_retail_lms.action_report_zlms_certificate').report_action(self)

    @api.model
    def _cron_check_expiry(self):
        certs = self.search([('state', 'in', ('valid', 'expiring')), ('expiry_date', '!=', False)])
        before = {c.id: c.state for c in certs}
        certs._refresh_state()
        for cert in certs:
            if before[cert.id] == cert.state:
                continue
            if cert.state == 'expiring' and cert.store_manager_user_id:
                cert.activity_schedule('mail.mail_activity_data_todo', date_deadline=cert.expiry_date,
                                       summary=_('Certificate expiring: %s', cert.display_name),
                                       user_id=cert.store_manager_user_id.id)
            if cert.state == 'expired':
                if cert.assignment_id and cert.assignment_id.state == 'completed':
                    cert.assignment_id.state = 'expired'
                if cert.path_id.state == 'published':
                    cert.path_id._assign_to_learners(cert.learner_id)

    @api.depends('name', 'learner_id', 'path_id')
    def _compute_display_name(self):
        for cert in self:
            cert.display_name = '%s - %s (%s)' % (cert.name, cert.learner_id.name or '', cert.path_id.name or '')
