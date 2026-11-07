# -*- coding: utf-8 -*-
from datetime import date, timedelta

from odoo import _, api, fields, models
from odoo.tools.misc import format_date


class ZivoJobWork(models.Model):
    _inherit = 'zivo.job.work'

    challan_ids = fields.One2many('zivo.job.work.challan', 'job_id', 'Challans')
    challan_count = fields.Integer(compute='_compute_challan_count', compute_sudo=True)

    def _compute_challan_count(self):
        for job in self:
            job.challan_count = len(job.challan_ids)

    def action_view_challans(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivo_job_work_360.action_zivo_job_work_challan')
        action['domain'] = [('job_id', '=', self.id)]
        action['context'] = {'default_job_id': self.id, 'default_partner_id': self.partner_id.id,
                             'default_company_id': self.company_id.id}
        return action

    @api.model
    def _jw_in_financial_year(self, today=None):
        today = today or fields.Date.context_today(self)
        start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
        return start, date(start.year + 1, 3, 31)

    @api.model
    def _cron_jw_in_return_alerts(self):
        if not self.env['res.company']._jw_in_enabled():
            return
        Material = self.env['zivo.job.work.material']
        today = fields.Date.context_today(self)
        materials = Material.search([('balance_qty', '>', 0), ('date_first_issue', '!=', False),
                                     ('job_id.state', 'not in', ('draft', 'cancel', 'closed'))])
        for mat in materials:
            due = mat.jw_in_due_date
            if not due:
                continue
            alert_from = due - timedelta(days=mat.company_id.jw_in_alert_days or 0)
            active = today >= alert_from
            mat.job_id._jw_set_exception(
                'statutory', active,
                _("%(qty)s %(uom)s of %(product)s sent on %(sent)s must be received back by %(due)s "
                  "(configured return period).",
                  qty=mat.balance_qty, uom=mat.product_uom_id.name, product=mat.product_id.display_name,
                  sent=format_date(self.env, mat.date_first_issue), due=format_date(self.env, due)),
                severity='high' if today > due else 'medium', material=mat)

    @api.model
    def get_jw_dashboard_data(self):
        data = super().get_jw_dashboard_data()
        if not self.env['res.company']._jw_in_enabled():
            return data
        companies = self.env.companies
        fy_start, fy_end = self._jw_in_financial_year()
        Challan = self.env['zivo.job.work.challan']
        sent = Challan.search_count([('state', '=', 'confirmed'), ('company_id', 'in', companies.ids),
                                     ('date', '>=', fy_start), ('date', '<=', fy_end)])
        received = self.env['zivo.job.work.production'].search_count([
            ('company_id', 'in', companies.ids), ('state', '!=', 'cancel'),
            ('date', '>=', fields.Datetime.to_datetime(fy_start))])
        pending = self.env['zivo.job.work.material'].search_count([
            ('company_id', 'in', companies.ids), ('balance_qty', '>', 0),
            ('job_id.state', 'not in', ('draft', 'cancel', 'closed'))])
        jobs = self.search([('company_id', 'in', companies.ids), ('date_issued', '>=',
                                                                  fields.Datetime.to_datetime(fy_start))])
        loss_value = sum(jobs.mapped('material_scrap_value'))
        statutory = self.env['zivo.job.work.exception'].search_count([
            ('company_id', 'in', companies.ids), ('state', 'in', ('open', 'to_approve')), ('exception_type', '=', 'statutory')])
        currency = self.env.company.currency_id
        data['compliance'] = {
            'title': _('Compliance Control Center (India)'),
            'period': _('Financial year %(start)s - %(end)s', start=fy_start.year, end=fy_end.year),
            'kpis': [
                {'label': _('Challans Sent'), 'value': sent},
                {'label': _('Receipts Back'), 'value': received},
                {'label': _('Pending Lines'), 'value': pending},
                {'label': _('Loss / Waste'), 'value': currency.format(loss_value)},
                {'label': _('Return Period Alerts'), 'value': statutory, 'alert': bool(statutory)},
                {'label': _('Overdue Jobs'), 'value': data['kpis']['overdue_jobs'],
                 'alert': bool(data['kpis']['overdue_jobs'])},
            ],
            'action': 'zivo_job_work_360.action_zivo_job_work_itc04_wizard',
            'note': _('Prepared data only. Validate periods, forms and values against current GST requirements.'),
        }
        return data


class ZivoJobWorkMaterial(models.Model):
    _inherit = 'zivo.job.work.material'

    jw_in_due_date = fields.Date('Statutory Return Due', compute='_compute_jw_in_due_date')

    @api.depends('date_first_issue', 'is_capital_goods', 'company_id')
    def _compute_jw_in_due_date(self):
        for mat in self:
            if mat.date_first_issue:
                days = mat.company_id.jw_in_return_days_capital if mat.is_capital_goods \
                    else mat.company_id.jw_in_return_days_inputs
                mat.jw_in_due_date = mat.date_first_issue.date() + timedelta(days=days or 0) if days else False
            else:
                mat.jw_in_due_date = False
