# -*- coding: utf-8 -*-
"""Booking offers: a transporter accepts or declines each load before a deadline.
A decline or no reply passes the load to the next contracted transporter, then to bidding."""
import secrets
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

OFFER_STATES = [
    ('pending', 'Waiting for reply'),
    ('accepted', 'Accepted'),
    ('declined', 'Declined'),
    ('expired', 'No reply'),
    ('withdrawn', 'Withdrawn'),
]
DECLINE_REASONS = [
    ('no_vehicle', 'No vehicle available'),
    ('rate', 'Rate not workable'),
    ('lane', 'Lane not served now'),
    ('timing', 'Placement time not possible'),
    ('other', 'Other'),
]


class CpTripOffer(models.Model):
    _name = 'cp.trip.offer'
    _description = 'Booking Offer to a Transporter'
    _order = 'offered_at desc, id desc'

    trip_id = fields.Many2one('cp.trip', required=True, ondelete='cascade', index=True)
    transporter_id = fields.Many2one('res.partner', required=True, string='Transporter', index=True)
    contract_line_id = fields.Many2one('cp.rate.contract.line', string='Contract Rate')
    price = fields.Monetary(string='Offered Freight')
    currency_id = fields.Many2one(related='trip_id.currency_id')
    offered_at = fields.Datetime(default=fields.Datetime.now, required=True)
    deadline = fields.Datetime(required=True)
    responded_at = fields.Datetime(readonly=True)
    response_minutes = fields.Integer(compute='_compute_response', store=True, string='Reply Time (min)',
                                      aggregator='avg')
    state = fields.Selection(OFFER_STATES, default='pending', required=True, index=True)
    decline_reason = fields.Selection(DECLINE_REASONS)
    note = fields.Char()
    channel = fields.Selection([('backend', 'Planner'), ('portal', 'Transporter portal'), ('link', 'Offer link'),
                                ('api', 'API'), ('whatsapp', 'WhatsApp'), ('system', 'System')],
                               readonly=True)
    access_token = fields.Char(default=lambda self: secrets.token_urlsafe(20), copy=False, readonly=True)
    offer_url = fields.Char(compute='_compute_offer_url')
    lane_id = fields.Many2one(related='trip_id.lane_id')
    planned_dispatch = fields.Datetime(related='trip_id.planned_dispatch')
    company_id = fields.Many2one(related='trip_id.company_id', store=True)

    @api.depends('offered_at', 'responded_at')
    def _compute_response(self):
        for rec in self:
            rec.response_minutes = int((rec.responded_at - rec.offered_at).total_seconds() // 60) \
                if rec.responded_at and rec.offered_at else 0

    def _compute_offer_url(self):
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        for rec in self:
            rec.offer_url = '%s/cp/offer/%s' % (base, rec.access_token)

    def action_accept(self, channel='backend'):
        for rec in self:
            if rec.state != 'pending':
                raise UserError(_('This offer is no longer open (%s).', dict(OFFER_STATES)[rec.state]))
            rec.write({'state': 'accepted', 'responded_at': fields.Datetime.now(), 'channel': channel})
            rec.trip_id._cp_log('offer', _('%s accepted the booking.', rec.transporter_id.name))
            rec.trip_id.message_post(body=_('%(t)s accepted the booking (%(c)s).', t=rec.transporter_id.name,
                                            c=dict(rec._fields['channel'].selection).get(channel)))
            self.env['cp.exception'].cp_auto_resolve(rec.trip_id, ('offer',), _('Transporter accepted.'))
        return True

    def action_decline(self, reason='other', note=None, channel='backend'):
        for rec in self:
            if rec.state != 'pending':
                raise UserError(_('This offer is no longer open (%s).', dict(OFFER_STATES)[rec.state]))
            rec.write({'state': 'declined', 'responded_at': fields.Datetime.now(), 'channel': channel,
                       'decline_reason': reason, 'note': note})
            rec.trip_id._cp_log('offer', _('%(t)s declined: %(r)s', t=rec.transporter_id.name,
                                           r=dict(DECLINE_REASONS).get(reason, reason)))
            rec.trip_id._cp_cascade()
        return True

    def action_open_decline_wizard(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Decline booking'), 'res_model': 'cp.offer.decline',
                'view_mode': 'form', 'target': 'new', 'context': {'default_offer_id': self.id}}

    @api.model
    def _cron_offer_expiry(self):
        expired = self.search([('state', '=', 'pending'), ('deadline', '<', fields.Datetime.now())])
        for offer in expired:
            offer.write({'state': 'expired', 'channel': 'system'})
            offer.trip_id._cp_log('offer', _('%s did not reply in time.', offer.transporter_id.name))
            offer.trip_id._cp_cascade()
        return True


class CpOfferDecline(models.TransientModel):
    _name = 'cp.offer.decline'
    _description = 'Decline a Booking Offer'

    offer_id = fields.Many2one('cp.trip.offer', required=True)
    reason = fields.Selection(DECLINE_REASONS, required=True, default='no_vehicle')
    note = fields.Char()

    def action_confirm(self):
        self.offer_id.action_decline(self.reason, self.note, 'backend')
        return {'type': 'ir.actions.act_window_close'}


class CpTrip(models.Model):
    _inherit = 'cp.trip'

    offer_ids = fields.One2many('cp.trip.offer', 'trip_id', string='Offers')
    acceptance_state = fields.Selection([('none', 'Not needed'), ('pending', 'Waiting for transporter'),
                                         ('accepted', 'Accepted'), ('declined', 'Declined by all'),
                                         ('expired', 'No reply')],
                                        compute='_compute_acceptance', store=True, string='Acceptance')
    offer_deadline = fields.Datetime(compute='_compute_acceptance', store=True, string='Reply By')
    offer_count = fields.Integer(compute='_compute_acceptance', store=True, string='Offers Sent')

    @api.depends('offer_ids.state', 'offer_ids.deadline')
    def _compute_acceptance(self):
        for rec in self:
            offers = rec.offer_ids.sorted(lambda o: (o.offered_at, o.id), reverse=True)
            latest = offers[:1]
            rec.offer_count = len(offers)
            if not latest:
                rec.acceptance_state = 'none'
                rec.offer_deadline = False
            else:
                rec.acceptance_state = {'pending': 'pending', 'accepted': 'accepted', 'declined': 'declined',
                                        'expired': 'expired', 'withdrawn': 'none'}[latest.state]
                rec.offer_deadline = latest.deadline if latest.state == 'pending' else False

    @api.model_create_multi
    def create(self, vals_list):
        trips = super().create(vals_list)
        for trip in trips:
            if trip.transporter_id and trip.state == 'draft' and not trip.auction_id \
                    and trip.company_id.cp_require_acceptance and not self.env.context.get('cp_no_offer'):
                trip._cp_offer()
        return trips

    def _cp_offer(self, transporter=None, contract_line=None, price=None):
        """Send the load to a transporter and wait for a reply."""
        self.ensure_one()
        transporter = transporter or self.transporter_id
        hours = self.company_id.cp_acceptance_hours or 2.0
        now = fields.Datetime.now()
        deadline = now + timedelta(hours=hours)
        if self.planned_dispatch and self.planned_dispatch > now:
            deadline = min(deadline, max(self.planned_dispatch, now + timedelta(minutes=30)))
        self.offer_ids.filtered(lambda o: o.state == 'pending').write({'state': 'withdrawn'})
        offer = self.env['cp.trip.offer'].sudo().create({
            'trip_id': self.id, 'transporter_id': transporter.id,
            'contract_line_id': (contract_line or self.contract_line_id).id,
            'price': price if price is not None else self.agreed_freight, 'deadline': deadline,
        })
        self._cp_log('offer', _('Offered to %(t)s, reply by %(d)s.', t=transporter.name,
                                d=fields.Datetime.to_string(deadline)))
        mode = _('LTL docket') if self.shipment_mode == 'ltl' else (self.vehicle_type_id.name or _('truck'))
        body = _('New booking %(trip)s: %(lane)s, %(mode)s, placement %(date)s, freight %(price)s. '
                 'Please accept or decline by %(deadline)s:',
                 trip=self.name, lane=self.lane_id.name, mode=mode,
                 date=fields.Datetime.to_string(self.planned_dispatch) if self.planned_dispatch else '-',
                 price=offer.price, deadline=fields.Datetime.to_string(deadline))
        self._cp_notify('offer', partner=transporter, body=body, url=offer.offer_url)
        return offer

    def _cp_accept_pending_offer(self, channel='backend'):
        for rec in self:
            pending = rec.offer_ids.filtered(lambda o: o.state == 'pending' and o.transporter_id == rec.transporter_id)
            if pending:
                pending.action_accept(channel)

    def action_accept_offer(self):
        self._cp_accept_pending_offer('backend')
        return True

    def action_decline_offer(self):
        self.ensure_one()
        pending = self.offer_ids.filtered(lambda o: o.state == 'pending')[:1]
        if not pending:
            raise UserError(_('There is no open offer on %s.', self.name))
        return pending.action_open_decline_wizard()

    def action_resend_offer(self):
        for rec in self:
            if not rec.transporter_id:
                raise UserError(_('Set the transporter first.'))
            rec._cp_offer()
        return True

    def _cp_cascade(self):
        """Pass the load to the next contracted transporter, or to bidding when none is left."""
        Contract = self.env['cp.rate.contract']
        Issue = self.env['cp.exception']
        for rec in self:
            if rec.state != 'draft':
                continue
            tried = rec.offer_ids.transporter_id
            lines = Contract.cp_find_rates(rec.lane_id, rec.vehicle_type_id, fields.Date.to_date(rec.planned_dispatch),
                                           mode=rec.shipment_mode, exclude_transporters=tried)
            if rec.indent_id:
                lines = rec.indent_id._cp_allowed_lines(lines)
            line = lines[:1]
            if line:
                kwargs = rec.indent_id._cp_rate_kwargs() if rec.indent_id else dict(rec._cp_rate_kwargs(),
                                                                                  weight_ton=rec.loaded_weight_ton)
                price = line.cp_compute_amount(**kwargs)
                previous = rec.transporter_id
                rec.write({'transporter_id': line.contract_id.transporter_id.id, 'contract_line_id': line.id,
                           'agreed_freight': price})
                rec.message_post(body=_('Passed from %(a)s to %(b)s (next contracted transporter).',
                                        a=previous.name or '-', b=line.contract_id.transporter_id.name))
                rec._cp_offer(line.contract_id.transporter_id, line, price)
                continue
            Issue.cp_raise('offer', rec, _('No transporter accepted %s', rec.name), severity='high',
                           detail=_('Tried: %s', ', '.join(tried.mapped('name')) or '-'),
                           suggestion=_('Open the load for spot bidding or call a market transporter.'))
            if rec.company_id.cp_auction_on_decline and rec.indent_id:
                indent = rec.indent_id
                rec.with_context(cp_cascade=True).action_cancel()
                if indent.state == 'allocated':
                    indent.state = 'sourcing'
                try:
                    indent.action_create_auction()
                    rec.message_post(body=_('No contracted transporter accepted; the load is open for bidding.'))
                except UserError:
                    rec.action_reset_draft()
