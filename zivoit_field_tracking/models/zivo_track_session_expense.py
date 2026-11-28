# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .zivo_expense_line import DA_KINDS
from .zivo_expense_policy import VEHICLE_TYPES
from .zivo_track_device import expense_currency

SESSION_SYSTEM_FIELDS = {
    'distance_km', 'duration_hours', 'first_fix', 'last_fix', 'point_count', 'offline_point_count',
    'max_delay_minutes', 'stop_count', 'mock_point_count', 'ta_rate', 'ta_km', 'ta_amount', 'da_kind',
    'da_amount', 'claim_id', 'user_id', 'date', 'company_id', 'start_time', 'end_time', 'state',
}


class ZivoTrackSession(models.Model):
    _inherit = 'zivo.track.session'

    vehicle_type = fields.Selection(VEHICLE_TYPES, string='Travelled By', tracking=True,
                                    help='Chosen on the phone when the day starts. Defines the mileage rate.')
    claim_id = fields.Many2one('zivo.expense.claim', string='Expense Claim', index=True, ondelete='set null',
                               copy=False, readonly=True)
    claim_state = fields.Selection(related='claim_id.state', string='Claim Status')
    currency_id = fields.Many2one('res.currency', compute='_compute_currency_id')
    mock_point_count = fields.Integer(string='Fake GPS Points', readonly=True)
    ta_rate = fields.Monetary(string='Mileage Rate', readonly=True)
    ta_km = fields.Float(string='Payable km', digits=(10, 2), readonly=True)
    ta_amount = fields.Monetary(string='Mileage (TA)', readonly=True)
    da_kind = fields.Selection(DA_KINDS, string='Day Type', readonly=True)
    da_amount = fields.Monetary(string='Daily Allowance (DA)', readonly=True)
    expense_line_ids = fields.One2many('zivo.expense.line', 'session_id', string='Expenses')
    bill_amount = fields.Monetary(string='Bills', compute='_compute_bill_amount', store=True)
    allowance_total = fields.Monetary(string='Day Total', compute='_compute_bill_amount', store=True)

    @api.depends('company_id')
    def _compute_currency_id(self):
        for session in self:
            session.currency_id = expense_currency(self.env, session.company_id or self.env.company)

    @api.depends('expense_line_ids.amount', 'expense_line_ids.line_type', 'expense_line_ids.state',
                 'ta_amount', 'da_amount')
    def _compute_bill_amount(self):
        for session in self:
            currency = expense_currency(self.env, session.company_id or self.env.company)
            bills = session.expense_line_ids.filtered(
                lambda l: l.line_type == 'expense' and l.state != 'rejected' and l.currency_id == currency)
            session.bill_amount = sum(bills.mapped('amount'))
            session.allowance_total = session.ta_amount + session.da_amount + session.bill_amount

    @api.model_create_multi
    def create(self, vals_list):
        users = self.env['res.users'].sudo().browse([v.get('user_id') for v in vals_list if v.get('user_id')])
        default_vehicle = {u.id: u.zivo_vehicle_type for u in users}
        for vals in vals_list:
            if not vals.get('vehicle_type'):
                vals['vehicle_type'] = default_vehicle.get(vals.get('user_id')) or 'two_wheeler'
        sessions = super().create(vals_list)
        sessions._compute_allowances()
        return sessions

    def write(self, vals):
        if not self.env.su and not self.env.user.has_group('zivoit_field_tracking.group_field_manager'):
            locked = set(vals) & SESSION_SYSTEM_FIELDS
            if locked:
                raise AccessError(_('Day log values are measured by GPS and cannot be edited: %s',
                                    ', '.join(sorted(locked))))
        if 'vehicle_type' in vals and not self.env.su:
            if any(s.claim_id and s.claim_id.state not in ('draft', 'cancel') for s in self):
                raise UserError(_('The vehicle of a claimed day cannot be changed.'))
        res = super().write(vals)
        if 'vehicle_type' in vals or 'user_id' in vals:
            self._compute_allowances()
        return res

    def _recompute_stats(self):
        res = super()._recompute_stats()
        for session in self.sudo():
            self.env.cr.execute("SELECT COUNT(*) FROM zivo_track_point WHERE session_id = %s AND is_mock",
                                [session.id])
            session.mock_point_count = self.env.cr.fetchone()[0]
        self._compute_allowances()
        return res

    def _compute_allowances(self):
        """Store the travel and daily allowance earned by the day, according to the policy."""
        Policy = self.env['zivo.expense.policy']
        for session in self.sudo():
            policy = Policy._for_user(session.user_id, session.company_id)
            vals = {'ta_rate': 0.0, 'ta_km': 0.0, 'ta_amount': 0.0, 'da_kind': 'none', 'da_amount': 0.0}
            if policy:
                rate, _cap, km, amount = policy._travel_allowance(session.vehicle_type, session.distance_km)
                kind, da = policy._daily_allowance(session.duration_hours, len(session.visit_ids),
                                                   session.distance_km)
                vals.update({'ta_rate': rate, 'ta_km': km, 'ta_amount': amount, 'da_kind': kind, 'da_amount': da})
            if any(session[k] != v for k, v in vals.items()):
                session.write(vals)
        return True

    def action_view_claim(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'res_model': 'zivo.expense.claim', 'view_mode': 'form',
            'res_id': self.claim_id.id,
        }

    def action_add_expense(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Add Bill'), 'res_model': 'zivo.expense.line',
            'view_mode': 'form', 'target': 'new',
            'context': {'default_user_id': self.user_id.id, 'default_date': self.date,
                        'default_session_id': self.id, 'default_company_id': self.company_id.id},
        }


class ZivoTrackVisit(models.Model):
    _inherit = 'zivo.track.visit'

    @api.model_create_multi
    def create(self, vals_list):
        visits = super().create(vals_list)
        visits.mapped('session_id')._compute_allowances()
        return visits

    def write(self, vals):
        before = self.mapped('session_id')
        res = super().write(vals)
        if 'session_id' in vals:
            (before | self.mapped('session_id'))._compute_allowances()
        return res


class HrExpense(models.Model):
    _inherit = 'hr.expense'

    zivo_claim_id = fields.Many2one('zivo.expense.claim', string='Field Expense Claim', index=True,
                                    readonly=True, copy=False, ondelete='set null')
    zivo_line_id = fields.Many2one('zivo.expense.line', string='Field Expense Line', readonly=True, copy=False,
                                   ondelete='set null')
