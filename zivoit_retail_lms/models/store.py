# -*- coding: utf-8 -*-
from dateutil.relativedelta import relativedelta

from odoo import api, fields, models

STORE_FORMATS = [
    ('standard', 'Company-owned Store'),
    ('flagship', 'Flagship / Large Format'),
    ('franchise', 'Franchise Store'),
    ('sis', 'Shop-in-Shop / Concession'),
    ('outlet', 'Outlet / Factory Store'),
    ('kiosk', 'Kiosk / Pop-up'),
    ('dark', 'Dark Store / Fulfilment Centre'),
]


class ZlmsStore(models.Model):
    _name = 'zlms.store'
    _description = 'Retail Store'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'region_id, name'

    name = fields.Char(required=True, tracking=True)
    code = fields.Char(string='Store Code', required=True, tracking=True)
    store_format = fields.Selection(STORE_FORMATS, default='standard', required=True, tracking=True)
    region_id = fields.Many2one('zlms.region', string='Region', tracking=True)
    city = fields.Char()
    state_id = fields.Many2one('res.country.state', string='State')
    mall_name = fields.Char(string='Mall / High Street')
    opening_date = fields.Date()
    area_sqft = fields.Integer(string='Area (sq. ft)')
    manager_user_id = fields.Many2one('res.users', string='Store Manager (User)', tracking=True,
                                      help='This user can follow and sign off training for the store team.')
    area_manager_user_id = fields.Many2one(related='region_id.manager_user_id', string='Area Manager', store=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    active = fields.Boolean(default=True)
    color = fields.Integer()

    learner_ids = fields.One2many('zlms.learner', 'store_id', string='Store Team')
    kpi_ids = fields.One2many('zlms.store.kpi', 'store_id', string='Store KPIs')
    headcount = fields.Integer(compute='_compute_learning_stats')
    avg_completion = fields.Float(string='Avg. Completion %', compute='_compute_learning_stats')
    onboarding_completion = fields.Float(string='Onboarding Completion %', compute='_compute_learning_stats')
    overdue_count = fields.Integer(string='Overdue Assignments', compute='_compute_learning_stats')
    certified_count = fields.Integer(string='Valid Certificates', compute='_compute_learning_stats')
    avg_score = fields.Float(string='Avg. Assessment Score', compute='_compute_learning_stats')
    readiness_index = fields.Float(string='Store Readiness Index', compute='_compute_learning_stats',
                                   help='Weighted index: 50% completion, 30% assessment score, 20% on-time.')
    last_conversion = fields.Float(string='Last Conversion %', compute='_compute_last_kpi')
    last_atv = fields.Float(string='Last ATV', compute='_compute_last_kpi')

    _code_uniq = models.Constraint('unique(code, company_id)', 'Store code must be unique per company.')

    @api.depends('learner_ids.assignment_ids.progress', 'learner_ids.assignment_ids.state')
    def _compute_learning_stats(self):
        Assignment = self.env['zlms.assignment'].sudo()
        Certificate = self.env['zlms.certificate'].sudo()
        for store in self:
            learners = store.learner_ids.filtered('active')
            store.headcount = len(learners)
            assignments = Assignment.search([('store_id', '=', store.id), ('state', '!=', 'cancelled')]) if store.id else Assignment
            store.avg_completion = round(sum(assignments.mapped('progress')) / len(assignments), 1) if assignments else 0.0
            onboarding = assignments.filtered(lambda a: a.path_type == 'onboarding')
            store.onboarding_completion = round(sum(onboarding.mapped('progress')) / len(onboarding), 1) if onboarding else 0.0
            store.overdue_count = len(assignments.filtered('is_overdue'))
            store.certified_count = Certificate.search_count([('store_id', '=', store.id), ('state', 'in', ('valid', 'expiring'))]) if store.id else 0
            scored = assignments.filtered(lambda a: a.score > 0)
            store.avg_score = round(sum(scored.mapped('score')) / len(scored), 1) if scored else 0.0
            on_time = (1 - (store.overdue_count / len(assignments))) * 100 if assignments else 0.0
            store.readiness_index = round(0.5 * store.avg_completion + 0.3 * store.avg_score + 0.2 * on_time, 1) if assignments else 0.0

    def _compute_last_kpi(self):
        for store in self:
            kpi = store.kpi_ids.sorted('period_date', reverse=True)[:1]
            store.last_conversion = kpi.conversion_rate
            store.last_atv = kpi.atv

    def action_view_learners(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_retail_lms.zlms_learner_action')
        action['domain'] = [('store_id', '=', self.id)]
        action['context'] = {'default_store_id': self.id}
        return action

    def action_view_assignments(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_retail_lms.zlms_assignment_action')
        action['domain'] = [('store_id', '=', self.id)]
        action['context'] = {}
        return action

    def action_ai_store_insight(self):
        self.ensure_one()
        insight = self.env['zlms.ai.insight'].generate_insight('store', store=self)
        return insight._open_form()


class ZlmsStoreKpi(models.Model):
    _name = 'zlms.store.kpi'
    _description = 'Store Monthly Performance KPI'
    _order = 'period_date desc, store_id'
    _rec_name = 'period_date'

    store_id = fields.Many2one('zlms.store', required=True, ondelete='cascade', index=True)
    region_id = fields.Many2one(related='store_id.region_id', store=True)
    period_date = fields.Date(string='Month', required=True,
                              default=lambda self: fields.Date.today().replace(day=1))
    footfall = fields.Integer()
    bills = fields.Integer(string='Bills / Transactions')
    units_sold = fields.Integer()
    net_sales = fields.Float()
    csat_score = fields.Float(string='CSAT / NPS (0-100)')
    mystery_shopper_score = fields.Float(string='Mystery Shopper (0-100)')
    conversion_rate = fields.Float(string='Conversion %', compute='_compute_ratios', store=True, aggregator='avg')
    atv = fields.Float(string='Avg. Transaction Value', compute='_compute_ratios', store=True, aggregator='avg')
    upt = fields.Float(string='Units per Transaction', compute='_compute_ratios', store=True, aggregator='avg')
    training_completion = fields.Float(string='Training Completion % (snapshot)', aggregator='avg',
                                       help='Store average training completion when the KPI was recorded.')

    _store_period_uniq = models.Constraint('unique(store_id, period_date)', 'One KPI record per store per month.')

    @api.depends('footfall', 'bills', 'units_sold', 'net_sales')
    def _compute_ratios(self):
        for kpi in self:
            kpi.conversion_rate = round(100.0 * kpi.bills / kpi.footfall, 2) if kpi.footfall else 0.0
            kpi.atv = round(kpi.net_sales / kpi.bills, 2) if kpi.bills else 0.0
            kpi.upt = round(kpi.units_sold / kpi.bills, 2) if kpi.bills else 0.0

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('period_date'):
                vals['period_date'] = fields.Date.to_date(vals['period_date']).replace(day=1)
            if 'training_completion' not in vals and vals.get('store_id'):
                vals['training_completion'] = self.env['zlms.store'].browse(vals['store_id']).avg_completion
        return super().create(vals_list)

    @api.model
    def _previous_period(self, date):
        return date - relativedelta(months=1)
