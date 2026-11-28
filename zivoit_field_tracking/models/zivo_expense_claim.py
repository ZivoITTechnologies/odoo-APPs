# -*- coding: utf-8 -*-
from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .zivo_track_device import expense_currency, get_param

WORKFLOW_FIELDS = {'state', 'submitted_date', 'approved_date', 'approved_by_id', 'reject_reason', 'policy_id'}
DA_LABELS = {'half': 'Half day', 'full': 'Full day', 'outstation': 'Outstation'}


class ZivoExpenseClaim(models.Model):
    """A period expense claim of one salesperson: GPS mileage + daily allowance + bills."""
    _name = 'zivo.expense.claim'
    _description = 'Field Expense Claim'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date_from desc, id desc'

    name = fields.Char(string='Reference', required=True, readonly=True, copy=False, default='/')
    user_id = fields.Many2one('res.users', string='Salesperson', required=True, index=True, tracking=True,
                              default=lambda self: self.env.user)
    employee_id = fields.Many2one('hr.employee', string='Employee', compute='_compute_employee_id', store=True,
                                  readonly=False, check_company=True)
    company_id = fields.Many2one('res.company', required=True, index=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one('res.currency', string='Currency', required=True, readonly=True, tracking=True,
                                  default=lambda self: expense_currency(self.env),
                                  help='INR or USD, from the settings when the claim is created. Sent to Odoo '
                                       'Expenses in this currency and converted at the exchange rate of the day.')
    date_from = fields.Date(string='From', required=True, tracking=True,
                            default=lambda self: fields.Date.context_today(self).replace(day=1))
    date_to = fields.Date(string='To', required=True, tracking=True,
                          default=lambda self: fields.Date.context_today(self))
    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('posted', 'Sent to Expenses'),
        ('rejected', 'Rejected'),
        ('cancel', 'Cancelled'),
    ], default='draft', required=True, tracking=True, copy=False, index=True)
    policy_id = fields.Many2one('zivo.expense.policy', string='Policy', readonly=True, copy=False)
    approver_id = fields.Many2one('res.users', string='Approver', tracking=True,
                                  compute='_compute_approver_id', store=True, readonly=False,
                                  help='Expense approver of the employee, otherwise the manager of the employee. '
                                       'Field Tracking managers can always approve.')
    line_ids = fields.One2many('zivo.expense.line', 'claim_id', string='Lines')
    session_ids = fields.One2many('zivo.track.session', 'claim_id', string='Day Logs')
    hr_expense_ids = fields.One2many('hr.expense', 'zivo_claim_id', string='Odoo Expenses')

    day_count = fields.Integer(string='Field Days', compute='_compute_totals', store=True)
    tracked_km = fields.Float(string='GPS km', digits=(10, 1), compute='_compute_totals', store=True)
    claimed_km = fields.Float(string='Claimed km', digits=(10, 1), compute='_compute_totals', store=True)
    travel_amount = fields.Monetary(string='Mileage (TA)', compute='_compute_totals', store=True)
    daily_amount = fields.Monetary(string='Daily Allowance (DA)', compute='_compute_totals', store=True)
    expense_amount = fields.Monetary(string='Bills', compute='_compute_totals', store=True)
    total_amount = fields.Monetary(string='Total', compute='_compute_totals', store=True, tracking=True)
    warning_count = fields.Integer(string='Warnings', compute='_compute_totals', store=True)
    receipt_count = fields.Integer(string='Bills Attached', compute='_compute_totals', store=True)
    payment_state = fields.Selection([
        ('none', 'Not Sent'),
        ('to_pay', 'To Pay'),
        ('in_payment', 'In Payment'),
        ('paid', 'Paid'),
    ], string='Reimbursement', compute='_compute_payment_state', store=True, tracking=True)
    submitted_date = fields.Datetime(readonly=True, copy=False)
    approved_date = fields.Datetime(readonly=True, copy=False)
    approved_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    reject_reason = fields.Text(readonly=True, copy=False)
    note = fields.Text(string='Remarks')
    can_approve = fields.Boolean(compute='_compute_can_approve')

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for claim in self:
            if claim.date_from > claim.date_to:
                raise ValidationError(_('The start date must be before the end date.'))
            if (claim.date_to - claim.date_from).days > 92:
                raise ValidationError(_('A claim can cover at most 3 months.'))

    # ------------------------------------------------------------------ computes
    @api.depends('user_id', 'company_id')
    def _compute_employee_id(self):
        for claim in self:
            claim.employee_id = claim.user_id.sudo().with_company(claim.company_id).employee_id

    @api.depends('employee_id')
    def _compute_approver_id(self):
        for claim in self:
            employee = claim.employee_id.sudo()
            claim.approver_id = employee.expense_manager_id or employee.parent_id.user_id or False

    @api.depends('line_ids.amount', 'line_ids.quantity', 'line_ids.tracked_km', 'line_ids.line_type',
                 'line_ids.has_warning', 'line_ids.has_receipt', 'session_ids')
    def _compute_totals(self):
        for claim in self:
            lines = claim.line_ids
            travel = lines.filtered(lambda l: l.line_type == 'travel')
            daily = lines.filtered(lambda l: l.line_type == 'daily')
            bills = lines.filtered(lambda l: l.line_type == 'expense')
            claim.day_count = len(claim.session_ids)
            claim.tracked_km = sum(travel.mapped('tracked_km'))
            claim.claimed_km = sum(travel.mapped('quantity'))
            claim.travel_amount = sum(travel.mapped('amount'))
            claim.daily_amount = sum(daily.mapped('amount'))
            claim.expense_amount = sum(bills.mapped('amount'))
            claim.total_amount = sum(lines.mapped('amount'))
            claim.warning_count = len(lines.filtered('has_warning'))
            claim.receipt_count = len(bills.filtered('has_receipt'))

    @api.depends('hr_expense_ids.state')
    def _compute_payment_state(self):
        for claim in self:
            states = set(claim.hr_expense_ids.mapped('state'))
            if not states:
                claim.payment_state = 'none'
            elif states <= {'paid'}:
                claim.payment_state = 'paid'
            elif states & {'in_payment', 'paid'}:
                claim.payment_state = 'in_payment'
            else:
                claim.payment_state = 'to_pay'

    def _compute_can_approve(self):
        for claim in self:
            claim.can_approve = claim._user_can_approve(self.env.user)

    def _user_can_approve(self, user):
        self.ensure_one()
        if user == self.user_id and not user.has_group('base.group_system'):
            return False
        return user == self.approver_id or user.has_group('zivoit_field_tracking.group_field_manager')

    # ------------------------------------------------------------------ ORM
    @api.model_create_multi
    def create(self, vals_list):
        is_manager = self.env.su or self.env.user.has_group('zivoit_field_tracking.group_field_manager')
        for vals in vals_list:
            if not self.env.su:
                if set(vals) & WORKFLOW_FIELDS - {'state'} or vals.get('state', 'draft') != 'draft':
                    raise AccessError(_('New claims always start as draft.'))
                if not is_manager and (vals.get('user_id', self.env.user.id) != self.env.user.id
                                       or 'approver_id' in vals):
                    raise AccessError(_('You can only create your own claims.'))
            company = self.env['res.company'].browse(vals.get('company_id') or self.env.company.id)
            if not self.env.su or not vals.get('currency_id'):
                vals['currency_id'] = expense_currency(self.env, company).id
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].with_company(
                    vals.get('company_id') or self.env.company.id).next_by_code('zivo.expense.claim') or '/'
        return super().create(vals_list)

    def write(self, vals):
        if 'currency_id' in vals and not self.env.su:
            if any(claim.currency_id.id != vals['currency_id'] for claim in self):
                raise AccessError(_('The currency of a claim is fixed when it is created.'))
            vals = dict(vals)
            del vals['currency_id']
        if not self.env.su:
            workflow = set(vals) & WORKFLOW_FIELDS
            if workflow:
                raise AccessError(_('Use the claim buttons to change: %s.', ', '.join(sorted(workflow))))
            if 'approver_id' in vals and not self.env.user.has_group('zivoit_field_tracking.group_field_manager'):
                raise AccessError(_('Only a Field Tracking manager can change the approver.'))
            if set(vals) & {'user_id', 'date_from', 'date_to', 'company_id', 'employee_id'}:
                if any(claim.state != 'draft' for claim in self):
                    raise UserError(_('Only draft claims can change salesperson or period.'))
                if 'user_id' in vals and vals['user_id'] != self.env.user.id \
                        and not self.env.user.has_group('zivoit_field_tracking.group_field_manager'):
                    raise AccessError(_('You can only manage your own claims.'))
        return super().write(vals)

    def unlink(self):
        for claim in self:
            if claim.state not in ('draft', 'cancel'):
                raise UserError(_('Only draft or cancelled claims can be deleted. Cancel %s first.', claim.name))
        self._release()
        return super().unlink()

    # ------------------------------------------------------------------ building
    @api.model
    def _create_for(self, user, date_from, date_to, company=None):
        """Create a draft claim for ``user`` and fill it from the GPS day logs and unclaimed bills."""
        company = company or user.company_id
        claim = self.create({
            'user_id': user.id, 'company_id': company.id, 'date_from': date_from, 'date_to': date_to,
        })
        claim._fill_from_tracking()
        return claim

    def _release(self):
        """Detach day logs and bills, drop the generated allowance lines."""
        Line = self.env['zivo.expense.line'].sudo().with_context(zivo_internal=True)
        for claim in self:
            lines = Line.browse(claim.line_ids.ids)
            lines.filtered(lambda l: l.source == 'auto').unlink()
            lines.exists().write({'claim_id': False})
            claim.session_ids.sudo().write({'claim_id': False})

    def _fill_from_tracking(self):
        Session = self.env['zivo.track.session'].sudo()
        Line = self.env['zivo.expense.line'].sudo().with_context(zivo_internal=True)
        Category = self.env['zivo.expense.category'].sudo()
        ta_cat = Category._system_category('travel')
        da_cat = Category._system_category('daily')
        if not ta_cat or not da_cat:
            raise UserError(_('The travel and daily allowance categories are missing. '
                              'Reinstall or update the Field Tracking module.'))
        for claim in self:
            if claim.state != 'draft':
                raise UserError(_('Only draft claims can be refreshed.'))
            current = expense_currency(self.env, claim.company_id)
            if claim.currency_id != current:
                raise UserError(_('Claim %(claim)s is in %(old)s but field expenses now use %(new)s. Cancel it and '
                                  'create a new claim, or switch the currency back in the settings.',
                                  claim=claim.name, old=claim.currency_id.name, new=current.name))
            policy = self.env['zivo.expense.policy']._for_user(claim.user_id, claim.company_id)
            claim.sudo().policy_id = policy
            # own sessions of the period that are finished and not in another claim
            sessions = Session.search([
                ('user_id', '=', claim.user_id.id), ('company_id', '=', claim.company_id.id),
                ('date', '>=', claim.date_from), ('date', '<=', claim.date_to),
                ('state', '=', 'closed'), '|', ('claim_id', '=', False), ('claim_id', '=', claim.id),
            ], order='date')
            # drop previous automatic lines (refresh) but keep manual corrections of claimed km
            previous = {(l.session_id.id, l.line_type): l.quantity
                        for l in claim.line_ids.filtered(lambda l: l.source == 'auto')}
            Line.browse(claim.line_ids.filtered(lambda l: l.source == 'auto').ids).unlink()
            claim.session_ids.filtered(lambda s: s not in sessions).sudo().write({'claim_id': False})
            sessions.write({'claim_id': claim.id})
            vals_list = []
            for session in sessions:
                session._compute_allowances()
                if session.ta_rate and session.distance_km > 0:
                    rate = policy._rate_for(session.vehicle_type)
                    vals_list.append({
                        'name': _('Mileage %(km)s km (%(vehicle)s)', km=round(session.distance_km, 1),
                                  vehicle=dict(session._fields['vehicle_type']._description_selection(self.env)).get(
                                      session.vehicle_type, '')),
                        'claim_id': claim.id, 'user_id': claim.user_id.id, 'company_id': claim.company_id.id,
                        'date': session.date, 'session_id': session.id, 'category_id': ta_cat.id,
                        'tracked_km': session.distance_km,
                        'quantity': previous.get((session.id, 'travel'), round(session.distance_km, 2)),
                        'max_quantity': rate.max_km_per_day if rate else 0.0,
                        'unit_amount': session.ta_rate, 'source': 'auto',
                    })
                if session.da_amount:
                    vals_list.append({
                        'name': _('Daily allowance - %(kind)s (%(hours)s h, %(visits)s visits)',
                                  kind=_(DA_LABELS.get(session.da_kind, '')),
                                  hours=round(session.duration_hours, 1), visits=len(session.visit_ids)),
                        'claim_id': claim.id, 'user_id': claim.user_id.id, 'company_id': claim.company_id.id,
                        'date': session.date, 'session_id': session.id, 'category_id': da_cat.id,
                        'quantity': 1.0, 'unit_amount': session.da_amount, 'da_kind': session.da_kind,
                        'source': 'auto',
                    })
            if vals_list:
                Line.create(vals_list)
            # bills of the period not claimed yet
            bills = Line.search([
                ('user_id', '=', claim.user_id.id), ('company_id', '=', claim.company_id.id),
                ('claim_id', '=', False), ('line_type', '=', 'expense'),
                ('currency_id', '=', claim.currency_id.id),
                ('date', '>=', claim.date_from), ('date', '<=', claim.date_to),
            ])
            bills.write({'claim_id': claim.id})
            claim.line_ids._refresh_warnings()
        return True

    # ------------------------------------------------------------------ workflow
    def action_refresh(self):
        self._fill_from_tracking()
        return True

    def action_submit(self):
        for claim in self:
            if claim.state not in ('draft', 'rejected'):
                raise UserError(_('Only draft claims can be submitted.'))
            if not self.env.su and claim.user_id != self.env.user \
                    and not self.env.user.has_group('zivoit_field_tracking.group_field_manager'):
                raise AccessError(_('You can only submit your own claims.'))
            if not claim.line_ids:
                raise UserError(_('Claim %s has no lines. Use "Refresh from GPS" or add bills.', claim.name))
            if claim.total_amount <= 0:
                raise UserError(_('Claim %s has nothing to reimburse.', claim.name))
            claim.line_ids._refresh_warnings()
            claim.sudo().write({'state': 'submitted', 'submitted_date': fields.Datetime.now(),
                                'reject_reason': False})
            if claim.approver_id:
                claim.activity_schedule(
                    'mail.mail_activity_data_todo', user_id=claim.approver_id.id,
                    summary=_('Approve field expense claim %s', claim.name),
                    note=_('%(user)s claims %(amount)s for %(from)s to %(to)s (%(warn)s warnings).',
                           user=claim.user_id.name, amount=claim.currency_id.format(claim.total_amount),
                           **{'from': claim.date_from, 'to': claim.date_to, 'warn': claim.warning_count}))
            claim.message_post(body=_('Claim submitted for approval: %s', claim.currency_id.format(claim.total_amount)))
        return True

    def _check_approver(self):
        for claim in self:
            if self.env.su:
                continue
            if self.env.user == claim.user_id and not self.env.user.has_group('base.group_system'):
                raise AccessError(_('You cannot approve or reject your own expense claim.'))
            if not claim._user_can_approve(self.env.user):
                raise AccessError(_('Only %s or a Field Tracking manager can approve this claim.',
                                    claim.approver_id.name or _('the approver')))

    def action_approve(self):
        self._check_approver()
        for claim in self:
            if claim.state != 'submitted':
                raise UserError(_('Only submitted claims can be approved.'))
            claim.sudo().write({'state': 'approved', 'approved_date': fields.Datetime.now(),
                                'approved_by_id': self.env.user.id})
            claim.activity_feedback(['mail.mail_activity_data_todo'])
            claim.message_post(body=_('Claim approved by %s.', self.env.user.name))
        if get_param(self.env, 'expense_auto_post'):
            self.filtered(lambda c: c.employee_id).action_post_expenses()
        return True

    def action_reject(self):
        self._check_approver()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Reject Claim'),
            'res_model': 'zivo.expense.reject.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_claim_ids': self.ids},
        }

    def _do_reject(self, reason):
        self._check_approver()
        for claim in self:
            if claim.state not in ('submitted', 'approved'):
                raise UserError(_('Only submitted or approved claims can be rejected.'))
            claim.sudo().write({'state': 'rejected', 'reject_reason': reason})
            claim.activity_unlink(['mail.mail_activity_data_todo'])
            claim.message_post(body=_('Claim rejected by %(user)s: %(reason)s', user=self.env.user.name,
                                      reason=reason), partner_ids=claim.user_id.partner_id.ids)
        return True

    def action_reset_draft(self):
        for claim in self:
            if claim.state not in ('submitted', 'rejected', 'cancel', 'approved'):
                raise UserError(_('This claim cannot be reset to draft.'))
            if claim.hr_expense_ids:
                raise UserError(_('Claim %s was already sent to Odoo Expenses.', claim.name))
            if not self.env.su and self.env.user != claim.user_id \
                    and not self.env.user.has_group('zivoit_field_tracking.group_field_manager'):
                raise AccessError(_('Only the salesperson or a manager can reset the claim.'))
            if claim.state == 'cancel':
                claim.sudo().write({'state': 'draft'})
                claim._fill_from_tracking()
            else:
                claim.sudo().write({'state': 'draft', 'approved_date': False, 'approved_by_id': False})
            claim.activity_unlink(['mail.mail_activity_data_todo'])
        return True

    def action_cancel(self):
        for claim in self:
            if claim.hr_expense_ids:
                raise UserError(_('Claim %s was already sent to Odoo Expenses.', claim.name))
            if claim.state not in ('draft', 'rejected', 'submitted'):
                if not self.env.user.has_group('zivoit_field_tracking.group_field_manager'):
                    raise AccessError(_('Only a manager can cancel an approved claim.'))
        self._release()
        self.sudo().write({'state': 'cancel'})
        self.activity_unlink(['mail.mail_activity_data_todo'])
        return True

    # ------------------------------------------------------------------ Odoo Expenses
    def action_post_expenses(self):
        """Create approved hr.expense records (one per line) ready for accounting and reimbursement."""
        Expense = self.env['hr.expense'].sudo()
        Attachment = self.env['ir.attachment'].sudo()
        for claim in self:
            if claim.state != 'approved':
                raise UserError(_('Only approved claims can be sent to Odoo Expenses.'))
            if not self.env.su and not claim._user_can_approve(self.env.user):
                raise AccessError(_('Only the approver or a manager can send the claim to Odoo Expenses.'))
            employee = claim.employee_id.sudo()
            if not employee:
                raise UserError(_('%(user)s has no employee record in %(company)s. Create the employee (HR app) '
                                  'and link the user, then send the claim again.',
                                  user=claim.user_id.name, company=claim.company_id.name))
            approver = claim.approved_by_id or self.env.user
            company_currency = claim.company_id.currency_id
            if claim.currency_id != company_currency and not self.env['res.currency.rate'].sudo().search_count([
                    ('currency_id', '=', claim.currency_id.id),
                    ('company_id', 'in', [claim.company_id.id, False])]):
                raise UserError(_('Claim %(claim)s is in %(cur)s and the company currency is %(company_cur)s. '
                                  'Set an exchange rate for %(cur)s (Accounting > Configuration > Currencies) '
                                  'so Odoo Expenses can convert it.', claim=claim.name, cur=claim.currency_id.name,
                                  company_cur=company_currency.name))
            if not claim.currency_id.active:
                claim.currency_id.sudo().active = True
            for line in claim.line_ids.sudo().filtered(lambda l: l.amount > 0 and not l.hr_expense_id):
                product = line.category_id.product_id
                if not product:
                    raise UserError(_('Set an expense product on the category "%s".', line.category_id.name))
                description = [line.name]
                if line.line_type == 'travel':
                    description.append(_('GPS tracked %(t)s km, claimed %(c)s km, rate %(r)s',
                                         t=round(line.tracked_km, 2), c=round(line.quantity, 2),
                                         r=claim.currency_id.format(line.unit_amount)))
                if line.warning_text:
                    description.append(_('Warnings: %s', line.warning_text.replace('\n', '; ')))
                expense = Expense.with_context(default_employee_id=employee.id).with_company(claim.company_id).create({
                    'name': '%s - %s' % (line.category_id.name, line.date.strftime('%d %b %Y')),
                    'employee_id': employee.id,
                    'company_id': claim.company_id.id,
                    'product_id': product.id,
                    'date': line.date,
                    'quantity': 1,
                    'total_amount_currency': line.amount,
                    'currency_id': claim.currency_id.id,
                    'payment_mode': 'own_account',
                    'description': '\n'.join(description),
                    'zivo_claim_id': claim.id,
                    'zivo_line_id': line.id,
                })
                if expense.total_amount_currency != line.amount:
                    expense.write({'total_amount_currency': line.amount})
                if line.receipt:
                    Attachment.create({
                        'name': 'bill_%s.jpg' % line.id, 'datas': line.receipt, 'mimetype': 'image/jpeg',
                        'res_model': 'hr.expense', 'res_id': expense.id,
                    })
                for att in line.receipt_attachment_ids:
                    att.sudo().copy({'res_model': 'hr.expense', 'res_id': expense.id})
                expense.write({'approval_state': 'approved', 'approval_date': fields.Datetime.now(),
                               'manager_id': approver.id})
                line.with_context(zivo_internal=True).write({'hr_expense_id': expense.id})
            claim.sudo().write({'state': 'posted'})
            claim.message_post(body=_('%s expenses created in Odoo Expenses, ready for accounting.',
                                      len(claim.hr_expense_ids)))
        return True

    # ------------------------------------------------------------------ navigation & reports
    def action_view_expenses(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Odoo Expenses'), 'res_model': 'hr.expense',
            'view_mode': 'list,form', 'domain': [('zivo_claim_id', '=', self.id)],
        }

    def action_view_sessions(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Day Logs'), 'res_model': 'zivo.track.session',
            'view_mode': 'list,form', 'domain': [('claim_id', '=', self.id)],
        }

    def action_view_warnings(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Lines with Warnings'), 'res_model': 'zivo.expense.line',
            'view_mode': 'list,form', 'domain': [('claim_id', '=', self.id), ('has_warning', '=', True)],
        }

    def _report_days(self):
        """Lines grouped per day for the PDF."""
        self.ensure_one()
        days = {}
        for line in self.line_ids.sorted(lambda l: (l.date, {'travel': 0, 'daily': 1}.get(l.line_type, 2), l.id)):
            days.setdefault(line.date, []).append(line)
        return sorted(days.items())

    # ------------------------------------------------------------------ cron
    @api.model
    def _cron_create_monthly_claims(self):
        """On the 1st of the month, prepare draft claims of the previous month for every tracked user."""
        if not get_param(self.env, 'expense_auto_claim'):
            return True
        today = fields.Date.context_today(self)
        if today.day != 1 and not self.env.context.get('zivo_force_monthly'):
            return True
        date_to = today.replace(day=1) - timedelta(days=1)
        date_from = date_to.replace(day=1)
        sessions = self.env['zivo.track.session'].sudo().search([
            ('date', '>=', date_from), ('date', '<=', date_to), ('state', '=', 'closed'), ('claim_id', '=', False),
        ])
        for (user, company) in {(s.user_id, s.company_id) for s in sessions}:
            if not user.active:
                continue
            existing = self.sudo().search([('user_id', '=', user.id), ('company_id', '=', company.id),
                                           ('date_from', '=', date_from), ('date_to', '=', date_to),
                                           ('state', '!=', 'cancel')], limit=1)
            if not existing:
                self.sudo().with_company(company)._create_for(user, date_from, date_to, company)
        return True

    @api.model
    def _default_period(self, ref=None):
        ref = ref or fields.Date.context_today(self)
        return ref.replace(day=1), (ref.replace(day=1) + relativedelta(months=1)) - timedelta(days=1)
