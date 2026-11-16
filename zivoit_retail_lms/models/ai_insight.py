# -*- coding: utf-8 -*-
"""AI skill-gap analysis and L&D advisor for learners, stores and the whole network."""
import json
from statistics import mean

from markupsafe import Markup, escape

from odoo import _, api, fields, models

from .ai_service import ZlmsAiUnavailable


def _pearson(xs, ys):
    if len(xs) < 3:
        return None
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = (sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** 0.5
    return round(num / den, 2) if den else None


class ZlmsAiInsight(models.Model):
    _name = 'zlms.ai.insight'
    _description = 'AI Learning Insight'
    _order = 'id desc'

    name = fields.Char(required=True)
    scope = fields.Selection([('learner', 'Learner'), ('store', 'Store'), ('network', 'Network')], required=True)
    learner_id = fields.Many2one('zlms.learner', ondelete='cascade')
    store_id = fields.Many2one('zlms.store', ondelete='cascade')
    store_manager_user_id = fields.Many2one(related='store_id.manager_user_id', store=True)
    engine = fields.Selection([('ai', 'AI'), ('offline', 'Offline engine')], readonly=True)
    content_html = fields.Html(readonly=True, sanitize=True)
    snapshot = fields.Text(readonly=True, help='Data sent to the analysis engine.')
    recommended_path_ids = fields.Many2many('zlms.learning.path', string='Recommended Paths')
    create_date = fields.Datetime(readonly=True)

    def _open_form(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'current'}

    def action_assign_recommended(self):
        for insight in self.filtered(lambda i: i.scope == 'learner' and i.learner_id):
            for path in insight.recommended_path_ids.filtered(lambda p: p.state == 'published'):
                path._assign_to_learners(insight.learner_id)
        for insight in self.filtered(lambda i: i.scope == 'store' and i.store_id):
            for path in insight.recommended_path_ids.filtered(lambda p: p.state == 'published'):
                path._assign_to_learners(insight.store_id.learner_ids)
        return True

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------
    @api.model
    def _learner_snapshot(self, learner):
        assignments = learner.assignment_ids.filtered(lambda a: a.state != 'cancelled')
        weak_steps = []
        for a in assignments:
            for line in a.line_ids:
                if (line.score and line.score < 70) or (line.state != 'done' and line.due_date
                                                       and line.due_date < fields.Date.context_today(self)):
                    weak_steps.append({'path': a.path_id.name, 'step': line.name, 'type': line.line_type,
                                       'score': line.score, 'state': line.state})
        crit = {}
        for c in learner.roleplay_session_ids.filtered(lambda s: s.state == 'evaluated').criterion_ids:
            crit.setdefault(c.name, []).append(c.score)
        boost = {}
        for card in learner.boost_card_ids:
            key = card.channel_id.name or _('General')
            stats = boost.setdefault(key, [0, 0])
            stats[0] += card.correct_count
            stats[1] += card.wrong_count
        return {
            'learner': learner.name, 'role': learner.role_id.name, 'store': learner.store_id.name,
            'tenure_days': learner.tenure_days, 'completion_rate': learner.completion_rate,
            'avg_score': learner.avg_score, 'overdue': learner.overdue_count,
            'roleplay_avg': learner.roleplay_avg, 'boost_mastery': learner.boost_mastery,
            'paths': [{'path': a.path_id.name, 'type': a.path_type, 'progress': a.progress, 'score': a.score,
                       'state': a.state, 'overdue': a.is_overdue} for a in assignments],
            'weak_steps': weak_steps[:15],
            'roleplay_criteria': {k: round(mean(v), 1) for k, v in crit.items()},
            'boost_accuracy': {k: round(100.0 * v[0] / (v[0] + v[1]), 1) for k, v in boost.items() if v[0] + v[1]},
        }

    @api.model
    def _store_snapshot(self, store):
        learners = store.learner_ids.filtered('active')
        kpis = store.kpi_ids.sorted('period_date')[-6:]
        return {
            'store': store.name, 'format': store.store_format, 'region': store.region_id.name,
            'headcount': store.headcount, 'avg_completion': store.avg_completion,
            'onboarding_completion': store.onboarding_completion, 'overdue': store.overdue_count,
            'avg_score': store.avg_score, 'readiness_index': store.readiness_index,
            'at_risk_learners': [{'name': l.name, 'role': l.role_id.name, 'completion': l.completion_rate,
                                  'score': l.avg_score, 'overdue': l.overdue_count}
                                 for l in learners if l.risk_level == 'high'][:10],
            'kpis': [{'month': str(k.period_date), 'conversion': k.conversion_rate, 'atv': k.atv, 'upt': k.upt,
                      'csat': k.csat_score, 'training_completion': k.training_completion} for k in kpis],
        }

    @api.model
    def _network_snapshot(self):
        stores = self.env['zlms.store'].search([])
        kpis = self.env['zlms.store.kpi'].search([('training_completion', '>', 0)])
        corr_conv = _pearson(kpis.mapped('training_completion'), kpis.mapped('conversion_rate'))
        corr_atv = _pearson(kpis.mapped('training_completion'), kpis.mapped('atv'))
        high = kpis.filtered(lambda k: k.training_completion >= 80)
        low = kpis.filtered(lambda k: k.training_completion < 80)
        paths = self.env['zlms.learning.path'].search([('state', '=', 'published')])
        return {
            'stores': [{'store': s.name, 'region': s.region_id.name, 'completion': s.avg_completion,
                        'score': s.avg_score, 'overdue': s.overdue_count, 'readiness': s.readiness_index}
                       for s in stores.sorted('readiness_index')],
            'paths': [{'path': p.name, 'type': p.path_type, 'assigned': p.assignment_count,
                       'completion_rate': p.completion_rate} for p in paths],
            'training_vs_conversion_correlation': corr_conv,
            'training_vs_atv_correlation': corr_atv,
            'avg_conversion_trained_stores': round(mean(high.mapped('conversion_rate')), 2) if high else None,
            'avg_conversion_other_stores': round(mean(low.mapped('conversion_rate')), 2) if low else None,
        }

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------
    @api.model
    def generate_insight(self, scope, learner=None, store=None):
        if scope == 'learner':
            snap, title = self._learner_snapshot(learner), _('Skill gap analysis: %s', learner.name)
        elif scope == 'store':
            snap, title = self._store_snapshot(store), _('Store learning health: %s', store.name)
        else:
            snap, title = self._network_snapshot(), _('Network L&D advisor')
        paths = self.env['zlms.learning.path'].search([('state', '=', 'published')])
        catalogue = [{'code': p.code or str(p.id), 'name': p.name, 'type': p.path_type} for p in paths]
        result, engine = None, 'offline'
        if self.env['zlms.ai.service'].is_enabled():
            prompt = (
                '%s You are its head of Learning & Development. Analyse this %s learning data and give a crisp, '
                'practical diagnosis for store operations.\nDATA:\n%s\n\nAVAILABLE LEARNING PATHS:\n%s\n\n'
                'Return JSON: {"summary": str, "gaps": [str], "actions": [str], '
                '"recommended_path_codes": [str], "manager_tip": str}'
            ) % (self.env['zlms.ai.service'].brand_context(), scope, json.dumps(snap, default=str), json.dumps(catalogue))
            try:
                result = self.env['zlms.ai.service'].chat_json(prompt, feature='insight_%s' % scope)
                engine = 'ai'
            except ZlmsAiUnavailable:
                result = None
        if not result:
            result = self._offline_analysis(scope, snap, paths)
        codes = {str(c) for c in result.get('recommended_path_codes') or []}
        recommended = paths.filtered(lambda p: (p.code or str(p.id)) in codes)
        return self.create({
            'name': title, 'scope': scope,
            'learner_id': learner.id if learner else False,
            'store_id': store.id if store else False,
            'engine': engine,
            'snapshot': json.dumps(snap, default=str, indent=1),
            'content_html': self._render_html(result),
            'recommended_path_ids': [(6, 0, recommended.ids)],
        })

    @api.model
    def _render_html(self, result):
        e = escape
        html = '<p>%s</p>' % e(result.get('summary') or '')
        for key, label in (('gaps', _('Gaps found')), ('actions', _('Recommended actions'))):
            items = result.get(key) or []
            if items:
                html += '<h5>%s</h5><ul>%s</ul>' % (e(label), ''.join('<li>%s</li>' % e(i) for i in items))
        if result.get('manager_tip'):
            html += '<div class="alert alert-info"><strong>%s</strong> %s</div>' % (
                e(_('Manager tip:')), e(result['manager_tip']))
        return Markup(html)

    @api.model
    def _offline_analysis(self, scope, snap, paths):
        gaps, actions, rec_types = [], [], set()
        by_type = {p.path_type: p for p in paths}
        if scope == 'learner':
            if snap['overdue']:
                gaps.append(_('%s overdue learning assignment(s).', snap['overdue']))
                actions.append(_('Block 15 minutes at shift start for the overdue modules this week.'))
            for step in snap['weak_steps'][:5]:
                gaps.append(_('%(step)s (%(path)s): score %(score)s, status %(state)s', **step))
            weak_crit = sorted(snap['roleplay_criteria'].items(), key=lambda kv: kv[1])[:2]
            for name, score in weak_crit:
                if score < 6:
                    gaps.append(_('Role-play: "%(name)s" averages %(score)s/10.', name=name, score=score))
                    rec_types.add('selling' if 'close' in name.lower() or 'suggest' in name.lower() else 'service')
            weak_boost = [k for k, v in snap['boost_accuracy'].items() if v < 70]
            if weak_boost:
                gaps.append(_('Daily Boost accuracy below 70%% in: %s', ', '.join(weak_boost)))
                rec_types.add('product')
                actions.append(_('Keep the Daily Boost streak: 5 questions a day closes product gaps fastest.'))
            if snap['avg_score'] and snap['avg_score'] < 70:
                rec_types.add('refresher')
            if snap['roleplay_avg'] == 0:
                actions.append(_('Do one AI role-play on price objections this week.'))
                rec_types.add('selling')
            summary = _('%(name)s (%(role)s, %(days)s days in role) has completed %(comp)s%% of assigned learning '
                        'with an average score of %(score)s%%. Readiness is %(level)s.',
                        name=snap['learner'], role=snap['role'] or '-', days=snap['tenure_days'],
                        comp=snap['completion_rate'], score=snap['avg_score'],
                        level=_('strong') if snap['completion_rate'] >= 80 and not snap['overdue'] else _('building'))
            tip = _('Pair this learner with a top performer for two shifts and observe one customer interaction.')
        elif scope == 'store':
            if snap['overdue']:
                gaps.append(_('%s overdue assignments in the store.', snap['overdue']))
                actions.append(_('Run a 10-minute huddle before opening to finish overdue modules together.'))
            if snap['onboarding_completion'] < 80:
                gaps.append(_('Onboarding completion is %s%%.', snap['onboarding_completion']))
                rec_types.add('onboarding')
            for l in snap['at_risk_learners'][:5]:
                gaps.append(_('At risk: %(name)s - completion %(completion)s%%, score %(score)s%%', **l))
            kpis = snap['kpis']
            if len(kpis) >= 2 and kpis[-1]['conversion'] < kpis[-2]['conversion']:
                gaps.append(_('Conversion fell from %(a)s%% to %(b)s%% last month.',
                              a=kpis[-2]['conversion'], b=kpis[-1]['conversion']))
                rec_types.add('selling')
                actions.append(_('Focus coaching on need discovery and closing; review with mystery-shopper feedback.'))
            if snap['avg_score'] and snap['avg_score'] < 75:
                rec_types.add('product')
            summary = _('%(store)s: %(hc)s learners, %(comp)s%% average completion, readiness index %(ri)s.',
                        store=snap['store'], hc=snap['headcount'], comp=snap['avg_completion'],
                        ri=snap['readiness_index'])
            tip = _('Recognise the top learner of the week in the morning huddle - it lifts participation.')
        else:
            stores = snap['stores']
            for s in stores[:3]:
                gaps.append(_('Lowest readiness: %(store)s (%(readiness)s) - %(overdue)s overdue.', **s))
            for p in snap['paths']:
                if p['assigned'] and p['completion_rate'] < 50:
                    gaps.append(_('"%(path)s" completion is only %(completion_rate)s%%.', **p))
            corr = snap['training_vs_conversion_correlation']
            if corr is not None:
                actions.append(_('Training completion vs. conversion correlation across store-months: %s. %s', corr,
                                 _('Higher-trained stores convert better - prioritise completion targets.')
                                 if corr > 0.3 else _('Link training to floor behaviours with OJT sign-offs.')))
            if snap['avg_conversion_trained_stores'] is not None and snap['avg_conversion_other_stores'] is not None:
                actions.append(_('Stores with 80%%+ completion convert at %(a)s%% vs %(b)s%% for the rest.',
                                 a=snap['avg_conversion_trained_stores'], b=snap['avg_conversion_other_stores']))
            actions.append(_('Ask area managers to review the bottom three stores weekly.'))
            rec_types.update({'selling', 'product'})
            summary = _('%s stores analysed. Focus on the stores with the lowest readiness first.', len(stores))
            tip = _('Make each new collection launch a 3-day micro-course plus Daily Boost questions.')
        rec_paths = [by_type[t] for t in rec_types if t in by_type]
        return {'summary': summary, 'gaps': gaps or [_('No major gaps detected.')], 'actions': actions,
                'recommended_path_codes': [p.code or str(p.id) for p in rec_paths], 'manager_tip': tip}
