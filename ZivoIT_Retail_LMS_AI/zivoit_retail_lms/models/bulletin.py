# -*- coding: utf-8 -*-
"""Store bulletins: launch briefs, policy updates, promotions and VM guides with read & acknowledge."""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ZlmsBulletin(models.Model):
    _name = 'zlms.bulletin'
    _description = 'Store Bulletin'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'publish_date desc, id desc'

    name = fields.Char(string='Title', required=True, tracking=True)
    bulletin_type = fields.Selection([
        ('launch', 'Product / Collection Launch'),
        ('promo', 'Promotion & Pricing'),
        ('policy', 'Policy / SOP Update'),
        ('vm', 'Visual Merchandising / Planogram'),
        ('safety', 'Safety & Compliance'),
        ('recognition', 'Recognition & Wins'),
        ('general', 'General Announcement'),
    ], required=True, default='general', tracking=True)
    priority = fields.Selection([('normal', 'Normal'), ('urgent', 'Urgent')], default='normal')
    body = fields.Html(required=True, sanitize=True)
    attachment_ids = fields.Many2many('ir.attachment', string='Attachments')
    publish_date = fields.Date(readonly=True)
    due_date = fields.Date(string='Acknowledge By')
    require_ack = fields.Boolean(string='Requires Acknowledgement', default=True)
    store_ids = fields.Many2many('zlms.store', string='Stores', help='Empty = all stores.')
    region_ids = fields.Many2many('zlms.region', string='Regions', help='Empty = all regions.')
    role_ids = fields.Many2many('zlms.role', string='Roles', help='Empty = all roles.')
    channel_id = fields.Many2one('slide.channel', string='Linked Course / Quiz',
                                 help='Optional micro-course learners should take with this bulletin.')
    state = fields.Selection([('draft', 'Draft'), ('published', 'Published'), ('archived', 'Archived')],
                             default='draft', tracking=True)
    ack_ids = fields.One2many('zlms.bulletin.ack', 'bulletin_id', string='Recipients')
    target_count = fields.Integer(compute='_compute_ack_stats')
    read_count = fields.Integer(compute='_compute_ack_stats')
    ack_count = fields.Integer(compute='_compute_ack_stats')
    ack_rate = fields.Float(string='Acknowledged %', compute='_compute_ack_stats')

    @api.depends('ack_ids.state')
    def _compute_ack_stats(self):
        for bulletin in self:
            acks = bulletin.ack_ids
            bulletin.target_count = len(acks)
            bulletin.read_count = len(acks.filtered(lambda a: a.state in ('read', 'acknowledged')))
            bulletin.ack_count = len(acks.filtered(lambda a: a.state == 'acknowledged'))
            done = bulletin.ack_count if bulletin.require_ack else bulletin.read_count
            bulletin.ack_rate = round(100.0 * done / len(acks), 1) if acks else 0.0

    def _target_learners(self):
        self.ensure_one()
        domain = [('active', '=', True)]
        if self.store_ids:
            domain.append(('store_id', 'in', self.store_ids.ids))
        if self.region_ids:
            domain.append(('region_id', 'in', self.region_ids.ids))
        if self.role_ids:
            domain.append(('role_id', 'in', self.role_ids.ids))
        return self.env['zlms.learner'].sudo().search(domain)

    def action_publish(self):
        for bulletin in self:
            learners = bulletin._target_learners()
            if not learners:
                raise UserError(_('No learners match the audience of "%s".', bulletin.name))
            existing = bulletin.ack_ids.learner_id
            self.env['zlms.bulletin.ack'].sudo().create([
                {'bulletin_id': bulletin.id, 'learner_id': learner.id} for learner in learners - existing])
            if bulletin.channel_id:
                bulletin.channel_id.sudo()._action_add_members(learners.partner_id)
            bulletin.write({'state': 'published', 'publish_date': fields.Date.context_today(self)})
            bulletin.message_post(body=_('Published to %s learners.', len(learners)))
        return True

    def action_archive_bulletin(self):
        self.write({'state': 'archived'})

    def action_reset_draft(self):
        self.write({'state': 'draft'})

    def action_view_acks(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Recipients'), 'res_model': 'zlms.bulletin.ack',
            'view_mode': 'list', 'domain': [('bulletin_id', '=', self.id)],
            'context': {'search_default_group_store': 1},
        }

    def action_remind_pending(self):
        for bulletin in self:
            pending = bulletin.ack_ids.filtered(lambda a: a.state != 'acknowledged')
            for store in pending.store_id.filtered('manager_user_id'):
                bulletin.activity_schedule(
                    'mail.mail_activity_data_todo', user_id=store.manager_user_id.id,
                    date_deadline=bulletin.due_date or fields.Date.context_today(self),
                    summary=_('%(count)s team member(s) at %(store)s have not acknowledged "%(name)s"',
                              count=len(pending.filtered(lambda a, s=store: a.store_id == s)), store=store.name,
                              name=bulletin.name))
        return True

    @api.model
    def _cron_overdue_bulletins(self):
        today = fields.Date.context_today(self)
        self.search([('state', '=', 'published'), ('require_ack', '=', True), ('due_date', '<', today)]) \
            .filtered(lambda b: b.ack_rate < 100).action_remind_pending()


class ZlmsBulletinAck(models.Model):
    _name = 'zlms.bulletin.ack'
    _description = 'Bulletin Acknowledgement'
    _order = 'id desc'

    bulletin_id = fields.Many2one('zlms.bulletin', required=True, ondelete='cascade', index=True)
    learner_id = fields.Many2one('zlms.learner', required=True, ondelete='cascade', index=True)
    learner_user_id = fields.Many2one(related='learner_id.user_id', store=True)
    store_id = fields.Many2one(related='learner_id.store_id', store=True)
    store_manager_user_id = fields.Many2one(related='learner_id.store_id.manager_user_id', store=True)
    state = fields.Selection([('pending', 'Not Read'), ('read', 'Read'), ('acknowledged', 'Acknowledged')],
                             default='pending', required=True, index=True)
    read_date = fields.Datetime(readonly=True)
    ack_date = fields.Datetime(readonly=True)
    is_overdue = fields.Boolean(compute='_compute_is_overdue')

    _bulletin_learner_uniq = models.Constraint('unique(bulletin_id, learner_id)',
                                               'One acknowledgement per learner per bulletin.')

    def _compute_is_overdue(self):
        today = fields.Date.context_today(self)
        for ack in self:
            due = ack.bulletin_id.due_date
            ack.is_overdue = bool(due and due < today and ack.state != 'acknowledged')

    def action_mark_read(self):
        for ack in self.filtered(lambda a: a.state == 'pending'):
            ack.write({'state': 'read', 'read_date': fields.Datetime.now()})
            if not ack.bulletin_id.require_ack:
                ack.learner_id._add_points(2, reason=_('Read bulletin'))
        return True

    def action_acknowledge(self):
        for ack in self.filtered(lambda a: a.state != 'acknowledged'):
            ack.write({'state': 'acknowledged', 'ack_date': fields.Datetime.now(),
                       'read_date': ack.read_date or fields.Datetime.now()})
            ack.learner_id._add_points(5, reason=_('Acknowledged bulletin'))
        return True
