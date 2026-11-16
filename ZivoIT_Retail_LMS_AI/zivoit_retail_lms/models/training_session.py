# -*- coding: utf-8 -*-
"""Instructor-led training (classroom, virtual, on-the-floor) with registration, attendance and feedback."""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ZlmsIltCourse(models.Model):
    _name = 'zlms.ilt.course'
    _description = 'Instructor-led Course'
    _order = 'name'

    name = fields.Char(required=True, translate=True)
    description = fields.Html(translate=True)
    duration_hours = fields.Float(default=2.0)
    skill_ids = fields.Many2many('zlms.skill', string='Skills Developed')
    session_ids = fields.One2many('zlms.training.session', 'ilt_course_id')
    session_count = fields.Integer(compute='_compute_stats')
    avg_rating = fields.Float(string='Avg. Feedback', compute='_compute_stats')
    active = fields.Boolean(default=True)

    def _compute_stats(self):
        for course in self:
            course.session_count = len(course.session_ids)
            rated = course.session_ids.attendee_ids.filtered('feedback_rating')
            course.avg_rating = round(sum(int(a.feedback_rating) for a in rated) / len(rated), 2) if rated else 0.0


class ZlmsTrainingSession(models.Model):
    _name = 'zlms.training.session'
    _description = 'Training Session'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'start_datetime desc'

    name = fields.Char(required=True, tracking=True)
    ilt_course_id = fields.Many2one('zlms.ilt.course', string='Course', required=True)
    session_type = fields.Selection([('classroom', 'Classroom'), ('virtual', 'Virtual / Video Call'),
                                     ('on_floor', 'On the Floor (store)')], default='classroom', required=True)
    trainer_user_id = fields.Many2one('res.users', string='Trainer', default=lambda self: self.env.user)
    start_datetime = fields.Datetime(string='Start', required=True)
    end_datetime = fields.Datetime(string='End', required=True)
    store_id = fields.Many2one('zlms.store', string='Venue Store')
    region_id = fields.Many2one('zlms.region', string='Region',
                                help='Learners of this region can self-register from the portal.')
    location = fields.Char()
    meeting_url = fields.Char(string='Meeting Link')
    capacity = fields.Integer(default=20)
    attendee_ids = fields.One2many('zlms.training.attendee', 'session_id', string='Attendees')
    seats_taken = fields.Integer(compute='_compute_seats')
    seats_left = fields.Integer(compute='_compute_seats')
    attended_count = fields.Integer(compute='_compute_seats')
    avg_rating = fields.Float(string='Avg. Feedback', compute='_compute_seats')
    state = fields.Selection([('planned', 'Planned'), ('done', 'Done'), ('cancelled', 'Cancelled')],
                             default='planned', tracking=True)

    @api.depends('attendee_ids.state', 'attendee_ids.feedback_rating', 'capacity')
    def _compute_seats(self):
        for session in self:
            active = session.attendee_ids.filtered(lambda a: a.state != 'cancelled')
            session.seats_taken = len(active)
            session.seats_left = max(0, session.capacity - len(active))
            session.attended_count = len(active.filtered(lambda a: a.state == 'attended'))
            rated = active.filtered('feedback_rating')
            session.avg_rating = round(sum(int(a.feedback_rating) for a in rated) / len(rated), 2) if rated else 0.0

    @api.constrains('start_datetime', 'end_datetime')
    def _check_dates(self):
        for session in self:
            if session.end_datetime <= session.start_datetime:
                raise UserError(_('The session must end after it starts.'))

    def register(self, learners):
        self.ensure_one()
        if self.state != 'planned':
            raise UserError(_('Registration is closed for this session.'))
        Attendee = self.env['zlms.training.attendee'].sudo()
        existing = self.attendee_ids
        for learner in learners:
            rec = existing.filtered(lambda a, l=learner: a.learner_id == l)
            if rec:
                if rec.state == 'cancelled':
                    rec.state = 'registered'
                continue
            if self.seats_left <= 0:
                raise UserError(_('Session "%s" is full.', self.name))
            Attendee.create({'session_id': self.id, 'learner_id': learner.id})
            self.invalidate_recordset(['seats_left', 'seats_taken'])
        return True

    def action_mark_all_attended(self):
        self.attendee_ids.filtered(lambda a: a.state == 'registered').write({'state': 'attended'})
        return True

    def action_done(self):
        for session in self:
            session.attendee_ids.filtered(lambda a: a.state == 'registered').write({'state': 'absent'})
            session.state = 'done'
            attended = session.attendee_ids.filtered(lambda a: a.state == 'attended')
            attended.learner_id._add_points(15, reason=_('Attended %s', session.name))
            self.env['zlms.assignment'].sudo().search([
                ('learner_id', 'in', attended.learner_id.ids), ('state', 'in', ('assigned', 'in_progress')),
                ('line_ids.ilt_course_id', '=', session.ilt_course_id.id)])._sync_progress()
        return True

    def action_cancel(self):
        self.write({'state': 'cancelled'})

    def action_invite_due_learners(self):
        """Register learners whose open journeys contain this course."""
        for session in self:
            lines = self.env['zlms.assignment.line'].sudo().search([
                ('ilt_course_id', '=', session.ilt_course_id.id), ('state', '!=', 'done'),
                ('assignment_id.state', 'in', ('assigned', 'in_progress'))])
            learners = lines.assignment_id.learner_id
            if session.region_id:
                learners = learners.filtered(lambda l: l.region_id == session.region_id)
            to_add = (learners - session.attendee_ids.learner_id)[:session.seats_left]
            if to_add:
                session.register(to_add)
        return True


class ZlmsTrainingAttendee(models.Model):
    _name = 'zlms.training.attendee'
    _description = 'Training Session Attendee'
    _order = 'session_id, id'

    session_id = fields.Many2one('zlms.training.session', required=True, ondelete='cascade', index=True)
    ilt_course_id = fields.Many2one(related='session_id.ilt_course_id', store=True)
    start_datetime = fields.Datetime(related='session_id.start_datetime', store=True)
    learner_id = fields.Many2one('zlms.learner', required=True, ondelete='cascade', index=True)
    store_id = fields.Many2one(related='learner_id.store_id', store=True)
    store_manager_user_id = fields.Many2one(related='learner_id.store_id.manager_user_id', store=True)
    state = fields.Selection([('registered', 'Registered'), ('attended', 'Attended'), ('absent', 'Absent'),
                              ('cancelled', 'Cancelled')], default='registered', required=True)
    feedback_rating = fields.Selection([('1', '1'), ('2', '2'), ('3', '3'), ('4', '4'), ('5', '5')],
                                       string='Feedback')
    feedback_comment = fields.Text()

    _session_learner_uniq = models.Constraint('unique(session_id, learner_id)', 'Learner already registered.')
