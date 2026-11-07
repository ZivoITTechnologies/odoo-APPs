# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _

from .cp_common import CHANNELS


class CpChannelRule(models.Model):
    """What each sales channel (or key account) expects from delivery."""
    _name = 'cp.channel.rule'
    _description = 'Channel Delivery Rule'
    _order = 'sequence, id'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    channel = fields.Selection(CHANNELS, required=True)
    customer_id = fields.Many2one('res.partner', string='Key Account',
                                  help='Leave empty to apply to every customer of the channel.')
    promise_hours = fields.Float(string='Delivery Promise (hours)',
                                 help='Delivery deadline = placement + this many hours, when none is given.')
    appointment_required = fields.Boolean(string='Needs Delivery Appointment',
                                          help='Retail DCs that only receive in a booked slot.')
    grn_required = fields.Boolean(string='Needs GRN with POD')
    serial_scan_required = fields.Boolean(string='Scan Serials at Delivery')
    allowed_transporter_ids = fields.Many2many('res.partner', string='Approved Transporters',
                                               domain=[('cp_is_transporter', '=', True)],
                                               help='Only these transporters are offered loads for this channel.')
    note = fields.Text(string='Packing / Delivery Notes')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    @api.model
    def cp_match(self, channel, partner=None, company=None):
        if not channel:
            return self.browse()
        company = company or self.env.company
        rules = self.search([('channel', '=', channel), '|', ('company_id', '=', company.id),
                             ('company_id', '=', False)])
        commercial = partner.commercial_partner_id if partner else self.env['res.partner']
        specific = rules.filtered(lambda r: r.customer_id and commercial and
                                  r.customer_id.commercial_partner_id == commercial)
        return (specific or rules.filtered(lambda r: not r.customer_id))[:1]


class CpCapacityPlan(models.Model):
    """Weekly truck forecast per lane against what contracts commit, for peak planning."""
    _name = 'cp.capacity.plan'
    _description = 'Capacity Plan'
    _inherit = ['mail.thread']
    _order = 'date_from desc, id desc'

    name = fields.Char(required=True, default=lambda self: _('Capacity plan'))
    date_from = fields.Date(required=True, default=fields.Date.context_today, string='First Week')
    weeks = fields.Integer(default=12, required=True)
    growth_pct = fields.Float(string='Growth vs Last Year (%)', default=10.0)
    line_ids = fields.One2many('cp.capacity.line', 'plan_id', string='Weekly Lines')
    total_forecast = fields.Integer(compute='_compute_totals', string='Trucks Needed')
    total_committed = fields.Integer(compute='_compute_totals', string='Trucks Committed')
    total_gap = fields.Integer(compute='_compute_totals', string='Shortfall')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    @api.depends('line_ids.forecast_trucks', 'line_ids.committed_trucks', 'line_ids.gap')
    def _compute_totals(self):
        for rec in self:
            rec.total_forecast = sum(rec.line_ids.mapped('forecast_trucks'))
            rec.total_committed = sum(rec.line_ids.mapped('committed_trucks'))
            rec.total_gap = sum(g for g in rec.line_ids.mapped('gap') if g > 0)

    def action_suggest(self):
        """Fill the plan from the same weeks last year (or the recent run rate), plus growth."""
        from datetime import timedelta
        Trip = self.env['cp.trip']
        for rec in self:
            rec.line_ids.unlink()
            start = rec.date_from - timedelta(days=rec.date_from.weekday())
            recent_from = fields.Datetime.to_datetime(start - timedelta(weeks=8))
            recent = Trip.search([('state', '!=', 'cancel'), ('shipment_mode', '=', 'ftl'),
                                  ('planned_dispatch', '>=', recent_from),
                                  ('planned_dispatch', '<', fields.Datetime.to_datetime(start))])
            combos = {(t.lane_id.id, t.vehicle_type_id.id) for t in recent}
            last_year = Trip.search([('state', '!=', 'cancel'), ('shipment_mode', '=', 'ftl'),
                                     ('planned_dispatch', '>=', fields.Datetime.to_datetime(start - timedelta(weeks=52))),
                                     ('planned_dispatch', '<', fields.Datetime.to_datetime(
                                         start - timedelta(weeks=52 - rec.weeks)))])
            combos |= {(t.lane_id.id, t.vehicle_type_id.id) for t in last_year}
            vals = []
            factor = 1 + (rec.growth_pct or 0.0) / 100.0
            for lane_id, vtype_id in sorted(combos):
                run_rate = len(recent.filtered(lambda t: t.lane_id.id == lane_id and t.vehicle_type_id.id == vtype_id)) / 8.0
                for w in range(rec.weeks):
                    week = start + timedelta(weeks=w)
                    ly_from = fields.Datetime.to_datetime(week - timedelta(weeks=52))
                    ly = len(last_year.filtered(lambda t: t.lane_id.id == lane_id and t.vehicle_type_id.id == vtype_id
                                                and ly_from <= t.planned_dispatch < ly_from + timedelta(weeks=1)))
                    history = ly or run_rate
                    vals.append({'plan_id': rec.id, 'week_start': week, 'lane_id': lane_id,
                                 'vehicle_type_id': vtype_id or False, 'history_trucks': round(history, 1),
                                 'forecast_trucks': int(round(history * factor + 0.49))})
            self.env['cp.capacity.line'].create(vals)
        return True


class CpCapacityLine(models.Model):
    _name = 'cp.capacity.line'
    _description = 'Capacity Plan Week'
    _order = 'week_start, lane_id'

    plan_id = fields.Many2one('cp.capacity.plan', required=True, ondelete='cascade')
    week_start = fields.Date(required=True, string='Week')
    lane_id = fields.Many2one('cp.lane', required=True)
    vehicle_type_id = fields.Many2one('cp.vehicle.type')
    history_trucks = fields.Float(string='Same Week Last Year', digits=(8, 1))
    forecast_trucks = fields.Integer(string='Trucks Needed')
    committed_trucks = fields.Integer(compute='_compute_committed', store=True, string='Committed')
    gap = fields.Integer(compute='_compute_committed', store=True, string='Shortfall')

    @api.depends('forecast_trucks', 'lane_id', 'vehicle_type_id', 'week_start')
    def _compute_committed(self):
        Line = self.env['cp.rate.contract.line']
        for rec in self:
            week_end = rec.week_start + timedelta(days=6) if rec.week_start else False
            lines = Line.search([('lane_id', '=', rec.lane_id.id), ('contract_id.state', '=', 'active'),
                                 ('contract_id.date_start', '<=', week_end),
                                 ('contract_id.date_end', '>=', rec.week_start),
                                 '|', ('vehicle_type_id', '=', rec.vehicle_type_id.id),
                                 ('vehicle_type_id', '=', False)]) if rec.lane_id and rec.week_start else Line
            rec.committed_trucks = sum(lines.mapped('weekly_capacity'))
            rec.gap = rec.forecast_trucks - rec.committed_trucks
