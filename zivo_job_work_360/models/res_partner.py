# -*- coding: utf-8 -*-
from odoo import _, fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    jw_is_job_worker = fields.Boolean(
        'Job Worker', help="Mark this contact as a job worker / subcontractor for Job Work 360.")
    jw_job_ids = fields.One2many('zivo.job.work', 'partner_id', string='Job Work Orders',
                                 groups='zivo_job_work_360.group_job_work_viewer')
    jw_job_count = fields.Integer('Job Work Count', compute='_compute_jw_stats', compute_sudo=True)
    jw_open_job_count = fields.Integer('Open Job Work Count', compute='_compute_jw_stats', compute_sudo=True)
    jw_score_id = fields.Many2one(
        'zivo.job.work.vendor.score', 'Latest Scorecard', compute='_compute_jw_score', compute_sudo=True,
        groups='zivo_job_work_360.group_job_work_viewer')
    jw_score = fields.Float('Vendor Score', compute='_compute_jw_score', compute_sudo=True)
    jw_grade = fields.Selection([('A', 'A'), ('B', 'B'), ('C', 'C'), ('D', 'D')], 'Vendor Grade',
                                compute='_compute_jw_score', compute_sudo=True)

    def _compute_jw_stats(self):
        Job = self.env['zivo.job.work']
        all_groups = dict(Job._read_group(
            [('partner_id.commercial_partner_id', 'in', self.commercial_partner_id.ids)],
            ['partner_id'], ['__count']))
        open_groups = dict(Job._read_group(
            [('partner_id.commercial_partner_id', 'in', self.commercial_partner_id.ids),
             ('state', 'not in', ('draft', 'closed', 'cancel'))],
            ['partner_id'], ['__count']))
        for partner in self:
            commercial = partner.commercial_partner_id
            partner.jw_job_count = sum(
                count for p, count in all_groups.items() if p.commercial_partner_id == commercial)
            partner.jw_open_job_count = sum(
                count for p, count in open_groups.items() if p.commercial_partner_id == commercial)

    def _compute_jw_score(self):
        Score = self.env['zivo.job.work.vendor.score']
        for partner in self:
            score = Score.search(
                [('partner_id', '=', partner.commercial_partner_id.id)],
                order='date_to desc, id desc', limit=1)
            partner.jw_score_id = score
            partner.jw_score = score.score
            partner.jw_grade = score.grade

    def action_view_jw_jobs(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivo_job_work_360.action_zivo_job_work')
        action['domain'] = [('partner_id', 'child_of', self.commercial_partner_id.id)]
        action['context'] = {'default_partner_id': self.commercial_partner_id.id}
        return action

    def action_view_jw_scores(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivo_job_work_360.action_zivo_job_work_vendor_score')
        action['domain'] = [('partner_id', '=', self.commercial_partner_id.id)]
        return action

    def _jw_get_vendor_location(self, company):
        """Return the custody location of this job worker for ``company``.

        When the company works with dedicated locations, a child location of the company
        subcontracting location is created the first time and stored on the partner.
        """
        partner = self.commercial_partner_id.with_company(company)
        location = partner.property_stock_subcontractor
        main_location = company.subcontracting_location_id
        if not main_location:
            company.sudo()._create_subcontracting_location()
            main_location = company.subcontracting_location_id
        if company.jw_auto_vendor_location and (not location or location == main_location):
            location = self.env['stock.location'].sudo().search([
                ('location_id', '=', main_location.id),
                ('company_id', '=', company.id),
                ('name', '=', partner.name),
            ], limit=1)
            if not location:
                location = self.env['stock.location'].sudo().create({
                    'name': partner.name or _('Job Worker'),
                    'usage': 'internal',
                    'location_id': main_location.id,
                    'company_id': company.id,
                })
            partner.sudo().with_company(company).property_stock_subcontractor = location
        return location or main_location
