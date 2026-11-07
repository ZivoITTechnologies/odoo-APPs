# -*- coding: utf-8 -*-
"""FR-10 KPIs: regular vs 3PL share, bid savings and participation, detention by cause,
on-time reporting, transit compliance, POD turnaround, outstanding PODs."""
from collections import defaultdict
from datetime import timedelta

from odoo import api, fields, models, _

from .cp_gate_detention import DELAY_REASONS


class CpDispatchDashboard(models.AbstractModel):
    _name = 'cp.dispatch.dashboard'
    _description = 'Dispatch Dashboard'

    @api.model
    def _cp_since(self, period):
        today = fields.Date.context_today(self)
        if period == '7d':
            start = today - timedelta(days=7)
        elif period == '30d':
            start = today - timedelta(days=30)
        elif period == 'quarter':
            start = today - timedelta(days=91)
        else:
            start = today.replace(day=1)
        return fields.Datetime.to_datetime(start)

    @api.model
    def get_data(self, period='month'):
        since = self._cp_since(period)
        Plan = self.env['cp.dispatch.plan']
        plans = Plan.search([('create_date', '>=', since), ('state', '!=', 'cancel')])
        company = self.env.company
        currency = company.currency_id
        booked = plans.filtered(lambda p: p.carrier_type and p.vendor_id)
        regular = booked.filtered(lambda p: p.carrier_type == 'regular')
        third = booked - regular
        auctions = plans.auction_ids.filtered(lambda a: a.state in ('closed', 'awarded'))
        awarded = third.filtered(lambda p: p.target_rate and p.freight_amount)
        savings = sum(p.target_rate - p.freight_amount for p in awarded)
        detentions = plans.detention_ids.filtered(lambda d: d.state == 'approved')
        by_reason = defaultdict(float)
        for d in plans.detention_ids.filtered(lambda d: d.state != 'rejected'):
            by_reason[d.delay_reason or 'unassigned'] += d.approved_amount if d.state == 'approved' else d.calculated_amount
        labels = dict(DELAY_REASONS, unassigned=_('Reason not given'))
        reported = third.filtered('gate_in_time')
        delivered = plans.filtered('delivered_at')
        pod_done = plans.filtered(lambda p: p.pod_status == 'verified')
        vendors = defaultdict(lambda: {'count': 0, 'freight': 0.0, 'on_time': 0, 'reported': 0, 'type': ''})
        for p in booked:
            v = vendors[p.vendor_id.display_name or '-']
            v['count'] += 1
            v['freight'] += p.freight_amount
            v['type'] = _('Regular') if p.carrier_type == 'regular' else '3PL'
            if p.gate_in_time:
                v['reported'] += 1
                v['on_time'] += 1 if p.reported_on_time else 0
        board = defaultdict(int)
        for p in Plan.search([('state', 'in', ('assigned', 'loading', 'in_transit', 'delivered'))]):
            board[p.board_status or p.state] += 1
        states = dict(Plan._fields['state']._description_selection(self.env))
        by_state = defaultdict(int)
        for p in plans:
            by_state[p.state] += 1

        def pct(part, whole):
            return round(part * 100.0 / whole, 1) if whole else 0.0

        return {
            'currency': {'symbol': currency.symbol, 'position': currency.position},
            'kpis': {
                'plans': len(plans),
                'regular_share': pct(len(regular), len(booked)),
                'regular_target': company.cp_regular_share_target or 75.0,
                'bids': len(auctions),
                'participation': round(sum(auctions.mapped('participation_rate')) / len(auctions), 1) if auctions else 0.0,
                'savings': savings,
                'savings_pct': pct(savings, sum(awarded.mapped('target_rate'))),
                'freight': sum(booked.mapped('freight_amount')),
                'detention': sum(detentions.mapped('approved_amount')),
                'detention_pending': sum(plans.detention_ids.filtered(
                    lambda d: d.state in ('draft', 'submitted', 'logistics_approved')).mapped('calculated_amount')),
                'on_time_reporting': pct(len(reported.filtered('reported_on_time')), len(reported)),
                'transit_compliance': pct(len(delivered.filtered('on_time_delivery')), len(delivered)),
                'pod_tat': round(sum(pod_done.mapped('pod_tat_hours')) / len(pod_done), 1) if pod_done else 0.0,
                'pod_pending': Plan.search_count([('state', '=', 'delivered')]),
                'bills_on_hold': self.env['account.move'].search_count([
                    ('cp_dispatch_id', '!=', False), ('state', '=', 'draft'), ('move_type', '=', 'in_invoice')]),
            },
            'states': [{'key': k, 'label': states[k], 'count': by_state[k]} for k in states if by_state.get(k)],
            'board': [{'key': k, 'label': label, 'count': board.get(k, 0)} for k, label in (
                ('assigned', _('Waiting for vehicle')), ('loading', _('Loading')), ('in_transit', _('In transit')),
                ('delayed', _('Delayed')), ('out_for_delivery', _('Out for delivery')),
                ('delivered', _('POD pending')))],
            'detention': sorted([{'reason': labels.get(k, k), 'amount': v} for k, v in by_reason.items()],
                                key=lambda r: -r['amount']),
            'vendors': sorted([dict(v, name=k, on_time_pct=pct(v['on_time'], v['reported']))
                               for k, v in vendors.items()], key=lambda r: -r['count'])[:10],
        }
