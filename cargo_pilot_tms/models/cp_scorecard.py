# -*- coding: utf-8 -*-
"""Transporter scorecard: six measures the shipper cares about, weighted into one score."""
from datetime import timedelta

from odoo import api, fields, models

# measure -> weight in the overall score
SCORE_WEIGHTS = {
    'acceptance': 15,
    'placement': 20,
    'on_time': 25,
    'damage_free': 15,
    'pod_on_time': 10,
    'billing': 15,
}


class ResPartner(models.Model):
    _inherit = 'res.partner'

    cp_offer_count = fields.Integer(compute='_compute_cp_stats', string='Bookings Offered')
    cp_acceptance_rate = fields.Float(compute='_compute_cp_stats', string='Acceptance %')
    cp_reply_minutes = fields.Float(compute='_compute_cp_stats', string='Avg Reply (min)')
    cp_placement_rate = fields.Float(compute='_compute_cp_stats', string='Placed on Time %')
    cp_damage_free_rate = fields.Float(compute='_compute_cp_stats', string='Damage-free %')
    cp_pod_tat_hours = fields.Float(compute='_compute_cp_stats', string='Avg POD Turnaround (h)')
    cp_pod_on_time_rate = fields.Float(compute='_compute_cp_stats', string='POD within 48 h %')
    cp_billing_accuracy = fields.Float(compute='_compute_cp_stats', string='Billing Accuracy %')
    cp_freight_spend = fields.Monetary(compute='_compute_cp_stats', string='Freight Spend',
                                       currency_field='cp_currency_id')
    cp_currency_id = fields.Many2one('res.currency', compute='_compute_cp_currency')
    cp_score_grade = fields.Selection([('a', 'A'), ('b', 'B'), ('c', 'C'), ('d', 'D'), ('new', 'New')],
                                      compute='_compute_cp_stats', string='Grade')

    def _compute_cp_currency(self):
        for partner in self:
            partner.cp_currency_id = self.env.company.currency_id

    def _compute_cp_stats(self):
        super()._compute_cp_stats()
        Trip = self.env['cp.trip'].sudo()
        Offer = self.env['cp.trip.offer'].sudo()
        Epod = self.env['cp.epod'].sudo()
        BillLine = self.env['cp.freight.bill.line'].sudo()
        days = self.env.company.cp_scorecard_days or 90
        since = fields.Datetime.now() - timedelta(days=days)
        empty = {'cp_offer_count': 0, 'cp_acceptance_rate': 0.0, 'cp_reply_minutes': 0.0, 'cp_placement_rate': 0.0,
                 'cp_damage_free_rate': 0.0, 'cp_pod_tat_hours': 0.0, 'cp_pod_on_time_rate': 0.0,
                 'cp_billing_accuracy': 0.0, 'cp_freight_spend': 0.0, 'cp_score_grade': 'new'}
        for partner in self:
            if not partner.cp_is_transporter or not partner.id:
                partner.update(empty)
                continue
            measures = {}
            offers = Offer.search([('transporter_id', '=', partner.id), ('offered_at', '>=', since),
                                   ('state', '!=', 'withdrawn')])
            answered = offers.filtered(lambda o: o.state in ('accepted', 'declined', 'expired'))
            accepted = offers.filtered(lambda o: o.state == 'accepted')
            replied = offers.filtered('responded_at')
            partner.cp_offer_count = len(offers)
            partner.cp_acceptance_rate = len(accepted) / len(answered) * 100.0 if answered else 0.0
            partner.cp_reply_minutes = sum(replied.mapped('response_minutes')) / len(replied) if replied else 0.0
            if answered:
                measures['acceptance'] = partner.cp_acceptance_rate

            trips = Trip.search([('transporter_id', '=', partner.id), ('state', '!=', 'cancel'),
                                 ('planned_dispatch', '>=', since)])
            placed = trips.filtered(lambda t: t.reported_at or t.actual_dispatch)
            partner.cp_placement_rate = len(placed.filtered('placement_on_time')) / len(placed) * 100.0 \
                if placed else 0.0
            if placed:
                measures['placement'] = partner.cp_placement_rate
            done = trips.filtered(lambda t: t.state in ('delivered', 'closed'))
            if done:
                measures['on_time'] = len(done.filtered(lambda t: not t.is_late)) / len(done) * 100.0

            epods = Epod.search([('trip_id', 'in', trips.ids), ('state', 'in', ('verified', 'exception', 'submitted'))])
            if epods:
                clean = epods.filtered(lambda e: not e.shortage_packages and not e.damaged_packages
                                       and not e.missing_serials)
                partner.cp_damage_free_rate = len(clean) / len(epods) * 100.0
                measures['damage_free'] = partner.cp_damage_free_rate
            else:
                partner.cp_damage_free_rate = 0.0
            with_tat = done.filtered('epod_tat_hours')
            partner.cp_pod_tat_hours = sum(with_tat.mapped('epod_tat_hours')) / len(with_tat) if with_tat else 0.0
            if with_tat:
                partner.cp_pod_on_time_rate = len(with_tat.filtered(lambda t: t.epod_tat_hours <= 48)) \
                    / len(with_tat) * 100.0
                measures['pod_on_time'] = partner.cp_pod_on_time_rate
            else:
                partner.cp_pod_on_time_rate = 0.0

            lines = BillLine.search([('bill_id.transporter_id', '=', partner.id), ('audited', '=', True),
                                     ('bill_id.state', '!=', 'cancel'), ('bill_id.bill_date', '>=', since.date())])
            if lines:
                accurate = lines.filtered(lambda l: l.deduction_amount <= 0.01 and l.check_duplicate)
                partner.cp_billing_accuracy = len(accurate) / len(lines) * 100.0
                measures['billing'] = partner.cp_billing_accuracy
            else:
                partner.cp_billing_accuracy = 0.0
            partner.cp_freight_spend = sum(done.mapped('freight_cost_total'))

            if measures:
                weight = sum(SCORE_WEIGHTS[k] for k in measures)
                score = sum(measures[k] * SCORE_WEIGHTS[k] for k in measures) / weight
                partner.cp_score = round(score, 1)
                partner.cp_score_grade = 'a' if score >= 90 else 'b' if score >= 75 else 'c' if score >= 60 else 'd'
            else:
                partner.cp_score = 0.0
                partner.cp_score_grade = 'new'

    def action_cp_scorecard_trips(self):
        return self.action_cp_view_trips()
