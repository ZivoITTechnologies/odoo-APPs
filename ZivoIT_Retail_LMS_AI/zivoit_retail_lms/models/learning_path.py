# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .store import STORE_FORMATS

PATH_TYPES = [
    ('onboarding', 'Onboarding Journey'),
    ('product', 'Product Knowledge'),
    ('service', 'Customer Service'),
    ('selling', 'Selling Skills'),
    ('vm', 'Visual Merchandising'),
    ('compliance', 'Compliance & SOP'),
    ('leadership', 'Store Leadership'),
    ('launch', 'New Collection Launch'),
    ('refresher', 'Refresher'),
]


class ZlmsLearningPath(models.Model):
    _name = 'zlms.learning.path'
    _description = 'Retail Learning Path / Journey'
    _inherit = ['mail.thread']
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True, tracking=True)
    code = fields.Char()
    sequence = fields.Integer(default=10)
    path_type = fields.Selection(PATH_TYPES, required=True, default='onboarding', tracking=True)
    state = fields.Selection([('draft', 'Draft'), ('published', 'Published'), ('archived', 'Retired')],
                             default='draft', tracking=True)
    description = fields.Html(translate=True)
    image_1920 = fields.Image(max_width=1024, max_height=1024)
    color = fields.Integer()
    role_ids = fields.Many2many('zlms.role', string='Target Roles',
                                help='Leave empty to target every role.')
    store_format = fields.Selection(STORE_FORMATS, string='Only for Store Format')
    skill_ids = fields.Many2many('zlms.skill', string='Skills Developed')
    auto_assign = fields.Boolean(help='Automatically assign this path to new learners whose role matches.')
    duration_days = fields.Integer(string='Complete Within (days)', default=30)
    pass_score = fields.Float(string='Pass Score %', default=70.0)
    issue_certificate = fields.Boolean(default=True)
    certificate_validity_months = fields.Integer(default=12, help='0 = never expires.')
    completion_points = fields.Integer(default=100)
    line_ids = fields.One2many('zlms.learning.path.line', 'path_id', string='Steps', copy=True)
    assignment_ids = fields.One2many('zlms.assignment', 'path_id')
    line_count = fields.Integer(compute='_compute_counts')
    assignment_count = fields.Integer(compute='_compute_counts')
    completed_count = fields.Integer(compute='_compute_counts')
    completion_rate = fields.Float(compute='_compute_counts', string='Completion %')
    active = fields.Boolean(default=True)

    @api.depends('line_ids', 'assignment_ids.state')
    def _compute_counts(self):
        for path in self:
            assignments = path.assignment_ids.filtered(lambda a: a.state != 'cancelled')
            path.line_count = len(path.line_ids)
            path.assignment_count = len(assignments)
            path.completed_count = len(assignments.filtered(lambda a: a.state == 'completed'))
            path.completion_rate = round(100.0 * path.completed_count / len(assignments), 1) if assignments else 0.0

    def action_publish(self):
        for path in self:
            if not path.line_ids:
                raise ValidationError(_('Add at least one step to "%s" before publishing.', path.name))
        self.write({'state': 'published'})
        for path in self.filtered('auto_assign'):
            learners = self.env['zlms.learner'].search(
                [('role_id', 'in', path.role_ids.ids)] if path.role_ids else [('role_id', '!=', False)])
            if path.store_format:
                learners = learners.filtered(lambda l: l.store_id.store_format == path.store_format)
            path._assign_to_learners(learners)
        return True

    def action_retire(self):
        self.write({'state': 'archived'})

    def action_reset_draft(self):
        self.write({'state': 'draft'})

    def action_view_assignments(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_retail_lms.zlms_assignment_action')
        action['domain'] = [('path_id', '=', self.id)]
        action['context'] = {'default_path_id': self.id}
        return action

    def action_open_assign_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Assign "%s"', self.name),
            'res_model': 'zlms.assign.path.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_path_id': self.id},
        }

    def _assign_to_learners(self, learners, due_date=None):
        """Create one open assignment per learner (idempotent) and enrol them in courses."""
        self.ensure_one()
        Assignment = self.env['zlms.assignment'].sudo()
        existing = Assignment.search([('path_id', '=', self.id), ('learner_id', 'in', learners.ids),
                                      ('state', 'not in', ('cancelled', 'expired'))])
        to_assign = learners - existing.learner_id
        today = fields.Date.context_today(self)
        vals_list = []
        for learner in to_assign:
            start = max(today, learner.joining_date or today) if self.path_type == 'onboarding' else today
            vals_list.append({
                'learner_id': learner.id,
                'path_id': self.id,
                'assign_date': today,
                'due_date': due_date or (start + timedelta(days=self.duration_days or 30)),
            })
        assignments = Assignment.create(vals_list)
        return assignments


class ZlmsLearningPathLine(models.Model):
    _name = 'zlms.learning.path.line'
    _description = 'Learning Path Step'
    _order = 'path_id, sequence, id'

    path_id = fields.Many2one('zlms.learning.path', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    name = fields.Char(required=True, translate=True)
    line_type = fields.Selection([
        ('course', 'eLearning Course'),
        ('certification', 'Certification Exam'),
        ('ojt', 'On-the-Job Task (manager sign-off)'),
        ('roleplay', 'AI Role-play Practice'),
        ('session', 'Instructor-led Session'),
    ], required=True, default='course')
    ilt_course_id = fields.Many2one('zlms.ilt.course', string='Classroom Course')
    channel_id = fields.Many2one('slide.channel', string='Course')
    survey_id = fields.Many2one('survey.survey', string='Certification', domain=[('certification', '=', True)])
    scenario_id = fields.Many2one('zlms.roleplay.scenario', string='Role-play Scenario')
    instructions = fields.Text(translate=True, help='What the learner must demonstrate (for OJT tasks).')
    day_offset = fields.Integer(string='Due on Day', default=7,
                                help='Day number of the journey by which this step should be done.')
    min_score = fields.Float(string='Min. Score %', default=0.0)
    mandatory = fields.Boolean(default=True)
    weight = fields.Float(default=1.0)

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        # new steps in a live journey are added to everyone already on it
        for line in lines:
            open_assignments = self.env['zlms.assignment'].sudo().search([
                ('path_id', '=', line.path_id.id), ('state', 'in', ('assigned', 'in_progress'))])
            if open_assignments:
                self.env['zlms.assignment.line'].sudo().create(
                    [{'assignment_id': a.id, 'path_line_id': line.id} for a in open_assignments])
                if line.line_type == 'course' and line.channel_id:
                    line.channel_id.sudo()._action_add_members(open_assignments.partner_id)
        return lines

    @api.constrains('line_type', 'channel_id', 'survey_id', 'scenario_id', 'ilt_course_id')
    def _check_target(self):
        for line in self:
            if line.line_type == 'course' and not line.channel_id:
                raise ValidationError(_('Step "%s" needs a course.', line.name))
            if line.line_type == 'certification' and not line.survey_id:
                raise ValidationError(_('Step "%s" needs a certification.', line.name))
            if line.line_type == 'roleplay' and not line.scenario_id:
                raise ValidationError(_('Step "%s" needs a role-play scenario.', line.name))
            if line.line_type == 'session' and not line.ilt_course_id:
                raise ValidationError(_('Step "%s" needs an instructor-led course.', line.name))

    @api.onchange('channel_id', 'survey_id', 'scenario_id', 'ilt_course_id')
    def _onchange_target(self):
        target = self.channel_id or self.survey_id or self.scenario_id or self.ilt_course_id
        if target and not self.name:
            self.name = target.display_name
