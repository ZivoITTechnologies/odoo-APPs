# -*- coding: utf-8 -*-
import json
import logging
from datetime import timedelta

from markupsafe import Markup, escape

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .zivo_equipment import ACTIVE_BOOKING_STATES

_logger = logging.getLogger(__name__)

AI_DECISIONS = [
    ('approve', 'Approve'),
    ('partial', 'Approve fewer units'),
    ('reschedule', 'Suggest another slot'),
    ('reject', 'Reject'),
]


class ZivoEquipmentBooking(models.Model):
    _name = 'zivo.equipment.booking'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'zivo.audit.mixin']
    _description = 'Equipment Booking'
    _order = 'start desc, id desc'

    _zivo_audit_fields = ['equipment_id', 'quantity', 'start', 'stop', 'state', 'event_id', 'user_id',
                          'approver_id', 'reject_reason']

    name = fields.Char(string='Reference', required=True, copy=False, readonly=True, default='New')
    equipment_id = fields.Many2one('zivo.equipment', required=True, tracking=True, index=True,
                                   domain="[('company_id', 'in', [company_id, False])]")
    category_id = fields.Many2one(related='equipment_id.category_id', store=True)
    quantity = fields.Integer(default=1, required=True, tracking=True)
    start = fields.Datetime(required=True, tracking=True, index=True)
    stop = fields.Datetime(required=True, tracking=True)
    duration = fields.Float(string='Duration (h)', compute='_compute_duration', store=True)
    user_id = fields.Many2one('res.users', string='Requested By', required=True, index=True,
                              default=lambda self: self.env.user, tracking=True)
    partner_id = fields.Many2one(related='user_id.partner_id', store=True, string='Requester Contact')
    event_id = fields.Many2one('event.event', string='Event', tracking=True, index=True)
    department_id = fields.Many2one('zivo.event.department', string='Department')
    purpose = fields.Text()
    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Waiting Approval'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('cancel', 'Cancelled'),
        ('done', 'Returned'),
    ], default='draft', required=True, tracking=True, index=True, copy=False)
    approver_id = fields.Many2one('res.users', string='Decided By', readonly=True, copy=False)
    decision_date = fields.Datetime(readonly=True, copy=False)
    reject_reason = fields.Text(readonly=True, copy=False)
    source = fields.Selection([('backend', 'Backend'), ('portal', 'Portal / Website'), ('api', 'API')],
                              default='backend', readonly=True)
    can_approve = fields.Boolean(compute='_compute_can_approve')
    approver_ids = fields.Many2many('res.users', compute='_compute_can_approve', string='Approvers')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)
    color = fields.Integer(compute='_compute_color')
    # AI approval assistant
    zivo_ai_decision = fields.Selection(AI_DECISIONS, string='AI Recommendation', readonly=True, copy=False)
    zivo_ai_confidence = fields.Integer(string='AI Confidence (%)', readonly=True, copy=False)
    zivo_ai_reason = fields.Text(string='AI Reasoning', readonly=True, copy=False)
    zivo_ai_suggested_qty = fields.Integer(string='AI Suggested Quantity', readonly=True, copy=False)
    zivo_ai_alternatives = fields.Text(string='Alternatives', readonly=True, copy=False)
    zivo_ai_facts = fields.Text(string='Facts used by AI', readonly=True, copy=False)
    zivo_ai_date = fields.Datetime(string='Assessed On', readonly=True, copy=False)

    @api.depends('start', 'stop')
    def _compute_duration(self):
        for b in self:
            b.duration = (b.stop - b.start).total_seconds() / 3600.0 if b.start and b.stop else 0.0

    @api.depends('state')
    def _compute_color(self):
        colors = {'draft': 0, 'submitted': 3, 'approved': 10, 'rejected': 1, 'cancel': 0, 'done': 4}
        for b in self:
            b.color = colors.get(b.state, 0)

    @api.depends_context('uid')
    @api.depends('equipment_id')
    def _compute_can_approve(self):
        is_admin = self.env.user.has_group('zivoit_event_connect.group_event_connect_manager')
        for b in self:
            approvers = b.equipment_id.sudo()._zivo_get_approvers() if b.equipment_id else self.env['res.users']
            b.approver_ids = approvers
            b.can_approve = is_admin or self.env.user in approvers

    @api.onchange('equipment_id', 'start')
    def _onchange_default_stop(self):
        for b in self:
            if b.equipment_id and b.start and (not b.stop or b.stop <= b.start):
                b.stop = fields.Datetime.add(b.start, hours=b.equipment_id.slot_duration)

    @api.constrains('start', 'stop', 'quantity', 'state', 'equipment_id')
    def _check_booking(self):
        for b in self:
            if b.stop <= b.start:
                raise ValidationError(self.env._('The booking end must be after its start.'))
            if b.quantity < 1:
                raise ValidationError(self.env._('Quantity must be at least 1.'))
            if b.quantity > b.equipment_id.quantity:
                raise ValidationError(self.env._('Only %(n)s unit(s) of %(eq)s exist.',
                                                 n=b.equipment_id.quantity, eq=b.equipment_id.name))
            if b.state in ACTIVE_BOOKING_STATES:
                available = b.equipment_id._zivo_available_qty(b.start, b.stop, exclude=b)
                if b.quantity > available:
                    raise ValidationError(self.env._(
                        '%(eq)s is not available for the selected time: %(a)s unit(s) free, %(q)s requested.',
                        eq=b.equipment_id.name, a=available, q=b.quantity))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].sudo().next_by_code('zivo.equipment.booking') or 'New'
        bookings = super().create(vals_list)
        for b in bookings:
            b.message_subscribe(partner_ids=b.user_id.partner_id.ids)
        return bookings

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def _zivo_notify(self, template_xmlid):
        template = self.env.ref(template_xmlid, raise_if_not_found=False)
        if not template:
            return
        for b in self:
            b.message_post_with_source(template, subtype_xmlid='mail.mt_comment')

    def _zivo_check_requester_or_approver(self):
        for b in self:
            if b.user_id != self.env.user and not b.can_approve:
                raise AccessError(self.env._('Only the requester or an approver can do this on %s.', b.name))

    def action_submit(self):
        for b in self:
            if b.state != 'draft':
                raise UserError(self.env._('Only draft bookings can be submitted.'))
            b.equipment_id._zivo_check_window(b.start, b.stop)
        self.write({'state': 'submitted'})
        auto = self.filtered(lambda b: not b.equipment_id.requires_approval)
        if auto:
            auto.sudo()._zivo_do_approve(approver=self.env.user, auto=True)
        need = self - auto
        for b in need:
            for approver in b.approver_ids:
                b.activity_schedule(
                    'zivoit_event_connect.mail_activity_booking_approval',
                    user_id=approver.id,
                    note=self.env._('%(user)s requests %(qty)s × %(eq)s', user=b.user_id.name,
                                    qty=b.quantity, eq=b.equipment_id.name))
        need._zivo_notify('zivoit_event_connect.mail_template_booking_submitted')
        need._zivo_ai_auto_assess()
        self.env['zivo.webhook']._zivo_enqueue_raw('booking.submitted', [b._zivo_api_payload() for b in self])
        return True

    def _zivo_do_approve(self, approver, auto=False):
        self.write({'state': 'approved', 'approver_id': approver.id, 'decision_date': fields.Datetime.now()})
        self.activity_feedback(['zivoit_event_connect.mail_activity_booking_approval'],
                               feedback=self.env._('Auto-approved') if auto else self.env._('Approved'))
        self._zivo_notify('zivoit_event_connect.mail_template_booking_approved')
        self.env['zivo.webhook']._zivo_enqueue_raw('booking.approved', [b._zivo_api_payload() for b in self])

    def action_approve(self):
        for b in self:
            if b.state != 'submitted':
                raise UserError(self.env._('Only bookings waiting for approval can be approved.'))
            if not b.can_approve:
                raise AccessError(self.env._('You are not an approver for %s.', b.equipment_id.name))
        self.sudo()._zivo_do_approve(approver=self.env.user)
        return True

    def action_reject(self, reason=None):
        for b in self:
            if b.state != 'submitted':
                raise UserError(self.env._('Only bookings waiting for approval can be rejected.'))
            if not b.can_approve:
                raise AccessError(self.env._('You are not an approver for %s.', b.equipment_id.name))
        self.sudo().write({'state': 'rejected', 'approver_id': self.env.user.id,
                           'decision_date': fields.Datetime.now(), 'reject_reason': reason or False})
        self.sudo().activity_feedback(['zivoit_event_connect.mail_activity_booking_approval'],
                                      feedback=self.env._('Rejected: %s', reason or ''))
        self.sudo()._zivo_notify('zivoit_event_connect.mail_template_booking_rejected')
        self.env['zivo.webhook']._zivo_enqueue_raw('booking.rejected', [b._zivo_api_payload() for b in self])
        return True

    def action_open_reject_wizard(self):
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Reject Booking'),
            'res_model': 'zivo.booking.reject.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_booking_ids': [(6, 0, self.ids)]},
        }

    def action_cancel(self):
        self._zivo_check_requester_or_approver()
        for b in self:
            if b.state in ('done', 'cancel'):
                raise UserError(self.env._('Booking %s is already closed.', b.name))
        self.sudo().write({'state': 'cancel'})
        self.sudo().activity_unlink(['zivoit_event_connect.mail_activity_booking_approval'])
        return True

    def action_mark_returned(self):
        for b in self:
            if b.state != 'approved':
                raise UserError(self.env._('Only approved bookings can be marked as returned.'))
            if not b.can_approve:
                raise AccessError(self.env._('Only approvers can confirm returns.'))
        self.sudo().write({'state': 'done'})
        return True

    def action_reset_draft(self):
        self._zivo_check_requester_or_approver()
        self.filtered(lambda b: b.state in ('rejected', 'cancel')).sudo().write(
            {'state': 'draft', 'approver_id': False, 'decision_date': False, 'reject_reason': False})
        return True

    # ------------------------------------------------------------------
    # AI approval assistant
    # ------------------------------------------------------------------
    def _zivo_booking_facts(self):
        """Deterministic facts (no AI): availability, utilisation, requester history and alternatives."""
        self.ensure_one()
        b = self.sudo()
        eq = b.equipment_id
        tz_start = eq._zivo_utc_to_local(b.start)
        free = eq._zivo_available_qty(b.start, b.stop, exclude=b)
        # utilisation of the week (Mon-Sun, local) in unit-hours
        week_start_local = tz_start.replace(hour=0, minute=0, second=0) - timedelta(days=tz_start.weekday())
        week_start = eq._zivo_local_to_utc(week_start_local)
        week_end = week_start + timedelta(days=7)
        week_bookings = self.search([('equipment_id', '=', eq.id), ('state', 'in', ACTIVE_BOOKING_STATES),
                                     ('start', '<', week_end), ('stop', '>', week_start), ('id', '!=', b.id)])
        booked_unit_hours = sum(x.duration * x.quantity for x in week_bookings)
        open_days = sum(1 for i in range(7) if eq._zivo_is_open_day((week_start_local + timedelta(days=i)).date()))
        capacity = max(open_days * (eq.close_hour - eq.open_hour) * eq.quantity, 1)
        since = fields.Datetime.now() - timedelta(days=180)
        history = dict((state, count) for state, count in self._read_group(
            [('user_id', '=', b.user_id.id), ('create_date', '>=', since), ('id', '!=', b.id)],
            ['state'], ['__count']))
        notice_h = round((b.start - (b.create_date or fields.Datetime.now())).total_seconds() / 3600.0, 1)
        alternatives = []
        # same equipment, same day, other slots with enough units
        if free < b.quantity:
            n_slots = max(int(round(b.duration / (eq.slot_duration or 1))), 1)
            slots = [s for s in eq._zivo_get_slots(tz_start.date())]
            for i in range(len(slots) - n_slots + 1):
                window = slots[i:i + n_slots]
                if all(sl['bookable'] for sl in window) and window[0]['start'] != b.start:
                    qty = eq._zivo_available_qty(window[0]['start'], window[-1]['stop'], exclude=b)
                    if qty >= b.quantity:
                        alternatives.append('%s, %s - %s (%s unit(s) free)' % (
                            eq.name, window[0]['label'].split(' - ')[0], window[-1]['label'].split(' - ')[1], qty))
                if len(alternatives) >= 3:
                    break
        # similar equipment (same category), same time
        if eq.category_id:
            for other in self.env['zivo.equipment'].sudo().search([
                    ('category_id', '=', eq.category_id.id), ('id', '!=', eq.id), ('active', '=', True)], limit=5):
                qty = other._zivo_available_qty(b.start, b.stop)
                if qty >= 1:
                    alternatives.append('%s at the same time (%s unit(s) free)' % (other.name, qty))
        facts = {
            'equipment': eq.name,
            'category': eq.category_id.name or None,
            'units_total': eq.quantity,
            'units_requested': b.quantity,
            'units_free_for_this_interval': free,
            'start_local': tz_start.strftime('%A %Y-%m-%d %H:%M'),
            'duration_hours': round(b.duration, 2),
            'week_utilisation_percent': round(100.0 * booked_unit_hours / capacity, 1),
            'notice_hours': notice_h,
            'min_notice_hours': eq.min_notice_hours,
            'requester': b.user_id.name,
            'requester_history_180d': {
                'approved': history.get('approved', 0) + history.get('done', 0),
                'rejected': history.get('rejected', 0),
                'cancelled': history.get('cancel', 0),
            },
            'linked_event': ({'name': b.event_id.name,
                              'event_start': str(b.event_id.date_begin),
                              'booking_within_event_window': bool(
                                  b.event_id.date_begin - timedelta(days=1) <= b.start <= b.event_id.date_end)}
                             if b.event_id else None),
            'purpose': b.purpose or None,
            'competing_pending_requests': self.search_count([
                ('equipment_id', '=', eq.id), ('state', '=', 'submitted'), ('id', '!=', b.id),
                ('start', '<', b.stop), ('stop', '>', b.start)]),
            'alternatives': alternatives,
        }
        return facts

    def action_zivo_ai_assess(self):
        service = self.env['zivo.ai.service']
        service._ensure_feature('booking')
        for b in self:
            facts = b._zivo_booking_facts()
            prompt = (
                'Assess this equipment booking request for an approver. Facts (JSON):\n%s\n\n'
                'Rules: never recommend approving more units than units_free_for_this_interval; if 0 units are free '
                'recommend "reschedule" with one of the alternatives; recommend "partial" when only some units are '
                'free; flag short notice, a vague purpose or a history of cancellations. '
                'Return JSON {"decision": "approve"|"partial"|"reschedule"|"reject", "confidence": 0-100, '
                '"suggested_quantity": int, "reason": "2-4 short sentences for the approver", '
                '"alternative": str|null}.'
            ) % json.dumps(facts, default=str, indent=1)
            data = service._zivo_ai_complete('booking', 'You are a careful resource-planning assistant.',
                                             prompt, as_json=True, record=b)
            decision = data.get('decision') if data.get('decision') in dict(AI_DECISIONS) else 'approve'
            free = facts['units_free_for_this_interval']
            try:
                suggested = int(data.get('suggested_quantity') or b.quantity)
            except (TypeError, ValueError):
                suggested = b.quantity
            suggested = max(0, min(suggested, free, b.quantity))
            if decision in ('approve', 'partial') and free < b.quantity:
                decision = 'partial' if free > 0 else 'reschedule'
            alternatives = list(facts['alternatives'])
            if data.get('alternative') and data['alternative'] not in alternatives:
                alternatives.insert(0, str(data['alternative']))
            try:
                confidence = max(0, min(int(data.get('confidence') or 0), 100))
            except (TypeError, ValueError):
                confidence = 0
            b.sudo().with_context(zivo_audit_skip=True).write({
                'zivo_ai_decision': decision,
                'zivo_ai_confidence': confidence,
                'zivo_ai_reason': str(data.get('reason') or '')[:2000],
                'zivo_ai_suggested_qty': suggested,
                'zivo_ai_alternatives': '\n'.join(alternatives[:5]) or False,
                'zivo_ai_facts': json.dumps(facts, default=str, indent=1),
                'zivo_ai_date': fields.Datetime.now(),
            })
            b.sudo().message_post(body=Markup('<b>%s</b>: %s (%s%%)<br/>%s') % (
                self.env._('AI recommendation'), dict(AI_DECISIONS)[decision], confidence,
                escape(b.zivo_ai_reason or '')), subtype_xmlid='mail.mt_note')
        return True

    def _zivo_ai_auto_assess(self):
        """Called on submit: assess silently, never blocks the booking."""
        if not self.env['zivo.ai.service']._feature_enabled('booking'):
            return
        if str(self.env['ir.config_parameter'].sudo().get_param(
                'zivoit_event_connect.ai_booking_auto', 'False')).lower() not in ('1', 'true'):
            return
        for b in self:
            try:
                with self.env.cr.savepoint():
                    b.action_zivo_ai_assess()
            except Exception as exc:  # noqa: BLE001
                _logger.info('AI booking assessment skipped for %s: %s', b.name, exc)

    def action_zivo_approve_suggested(self):
        """Approve with the AI suggested quantity (partial approval)."""
        for b in self:
            if b.zivo_ai_suggested_qty and b.zivo_ai_suggested_qty < b.quantity:
                old = b.quantity
                b.sudo().write({'quantity': b.zivo_ai_suggested_qty})
                b.sudo().message_post(body=self.env._('Quantity reduced from %(o)s to %(n)s following the AI suggestion.',
                                                      o=old, n=b.zivo_ai_suggested_qty))
        return self.action_approve()

    def _zivo_api_payload(self):
        self.ensure_one()
        b = self.sudo()
        return {
            'id': b.id,
            'reference': b.name,
            'equipment': {'id': b.equipment_id.id, 'name': b.equipment_id.name, 'code': b.equipment_id.code},
            'quantity': b.quantity,
            'start': self.env['event.event']._zivo_dt(b.start),
            'stop': self.env['event.event']._zivo_dt(b.stop),
            'state': b.state,
            'requested_by': b.user_id.name,
            'event_id': b.event_id.id or None,
            'access_mode': 'open',
        }

    def _get_portal_url(self):
        self.ensure_one()
        return '/my/equipment-bookings/%s' % self.id
