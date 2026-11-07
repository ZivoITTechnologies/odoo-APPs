# -*- coding: utf-8 -*-
from odoo import api, fields, models, _

SEVERITY = [('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('critical', 'Critical')]
SEVERITY_RANK = {'low': 0, 'medium': 1, 'high': 2, 'critical': 3}

ISSUE_TYPES = [
    ('late', 'Running late'),
    ('stopped', 'Vehicle stopped'),
    ('epod', 'Delivery proof problem'),
    ('epod_missing', 'Delivery proof missing'),
    ('eway', 'E-way bill expiring'),
    ('document', 'Document expiring'),
    ('fuel', 'Fuel anomaly'),
    ('audit', 'Invoice overcharge'),
    ('no_bids', 'Auction got no bids'),
    ('vehicle', 'Vehicle not assigned'),
    ('safety', 'Unsafe driving'),
    ('offer', 'Transporter declined / no reply'),
    ('appointment', 'Delivery appointment at risk'),
    ('container', 'Container free time ending'),
    ('compliance', 'Vehicle compliance'),
    ('integration', 'Integration problem'),
    ('other', 'Other'),
]


class CpException(models.Model):
    """One inbox for everything that needs a person: detect, assign, resolve, close."""
    _name = 'cp.exception'
    _description = 'Issue'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'state_rank, severity_rank desc, detected_at desc'

    name = fields.Char(string='Issue', required=True)
    issue_type = fields.Selection(ISSUE_TYPES, required=True, default='other', string='Type')
    severity = fields.Selection(SEVERITY, required=True, default='medium', tracking=True)
    severity_rank = fields.Integer(compute='_compute_ranks', store=True)
    state = fields.Selection([('open', 'Open'), ('working', 'Working on it'), ('resolved', 'Resolved')],
                             default='open', required=True, tracking=True, group_expand='_group_expand_states')
    state_rank = fields.Integer(compute='_compute_ranks', store=True)
    detail = fields.Text()
    suggested_action = fields.Char(help='What usually fixes this kind of issue.')
    user_id = fields.Many2one('res.users', string='Owner', tracking=True,
                              default=lambda self: self.env.user)
    detected_at = fields.Datetime(default=fields.Datetime.now, readonly=True)
    resolved_at = fields.Datetime(readonly=True)
    resolution = fields.Text()
    auto_resolved = fields.Boolean(readonly=True, help='Closed by the system when the cause went away.')
    response_minutes = fields.Integer(compute='_compute_response', store=True, string='Time to Resolve (min)')
    trip_id = fields.Many2one('cp.trip', ondelete='cascade', index=True)
    vehicle_id = fields.Many2one('fleet.vehicle', ondelete='cascade')
    transporter_id = fields.Many2one('res.partner', string='Transporter')
    res_model = fields.Char(readonly=True)
    res_id = fields.Integer(readonly=True)
    record_ref = fields.Char(compute='_compute_record_ref', string='Related To')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    @api.model
    def _group_expand_states(self, states, domain):
        return ['open', 'working', 'resolved']

    @api.depends('severity', 'state')
    def _compute_ranks(self):
        for rec in self:
            rec.severity_rank = SEVERITY_RANK.get(rec.severity, 0)
            rec.state_rank = {'open': 0, 'working': 1, 'resolved': 2}.get(rec.state, 0)

    @api.depends('detected_at', 'resolved_at')
    def _compute_response(self):
        for rec in self:
            rec.response_minutes = int((rec.resolved_at - rec.detected_at).total_seconds() // 60) \
                if rec.resolved_at and rec.detected_at else 0

    def _compute_record_ref(self):
        for rec in self:
            record = rec._cp_record()
            rec.record_ref = record.display_name if record else (rec.trip_id.display_name or '')

    def _cp_record(self):
        self.ensure_one()
        if self.res_model and self.res_id and self.res_model in self.env:
            record = self.env[self.res_model].browse(self.res_id).exists()
            return record
        return False

    # ------------------------------------------------------------------
    # API used by the rest of the app
    # ------------------------------------------------------------------
    @api.model
    def cp_raise(self, issue_type, record, title, severity='medium', detail=None, suggestion=None,
                 user=None, trip=None, vehicle=None, transporter=None):
        """Open an issue unless the same one is already open for this record."""
        existing = self.sudo().search([
            ('issue_type', '=', issue_type), ('res_model', '=', record._name), ('res_id', '=', record.id),
            ('state', '!=', 'resolved')], limit=1)
        if existing:
            if SEVERITY_RANK[severity] > SEVERITY_RANK[existing.severity]:
                existing.severity = severity
            return existing
        trip = trip or (record if record._name == 'cp.trip' else self.env['cp.trip'])
        issue = self.sudo().create({
            'name': title,
            'issue_type': issue_type,
            'severity': severity,
            'detail': detail,
            'suggested_action': suggestion,
            'res_model': record._name,
            'res_id': record.id,
            'trip_id': trip.id if trip else False,
            'vehicle_id': (vehicle or (trip.vehicle_id if trip else False) or self.env['fleet.vehicle']).id,
            'transporter_id': (transporter or (trip.transporter_id if trip else False)
                               or self.env['res.partner']).id,
            'user_id': (user or (trip.indent_id.user_id if trip else False) or self.env.user).id,
            'company_id': (record.company_id.id if 'company_id' in record._fields and record.company_id
                           else self.env.company.id),
        })
        return issue

    @api.model
    def cp_auto_resolve(self, record, issue_types, note=None):
        issues = self.sudo().search([('res_model', '=', record._name), ('res_id', '=', record.id),
                                     ('issue_type', 'in', list(issue_types)), ('state', '!=', 'resolved')])
        issues.write({'state': 'resolved', 'resolved_at': fields.Datetime.now(), 'auto_resolved': True,
                      'resolution': note or _('Cleared automatically.')})
        return issues

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def action_take(self):
        self.write({'state': 'working', 'user_id': self.env.user.id})

    def action_resolve(self):
        self.write({'state': 'resolved', 'resolved_at': fields.Datetime.now()})

    def action_reopen(self):
        self.write({'state': 'open', 'resolved_at': False, 'auto_resolved': False})

    def action_open_record(self):
        self.ensure_one()
        record = self._cp_record() or self.trip_id
        if not record:
            return False
        return {'type': 'ir.actions.act_window', 'res_model': record._name, 'res_id': record.id,
                'view_mode': 'form', 'views': [(False, 'form')]}
