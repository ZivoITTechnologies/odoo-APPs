# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError


# First-attempt accuracy is the best signal of knowledge for a native Odoo quiz,
# because learners must eventually answer correctly to complete it.
QUIZ_ATTEMPT_SCORE = {1: 100.0, 2: 80.0, 3: 60.0}
QUIZ_ATTEMPT_SCORE_DEFAULT = 40.0


class ZlmsAssignment(models.Model):
    _name = 'zlms.assignment'
    _description = 'Learning Path Assignment'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'due_date, id desc'

    name = fields.Char(compute='_compute_name', store=True)
    learner_id = fields.Many2one('zlms.learner', required=True, ondelete='cascade', index=True, tracking=True)
    learner_user_id = fields.Many2one(related='learner_id.user_id', store=True, string='Learner Login')
    partner_id = fields.Many2one(related='learner_id.partner_id', store=True)
    path_id = fields.Many2one('zlms.learning.path', required=True, ondelete='restrict', index=True, tracking=True)
    path_type = fields.Selection(related='path_id.path_type', store=True)
    store_id = fields.Many2one(related='learner_id.store_id', store=True, index=True)
    region_id = fields.Many2one(related='learner_id.region_id', store=True)
    role_id = fields.Many2one(related='learner_id.role_id', store=True)
    store_manager_user_id = fields.Many2one(related='learner_id.store_id.manager_user_id', store=True)
    company_id = fields.Many2one(related='learner_id.company_id', store=True)
    assign_date = fields.Date(default=fields.Date.context_today, required=True)
    due_date = fields.Date(tracking=True)
    completion_date = fields.Date(readonly=True, tracking=True)
    state = fields.Selection([
        ('assigned', 'Assigned'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('expired', 'Re-certification Due'),
        ('cancelled', 'Cancelled'),
    ], default='assigned', required=True, tracking=True, index=True)
    progress = fields.Float(string='Progress %', readonly=True, aggregator='avg')
    score = fields.Float(string='Score %', readonly=True, aggregator='avg')
    passed = fields.Boolean(readonly=True)
    is_overdue = fields.Boolean(compute='_compute_is_overdue', search='_search_is_overdue')
    days_left = fields.Integer(compute='_compute_is_overdue')
    line_ids = fields.One2many('zlms.assignment.line', 'assignment_id', string='Steps')
    certificate_id = fields.Many2one('zlms.certificate', readonly=True)
    last_sync = fields.Datetime(readonly=True)

    @api.depends('learner_id.name', 'path_id.name')
    def _compute_name(self):
        for rec in self:
            rec.name = '%s - %s' % (rec.learner_id.name or '', rec.path_id.name or '')

    def _compute_is_overdue(self):
        today = fields.Date.context_today(self)
        for rec in self:
            open_state = rec.state in ('assigned', 'in_progress')
            rec.is_overdue = bool(open_state and rec.due_date and rec.due_date < today)
            rec.days_left = (rec.due_date - today).days if (open_state and rec.due_date) else 0

    def _search_is_overdue(self, operator, value):
        today = fields.Date.context_today(self)
        if operator in ('in', 'not in'):
            values = set(value)
            positive = (True in values) if operator == 'in' else (True not in values)
        else:
            positive = (operator == '=' and bool(value)) or (operator == '!=' and not value)
        if positive:
            return [('state', 'in', ('assigned', 'in_progress')), ('due_date', '<', today)]
        return ['|', '|', ('state', 'not in', ('assigned', 'in_progress')), ('due_date', '=', False),
                ('due_date', '>=', today)]

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        assignments = super().create(vals_list)
        for assignment in assignments:
            assignment.line_ids = [(0, 0, {'path_line_id': line.id}) for line in assignment.path_id.line_ids]
        assignments._enrol_courses()
        assignments._notify_assigned()
        return assignments

    def _enrol_courses(self):
        for assignment in self:
            channels = assignment.path_id.line_ids.filtered(lambda l: l.line_type == 'course').channel_id
            if channels and assignment.partner_id:
                channels.sudo()._action_add_members(assignment.partner_id)

    def _notify_assigned(self):
        template = self.env.ref('zivoit_retail_lms.mail_template_assignment_new', raise_if_not_found=False)
        for assignment in self:
            assignment.message_post(body=_('Learning path "%(path)s" assigned, due on %(due)s.',
                                           path=assignment.path_id.name, due=assignment.due_date))
            if template and assignment.learner_id.email and not self.env.context.get('zlms_no_mail'):
                template.send_mail(assignment.id, force_send=False)
            if assignment.store_manager_user_id:
                assignment.activity_schedule(
                    'mail.mail_activity_data_todo', date_deadline=assignment.due_date,
                    summary=_('Coach %s through %s', assignment.learner_id.name, assignment.path_id.name),
                    user_id=assignment.store_manager_user_id.id)

    # ------------------------------------------------------------------
    # Progress engine
    # ------------------------------------------------------------------
    def _sync_progress(self):
        """Recompute every step from native eLearning, Survey and role-play data."""
        SlidePartner = self.env['slide.slide.partner'].sudo()
        ChannelPartner = self.env['slide.channel.partner'].sudo()
        UserInput = self.env['survey.user_input'].sudo()
        Session = self.env['zlms.roleplay.session'].sudo()
        for assignment in self.sudo():
            if assignment.state in ('cancelled', 'expired'):
                continue
            partner = assignment.partner_id
            for line in assignment.line_ids:
                ptype = line.line_type
                vals = {}
                if ptype == 'course' and line.channel_id:
                    membership = ChannelPartner.with_context(active_test=False).search(
                        [('channel_id', '=', line.channel_id.id), ('partner_id', '=', partner.id)], limit=1)
                    quiz_partners = SlidePartner.search([
                        ('partner_id', '=', partner.id), ('slide_id.channel_id', '=', line.channel_id.id),
                        ('completed', '=', True), ('slide_id.question_ids', '!=', False)])
                    scores = [QUIZ_ATTEMPT_SCORE.get(sp.quiz_attempts_count, QUIZ_ATTEMPT_SCORE_DEFAULT)
                              for sp in quiz_partners]
                    vals['progress'] = float(membership.completion or 0)
                    vals['score'] = round(sum(scores) / len(scores), 1) if scores else 0.0
                    done = membership.member_status == 'completed'
                    vals['state'] = 'done' if done else ('in_progress' if vals['progress'] else 'todo')
                elif ptype == 'certification' and line.survey_id:
                    inputs = UserInput.search([('survey_id', '=', line.survey_id.id), ('partner_id', '=', partner.id),
                                               ('state', '=', 'done')])
                    best = max(inputs.mapped('scoring_percentage') or [0.0])
                    success = any(inputs.mapped('scoring_success'))
                    vals['score'] = round(best, 1)
                    vals['progress'] = 100.0 if success else (50.0 if inputs else 0.0)
                    vals['state'] = 'done' if success else ('in_progress' if inputs else 'todo')
                elif ptype == 'roleplay' and line.scenario_id:
                    sessions = Session.search([('learner_id', '=', assignment.learner_id.id),
                                               ('scenario_id', '=', line.scenario_id.id), ('state', '=', 'evaluated')])
                    best = max(sessions.mapped('score') or [0.0])
                    threshold = line.min_score or 60.0
                    vals['score'] = best
                    vals['progress'] = 100.0 if best >= threshold else (50.0 if sessions else 0.0)
                    vals['state'] = 'done' if best >= threshold else ('in_progress' if sessions else 'todo')
                elif ptype == 'session' and line.ilt_course_id:
                    attendance = self.env['zlms.training.attendee'].sudo().search([
                        ('learner_id', '=', assignment.learner_id.id),
                        ('ilt_course_id', '=', line.ilt_course_id.id), ('state', '!=', 'cancelled')])
                    attended = attendance.filtered(lambda a: a.state == 'attended')
                    vals['progress'] = 100.0 if attended else (50.0 if attendance.filtered(
                        lambda a: a.state == 'registered') else 0.0)
                    vals['state'] = 'done' if attended else ('in_progress' if vals['progress'] else 'todo')
                elif ptype == 'ojt':
                    continue  # driven by manager sign-off
                if vals.get('state') == 'done' and not line.completed_on:
                    vals['completed_on'] = fields.Date.context_today(self)
                if vals:
                    line.write(vals)
            assignment._update_from_lines()

    def _update_from_lines(self):
        for assignment in self:
            lines = assignment.line_ids
            total_weight = sum(lines.mapped('weight')) or 1.0
            progress = round(sum(l.progress * l.weight for l in lines) / total_weight, 1) if lines else 0.0
            scored = lines.filtered(lambda l: l.score > 0)
            score = round(sum(scored.mapped('score')) / len(scored), 1) if scored else 0.0
            vals = {'progress': progress, 'score': score, 'last_sync': fields.Datetime.now()}
            mandatory = lines.filtered('mandatory')
            if lines and all(l.state == 'done' for l in mandatory):
                vals['progress'] = 100.0 if all(l.state == 'done' for l in lines) else progress
                if assignment.state != 'completed':
                    vals['state'] = 'completed'
                    vals['completion_date'] = fields.Date.context_today(self)
                    vals['passed'] = not scored or score >= assignment.path_id.pass_score
            elif progress > 0 and assignment.state == 'assigned':
                vals['state'] = 'in_progress'
            just_completed = vals.get('state') == 'completed'
            assignment.write(vals)
            if just_completed:
                assignment._on_completed()

    def _on_completed(self):
        for assignment in self:
            path = assignment.path_id
            assignment.learner_id._add_points(path.completion_points, reason=_('Completed %s', path.name))
            if path.issue_certificate and assignment.passed:
                assignment.certificate_id = self.env['zlms.certificate'].sudo().create({
                    'learner_id': assignment.learner_id.id,
                    'path_id': path.id,
                    'assignment_id': assignment.id,
                    'score': assignment.score,
                })
            assignment.message_post(body=_('Journey completed with score %(score)s%%. %(cert)s',
                                           score=assignment.score,
                                           cert=_('Certificate issued.') if assignment.certificate_id else ''))
            assignment.activity_ids.action_feedback(feedback=_('Learning path completed'))
            template = self.env.ref('zivoit_retail_lms.mail_template_assignment_done', raise_if_not_found=False)
            if template and assignment.learner_id.email and not self.env.context.get('zlms_no_mail'):
                template.send_mail(assignment.id, force_send=False)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_sync(self):
        self._sync_progress()
        return True

    def action_cancel(self):
        self.write({'state': 'cancelled'})

    def action_reopen(self):
        self.write({'state': 'assigned', 'completion_date': False})
        self._sync_progress()

    def action_send_reminder(self):
        template = self.env.ref('zivoit_retail_lms.mail_template_assignment_reminder', raise_if_not_found=False)
        for assignment in self.filtered(lambda a: a.state in ('assigned', 'in_progress')):
            if template and assignment.learner_id.email:
                template.send_mail(assignment.id, force_send=False)
            assignment.message_post(body=_('Reminder sent to %s.', assignment.learner_id.name))
        return True

    # ------------------------------------------------------------------
    # Crons
    # ------------------------------------------------------------------
    @api.model
    def _cron_sync_progress(self):
        self.search([('state', 'in', ('assigned', 'in_progress'))])._sync_progress()

    @api.model
    def _cron_overdue_reminders(self):
        overdue = self.search([('is_overdue', '=', True)])
        overdue.action_send_reminder()
        for assignment in overdue.filtered('store_manager_user_id'):
            assignment.activity_schedule(
                'mail.mail_activity_data_todo', date_deadline=fields.Date.context_today(self),
                summary=_('Overdue: %s', assignment.name), user_id=assignment.store_manager_user_id.id)


class ZlmsAssignmentLine(models.Model):
    _name = 'zlms.assignment.line'
    _description = 'Assignment Step Progress'
    _order = 'sequence, id'

    assignment_id = fields.Many2one('zlms.assignment', required=True, ondelete='cascade', index=True)
    path_line_id = fields.Many2one('zlms.learning.path.line', required=True, ondelete='cascade')
    name = fields.Char(related='path_line_id.name')
    sequence = fields.Integer(related='path_line_id.sequence', store=True)
    line_type = fields.Selection(related='path_line_id.line_type', store=True)
    channel_id = fields.Many2one(related='path_line_id.channel_id')
    survey_id = fields.Many2one(related='path_line_id.survey_id')
    scenario_id = fields.Many2one(related='path_line_id.scenario_id')
    ilt_course_id = fields.Many2one(related='path_line_id.ilt_course_id', store=True)
    instructions = fields.Text(related='path_line_id.instructions')
    mandatory = fields.Boolean(related='path_line_id.mandatory')
    weight = fields.Float(related='path_line_id.weight')
    min_score = fields.Float(related='path_line_id.min_score')
    learner_id = fields.Many2one(related='assignment_id.learner_id', store=True)
    store_manager_user_id = fields.Many2one(related='assignment_id.store_manager_user_id', store=True)
    state = fields.Selection([('todo', 'To Do'), ('in_progress', 'In Progress'), ('done', 'Done')],
                             default='todo', required=True)
    progress = fields.Float(string='Progress %')
    score = fields.Float(string='Score %')
    due_date = fields.Date(compute='_compute_due_date', store=True)
    completed_on = fields.Date()
    signed_by_id = fields.Many2one('res.users', string='Signed off by', readonly=True)
    manager_rating = fields.Selection([('1', 'Needs support'), ('2', 'Developing'), ('3', 'Competent'),
                                       ('4', 'Strong'), ('5', 'Role model')], string='Manager Rating')
    notes = fields.Text()

    @api.depends('assignment_id.assign_date', 'path_line_id.day_offset')
    def _compute_due_date(self):
        for line in self:
            start = line.assignment_id.assign_date
            line.due_date = start + timedelta(days=line.path_line_id.day_offset or 0) if start else False

    def action_sign_off(self):
        user = self.env.user
        for line in self:
            if line.line_type != 'ojt':
                raise UserError(_('Only on-the-job tasks are signed off manually.'))
            allowed = user.has_group('zivoit_retail_lms.group_zlms_manager') or \
                line.assignment_id.store_manager_user_id == user or \
                line.assignment_id.store_id.area_manager_user_id == user
            if not allowed:
                raise UserError(_('Only the store manager, area manager or L&D team can sign off this task.'))
            rating = line.manager_rating or '3'
            line.sudo().write({
                'state': 'done', 'progress': 100.0, 'completed_on': fields.Date.context_today(self),
                'signed_by_id': user.id, 'manager_rating': rating, 'score': int(rating) * 20.0,
            })
            line.assignment_id.sudo().message_post(body=_('OJT task "%(task)s" signed off by %(user)s.',
                                                          task=line.name, user=user.name))
        self.assignment_id.sudo()._update_from_lines()
        return True

    def action_open_target(self):
        self.ensure_one()
        if self.line_type == 'course' and self.channel_id:
            return {'type': 'ir.actions.act_url', 'url': self.channel_id.website_url, 'target': 'new'}
        if self.line_type == 'certification' and self.survey_id:
            return {'type': 'ir.actions.act_url', 'url': self.survey_id.get_start_url(), 'target': 'new'}
        if self.line_type == 'session' and self.ilt_course_id:
            return {'type': 'ir.actions.act_window', 'res_model': 'zlms.training.session', 'name': self.ilt_course_id.name,
                    'view_mode': 'list,form', 'domain': [('ilt_course_id', '=', self.ilt_course_id.id)]}
        if self.line_type == 'roleplay' and self.scenario_id:
            return self.scenario_id.with_context(default_learner_id=self.learner_id.id).action_start_session()
        return True
