# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .zivo_track_device import expense_currency

VEHICLE_TYPES = [
    ('two_wheeler', 'Two-wheeler (own)'),
    ('four_wheeler', 'Car (own)'),
    ('public', 'Public transport / Cab'),
    ('company', 'Company vehicle (no mileage)'),
]


class ZivoExpensePolicy(models.Model):
    """Travel & daily allowance rules applied to the GPS day logs."""
    _name = 'zivo.expense.policy'
    _description = 'Field Expense Policy'
    _inherit = ['mail.thread']
    _order = 'sequence, id'

    name = fields.Char(required=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    currency_id = fields.Many2one('res.currency', compute='_compute_currency_id',
                                  help='Currency of the field expenses (INR / USD toggle in the settings).')
    is_default = fields.Boolean(string='Default Policy', tracking=True,
                                help='Used for salespeople who have no policy of their own.')
    rate_ids = fields.One2many('zivo.expense.policy.rate', 'policy_id', string='Mileage Rates', copy=True)

    # daily allowance
    da_full_amount = fields.Monetary(string='Full Day Allowance', tracking=True)
    da_full_min_hours = fields.Float(string='Full Day After (hours)', default=6.0,
                                     help='Minimum tracked hours in the field for the full daily allowance.')
    da_half_amount = fields.Monetary(string='Half Day Allowance', tracking=True)
    da_half_min_hours = fields.Float(string='Half Day After (hours)', default=3.0)
    da_min_visits = fields.Integer(string='Minimum Customer Visits', default=0,
                                   help='Daily allowance is paid only when at least this many customer visits '
                                        'were checked in (0 = no condition).')
    outstation_km = fields.Float(string='Outstation After (km)', default=0.0,
                                 help='When the tracked distance of the day is above this, the outstation '
                                      'allowance is paid instead of the full day allowance (0 = disabled).')
    da_outstation_amount = fields.Monetary(string='Outstation Allowance')

    # controls
    km_tolerance_pct = fields.Float(string='Km Tolerance (%)', default=10.0,
                                    help='Claimed km above tracked km by more than this are flagged.')
    location_check_km = fields.Float(string='Route Check Distance (km)', default=2.0,
                                     help='Expenses recorded on the phone further than this from the tracked '
                                          'route of the day are flagged (0 = disabled).')
    user_ids = fields.One2many('res.users', 'zivo_expense_policy_id', string='Salespeople')
    user_count = fields.Integer(compute='_compute_user_count')
    note = fields.Html(string='Policy Notes')

    @api.depends('company_id')
    def _compute_currency_id(self):
        for policy in self:
            policy.currency_id = expense_currency(self.env, policy.company_id or self.env.company)

    @api.depends('user_ids')
    def _compute_user_count(self):
        for policy in self:
            policy.user_count = len(policy.user_ids)

    @api.constrains('da_full_min_hours', 'da_half_min_hours', 'km_tolerance_pct', 'location_check_km',
                    'outstation_km', 'da_full_amount', 'da_half_amount', 'da_outstation_amount', 'da_min_visits')
    def _check_values(self):
        for policy in self:
            numbers = [policy.da_full_min_hours, policy.da_half_min_hours, policy.km_tolerance_pct,
                       policy.location_check_km, policy.outstation_km, policy.da_full_amount,
                       policy.da_half_amount, policy.da_outstation_amount, policy.da_min_visits]
            if any(n < 0 for n in numbers):
                raise ValidationError(_('Policy values cannot be negative.'))
            if policy.da_half_amount and policy.da_half_min_hours > policy.da_full_min_hours:
                raise ValidationError(_('The half day threshold must be lower than the full day threshold.'))

    @api.constrains('is_default', 'company_id', 'active')
    def _check_single_default(self):
        for policy in self.filtered(lambda p: p.is_default and p.active):
            others = self.search_count([('id', '!=', policy.id), ('is_default', '=', True),
                                        ('company_id', '=', policy.company_id.id)])
            if others:
                raise ValidationError(_('Only one default expense policy is allowed per company.'))

    def write(self, vals):
        res = super().write(vals)
        # allowances of days not claimed yet follow the new rules
        self.env['zivo.track.session'].sudo().search([
            ('claim_id', '=', False), ('user_id.zivo_expense_policy_id', 'in', self.ids),
        ])._compute_allowances()
        defaults = self.filtered('is_default')
        if defaults:
            self.env['zivo.track.session'].sudo().search([
                ('claim_id', '=', False), ('user_id.zivo_expense_policy_id', '=', False),
                ('company_id', 'in', defaults.company_id.ids),
            ])._compute_allowances()
        return res

    # ------------------------------------------------------------------
    @api.model
    def _for_user(self, user, company=None):
        company = company or user.company_id
        policy = user.sudo().zivo_expense_policy_id
        if policy and policy.active and policy.company_id == company:
            return policy
        return self.sudo().search([('is_default', '=', True), ('company_id', '=', company.id)], limit=1)

    def _rate_for(self, vehicle_type):
        self.ensure_one()
        return self.rate_ids.filtered(lambda r: r.vehicle_type == vehicle_type)[:1]

    def _travel_allowance(self, vehicle_type, km):
        """Return (rate, cap_km, payable_km, amount) for ``km`` tracked with ``vehicle_type``."""
        self.ensure_one()
        rate = self._rate_for(vehicle_type)
        if not rate or not rate.rate_per_km or km <= 0:
            return 0.0, rate.max_km_per_day if rate else 0.0, 0.0, 0.0
        payable = min(km, rate.max_km_per_day) if rate.max_km_per_day else km
        amount = self.currency_id.round(payable * rate.rate_per_km)
        return rate.rate_per_km, rate.max_km_per_day, payable, amount

    def _daily_allowance(self, hours, visits, km):
        """Return (kind, amount) where kind is none / half / full / outstation."""
        self.ensure_one()
        if self.da_min_visits and visits < self.da_min_visits:
            return 'none', 0.0
        if self.outstation_km and km > self.outstation_km and self.da_outstation_amount \
                and hours >= self.da_half_min_hours:
            return 'outstation', self.da_outstation_amount
        if self.da_full_amount and hours >= self.da_full_min_hours:
            return 'full', self.da_full_amount
        if self.da_half_amount and hours >= self.da_half_min_hours:
            return 'half', self.da_half_amount
        return 'none', 0.0

    def action_view_users(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Salespeople'),
            'res_model': 'res.users',
            'view_mode': 'list,form',
            'domain': [('zivo_expense_policy_id', '=', self.id)],
        }


class ZivoExpensePolicyRate(models.Model):
    _name = 'zivo.expense.policy.rate'
    _description = 'Mileage Rate'
    _order = 'policy_id, vehicle_type'

    policy_id = fields.Many2one('zivo.expense.policy', required=True, ondelete='cascade', index=True)
    currency_id = fields.Many2one(related='policy_id.currency_id')
    vehicle_type = fields.Selection(VEHICLE_TYPES, required=True)
    rate_per_km = fields.Monetary(string='Rate per km')
    max_km_per_day = fields.Float(string='Max km per Day', help='Mileage is paid up to this distance per day '
                                                                '(0 = no limit).')

    _policy_vehicle_unique = models.Constraint('UNIQUE(policy_id, vehicle_type)',
                                               'Only one rate per vehicle type in a policy.')

    @api.constrains('rate_per_km', 'max_km_per_day')
    def _check_positive(self):
        for rate in self:
            if rate.rate_per_km < 0 or rate.max_km_per_day < 0:
                raise ValidationError(_('Mileage rates and limits cannot be negative.'))
