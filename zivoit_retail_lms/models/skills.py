# -*- coding: utf-8 -*-
"""Skills matrix, role targets, career ladder / promotion readiness and coaching observations."""
from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .ai_service import ZlmsAiUnavailable

LEVELS = [('1', '1 - Beginner'), ('2', '2 - Developing'), ('3', '3 - Competent'), ('4', '4 - Strong'),
          ('5', '5 - Expert / Role model')]


class ZlmsRoleSkill(models.Model):
    _name = 'zlms.role.skill'
    _description = 'Role Skill Requirement'
    _order = 'role_id, skill_id'

    role_id = fields.Many2one('zlms.role', required=True, ondelete='cascade')
    skill_id = fields.Many2one('zlms.skill', required=True, ondelete='cascade')
    target_level = fields.Float(default=3.0)

    _role_skill_uniq = models.Constraint('unique(role_id, skill_id)', 'Skill already defined for this role.')


class ZlmsRoleCareer(models.Model):
    _inherit = 'zlms.role'

    skill_req_ids = fields.One2many('zlms.role.skill', 'role_id', string='Skill Targets')
    next_role_id = fields.Many2one('zlms.role', string='Next Role (Career Ladder)')
    promotion_path_ids = fields.Many2many('zlms.learning.path', string='Required for Promotion',
                                          help='Journeys to complete before moving to the next role.')
    min_tenure_days = fields.Integer(string='Min. Days in Role before Promotion', default=180)


class ZlmsLearnerSkill(models.Model):
    _name = 'zlms.learner.skill'
    _description = 'Learner Skill Level'
    _order = 'learner_id, skill_id'

    learner_id = fields.Many2one('zlms.learner', required=True, ondelete='cascade', index=True)
    skill_id = fields.Many2one('zlms.skill', required=True, ondelete='cascade')
    skill_area = fields.Selection(related='skill_id.skill_area', store=True)
    store_id = fields.Many2one(related='learner_id.store_id', store=True)
    role_id = fields.Many2one(related='learner_id.role_id', store=True)
    store_manager_user_id = fields.Many2one(related='learner_id.store_id.manager_user_id', store=True)
    level = fields.Float(default=1.0, aggregator='avg')
    target_level = fields.Float(compute='_compute_target', store=True, aggregator='avg')
    gap = fields.Float(compute='_compute_target', store=True, aggregator='avg')
    last_assessed = fields.Date(default=fields.Date.context_today)
    source = fields.Selection([('manual', 'Manager rating'), ('observation', 'Coaching observation'),
                               ('roleplay', 'AI role-play')], default='manual')

    _learner_skill_uniq = models.Constraint('unique(learner_id, skill_id)', 'One level per learner per skill.')

    @api.depends('level', 'learner_id.role_id.skill_req_ids.target_level', 'skill_id')
    def _compute_target(self):
        for rec in self:
            req = rec.learner_id.role_id.skill_req_ids.filtered(lambda r: r.skill_id == rec.skill_id)[:1]
            rec.target_level = req.target_level
            rec.gap = max(0.0, req.target_level - rec.level) if req else 0.0

    @api.model
    def _record(self, learner, skill, level, source, weight=0.5):
        """Blend a new evidence point into the learner's skill level (exponential smoothing)."""
        if not learner or not skill:
            return self.browse()
        level = max(1.0, min(5.0, level))
        rec = self.sudo().search([('learner_id', '=', learner.id), ('skill_id', '=', skill.id)], limit=1)
        if rec:
            rec.write({'level': round((1 - weight) * rec.level + weight * level, 2), 'source': source,
                       'last_assessed': fields.Date.context_today(self)})
        else:
            rec = self.sudo().create({'learner_id': learner.id, 'skill_id': skill.id, 'level': round(level, 2),
                                      'source': source})
        return rec


class ZlmsLearnerCareer(models.Model):
    _inherit = 'zlms.learner'

    skill_ids = fields.One2many('zlms.learner.skill', 'learner_id', string='Skills')
    observation_ids = fields.One2many('zlms.observation', 'learner_id', string='Coaching Observations')
    role_fit = fields.Float(string='Role Skill Fit %', compute='_compute_career')
    skill_gap_count = fields.Integer(string='Skill Gaps', compute='_compute_career')
    next_role_id = fields.Many2one(related='role_id.next_role_id', string='Next Role')
    promotion_readiness = fields.Float(string='Promotion Readiness %', compute='_compute_career')
    promotion_ready = fields.Boolean(compute='_compute_career', search='_search_promotion_ready')

    def _skill_fit(self, role):
        self.ensure_one()
        reqs = role.skill_req_ids
        if not reqs:
            return 100.0, 0
        levels = {s.skill_id.id: s.level for s in self.skill_ids}
        total = sum(reqs.mapped('target_level')) or 1.0
        got = sum(min(levels.get(r.skill_id.id, 0.0), r.target_level) for r in reqs)
        gaps = len([r for r in reqs if levels.get(r.skill_id.id, 0.0) < r.target_level])
        return round(100.0 * got / total, 1), gaps

    @api.depends('skill_ids.level', 'role_id', 'assignment_ids.state', 'tenure_days')
    def _compute_career(self):
        for learner in self:
            fit, gaps = learner._skill_fit(learner.role_id) if learner.role_id else (0.0, 0)
            learner.role_fit = fit
            learner.skill_gap_count = gaps
            role = learner.role_id
            if role and role.next_role_id:
                required = role.promotion_path_ids
                done = learner.assignment_ids.filtered(lambda a: a.state == 'completed').path_id
                path_part = 100.0 * len(required & done) / len(required) if required else 100.0
                next_fit, _gaps = learner._skill_fit(role.next_role_id)
                tenure_part = 100.0 if learner.tenure_days >= role.min_tenure_days else \
                    100.0 * learner.tenure_days / (role.min_tenure_days or 1)
                learner.promotion_readiness = round(0.5 * path_part + 0.4 * next_fit + 0.1 * tenure_part, 1)
            else:
                learner.promotion_readiness = 0.0
            learner.promotion_ready = learner.promotion_readiness >= 85.0

    def _search_promotion_ready(self, operator, value):
        if operator in ('in', 'not in'):
            positive = (True in set(value)) == (operator == 'in')
        else:
            positive = bool(value) == (operator == '=')
        ids = self.search([('role_id.next_role_id', '!=', False)]).filtered('promotion_ready').ids
        return [('id', 'in' if positive else 'not in', ids)]


class ZlmsObservationTemplate(models.Model):
    _name = 'zlms.observation.template'
    _description = 'Coaching Observation Checklist'
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    description = fields.Text(translate=True)
    role_ids = fields.Many2many('zlms.role', string='For Roles')
    item_ids = fields.One2many('zlms.observation.template.item', 'template_id', string='Checklist', copy=True)
    active = fields.Boolean(default=True)


class ZlmsObservationTemplateItem(models.Model):
    _name = 'zlms.observation.template.item'
    _description = 'Observation Checklist Item'
    _order = 'sequence, id'

    template_id = fields.Many2one('zlms.observation.template', required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    name = fields.Char(string='Behaviour to observe', required=True, translate=True)
    skill_id = fields.Many2one('zlms.skill')
    guidance = fields.Text(string='Coaching Tip', translate=True,
                           help='What great looks like / what to coach when it is missing.')


class ZlmsObservation(models.Model):
    _name = 'zlms.observation'
    _description = 'Coaching Observation (Floor Walk)'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'observation_date desc, id desc'

    name = fields.Char(compute='_compute_name', store=True)
    learner_id = fields.Many2one('zlms.learner', required=True, ondelete='cascade', index=True, tracking=True)
    store_id = fields.Many2one(related='learner_id.store_id', store=True)
    store_manager_user_id = fields.Many2one(related='learner_id.store_id.manager_user_id', store=True)
    observer_id = fields.Many2one('res.users', default=lambda self: self.env.user, required=True)
    template_id = fields.Many2one('zlms.observation.template', required=True)
    observation_date = fields.Date(default=fields.Date.context_today, required=True)
    line_ids = fields.One2many('zlms.observation.line', 'observation_id', string='Checklist')
    score = fields.Float(string='Score %', compute='_compute_score', store=True, aggregator='avg')
    strengths = fields.Text()
    next_steps = fields.Text(string='Agreed Next Steps')
    coaching_html = fields.Html(string='Coaching Plan', readonly=True, sanitize=True)
    state = fields.Selection([('draft', 'In Progress'), ('done', 'Completed')], default='draft', tracking=True)

    @api.depends('learner_id', 'observation_date')
    def _compute_name(self):
        for obs in self:
            obs.name = _('Observation - %(learner)s (%(date)s)', learner=obs.learner_id.name or '',
                         date=obs.observation_date or '')

    @api.depends('line_ids.rating')
    def _compute_score(self):
        for obs in self:
            rated = obs.line_ids.filtered('rating')
            obs.score = round(sum(int(l.rating) for l in rated) * 20.0 / len(rated), 1) if rated else 0.0

    @api.onchange('template_id')
    def _onchange_template(self):
        if self.template_id and not self.line_ids:
            self.line_ids = [(0, 0, {'item_id': item.id}) for item in self.template_id.item_ids]

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for obs in records.filtered(lambda o: not o.line_ids and o.template_id):
            obs.line_ids = [(0, 0, {'item_id': item.id}) for item in obs.template_id.item_ids]
        return records

    def action_done(self):
        for obs in self:
            if not obs.line_ids.filtered('rating'):
                raise UserError(_('Rate at least one behaviour before completing the observation.'))
            per_skill = {}
            for line in obs.line_ids.filtered(lambda l: l.rating and l.item_id.skill_id):
                per_skill.setdefault(line.item_id.skill_id, []).append(int(line.rating))
            for skill, ratings in per_skill.items():
                self.env['zlms.learner.skill']._record(obs.learner_id, skill, sum(ratings) / len(ratings),
                                                       'observation', weight=0.6)
            obs.state = 'done'
            if not obs.coaching_html:
                obs.action_ai_coaching_plan()
            obs.learner_id._add_points(10, reason=_('Coaching observation'))
            obs.message_post(body=_('Observation completed with score %s%%.', obs.score))
        return True

    def action_reset(self):
        self.write({'state': 'draft'})

    def action_ai_coaching_plan(self):
        for obs in self:
            weak = obs.line_ids.filtered(lambda l: l.rating and int(l.rating) <= 3).sorted(lambda l: int(l.rating))
            strong = obs.line_ids.filtered(lambda l: l.rating and int(l.rating) >= 4)
            html = None
            if self.env['zlms.ai.service'].is_enabled():
                lines = '\n'.join('- %s: %s/5 %s' % (l.item_id.name, l.rating or '-', l.comment or '')
                                  for l in obs.line_ids)
                prompt = ('Create a short, practical coaching plan for a store team member after a floor-walk '
                          'observation.\nRole: %s\nObservations:\n%s\nManager notes: %s\n\nReturn JSON: '
                          '{"praise": str, "focus": [str], "drills": [str], "follow_up": str}') % (
                    obs.learner_id.role_id.name or '-', lines, obs.next_steps or '-')
                try:
                    data = self.env['zlms.ai.service'].chat_json(
                        prompt, system=self.env['zlms.ai.service'].brand_context()
                        + ' You are an experienced retail store coach.', feature='coaching_plan')
                    html = self._render_plan(data)
                except ZlmsAiUnavailable:
                    html = None
            if not html:
                data = {
                    'praise': _('Great work on: %s.', ', '.join(strong.item_id.mapped('name')[:3]))
                    if strong else _('Thanks for being open to feedback - every observation builds your skills.'),
                    'focus': [l.item_id.name for l in weak[:3]],
                    'drills': [l.item_id.guidance for l in weak[:3] if l.item_id.guidance]
                    or [_('Shadow a top performer for one peak hour and note what they say.')],
                    'follow_up': _('Re-observe in 7 days on the same behaviours.'),
                }
                html = self._render_plan(data)
            obs.coaching_html = html
        return True

    @api.model
    def _render_plan(self, data):
        e = escape
        html = '<p><strong>%s</strong> %s</p>' % (e(_('Praise:')), e(data.get('praise') or ''))
        for key, label in (('focus', _('Focus areas')), ('drills', _('Practice drills'))):
            items = data.get(key) or []
            if items:
                html += '<p><strong>%s</strong></p><ul>%s</ul>' % (
                    e(label), ''.join('<li>%s</li>' % e(i) for i in items))
        if data.get('follow_up'):
            html += '<p><strong>%s</strong> %s</p>' % (e(_('Follow-up:')), e(data['follow_up']))
        return Markup(html)


class ZlmsObservationLine(models.Model):
    _name = 'zlms.observation.line'
    _description = 'Observation Rating'
    _order = 'sequence, id'

    observation_id = fields.Many2one('zlms.observation', required=True, ondelete='cascade', index=True)
    item_id = fields.Many2one('zlms.observation.template.item', required=True, ondelete='cascade')
    sequence = fields.Integer(related='item_id.sequence', store=True)
    name = fields.Char(related='item_id.name')
    skill_id = fields.Many2one(related='item_id.skill_id')
    guidance = fields.Text(related='item_id.guidance')
    rating = fields.Selection(LEVELS)
    comment = fields.Char()
