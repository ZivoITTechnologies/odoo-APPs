# -*- coding: utf-8 -*-
from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models


class ZlmsDashboard(models.AbstractModel):
    _name = 'zlms.dashboard'
    _description = 'Retail LMS Dashboard Data'

    @api.model
    def get_dashboard_data(self, region_id=False, store_id=False):
        today = fields.Date.context_today(self)
        store_domain = [('active', '=', True)]
        if region_id:
            store_domain.append(('region_id', '=', int(region_id)))
        if store_id:
            store_domain.append(('id', '=', int(store_id)))
        Store = self.env['zlms.store']
        stores = Store.search(store_domain)
        learner_domain = [('store_id', 'in', stores.ids), ('active', '=', True)]
        learners = self.env['zlms.learner'].search(learner_domain)
        a_domain = [('store_id', 'in', stores.ids), ('state', '!=', 'cancelled')]
        Assignment = self.env['zlms.assignment']
        assignments = Assignment.search(a_domain)
        open_a = assignments.filtered(lambda a: a.state in ('assigned', 'in_progress'))
        overdue = open_a.filtered('is_overdue')
        completed = assignments.filtered(lambda a: a.state == 'completed')
        scored = assignments.filtered(lambda a: a.score > 0)
        Cert = self.env['zlms.certificate']
        certs_valid = Cert.search_count([('store_id', 'in', stores.ids), ('state', 'in', ('valid', 'expiring'))])
        certs_expiring = Cert.search_count([('store_id', 'in', stores.ids), ('state', '=', 'expiring')])
        week_ago = today - timedelta(days=7)
        answers = self.env['zlms.boost.answer'].search([('store_id', 'in', stores.ids),
                                                         ('answer_date', '>=', week_ago)])
        sessions = self.env['zlms.roleplay.session'].search([('store_id', 'in', stores.ids),
                                                              ('state', '=', 'evaluated')])
        onboarding = assignments.filtered(lambda a: a.path_type == 'onboarding')

        def avg(values):
            values = list(values)
            return round(sum(values) / len(values), 1) if values else 0.0

        kpis = {
            'learners': len(learners),
            'stores': len(stores),
            'assignments_open': len(open_a),
            'completion_rate': round(100.0 * len(completed) / len(assignments), 1) if assignments else 0.0,
            'avg_progress': avg(assignments.mapped('progress')),
            'onboarding_completion': avg(onboarding.mapped('progress')),
            'overdue': len(overdue),
            'avg_score': avg(scored.mapped('score')),
            'certified': certs_valid,
            'certs_expiring': certs_expiring,
            'boost_answers_7d': len(answers),
            'boost_accuracy_7d': round(100.0 * len(answers.filtered('correct')) / len(answers), 1) if answers else 0.0,
            'boost_active_learners_7d': len(answers.learner_id),
            'roleplay_sessions': len(sessions),
            'roleplay_avg': avg(sessions.mapped('score')),
            'at_risk': len(learners.filtered(lambda l: l.risk_level == 'high')),
            'ai_enabled': self.env['zlms.ai.service'].is_enabled(),
        }
        acks = self.env['zlms.bulletin.ack'].search([('store_id', 'in', stores.ids),
                                                     ('bulletin_id.state', '=', 'published'),
                                                     ('bulletin_id.require_ack', '=', True)])
        month_ago = today - timedelta(days=30)
        observations = self.env['zlms.observation'].search([('store_id', 'in', stores.ids), ('state', '=', 'done'),
                                                            ('observation_date', '>=', month_ago)])
        now = fields.Datetime.now()
        upcoming = self.env['zlms.training.session'].search([('state', '=', 'planned'), ('start_datetime', '>=', now)],
                                                            order='start_datetime', limit=5)
        kpis.update({
            'bulletin_ack_rate': round(100.0 * len(acks.filtered(lambda a: a.state == 'acknowledged')) / len(acks), 1)
            if acks else 0.0,
            'bulletin_pending': len(acks.filtered(lambda a: a.state != 'acknowledged')),
            'badges_30d': self.env['zlms.badge.award'].search_count([('store_id', 'in', stores.ids),
                                                                     ('award_date', '>=', month_ago)]),
            'promotion_ready': len(learners.filtered('promotion_ready')),
            'observations_30d': len(observations),
            'observation_avg': avg(observations.mapped('score')),
            'skill_fit': avg(learners.filtered('role_id').mapped('role_fit')),
            'upcoming_sessions': len(upcoming),
        })
        challenge = [r for r in self.env['zlms.store'].get_store_challenge(limit=50) if r['id'] in stores.ids][:10]
        sessions = [{'id': s.id, 'name': s.name, 'start': fields.Datetime.context_timestamp(self, s.start_datetime)
                     .strftime('%d %b %H:%M'), 'type': dict(s._fields['session_type'].selection).get(s.session_type),
                     'seats': '%s/%s' % (s.seats_taken, s.capacity)} for s in upcoming]

        store_rows = []
        for store in stores:
            s_assign = assignments.filtered(lambda a, s=store: a.store_id == s)
            store_rows.append({
                'id': store.id, 'name': store.name, 'code': store.code, 'region': store.region_id.name or '',
                'city': store.city or '', 'headcount': len(learners.filtered(lambda l, s=store: l.store_id == s)),
                'completion': avg(s_assign.mapped('progress')),
                'overdue': len(s_assign.filtered('is_overdue')),
                'score': avg(s_assign.filtered(lambda a: a.score > 0).mapped('score')),
                'readiness': store.readiness_index,
                'conversion': store.last_conversion,
            })
        store_rows.sort(key=lambda r: r['readiness'], reverse=True)

        region_rows = {}
        for row in store_rows:
            reg = region_rows.setdefault(row['region'] or '-', {'name': row['region'] or '-', 'stores': 0,
                                                                'completion': [], 'headcount': 0})
            reg['stores'] += 1
            reg['headcount'] += row['headcount']
            reg['completion'].append(row['completion'])
        regions = [{'name': r['name'], 'stores': r['stores'], 'headcount': r['headcount'],
                    'completion': avg(r['completion'])} for r in region_rows.values()]
        regions.sort(key=lambda r: r['completion'], reverse=True)

        path_rows = []
        for path in assignments.path_id:
            p_assign = assignments.filtered(lambda a, p=path: a.path_id == p)
            path_rows.append({
                'id': path.id, 'name': path.name, 'type': dict(path._fields['path_type'].selection).get(path.path_type),
                'assigned': len(p_assign), 'completed': len(p_assign.filtered(lambda a: a.state == 'completed')),
                'progress': avg(p_assign.mapped('progress')),
                'overdue': len(p_assign.filtered('is_overdue')),
            })
        path_rows.sort(key=lambda r: r['assigned'], reverse=True)

        leaders = learners.sorted(lambda l: (l.points, l.readiness_score), reverse=True)[:10]
        leaderboard = [{'id': l.id, 'name': l.name, 'store': l.store_id.name or '', 'role': l.role_id.name or '',
                        'points': l.points, 'readiness': l.readiness_score} for l in leaders]
        risk = learners.filtered(lambda l: l.risk_level == 'high')[:10]
        at_risk = [{'id': l.id, 'name': l.name, 'store': l.store_id.name or '', 'completion': l.completion_rate,
                    'overdue': l.overdue_count, 'score': l.avg_score} for l in risk]

        trend = []
        first = today.replace(day=1)
        for i in range(5, -1, -1):
            start = first - relativedelta(months=i)
            end = start + relativedelta(months=1)
            done = completed.filtered(lambda a, s=start, e=end: a.completion_date and s <= a.completion_date < e)
            trend.append({'label': start.strftime('%b %Y'), 'value': len(done)})
        max_trend = max([t['value'] for t in trend] + [1])
        for t in trend:
            t['pct'] = round(100.0 * t['value'] / max_trend, 1)

        return {
            'kpis': kpis, 'stores': store_rows, 'regions': regions, 'paths': path_rows,
            'leaderboard': leaderboard, 'at_risk': at_risk, 'trend': trend,
            'challenge': challenge, 'sessions': sessions,
            'filters': {
                'regions': [{'id': r.id, 'name': r.name} for r in self.env['zlms.region'].search([])],
                'stores': [{'id': s.id, 'name': s.name} for s in Store.search(
                    [('region_id', '=', int(region_id))] if region_id else [])],
            },
        }

    @api.model
    def action_network_insight(self):
        insight = self.env['zlms.ai.insight'].generate_insight('network')
        return insight._open_form()
