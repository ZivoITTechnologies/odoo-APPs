# -*- coding: utf-8 -*-
"""Data for the Freight Insights screen: spend, cost per kg and per order, channel split,
lanes, transporter scorecards, POD ageing and billed-versus-agreed."""
from collections import defaultdict
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _

from .cp_common import CHANNELS, SHIPMENT_MODES
from .cp_trip_ext import POD_BUCKETS


class CpInsights(models.AbstractModel):
    _name = 'cp.insights'
    _description = 'Cargo Pilot Freight Insights'

    @api.model
    def _cp_period(self, period):
        today = fields.Date.context_today(self)
        if period == 'quarter':
            start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        elif period == 'year':
            # Indian financial year April to March
            start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
        elif period == '30d':
            start = today - timedelta(days=29)
        else:
            start = today.replace(day=1)
        return start, today

    @api.model
    def get_insights(self, period='month'):
        env = self.env
        Trip = env['cp.trip']
        start, end = self._cp_period(period)
        start_dt = fields.Datetime.to_datetime(start)
        end_dt = fields.Datetime.to_datetime(end) + timedelta(days=1)
        currency = env.company.currency_id
        base = [('state', '!=', 'cancel')]
        trips = Trip.search(base + [('planned_dispatch', '>=', start_dt), ('planned_dispatch', '<', end_dt)])
        done = trips.filtered(lambda t: t.state in ('delivered', 'closed'))

        spend = sum(trips.mapped('freight_cost_total'))
        kg = sum((t.order_weight_kg or (t.loaded_weight_ton or 0.0) * 1000.0) for t in trips)
        value = sum(trips.mapped('order_value'))
        on_time = len(done.filtered(lambda t: not t.is_late)) / len(done) * 100.0 if done else 0.0
        tat = done.filtered('epod_tat_hours')

        # Courier parcels (when the courier add-on is installed)
        parcels_spend, parcels_count = 0.0, 0
        if 'cp.parcel' in env:
            parcels = env['cp.parcel'].search([('state', 'not in', ('draft', 'cancelled')),
                                               ('booked_at', '>=', start_dt), ('booked_at', '<', end_dt)])
            parcels_spend, parcels_count = sum(parcels.mapped('shipping_cost')), len(parcels)

        modes = [{'key': key, 'label': label,
                  'count': len(trips.filtered(lambda t, k=key: t.shipment_mode == k)),
                  'spend': round(sum(trips.filtered(lambda t, k=key: t.shipment_mode == k).mapped('freight_cost_total')))}
                 for key, label in SHIPMENT_MODES]
        modes.append({'key': 'courier', 'label': _('Courier'), 'count': parcels_count, 'spend': round(parcels_spend)})

        # Channel split, from the orders on each shipment (falls back to the shipment's channel)
        channel_spend, channel_value, channel_count = defaultdict(float), defaultdict(float), defaultdict(int)
        for trip in trips:
            if trip.order_ids:
                for order in trip.order_ids:
                    key = order.channel or trip.channel or 'none'
                    channel_spend[key] += order.freight_cost
                    channel_value[key] += order.order_value
                    channel_count[key] += 1
            else:
                key = trip.channel or 'none'
                channel_spend[key] += trip.freight_cost_total
                channel_value[key] += trip.order_value
                channel_count[key] += 1
        labels = dict(CHANNELS, none=_('Not set'))
        channels = sorted([{'key': k, 'label': labels.get(k, k), 'spend': round(v), 'orders': channel_count[k],
                            'freight_pct': round(v / channel_value[k] * 100.0, 2) if channel_value[k] else 0.0}
                           for k, v in channel_spend.items()], key=lambda c: -c['spend'])

        # Monthly spend, last 12 months
        months = []
        first = end.replace(day=1) - relativedelta(months=11)
        for i in range(12):
            m_start = first + relativedelta(months=i)
            m_end = m_start + relativedelta(months=1)
            m_trips = Trip.search(base + [('planned_dispatch', '>=', fields.Datetime.to_datetime(m_start)),
                                          ('planned_dispatch', '<', fields.Datetime.to_datetime(m_end))])
            months.append({'label': m_start.strftime('%b'), 'year': m_start.year,
                           'ftl': round(sum(m_trips.filtered(lambda t: t.shipment_mode == 'ftl').mapped('freight_cost_total'))),
                           'ltl': round(sum(m_trips.filtered(lambda t: t.shipment_mode == 'ltl').mapped('freight_cost_total'))),
                           'count': len(m_trips)})

        # Lanes
        lanes = defaultdict(lambda: {'trips': 0, 'spend': 0.0, 'kg': 0.0, 'late': 0, 'done': 0})
        for trip in trips:
            row = lanes[trip.lane_id.id]
            row['name'] = trip.lane_id.name
            row['movement'] = trip.movement or ''
            row['trips'] += 1
            row['spend'] += trip.freight_cost_total
            row['kg'] += trip.order_weight_kg or (trip.loaded_weight_ton or 0.0) * 1000.0
            if trip.state in ('delivered', 'closed'):
                row['done'] += 1
                row['late'] += 1 if trip.is_late else 0
        top_lanes = sorted(lanes.values(), key=lambda r: -r['spend'])[:6]
        for row in top_lanes:
            row['cost_per_kg'] = round(row['spend'] / row['kg'], 2) if row['kg'] else 0.0
            row['on_time'] = round((row['done'] - row['late']) / row['done'] * 100) if row['done'] else None
            row['spend'] = round(row['spend'])

        # Transporter scorecard (transporters active in the period)
        transporters = trips.mapped('transporter_id').filtered('cp_is_transporter')
        scorecard = sorted([{
            'id': p.id, 'name': p.name, 'score': p.cp_score, 'grade': p.cp_score_grade,
            'acceptance': round(p.cp_acceptance_rate), 'placement': round(p.cp_placement_rate),
            'on_time': round(p.cp_on_time_rate), 'damage_free': round(p.cp_damage_free_rate),
            'pod_tat': round(p.cp_pod_tat_hours, 1), 'billing': round(p.cp_billing_accuracy),
            'trips': len(trips.filtered(lambda t, p=p: t.transporter_id == p)),
        } for p in transporters], key=lambda r: -r['score'])[:8]

        # POD ageing (all open shipments, not only this period)
        bucket_counts = dict(Trip._read_group([('pod_age_bucket', 'in', ('d0', 'd1', 'd3', 'd7')),
                                               ('state', '!=', 'cancel')], ['pod_age_bucket'], ['__count']))
        pod = [{'key': k, 'label': lbl, 'count': bucket_counts.get(k, 0)}
               for k, lbl in POD_BUCKETS if k in ('d0', 'd1', 'd3', 'd7')]

        # Billed vs agreed
        billed_trips = trips.filtered(lambda t: t.billed_amount)
        agreed = sum(billed_trips.mapped('agreed_freight'))
        billed = sum(billed_trips.mapped('billed_amount'))
        deductions = sum(env['cp.freight.bill'].search([('state', '=', 'approved'), ('bill_date', '>=', start),
                                                         ('bill_date', '<=', end)]).mapped('deduction_total'))

        offers = env['cp.trip.offer'].search([('offered_at', '>=', start_dt), ('offered_at', '<', end_dt),
                                              ('state', '!=', 'withdrawn')])
        answered = offers.filtered(lambda o: o.state in ('accepted', 'declined', 'expired'))
        replied = offers.filtered('responded_at')

        return {
            'period': period, 'date_from': fields.Date.to_string(start), 'date_to': fields.Date.to_string(end),
            'currency': {'symbol': currency.symbol, 'position': currency.position},
            'kpis': {
                'spend': round(spend + parcels_spend), 'shipments': len(trips) + parcels_count,
                'cost_per_kg': round(spend / kg, 2) if kg else 0.0,
                'freight_pct': round(spend / value * 100.0, 2) if value else 0.0,
                'on_time': round(on_time), 'delivered': len(done),
                'pod_pending': Trip.search_count([('state', 'in', ('arrived', 'delivered')),
                                                  ('epod_state', '!=', 'verified')]),
                'pod_tat': round(sum(tat.mapped('epod_tat_hours')) / len(tat), 1) if tat else 0.0,
                'acceptance': round(len(answered.filtered(lambda o: o.state == 'accepted')) / len(answered) * 100)
                if answered else 0,
                'reply_minutes': round(sum(replied.mapped('response_minutes')) / len(replied)) if replied else 0,
                'declines': len(offers.filtered(lambda o: o.state in ('declined', 'expired'))),
                'co2_t': round(sum(trips.mapped('co2_kg')) / 1000.0, 1),
            },
            'modes': modes, 'channels': channels, 'months': months, 'lanes': top_lanes, 'scorecard': scorecard,
            'pod': pod,
            'billing': {'agreed': round(agreed), 'billed': round(billed),
                        'variance_pct': round((billed - agreed) / agreed * 100.0, 2) if agreed else 0.0,
                        'deductions': round(deductions)},
        }
