# -*- coding: utf-8 -*-
"""Daily Boost: adaptive spaced-repetition micro-quizzes (Leitner system with confidence)."""
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

BOX_INTERVAL_DAYS = {1: 1, 2: 2, 3: 4, 4: 8, 5: 16}
MAX_BOX = 5
POINTS_CORRECT = 5
POINTS_PARTICIPATION = 1


class ZlmsBoostCard(models.Model):
    _name = 'zlms.boost.card'
    _description = 'Daily Boost Card (spaced repetition)'
    _order = 'next_review_date, box, id'

    learner_id = fields.Many2one('zlms.learner', required=True, ondelete='cascade', index=True)
    learner_user_id = fields.Many2one(related='learner_id.user_id', store=True)
    store_id = fields.Many2one(related='learner_id.store_id', store=True)
    store_manager_user_id = fields.Many2one(related='learner_id.store_id.manager_user_id', store=True)
    question_id = fields.Many2one('slide.question', required=True, ondelete='cascade', index=True)
    question = fields.Char(related='question_id.question')
    channel_id = fields.Many2one(related='question_id.slide_id.channel_id', store=True, string='Course')
    box = fields.Integer(default=1, aggregator='avg')
    next_review_date = fields.Date(default=fields.Date.context_today, index=True)
    last_review_date = fields.Date()
    correct_count = fields.Integer()
    wrong_count = fields.Integer()
    mastered = fields.Boolean(compute='_compute_mastered', store=True)

    _learner_question_uniq = models.Constraint('unique(learner_id, question_id)',
                                               'A learner has one boost card per question.')

    @api.depends('box')
    def _compute_mastered(self):
        for card in self:
            card.mastered = card.box >= 4

    def answer(self, answer_id, confident=True):
        """Grade one answer, move the card between Leitner boxes and return feedback."""
        self.ensure_one()
        answer = self.env['slide.answer'].sudo().browse(int(answer_id))
        if answer.question_id != self.question_id:
            raise UserError(_('This answer does not belong to the question.'))
        today = fields.Date.context_today(self)
        correct = bool(answer.is_correct)
        if correct and confident:
            box = min(self.box + 1, MAX_BOX)
            interval = BOX_INTERVAL_DAYS[box]
        elif correct:
            box = self.box  # right but unsure: repeat soon, don't promote
            interval = 1
        else:
            box = 1
            interval = BOX_INTERVAL_DAYS[1]
        self.sudo().write({
            'box': box,
            'next_review_date': today + timedelta(days=interval),
            'last_review_date': today,
            'correct_count': self.correct_count + (1 if correct else 0),
            'wrong_count': self.wrong_count + (0 if correct else 1),
        })
        self.env['zlms.boost.answer'].sudo().create({
            'card_id': self.id, 'learner_id': self.learner_id.id, 'correct': correct, 'confident': confident,
        })
        points = POINTS_CORRECT if correct else POINTS_PARTICIPATION
        self.learner_id._add_points(points, reason=_('Daily Boost'))
        right = self.question_id.answer_ids.filtered('is_correct')[:1]
        return {
            'correct': correct,
            'points': points,
            'correct_answer': right.text_value,
            'comment': answer.comment or right.comment or '',
            'next_review_date': self.next_review_date,
            'box': box,
        }

    @api.model
    def _seed_for_learners(self, learners):
        """Create cards for every quiz question in the courses each learner is enrolled in."""
        ChannelPartner = self.env['slide.channel.partner'].sudo()
        Question = self.env['slide.question'].sudo()
        created = self.browse()
        for learner in learners.filtered('partner_id'):
            channels = ChannelPartner.search([('partner_id', '=', learner.partner_id.id)]).channel_id \
                .filtered('zlms_boost_enabled')
            if not channels:
                continue
            questions = Question.search([('slide_id.channel_id', 'in', channels.ids),
                                         ('slide_id.is_published', '=', True)])
            existing = self.sudo().search([('learner_id', '=', learner.id)]).question_id
            new_questions = questions - existing
            if new_questions:
                created |= self.sudo().create([{'learner_id': learner.id, 'question_id': q.id}
                                               for q in new_questions])
        return created

    @api.model
    def _get_due_cards(self, learner, limit=5):
        today = fields.Date.context_today(self)
        return self.sudo().search([('learner_id', '=', learner.id), ('next_review_date', '<=', today)],
                                  order='box, next_review_date, id', limit=limit)

    @api.model
    def _cron_seed_cards(self):
        self._seed_for_learners(self.env['zlms.learner'].search([('active', '=', True)]))


class ZlmsBoostAnswer(models.Model):
    _name = 'zlms.boost.answer'
    _description = 'Daily Boost Answer Log'
    _order = 'id desc'

    card_id = fields.Many2one('zlms.boost.card', required=True, ondelete='cascade')
    learner_id = fields.Many2one('zlms.learner', required=True, ondelete='cascade', index=True)
    store_id = fields.Many2one(related='learner_id.store_id', store=True)
    store_manager_user_id = fields.Many2one(related='learner_id.store_id.manager_user_id', store=True)
    question_id = fields.Many2one(related='card_id.question_id', store=True)
    answer_date = fields.Date(default=fields.Date.context_today, index=True)
    correct = fields.Boolean()
    confident = fields.Boolean()
