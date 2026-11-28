# -*- coding: utf-8 -*-
import base64
import binascii
import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_compare

from . import geo
from .zivo_track_device import expense_currency

_logger = logging.getLogger(__name__)

LOCKED_STATES = ('submitted', 'approved', 'posted')
# set by the system only (GPS day log, phone sync, policy checks)
SYSTEM_FIELDS = {'tracked_km', 'max_quantity', 'warning_text', 'has_warning', 'route_distance_km', 'source',
                 'client_uid', 'hr_expense_id', 'latitude', 'longitude', 'gps_accuracy', 'captured_offline',
                 'da_kind'}
# fields a salesperson may change on an automatic allowance line
AUTO_EDITABLE = {'quantity', 'name'}
MAX_AMOUNT = 10000000
MAX_RECEIPT_BYTES = 3 * 1024 * 1024
DA_KINDS = [('none', 'Not eligible'), ('half', 'Half day'), ('full', 'Full day'), ('outstation', 'Outstation')]


class ZivoExpenseLine(models.Model):
    _name = 'zivo.expense.line'
    _description = 'Field Expense Line'
    _order = 'date desc, line_type desc, id desc'

    name = fields.Char(string='Description', required=True)
    claim_id = fields.Many2one('zivo.expense.claim', string='Claim', index=True, ondelete='set null', copy=False)
    user_id = fields.Many2one('res.users', string='Salesperson', required=True, index=True,
                              default=lambda self: self.env.user, ondelete='cascade')
    company_id = fields.Many2one('res.company', required=True, index=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one('res.currency', string='Currency', required=True, readonly=True,
                                  default=lambda self: expense_currency(self.env),
                                  help='Fixed when the expense is recorded (INR / USD toggle of the settings).')
    date = fields.Date(required=True, index=True, default=fields.Date.context_today)
    session_id = fields.Many2one('zivo.track.session', string='Day Log', index=True, ondelete='set null')
    category_id = fields.Many2one('zivo.expense.category', required=True, ondelete='restrict', index=True)
    line_type = fields.Selection(related='category_id.line_type', store=True)
    vehicle_type = fields.Selection(related='session_id.vehicle_type', string='Vehicle')
    quantity = fields.Float(string='Quantity / km', default=1.0, digits=(10, 2))
    tracked_km = fields.Float(string='Tracked km', digits=(10, 2), readonly=True,
                              help='Distance measured by GPS for the day (travel allowance lines).')
    max_quantity = fields.Float(string='Paid up to (km)', digits=(10, 2), readonly=True,
                                help='Daily km cap of the policy (0 = no cap).')
    unit_amount = fields.Monetary(string='Rate / Amount')
    amount = fields.Monetary(compute='_compute_amount', store=True)
    da_kind = fields.Selection(DA_KINDS, string='Day Type')
    receipt = fields.Image(string='Bill Photo', max_width=1600, max_height=1600, attachment=True)
    receipt_attachment_ids = fields.Many2many('ir.attachment', string='Bills (PDF / files)')
    has_receipt = fields.Boolean(compute='_compute_has_receipt', store=True)
    partner_id = fields.Many2one('res.partner', string='Customer', ondelete='set null')
    latitude = fields.Float(digits=(10, 7), readonly=True)
    longitude = fields.Float(digits=(10, 7), readonly=True)
    gps_accuracy = fields.Float(string='GPS Accuracy (m)', readonly=True)
    route_distance_km = fields.Float(string='Distance from Route (km)', digits=(10, 2), readonly=True)
    captured_offline = fields.Boolean(string='Recorded Offline', readonly=True)
    source = fields.Selection([('phone', 'Phone app'), ('backend', 'Odoo'), ('auto', 'GPS day log')],
                              default='backend', required=True, readonly=True)
    client_uid = fields.Char(index=True, copy=False, readonly=True)
    warning_text = fields.Text(string='Policy Warnings', readonly=True)
    has_warning = fields.Boolean(readonly=True, index=True)
    state = fields.Selection([
        ('unclaimed', 'Not Claimed'),
        ('draft', 'Draft Claim'),
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('posted', 'Sent to Expenses'),
        ('rejected', 'Rejected'),
    ], compute='_compute_state', store=True, index=True)
    hr_expense_id = fields.Many2one('hr.expense', string='Odoo Expense', readonly=True, copy=False,
                                    ondelete='set null')

    _user_uid_unique = models.Constraint('UNIQUE(user_id, client_uid)', 'This expense was already uploaded.')

    # ------------------------------------------------------------------ computes
    @api.depends('quantity', 'unit_amount', 'max_quantity', 'line_type', 'currency_id')
    def _compute_amount(self):
        for line in self:
            qty = line.quantity
            if line.line_type == 'travel' and line.max_quantity:
                qty = min(qty, line.max_quantity)
            amount = qty * line.unit_amount
            line.amount = line.currency_id.round(amount) if line.currency_id else amount

    @api.depends('receipt', 'receipt_attachment_ids')
    def _compute_has_receipt(self):
        for line in self:
            line.has_receipt = bool(line.receipt or line.receipt_attachment_ids)

    @api.depends('claim_id', 'claim_id.state')
    def _compute_state(self):
        for line in self:
            state = line.claim_id.state
            line.state = 'unclaimed' if not state or state == 'cancel' else state

    @api.constrains('quantity', 'unit_amount')
    def _check_amounts(self):
        for line in self:
            if line.quantity < 0 or line.unit_amount < 0:
                raise ValidationError(_('Quantities and amounts cannot be negative.'))
            if line.unit_amount * max(line.quantity, 1) > MAX_AMOUNT:
                raise ValidationError(_('The amount of %s is too large.', line.name))

    @api.constrains('claim_id', 'user_id', 'company_id')
    def _check_claim_owner(self):
        for line in self.filtered('claim_id'):
            if line.claim_id.user_id != line.user_id or line.claim_id.company_id != line.company_id:
                raise ValidationError(_('An expense line can only be added to a claim of the same salesperson '
                                        'and company.'))
            if line.claim_id.currency_id != line.currency_id:
                raise ValidationError(_('"%(line)s" is in %(line_cur)s but claim %(claim)s is in %(claim_cur)s.',
                                        line=line.name, line_cur=line.currency_id.name, claim=line.claim_id.name,
                                        claim_cur=line.claim_id.currency_id.name))

    @api.onchange('category_id')
    def _onchange_category_id(self):
        if self.category_id and not self.name:
            self.name = self.category_id.name

    # ------------------------------------------------------------------ ORM
    def _is_manager(self):
        return self.env.su or self.env.context.get('zivo_internal') \
            or self.env.user.has_group('zivoit_field_tracking.group_field_manager')

    @api.model_create_multi
    def create(self, vals_list):
        is_manager = self._is_manager()
        trusted = self.env.su or self.env.context.get('zivo_internal')
        for vals in vals_list:
            if not trusted:
                forbidden = set(vals) & SYSTEM_FIELDS
                if forbidden:
                    raise AccessError(_('These fields are set by the system: %s', ', '.join(sorted(forbidden))))
                if not is_manager:
                    if vals.get('user_id', self.env.user.id) != self.env.user.id:
                        raise AccessError(_('You can only record your own expenses.'))
                    category = self.env['zivo.expense.category'].browse(vals.get('category_id'))
                    if category.line_type != 'expense':
                        raise AccessError(_('Mileage and daily allowances are calculated from the GPS day log. '
                                            'Use "Refresh from GPS" on the claim.'))
            # currency: the claim's one, else the expense currency of the company (server side only)
            if vals.get('claim_id'):
                vals['currency_id'] = self.env['zivo.expense.claim'].browse(vals['claim_id']).currency_id.id
            elif not trusted or not vals.get('currency_id'):
                company = self.env['res.company'].browse(vals.get('company_id')) if vals.get('company_id') \
                    else self.env.company
                vals['currency_id'] = expense_currency(self.env, company).id
            if not vals.get('session_id') and vals.get('user_id', self.env.user.id) and vals.get('date'):
                session = self.env['zivo.track.session'].sudo().search([
                    ('user_id', '=', vals.get('user_id', self.env.user.id)), ('date', '=', vals['date'])], limit=1)
                vals['session_id'] = session.id or False
            if vals.get('claim_id'):
                claim = self.env['zivo.expense.claim'].browse(vals['claim_id'])
                if claim.state in LOCKED_STATES:
                    raise UserError(_('You cannot add expenses to a claim that is %s.',
                                      dict(claim._fields['state']._description_selection(self.env))[claim.state]))
        lines = super().create(vals_list)
        lines._refresh_warnings()
        return lines

    def write(self, vals):
        if self.env.context.get('zivo_internal'):
            return super().write(vals)
        protected = set(vals) - {'warning_text', 'has_warning', 'route_distance_km'}
        if 'currency_id' in vals and not self.env.su:
            if any(line.currency_id.id != vals['currency_id'] for line in self):
                raise AccessError(_('The currency of a recorded expense cannot be changed.'))
            vals = dict(vals)
            del vals['currency_id']
            protected.discard('currency_id')
        if not self.env.su:
            forbidden = set(vals) & SYSTEM_FIELDS
            if forbidden:
                raise AccessError(_('These fields are set by the system: %s', ', '.join(sorted(forbidden))))
            if not self._is_manager():
                if any(line.source == 'auto' for line in self) and set(vals) - AUTO_EDITABLE:
                    raise AccessError(_('Only the claimed km can be corrected on an allowance line.'))
                if 'category_id' in vals and self.env['zivo.expense.category'].browse(
                        vals['category_id']).line_type != 'expense':
                    raise AccessError(_('Mileage and daily allowances are calculated from the GPS day log.'))
                if 'user_id' in vals and vals['user_id'] != self.env.user.id:
                    raise AccessError(_('You can only record your own expenses.'))
        if protected:
            self._check_editable()
        if 'claim_id' in vals and vals['claim_id']:
            claim = self.env['zivo.expense.claim'].browse(vals['claim_id'])
            if claim.state in LOCKED_STATES:
                raise UserError(_('You cannot move expenses into a claim that is already submitted.'))
        if 'date' in vals and 'session_id' not in vals:
            for line in self:
                session = self.env['zivo.track.session'].sudo().search([
                    ('user_id', '=', vals.get('user_id', line.user_id.id)), ('date', '=', vals['date'])], limit=1)
                super(ZivoExpenseLine, line).write({'session_id': session.id or False})
        res = super().write(vals)
        if protected & {'quantity', 'unit_amount', 'date', 'category_id', 'receipt', 'receipt_attachment_ids',
                        'session_id', 'user_id', 'claim_id'}:
            self._refresh_warnings()
        return res

    def unlink(self):
        self._check_editable()
        return super().unlink()

    def _check_editable(self):
        if self.env.su or self.env.context.get('zivo_internal'):
            return
        for line in self:
            if line.claim_id.state in LOCKED_STATES:
                raise UserError(_('"%(line)s" belongs to claim %(claim)s, which is already %(state)s. '
                                  'Reset the claim to draft first.', line=line.name, claim=line.claim_id.name,
                                  state=line.claim_id.state))

    def action_open_line(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'res_model': 'zivo.expense.line', 'res_id': self.id,
            'view_mode': 'form', 'target': 'new',
        }

    def action_open_map(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url', 'target': 'new',
            'url': 'https://www.openstreetmap.org/?mlat=%s&mlon=%s#map=17/%s/%s' % (
                self.latitude, self.longitude, self.latitude, self.longitude),
        }

    # ------------------------------------------------------------------ policy checks
    def _route_distance_km(self):
        """Shortest distance (km) between the GPS position of the expense and the tracked route of the day."""
        self.ensure_one()
        if not (self.latitude or self.longitude) or not self.session_id:
            return 0.0
        self.env.cr.execute("""SELECT latitude, longitude FROM zivo_track_point
                                WHERE session_id = %s AND low_accuracy IS NOT TRUE""", [self.session_id.id])
        rows = self.env.cr.fetchall()
        if not rows:
            return 0.0
        best = min(geo.haversine_m(self.latitude, self.longitude, r[0], r[1]) for r in rows)
        return round(best / 1000.0, 2)

    def _refresh_warnings(self):
        Line = self.sudo().with_context(zivo_internal=True)
        Policy = self.env['zivo.expense.policy']
        for line in Line.browse(self.ids).exists():
            warnings = []
            policy = Policy._for_user(line.user_id, line.company_id)
            session = line.session_id
            currency = line.currency_id
            route_km = 0.0
            if line.line_type == 'expense':
                if line.category_id._receipt_required(line.amount) and not line.has_receipt:
                    warnings.append(_('Bill photo missing'))
                if line.category_id.daily_limit:
                    day_total = sum(Line.search([
                        ('user_id', '=', line.user_id.id), ('date', '=', line.date),
                        ('category_id', '=', line.category_id.id), ('state', '!=', 'rejected'),
                    ]).mapped('amount'))
                    if float_compare(day_total, line.category_id.daily_limit, precision_rounding=currency.rounding) > 0:
                        warnings.append(_('Daily limit of %(limit)s for %(cat)s exceeded (%(total)s that day)',
                                          limit=currency.format(line.category_id.daily_limit),
                                          cat=line.category_id.name, total=currency.format(day_total)))
                duplicate = Line.search_count([
                    ('id', '!=', line.id), ('user_id', '=', line.user_id.id), ('date', '=', line.date),
                    ('category_id', '=', line.category_id.id), ('amount', '=', line.amount),
                    ('state', '!=', 'rejected'),
                ])
                if duplicate:
                    warnings.append(_('Possible duplicate (same category, amount and day)'))
                if line.category_id.conflicts_with_mileage and Line.search_count([
                        ('user_id', '=', line.user_id.id), ('date', '=', line.date),
                        ('line_type', '=', 'travel'), ('amount', '>', 0), ('state', '!=', 'rejected')]):
                    warnings.append(_('%s claimed on a day that is also paid a mileage allowance',
                                      line.category_id.name))
                if policy and policy.location_check_km and (line.latitude or line.longitude):
                    route_km = line._route_distance_km()
                    if route_km > policy.location_check_km:
                        warnings.append(_('Recorded %s km away from the tracked route', route_km))
            if line.line_type == 'travel':
                tolerance = (policy.km_tolerance_pct if policy else 10.0) / 100.0
                if line.quantity > line.tracked_km * (1 + tolerance) + 0.5:
                    warnings.append(_('Claimed %(claimed)s km but GPS tracked %(tracked)s km',
                                      claimed=round(line.quantity, 1), tracked=round(line.tracked_km, 1)))
                if line.max_quantity and line.quantity > line.max_quantity:
                    warnings.append(_('Above the daily cap: paid for %s km only', round(line.max_quantity, 1)))
                if session and session.mock_point_count:
                    warnings.append(_('Fake GPS app detected on this day (%s points)', session.mock_point_count))
            if not session or not session.point_count:
                warnings.append(_('No GPS tracking on this day'))
            line.write({
                'warning_text': '\n'.join(warnings) or False,
                'has_warning': bool(warnings),
                'route_distance_km': route_km,
            })
        return True

    # ------------------------------------------------------------------ phone sync
    @api.model
    def _ingest(self, device, items):
        """Expenses captured on the phone (possibly offline, with a photo of the bill)."""
        result = {'accepted': [], 'duplicates': [], 'rejected': []}
        device = device.sudo()
        user = device.user_id
        Category = self.env['zivo.expense.category'].sudo()
        Line = self.sudo()
        Session = self.env['zivo.track.session'].sudo()
        now = fields.Datetime.now()
        for item in items[:20]:
            uid = str(item.get('uid') or '')[:64]
            if not uid:
                result['rejected'].append({'uid': uid, 'reason': 'missing_uid'})
                continue
            if Line.search_count([('user_id', '=', user.id), ('client_uid', '=', uid)]):
                result['duplicates'].append(uid)
                continue
            try:
                category = Category.browse(int(item.get('category_id') or 0)).exists()
            except (TypeError, ValueError):
                category = Category.browse()
            if not category or not category.active or category.line_type != 'expense' \
                    or (category.company_id and category.company_id != user.company_id):
                result['rejected'].append({'uid': uid, 'reason': 'unknown_category'})
                continue
            amount = geo.to_float(item.get('amount'))
            if amount is None or amount <= 0 or amount > MAX_AMOUNT:
                result['rejected'].append({'uid': uid, 'reason': 'invalid_amount'})
                continue
            when = geo.parse_timestamp(item.get('ts')) or now
            day = Session._local_date(user, when)
            receipt = False
            raw = item.get('receipt')
            if raw:
                if not isinstance(raw, str):
                    result['rejected'].append({'uid': uid, 'reason': 'invalid_receipt'})
                    continue
                if raw.startswith('data:'):
                    raw = raw.split(',', 1)[-1]
                try:
                    data = base64.b64decode(raw, validate=True)
                except (binascii.Error, ValueError):
                    result['rejected'].append({'uid': uid, 'reason': 'invalid_receipt'})
                    continue
                if len(data) > MAX_RECEIPT_BYTES:
                    result['rejected'].append({'uid': uid, 'reason': 'receipt_too_large'})
                    continue
                receipt = base64.b64encode(data)
            partner = self.env['res.partner'].with_user(user).browse()
            try:
                if item.get('partner_id'):
                    partner = self.env['res.partner'].with_user(user).browse(int(item['partner_id'])).exists()
                    partner.check_access('read')
            except Exception:  # noqa: BLE001 - access error / bad id: keep the expense, drop the customer
                partner = self.env['res.partner'].browse()
            draft_claim = self.env['zivo.expense.claim'].sudo().search([
                ('user_id', '=', user.id), ('state', '=', 'draft'), ('company_id', '=', user.company_id.id),
                ('currency_id', '=', expense_currency(self.env, user.company_id).id),
                ('date_from', '<=', day), ('date_to', '>=', day)], limit=1)
            vals = {
                'name': (str(item.get('note') or '').strip()[:200]) or category.name,
                'user_id': user.id,
                'company_id': user.company_id.id,
                'date': day,
                'category_id': category.id,
                'quantity': 1.0,
                'unit_amount': amount,
                'partner_id': partner.id or False,
                'latitude': geo.to_float(item.get('lat'), 0.0),
                'longitude': geo.to_float(item.get('lon'), 0.0),
                'gps_accuracy': geo.to_float(item.get('accuracy'), 0.0),
                'captured_offline': geo.to_bool(item.get('offline', False)),
                'source': 'phone',
                'client_uid': uid,
                'claim_id': draft_claim.id or False,
            }
            try:
                with self.env.cr.savepoint():
                    if receipt:
                        vals['receipt'] = receipt
                    Line.create(vals)
            except (UserError, ValidationError) as err:
                _logger.info('ZivoIT field expense %s rejected: %s', uid, err)
                result['rejected'].append({'uid': uid, 'reason': 'invalid_receipt' if receipt else 'invalid'})
                continue
            result['accepted'].append(uid)
        return result
