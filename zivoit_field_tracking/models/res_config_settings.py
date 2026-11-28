# -*- coding: utf-8 -*-
from odoo import api, fields, models

from .zivo_track_device import EXPENSE_CURRENCIES, expense_currency

P = 'zivoit_field_tracking.'


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    zivo_interval_sec = fields.Integer(string='GPS Interval (seconds)', default=60,
                                       config_parameter=P + 'interval_sec')
    zivo_min_distance_m = fields.Integer(string='Minimum Movement (m)', default=25,
                                         config_parameter=P + 'min_distance_m')
    zivo_max_accuracy_m = fields.Integer(string='Ignore Fixes Worse Than (m)', default=150,
                                         config_parameter=P + 'max_accuracy_m')
    zivo_offline_threshold_min = fields.Integer(string='Offline After Upload Delay (min)', default=3,
                                                config_parameter=P + 'offline_threshold_min')
    zivo_live_minutes = fields.Integer(string='"Live" Within (min)', default=5,
                                       config_parameter=P + 'live_minutes')
    zivo_delayed_minutes = fields.Integer(string='"Delayed" Within (min)', default=30,
                                          config_parameter=P + 'delayed_minutes')
    zivo_geofence_radius_m = fields.Integer(string='Visit Geo-fence Radius (m)', default=200,
                                            config_parameter=P + 'geofence_radius_m')
    zivo_learn_customer_location = fields.Boolean(string='Learn Customer Location From First Visit',
                                                  config_parameter=P + 'learn_customer_location')
    zivo_retention_days = fields.Integer(string='Keep Location History (days)', default=180,
                                         config_parameter=P + 'retention_days')
    zivo_max_speed_kmh = fields.Integer(string='GPS Spike Filter (km/h)', default=250,
                                        config_parameter=P + 'max_speed_kmh')
    zivo_stop_radius_m = fields.Integer(string='Stop Radius (m)', default=100, config_parameter=P + 'stop_radius_m')
    zivo_stop_minutes = fields.Integer(string='Stop Minimum Duration (min)', default=10,
                                       config_parameter=P + 'stop_minutes')
    zivo_tile_url = fields.Char(string='Map Tile URL',
                                default='https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
                                config_parameter=P + 'tile_url')
    zivo_tile_attribution = fields.Char(string='Map Attribution', default='&copy; OpenStreetMap contributors',
                                        config_parameter=P + 'tile_attribution')
    zivo_expense_auto_post = fields.Boolean(
        string='Send Approved Claims to Odoo Expenses',
        help='When a claim is approved, create the approved Odoo Expenses automatically '
             '(ready to post and reimburse in Accounting).')
    zivo_expense_auto_claim = fields.Boolean(
        string='Prepare Monthly Claims', config_parameter=P + 'expense_auto_claim',
        help='On the 1st of every month, prepare a draft claim of the previous month for every salesperson.')
    zivo_expense_currency = fields.Selection(
        [('INR', '₹ INR - Indian Rupee'), ('USD', '$ USD - US Dollar')], string='Expense Currency',
        help='Currency of mileage rates, allowances, limits, bills and claims (phone app and PDF included). '
             'Claims already created keep their currency.')
    zivo_expense_default_policy_id = fields.Many2one(
        'zivo.expense.policy', string='Default Expense Policy', compute='_compute_zivo_default_policy',
        readonly=False, domain="[('company_id', '=', company_id)]")

    @api.depends('company_id')
    def _compute_zivo_default_policy(self):
        Policy = self.env['zivo.expense.policy'].sudo()
        for settings in self:
            settings.zivo_expense_default_policy_id = Policy.search(
                [('is_default', '=', True), ('company_id', '=', settings.company_id.id)], limit=1)

    @api.model
    def get_values(self):
        res = super().get_values()
        raw = self.env['ir.config_parameter'].sudo().get_param(P + 'expense_auto_post')
        res['zivo_expense_auto_post'] = raw in (None, False, '') or raw not in ('0', 'False', 'false')
        code = expense_currency(self.env, self.env.company).name
        res['zivo_expense_currency'] = code if code in EXPENSE_CURRENCIES else False
        return res

    def set_values(self):
        super().set_values()
        self.env['ir.config_parameter'].sudo().set_param(P + 'expense_auto_post',
                                                         '1' if self.zivo_expense_auto_post else '0')
        for settings in self:
            policy = settings.zivo_expense_default_policy_id
            if policy and not policy.is_default:
                Policy = self.env['zivo.expense.policy'].sudo()
                Policy.search([('is_default', '=', True), ('company_id', '=', settings.company_id.id)]).write(
                    {'is_default': False})
                policy.sudo().write({'is_default': True})
        self._zivo_apply_currency()

    def _zivo_apply_currency(self):
        code = self.zivo_expense_currency
        if not code:
            return
        Param = self.env['ir.config_parameter'].sudo()
        previous = Param.get_param(P + 'expense_currency')
        currency = self.env['res.currency'].sudo().with_context(active_test=False).search([('name', '=', code)],
                                                                                         limit=1)
        if currency and not currency.active:
            currency.active = True
        if previous == code:
            return
        Param.set_param(P + 'expense_currency', code)
        for model in ('zivo.expense.policy', 'zivo.expense.policy.rate', 'zivo.expense.category',
                      'zivo.track.session'):
            self.env[model].invalidate_model(['currency_id'])
        # allowances of the days not claimed yet are now in the new currency
        sessions = self.env['zivo.track.session'].sudo().search([('claim_id', '=', False)])
        sessions._compute_allowances()
        for fname in ('bill_amount', 'allowance_total'):
            self.env.add_to_compute(sessions._fields[fname], sessions)
        sessions.flush_recordset()

    def action_zivo_currency_rates(self):
        return {
            'type': 'ir.actions.act_window', 'name': 'Currencies', 'res_model': 'res.currency',
            'view_mode': 'list,form', 'domain': [('name', 'in', list(EXPENSE_CURRENCIES))],
            'context': {'active_test': False},
        }
