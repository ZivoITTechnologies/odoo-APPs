# -*- coding: utf-8 -*-
"""Badges, points ledger, Daily Boost streaks and the monthly store challenge."""
from datetime import timedelta

from odoo import _, api, fields, models

BADGE_CRITERIA = [
    ('points', 'Total learning points'),
    ('paths_completed', 'Learning journeys completed'),
    ('certificates', 'Valid certificates'),
    ('boost_streak', 'Daily Boost streak (days)'),
    ('boost_mastered', 'Daily Boost cards mastered'),
    ('roleplay_best', 'Best role-play score'),
    ('bulletins_ack', 'Bulletins acknowledged'),
    ('observation_avg', 'Coaching observation average %'),
    ('sessions_attended', 'Training sessions attended'),
]


class ZlmsBadge(models.Model):
    _name = 'zlms.badge'
    _description = 'Retail Learning Badge'
    _order = 'sequence, threshold'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    description = fields.Text(translate=True)
    icon = fields.Char(default='fa-trophy', help='Font Awesome icon class, e.g. fa-trophy, fa-bolt, fa-star.')
    color = fields.Char(default='#f0ad4e')
    level = fields.Selection([('bronze', 'Bronze'), ('silver', 'Silver'), ('gold', 'Gold')], default='bronze')
    criterion = fields.Selection(BADGE_CRITERIA, required=True, default='points')
    threshold = fields.Float(required=True, default=100)
    bonus_points = fields.Integer(default=20)
    award_ids = fields.One2many('zlms.badge.award', 'badge_id')
    award_count = fields.Integer(compute='_compute_award_count')
    active = fields.Boolean(default=True)

    def _compute_award_count(self):
        for badge in self:
            badge.award_count = len(badge.award_ids)


class ZlmsBadgeAward(models.Model):
    _name = 'zlms.badge.award'
    _description = 'Badge Award'
    _order = 'award_date desc, id desc'

    badge_id = fields.Many2one('zlms.badge', required=True, ondelete='cascade', index=True)
    learner_id = fields.Many2one('zlms.learner', required=True, ondelete='cascade', index=True)
    store_id = fields.Many2one(related='learner_id.store_id', store=True)
    store_manager_user_id = fields.Many2one(related='learner_id.store_id.manager_user_id', store=True)
    award_date = fields.Date(default=fields.Date.context_today)
    icon = fields.Char(related='badge_id.icon')
    color = fields.Char(related='badge_id.color')

    _badge_learner_uniq = models.Constraint('unique(badge_id, learner_id)', 'A badge is awarded once per learner.')


class ZlmsPointsLog(models.Model):
    _name = 'zlms.points.log'
    _description = 'Learning Points Ledger'
    _order = 'id desc'

    learner_id = fields.Many2one('zlms.learner', required=True, ondelete='cascade', index=True)
    store_id = fields.Many2one('zlms.store', index=True)
    store_manager_user_id = fields.Many2one(related='store_id.manager_user_id', store=True)
    points = fields.Integer()
    reason = fields.Char()
    log_date = fields.Date(default=fields.Date.context_today, index=True)


class ZlmsLearnerGamification(models.Model):
    _inherit = 'zlms.learner'

    badge_award_ids = fields.One2many('zlms.badge.award', 'learner_id', string='Badges')
    badge_count = fields.Integer(compute='_compute_gamification')
    boost_streak = fields.Integer(string='Daily Boost Streak', compute='_compute_gamification')
    month_points = fields.Integer(string='Points this Month', compute='_compute_gamification')

    def _compute_gamification(self):
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)
        Answer = self.env['zlms.boost.answer'].sudo()
        Log = self.env['zlms.points.log'].sudo()
        for learner in self:
            learner.badge_count = len(learner.badge_award_ids)
            days = set(Answer.search([('learner_id', '=', learner.id),
                                      ('answer_date', '>=', today - timedelta(days=120))]).mapped('answer_date'))
            day = today if today in days else today - timedelta(days=1)
            streak = 0
            while day in days:
                streak += 1
                day -= timedelta(days=1)
            learner.boost_streak = streak
            learner.month_points = sum(Log.search([('learner_id', '=', learner.id),
                                                   ('log_date', '>=', month_start)]).mapped('points'))

    def _add_points(self, points, reason=None):
        res = super()._add_points(points, reason=reason)
        self.env['zlms.points.log'].sudo().create([
            {'learner_id': l.id, 'store_id': l.store_id.id, 'points': points, 'reason': reason or ''} for l in self])
        if not self.env.context.get('zlms_badge_check'):
            self._check_badges()
        return res

    def _badge_metric(self, criterion):
        self.ensure_one()
        if criterion == 'points':
            return self.points
        if criterion == 'paths_completed':
            return len(self.assignment_ids.filtered(lambda a: a.state == 'completed'))
        if criterion == 'certificates':
            return self.certificate_count
        if criterion == 'boost_streak':
            return self.boost_streak
        if criterion == 'boost_mastered':
            return len(self.boost_card_ids.filtered(lambda c: c.box >= 4))
        if criterion == 'roleplay_best':
            return max(self.roleplay_session_ids.filtered(lambda s: s.state == 'evaluated').mapped('score') or [0])
        if criterion == 'bulletins_ack':
            return self.env['zlms.bulletin.ack'].sudo().search_count(
                [('learner_id', '=', self.id), ('state', '=', 'acknowledged')])
        if criterion == 'observation_avg':
            obs = self.env['zlms.observation'].sudo().search([('learner_id', '=', self.id), ('state', '=', 'done')])
            return sum(obs.mapped('score')) / len(obs) if obs else 0
        if criterion == 'sessions_attended':
            return self.env['zlms.training.attendee'].sudo().search_count(
                [('learner_id', '=', self.id), ('state', '=', 'attended')])
        return 0

    def _check_badges(self):
        badges = self.env['zlms.badge'].sudo().search([])
        Award = self.env['zlms.badge.award'].sudo()
        new_awards = Award
        for learner in self.sudo():
            owned = learner.badge_award_ids.badge_id
            for badge in badges - owned:
                if learner._badge_metric(badge.criterion) >= badge.threshold:
                    new_awards |= Award.create({'badge_id': badge.id, 'learner_id': learner.id})
                    learner.message_post(body=_('Badge earned: %s', badge.name))
                    if badge.bonus_points:
                        learner.with_context(zlms_badge_check=True)._add_points(
                            badge.bonus_points, reason=_('Badge: %s', badge.name))
        return new_awards

    @api.model
    def _cron_check_badges(self):
        self.search([('active', '=', True)])._check_badges()


class ZlmsStoreChallenge(models.Model):
    _inherit = 'zlms.store'

    month_points = fields.Integer(string='Points this Month', compute='_compute_challenge')
    challenge_score = fields.Float(string='Challenge Score', compute='_compute_challenge',
                                   help='Points earned this month per team member - fair across store sizes.')

    def _compute_challenge(self):
        month_start = fields.Date.context_today(self).replace(day=1)
        data = dict(self.env['zlms.points.log'].sudo()._read_group(
            [('store_id', 'in', self.ids), ('log_date', '>=', month_start)], ['store_id'], ['points:sum']))
        for store in self:
            store.month_points = data.get(store, 0)
            store.challenge_score = round(store.month_points / store.headcount, 1) if store.headcount else 0.0

    @api.model
    def get_store_challenge(self, limit=10):
        stores = self.search([('active', '=', True)])
        rows = [{'id': s.id, 'name': s.name, 'region': s.region_id.name or '', 'points': s.month_points,
                 'score': s.challenge_score, 'headcount': s.headcount} for s in stores]
        rows.sort(key=lambda r: r['score'], reverse=True)
        return rows[:limit]
