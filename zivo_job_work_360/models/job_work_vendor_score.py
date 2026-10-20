# -*- coding: utf-8 -*-
from datetime import datetime, time, timedelta

from odoo import _, api, fields, models


class ZivoJobWorkVendorScore(models.Model):
    _name = 'zivo.job.work.vendor.score'
    _description = 'Job Worker Scorecard'
    _order = 'date_to desc, score desc'
    _rec_name = 'partner_id'

    partner_id = fields.Many2one('res.partner', 'Job Worker', required=True, index=True, ondelete='cascade')
    company_id = fields.Many2one('res.company', 'Company', required=True, default=lambda self: self.env.company)
    date_from = fields.Date('From', required=True)
    date_to = fields.Date('To', required=True)
    is_latest = fields.Boolean('Latest', default=True, index=True)
    job_count = fields.Integer('Completed Jobs')
    open_job_count = fields.Integer('Open Jobs')
    on_time_rate = fields.Float('On-time Delivery (%)', aggregator='avg')
    avg_delay_days = fields.Float('Average Delay (days)', aggregator='avg')
    quality_rate = fields.Float('Quality Acceptance (%)', aggregator='avg')
    yield_rate = fields.Float('Yield (%)', aggregator='avg')
    scrap_rate = fields.Float('Scrap (%)', aggregator='avg')
    material_variance_rate = fields.Float('Material Variance (%)', aggregator='avg')
    cost_variance_rate = fields.Float('Cost Variance (%)', aggregator='avg')
    response_hours = fields.Float('Avg. Response (hours)', aggregator='avg')
    score_on_time = fields.Float('On-time Score')
    score_quality = fields.Float('Quality Score')
    score_yield = fields.Float('Yield Score')
    score_scrap = fields.Float('Scrap Score')
    score_material = fields.Float('Material Score')
    score_cost = fields.Float('Cost Score')
    score_response = fields.Float('Responsiveness Score')
    responsiveness = fields.Selection([
        ('excellent', 'Excellent'), ('good', 'Good'), ('average', 'Average'), ('poor', 'Poor'), ('na', 'N/A'),
    ], default='na')
    score = fields.Float('Overall Score', aggregator='avg')
    grade = fields.Selection([('A', 'A'), ('B', 'B'), ('C', 'C'), ('D', 'D')], 'Grade')

    @api.model
    def _jw_kpis(self, jobs, company):
        """Return KPI values and 0-100 scores for a set of finished jobs."""
        vals = {'job_count': len(jobs)}
        scores = {}
        if not jobs:
            return vals, scores
        on_time = [j for j in jobs if j.date_completed and j.date_completed <= j.date_expected]
        delays = [max((j.date_completed - j.date_expected).total_seconds() / 86400.0, 0.0)
                  for j in jobs if j.date_completed]
        vals['on_time_rate'] = len(on_time) / len(jobs) * 100.0
        vals['avg_delay_days'] = sum(delays) / len(delays) if delays else 0.0
        scores['on_time'] = vals['on_time_rate']

        inspected = sum(j.qty_accepted + j.qty_rejected + j.qty_rework + j.qty_qc_scrap for j in jobs)
        if inspected:
            vals['quality_rate'] = sum(jobs.mapped('qty_accepted')) / inspected * 100.0
            scores['quality'] = vals['quality_rate']

        yielded = jobs.filtered(lambda j: j.actual_yield > 0)
        if yielded:
            act = sum(yielded.mapped('actual_yield')) / len(yielded)
            exp = sum(yielded.mapped('expected_yield')) / len(yielded) or 100.0
            vals['yield_rate'] = act
            scores['yield'] = min(act / exp * 100.0, 100.0)

        issued_value = sum(jobs.mapped('material_issued_value'))
        if issued_value:
            scrap_value = sum(jobs.mapped('material_scrap_value'))
            vals['scrap_rate'] = scrap_value / issued_value * 100.0
            scores['scrap'] = max(0.0, 100.0 - vals['scrap_rate'] * 10.0)
            variance_value = sum(abs(m.consumption_variance) * m.unit_cost for m in jobs.material_ids)
            vals['material_variance_rate'] = variance_value / issued_value * 100.0
            scores['material'] = max(0.0, 100.0 - vals['material_variance_rate'] * 10.0)

        exp_cost = sum(jobs.mapped('cost_exp_total'))
        if exp_cost:
            vals['cost_variance_rate'] = (sum(jobs.mapped('cost_act_total')) - exp_cost) / exp_cost * 100.0
            scores['cost'] = 100.0 if vals['cost_variance_rate'] <= 0 else max(
                0.0, 100.0 - vals['cost_variance_rate'] * 5.0)

        responded = jobs.filtered(lambda j: j.date_sent and j.vendor_accept_date)
        if responded:
            hours = [max((j.vendor_accept_date - j.date_sent).total_seconds() / 3600.0, 0.0) for j in responded]
            avg = sum(hours) / len(hours)
            vals['response_hours'] = avg
            scores['response'] = 100.0 if avg <= 24 else max(0.0, 100.0 - (avg - 24) / 144.0 * 100.0)
            vals['responsiveness'] = ('excellent' if avg <= 24 else 'good' if avg <= 48
                                      else 'average' if avg <= 96 else 'poor')
        return vals, scores

    @api.model
    def _jw_weighted_score(self, scores, company):
        weights = {
            'on_time': company.jw_weight_on_time, 'quality': company.jw_weight_quality,
            'yield': company.jw_weight_yield, 'scrap': company.jw_weight_scrap,
            'material': company.jw_weight_material, 'cost': company.jw_weight_cost,
            'response': company.jw_weight_response,
        }
        total_weight = sum(weights[k] for k in scores if weights.get(k))
        if not total_weight:
            return 0.0
        return sum(scores[k] * weights[k] for k in scores if weights.get(k)) / total_weight

    @api.model
    def _jw_grade(self, score):
        return 'A' if score >= 90 else 'B' if score >= 75 else 'C' if score >= 60 else 'D'

    @api.model
    def generate_scorecards(self, companies=None):
        companies = companies or self.env.companies
        Job = self.env['zivo.job.work']
        created = self.browse()
        for company in companies:
            date_to = fields.Date.context_today(self)
            date_from = date_to - timedelta(days=company.jw_score_period_days or 90)
            start = datetime.combine(date_from, time.min)
            jobs = Job.search([
                ('company_id', '=', company.id),
                ('state', 'in', ('completed', 'reconciled', 'closed')),
                ('date_completed', '>=', start),
                ('is_rework', '=', False),
            ])
            open_jobs = Job.search([('company_id', '=', company.id),
                                    ('state', 'not in', ('draft', 'closed', 'cancel'))])
            partners = jobs.partner_id.commercial_partner_id
            for partner in partners:
                pjobs = jobs.filtered(lambda j: j.partner_id.commercial_partner_id == partner)
                vals, scores = self._jw_kpis(pjobs, company)
                score = self._jw_weighted_score(scores, company)
                self.search([('partner_id', '=', partner.id), ('company_id', '=', company.id),
                             ('is_latest', '=', True)]).write({'is_latest': False})
                self.search([('partner_id', '=', partner.id), ('company_id', '=', company.id),
                             ('date_to', '=', date_to)]).unlink()
                vals.update({
                    'partner_id': partner.id,
                    'company_id': company.id,
                    'date_from': date_from,
                    'date_to': date_to,
                    'is_latest': True,
                    'open_job_count': len(open_jobs.filtered(
                        lambda j: j.partner_id.commercial_partner_id == partner)),
                    'score_on_time': scores.get('on_time', 0.0),
                    'score_quality': scores.get('quality', 0.0),
                    'score_yield': scores.get('yield', 0.0),
                    'score_scrap': scores.get('scrap', 0.0),
                    'score_material': scores.get('material', 0.0),
                    'score_cost': scores.get('cost', 0.0),
                    'score_response': scores.get('response', 0.0),
                    'score': score,
                    'grade': self._jw_grade(score),
                })
                created |= self.create(vals)
        return created

    @api.model
    def action_generate_scorecards(self):
        self.generate_scorecards()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Scorecards updated'),
                'message': _('Job worker scorecards have been recomputed.'),
                'type': 'success',
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    @api.model
    def _cron_generate_scorecards(self):
        self.generate_scorecards(self.env['res.company'].search([]))
