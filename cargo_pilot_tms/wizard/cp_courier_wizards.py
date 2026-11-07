# -*- coding: utf-8 -*-
import json
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..models.cp_parcel import PARCEL_STATES


class CpCourierRateWizard(models.TransientModel):
    _name = 'cp.courier.rate.wizard'
    _description = 'Compare Courier Rates'

    parcel_id = fields.Many2one('cp.parcel', required=True)
    currency_id = fields.Many2one(related='parcel_id.currency_id')
    line_ids = fields.One2many('cp.courier.rate.line', 'wizard_id')
    sort_by = fields.Selection([('best', 'Best value'), ('price', 'Cheapest'), ('days', 'Fastest')], default='best')
    summary = fields.Char(compute='_compute_summary')
    error_count = fields.Integer(compute='_compute_summary')

    @api.depends('line_ids', 'parcel_id')
    def _compute_summary(self):
        for wiz in self:
            p = wiz.parcel_id
            wiz.summary = _('%(origin)s → %(dest)s · %(pieces)s box(es) · %(weight)s kg charged · %(mode)s',
                            origin=p.origin_pin or '?', dest=p.dest_pin or '?', pieces=p.piece_count,
                            weight=p.chargeable_weight_kg,
                            mode=dict(p._fields['payment_mode'].selection).get(p.payment_mode))
            wiz.error_count = len(wiz.line_ids.filtered('error'))

    def action_fetch(self):
        self.ensure_one()
        parcel = self.parcel_id
        self.line_ids.unlink()
        accounts = self.env['cp.courier.account'].search([('company_id', '=', parcel.company_id.id)])
        if parcel.payment_mode == 'cod':
            accounts = accounts.filtered('supports_cod')
        if not accounts:
            raise UserError(_('Add a courier account first (Setup > Courier Accounts).'))
        lines = []
        for account in accounts:
            try:
                options = account._cp_provider().rates(parcel)
            except UserError as err:
                options = [{'courier_name': account.courier_display_name or account.name, 'price': 0.0,
                            'error': str(err.args[0] if err.args else err)}]
            for opt in options:
                if parcel.payment_mode == 'cod' and not opt.get('error') and opt.get('cod') is False:
                    continue
                lines.append({
                    'wizard_id': self.id, 'account_id': account.id,
                    'courier_name': opt.get('courier_name') or account.name, 'service': opt.get('service') or '',
                    'price': opt.get('price') or 0.0, 'days': opt.get('days') or 0,
                    'days_estimated': bool(opt.get('days_estimated')), 'cod': bool(opt.get('cod')),
                    'note': opt.get('note') or '', 'error': opt.get('error') or '',
                    'price_from_card': bool(opt.get('price_from_card')), 'rating': opt.get('rating') or 0.0,
                    'option_json': json.dumps({k: v for k, v in opt.items() if k in ('ref', 'service', 'price',
                                                                                     'days', 'courier_name')}),
                })
        self.env['cp.courier.rate.line'].create(lines)
        self._cp_rank()
        return True

    def _cp_rank(self):
        good = self.line_ids.filtered(lambda l: not l.error and l.price > 0)
        if not good:
            return
        cheapest = min(good.mapped('price'))
        with_days = good.filtered('days')
        fastest = min(with_days.mapped('days')) if with_days else 0
        max_price = max(good.mapped('price')) or 1.0
        max_days = max(with_days.mapped('days')) if with_days else 1
        for line in good:
            price_score = 1 - (line.price - cheapest) / max(max_price - cheapest, 1.0)
            day_score = (1 - (line.days - fastest) / max(max_days - fastest, 1)) if line.days else 0.4
            rating = (line.rating / 5.0) if line.rating else 0.6
            line.write({'score': round((price_score * 0.55 + day_score * 0.3 + rating * 0.15) * 100),
                        'is_cheapest': line.price == cheapest, 'is_fastest': bool(fastest and line.days == fastest)})
        best = max(good, key=lambda l: l.score)
        best.recommended = True

    def action_refresh(self):
        self.action_fetch()
        return self._reopen()

    def _reopen(self):
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new', 'name': _('Choose a courier')}


class CpCourierRateLine(models.TransientModel):
    _name = 'cp.courier.rate.line'
    _description = 'Courier Rate Option'
    _order = 'error, recommended desc, score desc, price'

    wizard_id = fields.Many2one('cp.courier.rate.wizard', required=True, ondelete='cascade')
    currency_id = fields.Many2one(related='wizard_id.currency_id')
    account_id = fields.Many2one('cp.courier.account', string='Account')
    initials = fields.Char(related='account_id.initials')
    courier_name = fields.Char(string='Courier')
    service = fields.Char()
    price = fields.Monetary()
    days = fields.Integer(string='Days')
    days_estimated = fields.Boolean()
    cod = fields.Boolean(string='COD')
    rating = fields.Float()
    score = fields.Integer(string='Score')
    recommended = fields.Boolean(string='Best')
    is_cheapest = fields.Boolean(string='Cheapest')
    is_fastest = fields.Boolean(string='Fastest')
    tags = fields.Char(compute='_compute_tags', string='Why')
    price_from_card = fields.Boolean()
    note = fields.Char()
    error = fields.Char()
    option_json = fields.Text()

    @api.depends('recommended', 'is_cheapest', 'is_fastest', 'note')
    def _compute_tags(self):
        for line in self:
            tags = []
            if line.recommended:
                tags.append(_('Best value'))
            if line.is_cheapest:
                tags.append(_('Cheapest'))
            if line.is_fastest:
                tags.append(_('Fastest'))
            line.tags = ' · '.join(tags)

    def action_choose(self):
        self.ensure_one()
        if self.error:
            raise UserError(self.error)
        parcel = self.wizard_id.parcel_id
        option = json.loads(self.option_json or '{}')
        parcel.write({'account_id': self.account_id.id, 'service': self.service or False})
        parcel.action_book(option)
        return {'type': 'ir.actions.act_window', 'res_model': 'cp.parcel', 'res_id': parcel.id,
                'view_mode': 'form', 'target': 'current'}


class CpCourierPickupWizard(models.TransientModel):
    _name = 'cp.courier.pickup.wizard'
    _description = 'Request Courier Pickup'

    parcel_ids = fields.Many2many('cp.parcel', string='Parcels')
    pickup_at = fields.Datetime(string='Pickup Time', required=True,
                                default=lambda self: fields.Datetime.now().replace(minute=0, second=0)
                                + timedelta(hours=2))
    parcel_count = fields.Integer(compute='_compute_counts')
    account_names = fields.Char(compute='_compute_counts')

    @api.depends('parcel_ids')
    def _compute_counts(self):
        for wiz in self:
            wiz.parcel_count = len(wiz.parcel_ids)
            wiz.account_names = ', '.join(wiz.parcel_ids.account_id.mapped('name'))

    def action_confirm(self):
        done = []
        for account in self.parcel_ids.account_id:
            parcels = self.parcel_ids.filtered(lambda p: p.account_id == account and p.state == 'booked')
            if not parcels:
                continue
            ref = account._cp_provider().request_pickup(parcels, self.pickup_at)
            parcels.write({'pickup_ref': ref, 'pickup_date': self.pickup_at})
            for p in parcels:
                p.message_post(body=_('Pickup requested for %(when)s (ref %(ref)s).', when=self.pickup_at, ref=ref))
            done.append('%s: %s' % (account.name, ref))
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'type': 'success', 'title': _('Pickup requested'), 'message': ' · '.join(done),
                           'next': {'type': 'ir.actions.act_window_close'}}}

    def action_print_manifest(self):
        return self.env.ref('cargo_pilot_tms.action_report_parcel_manifest').report_action(self.parcel_ids)


class CpParcelNdrWizard(models.TransientModel):
    _name = 'cp.parcel.ndr.wizard'
    _description = 'Failed Delivery Action'

    parcel_id = fields.Many2one('cp.parcel', required=True)
    reason = fields.Char(related='parcel_id.ndr_reason')
    attempts = fields.Integer(related='parcel_id.ndr_attempts')
    can_ndr = fields.Boolean(related='parcel_id.account_id.can_ndr')
    action = fields.Selection([('reattempt', 'Deliver again'), ('rto', 'Return to sender')], default='reattempt',
                              required=True)
    new_phone = fields.Char(string='Updated Phone')
    note = fields.Char(string='Note for the courier', help='Landmark, best time to deliver, alternate contact...')

    def action_confirm(self):
        self.ensure_one()
        parcel = self.parcel_id
        if parcel.state != 'ndr':
            raise UserError(_('%s has no failed delivery waiting for a decision.', parcel.name))
        note = ' '.join(x for x in (self.note, _('Phone: %s', self.new_phone) if self.new_phone else '') if x)
        sent = False
        if parcel.account_id.can_ndr:
            sent = parcel.account_id._cp_provider().ndr_action(parcel, self.action, note)
        if self.new_phone:
            parcel.recipient_partner_id.phone = self.new_phone
        parcel.ndr_action = self.action
        label = dict(self._fields['action'].selection)[self.action]
        parcel._cp_add_events([{'time': fields.Datetime.now(), 'code': 'NDR-' + self.action.upper(),
                                'description': _('Decision: %(a)s. %(n)s', a=label, n=note or ''), 'location': '',
                                'status': False}], source='manual')
        parcel.message_post(body=_('Failed delivery: %(action)s%(sent)s. %(note)s', action=label,
                                   sent=_(' (sent to the courier)') if sent else _(' (tell the courier)'),
                                   note=note))
        issues = self.env['cp.exception'].search([('res_model', '=', parcel._name), ('res_id', '=', parcel.id),
                                                   ('issue_type', '=', 'ndr'), ('state', '=', 'open')])
        issues.action_take()
        return {'type': 'ir.actions.act_window_close'}


class CpParcelUpdateWizard(models.TransientModel):
    _name = 'cp.parcel.update.wizard'
    _description = 'Manual Tracking Update'

    parcel_id = fields.Many2one('cp.parcel', required=True)
    status = fields.Selection([s for s in PARCEL_STATES if s[0] not in ('draft', 'cancelled')], required=True,
                              default='in_transit')
    event_time = fields.Datetime(default=fields.Datetime.now, required=True)
    location = fields.Char()
    description = fields.Char(required=True, default=lambda self: _('Status update from the courier'))
    expected_delivery = fields.Date()

    def action_confirm(self):
        self.ensure_one()
        info = {'status': self.status, 'status_text': self.description, 'expected_delivery': self.expected_delivery,
                'events': [{'time': self.event_time, 'code': 'MANUAL', 'description': self.description,
                            'location': self.location or '', 'status': self.status}]}
        self.parcel_id._cp_apply_tracking(info, source='manual')
        return {'type': 'ir.actions.act_window_close'}
