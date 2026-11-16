# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .ai_service import LANGUAGES


class ZlmsLearner(models.Model):
    _name = 'zlms.learner'
    _description = 'Retail Learner Profile'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'store_id, name'

    name = fields.Char(required=True, tracking=True)
    employee_code = fields.Char(string='Employee Code')
    employee_id = fields.Many2one('hr.employee', string='Employee', ondelete='set null')
    image_128 = fields.Image(related='partner_id.image_128')
    partner_id = fields.Many2one('res.partner', string='Contact', ondelete='restrict', copy=False,
                                 help='Contact used for course enrolment and progress tracking.')
    user_id = fields.Many2one('res.users', string='Learner Login', copy=False, tracking=True)
    email = fields.Char()
    mobile = fields.Char()
    store_id = fields.Many2one('zlms.store', string='Store', tracking=True, index=True)
    region_id = fields.Many2one(related='store_id.region_id', store=True, string='Region')
    store_manager_user_id = fields.Many2one(related='store_id.manager_user_id', store=True)
    role_id = fields.Many2one('zlms.role', string='Role', tracking=True)
    joining_date = fields.Date(default=fields.Date.context_today, tracking=True)
    preferred_language = fields.Selection(LANGUAGES, default='English')
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True)
    active = fields.Boolean(default=True)

    assignment_ids = fields.One2many('zlms.assignment', 'learner_id', string='Assignments')
    certificate_ids = fields.One2many('zlms.certificate', 'learner_id', string='Certificates')
    roleplay_session_ids = fields.One2many('zlms.roleplay.session', 'learner_id', string='Role-plays')
    boost_card_ids = fields.One2many('zlms.boost.card', 'learner_id', string='Daily Boost Cards')

    assignment_count = fields.Integer(compute='_compute_stats')
    completion_rate = fields.Float(string='Completion %', compute='_compute_stats')
    avg_score = fields.Float(string='Avg. Score', compute='_compute_stats')
    overdue_count = fields.Integer(compute='_compute_stats')
    certificate_count = fields.Integer(compute='_compute_stats')
    roleplay_avg = fields.Float(string='Role-play Avg.', compute='_compute_stats')
    boost_mastery = fields.Float(string='Boost Mastery %', compute='_compute_stats',
                                 help='Share of Daily Boost cards that reached box 4 or 5.')
    points = fields.Integer(string='Learning Points', tracking=True)
    tenure_days = fields.Integer(compute='_compute_stats')
    readiness_score = fields.Float(compute='_compute_stats',
                                   help='Composite of completion, assessment, role-play and retention.')
    risk_level = fields.Selection([('low', 'On Track'), ('medium', 'Needs Attention'), ('high', 'At Risk')],
                                  compute='_compute_stats', search='_search_risk_level')

    _user_uniq = models.Constraint('unique(user_id)', 'This login is already linked to another learner.')

    @api.depends('assignment_ids.progress', 'assignment_ids.score', 'assignment_ids.state',
                 'certificate_ids.state', 'roleplay_session_ids.score', 'boost_card_ids.box')
    def _compute_stats(self):
        today = fields.Date.context_today(self)
        for learner in self:
            assignments = learner.assignment_ids.filtered(lambda a: a.state != 'cancelled')
            learner.assignment_count = len(assignments)
            learner.completion_rate = round(sum(assignments.mapped('progress')) / len(assignments), 1) if assignments else 0.0
            scored = assignments.filtered(lambda a: a.score > 0)
            learner.avg_score = round(sum(scored.mapped('score')) / len(scored), 1) if scored else 0.0
            learner.overdue_count = len(assignments.filtered('is_overdue'))
            learner.certificate_count = len(learner.certificate_ids.filtered(lambda c: c.state in ('valid', 'expiring')))
            sessions = learner.roleplay_session_ids.filtered(lambda s: s.state == 'evaluated')
            learner.roleplay_avg = round(sum(sessions.mapped('score')) / len(sessions), 1) if sessions else 0.0
            cards = learner.boost_card_ids
            learner.boost_mastery = round(100.0 * len(cards.filtered(lambda c: c.box >= 4)) / len(cards), 1) if cards else 0.0
            learner.tenure_days = (today - learner.joining_date).days if learner.joining_date else 0
            parts = [(learner.completion_rate, 0.4)]
            if scored:
                parts.append((learner.avg_score, 0.3))
            if sessions:
                parts.append((learner.roleplay_avg, 0.2))
            if cards:
                parts.append((learner.boost_mastery, 0.1))
            weight = sum(w for _v, w in parts)
            learner.readiness_score = round(sum(v * w for v, w in parts) / weight, 1) if weight else 0.0
            learner.risk_level = learner._get_risk_level()

    def _get_risk_level(self):
        self.ensure_one()
        if not self.assignment_count:
            return 'low'
        if (self.overdue_count and self.completion_rate < 50) or (self.avg_score and self.avg_score < 60):
            return 'high'
        if self.overdue_count or (self.completion_rate < 50 and self.tenure_days > 30):
            return 'medium'
        return 'low'

    def _search_risk_level(self, operator, value):
        if operator in ('=', '!='):
            values, negate = {value}, operator == '!='
        elif operator in ('in', 'not in'):
            values, negate = set(value), operator == 'not in'
        else:
            raise UserError(_('Unsupported search on risk level.'))
        learners = self.search([]).filtered(lambda l: (l.risk_level in values) != negate)
        return [('id', 'in', learners.ids)]

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        Partner = self.env['res.partner'].sudo()
        for vals in vals_list:
            if not vals.get('partner_id'):
                if vals.get('user_id'):
                    vals['partner_id'] = self.env['res.users'].browse(vals['user_id']).partner_id.id
                elif vals.get('employee_id') and self.env['hr.employee'].sudo().browse(vals['employee_id']).work_contact_id:
                    vals['partner_id'] = self.env['hr.employee'].sudo().browse(vals['employee_id']).work_contact_id.id
                else:
                    vals['partner_id'] = Partner.create({
                        'name': vals.get('name'),
                        'email': vals.get('email'),
                        'phone': vals.get('mobile'),
                    }).id
        learners = super().create(vals_list)
        if not self.env.context.get('zlms_no_auto_assign'):
            learners._auto_assign_paths()
        return learners

    def write(self, vals):
        res = super().write(vals)
        if {'role_id', 'store_id'} & set(vals) and not self.env.context.get('zlms_no_auto_assign'):
            self._auto_assign_paths()
        return res

    # ------------------------------------------------------------------
    # Business logic
    # ------------------------------------------------------------------
    def _auto_assign_paths(self):
        Path = self.env['zlms.learning.path'].sudo()
        for learner in self.filtered(lambda l: l.active and l.role_id):
            paths = Path.search([('auto_assign', '=', True), ('state', '=', 'published'),
                                 '|', ('role_ids', '=', False), ('role_ids', 'in', learner.role_id.ids)])
            for path in paths.filtered(lambda p: not p.store_format or p.store_format == learner.store_id.store_format):
                path._assign_to_learners(learner)

    def action_grant_portal_access(self):
        portal_group = self.env.ref('base.group_portal')
        for learner in self:
            if learner.user_id:
                continue
            login = learner.email or learner.employee_code or learner.mobile
            if not login:
                raise UserError(_('Learner %s needs an email, mobile or employee code to create a login.', learner.name))
            user = self.env['res.users'].sudo().with_context(no_reset_password=True).create({
                'name': learner.name,
                'login': login,
                'email': learner.email,
                'partner_id': learner.partner_id.id,
                'group_ids': [(6, 0, portal_group.ids)],
            })
            learner.user_id = user
            learner.message_post(body=_('Learning portal access granted (login: %s).', login))
        return True

    def action_sync_progress(self):
        self.assignment_ids.filtered(lambda a: a.state not in ('completed', 'cancelled'))._sync_progress()
        return True

    def action_view_assignments(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_retail_lms.zlms_assignment_action')
        action['domain'] = [('learner_id', '=', self.id)]
        action['context'] = {'default_learner_id': self.id}
        return action

    def action_view_certificates(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_retail_lms.zlms_certificate_action')
        action['domain'] = [('learner_id', '=', self.id)]
        action['context'] = {}
        return action

    def action_ai_skill_gap(self):
        self.ensure_one()
        insight = self.env['zlms.ai.insight'].generate_insight('learner', learner=self)
        return insight._open_form()

    def _add_points(self, points, reason=None):
        for learner in self:
            learner.sudo().points += points
            if learner.user_id:
                learner.user_id.sudo()._add_karma(points, source=learner, reason=reason or _('Retail LMS'))

    @api.model
    def _get_current_learner(self):
        return self.sudo().search([('user_id', '=', self.env.uid)], limit=1)
