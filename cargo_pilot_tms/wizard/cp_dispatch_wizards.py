# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..models.dispatch_res_partner import CHECK_STATUSES


class CpCarrierCheckWizard(models.TransientModel):
    """FR-02 Carrier Availability Wizard: one line per regular partner."""
    _name = 'cp.carrier.check.wizard'
    _description = 'Regular Partner Availability'

    plan_id = fields.Many2one('cp.dispatch.plan', required=True)
    line_ids = fields.One2many('cp.carrier.check.wizard.line', 'wizard_id')
    currency_id = fields.Many2one(related='plan_id.currency_id')
    gross_weight_kg = fields.Float(related='plan_id.gross_weight_kg')
    dest_pincode = fields.Char(related='plan_id.dest_pincode')

    def _cp_save(self):
        self.ensure_one()
        logged = self.env['cp.carrier.check.log']
        for line in self.line_ids.filtered('status'):
            logged |= self.plan_id.cp_log_check(line.carrier_id, line.status, rate=line.quoted_rate, note=line.note,
                                                pickup=line.pickup_time, booking_ref=line.booking_ref)
        if not logged:
            raise UserError(_('Record the availability of at least one partner.'))
        return logged

    def action_save(self):
        self._cp_save()
        return {'type': 'ir.actions.act_window_close'}

    def action_book(self):
        self.ensure_one()
        confirmed = self.line_ids.filtered(lambda l: l.status == 'confirmed')
        chosen = confirmed.filtered('book') or confirmed
        if not chosen:
            # Keep what was entered: save the checks, then explain (an error would roll the checks back).
            logged = self._cp_save() if self.line_ids.filtered('status') else self.env['cp.carrier.check.log']
            msg = _('%s check(s) saved. No partner confirmed capacity, so nothing was booked.', len(logged)) \
                if logged else _('Record the availability of each partner first.')
            if logged and self.plan_id.regulars_exhausted:
                msg += ' ' + _('Every regular partner is unavailable: use Escalate to 3PL Bidding.')
            return {'type': 'ir.actions.client', 'tag': 'display_notification',
                    'params': {'type': 'warning', 'title': _('Nothing booked'), 'message': msg, 'sticky': False,
                               'next': {'type': 'ir.actions.act_window_close'} if logged else False}}
        if len(chosen) > 1:
            chosen = chosen.sorted(lambda l: (l.quoted_rate or float('inf'), l.carrier_id.cp_carrier_sequence))[:1]
        self._cp_save()
        self.plan_id.cp_book_regular(chosen.carrier_id, rate=chosen.quoted_rate, booking_ref=chosen.booking_ref)
        return {'type': 'ir.actions.act_window_close'}

    def action_save_and_escalate(self):
        self.ensure_one()
        self._cp_save()
        return self.plan_id.action_escalate_bid()


class CpCarrierCheckWizardLine(models.TransientModel):
    _name = 'cp.carrier.check.wizard.line'
    _description = 'Regular Partner Availability Line'

    wizard_id = fields.Many2one('cp.carrier.check.wizard', required=True, ondelete='cascade')
    carrier_id = fields.Many2one('res.partner', required=True, readonly=True)
    status = fields.Selection(CHECK_STATUSES, string='Availability')
    quoted_rate = fields.Monetary(string='Quoted Rate')
    currency_id = fields.Many2one(related='wizard_id.currency_id')
    pickup_time = fields.Datetime(string='Pickup Offered')
    booking_ref = fields.Char(string='AWB / Booking Ref.')
    note = fields.Char()
    auto_reason = fields.Char(string='Hint', readonly=True)
    book = fields.Boolean(string='Book This')


class CpBidLaunchWizard(models.TransientModel):
    """FR-03 bid set-up: truck type, pickup slot, 10-20 minutes, mode, invited vendors."""
    _name = 'cp.bid.launch.wizard'
    _description = 'Launch 3PL Bid'

    plan_id = fields.Many2one('cp.dispatch.plan', required=True)
    vehicle_type_id = fields.Many2one('cp.vehicle.type', string='Truck Type', required=True)
    pickup_slot = fields.Datetime(required=True, string='Pickup / Report By')
    duration_minutes = fields.Integer(string='Bid Duration (minutes)', default=15, required=True)
    bidding_mode = fields.Selection([('reverse_auction', 'Mode A: lowest quote (reverse auction)'),
                                     ('target_price', 'Mode B: fixed target price, first to accept wins')],
                                    default='reverse_auction', required=True)
    target_price = fields.Monetary(help='Mode B: the price offered. Mode A: the reference used to measure savings.')
    ceiling_price = fields.Monetary(string='Highest Acceptable Quote', help='Mode A only. 0 = no cap.')
    currency_id = fields.Many2one(related='plan_id.currency_id')
    vendor_ids = fields.Many2many('res.partner', string='Invited 3PL Vendors',
                                  domain=[('cp_is_3pl_vendor', '=', True)])
    vendor_count = fields.Integer(compute='_compute_vendor_count')

    @api.depends('vendor_ids')
    def _compute_vendor_count(self):
        for wiz in self:
            wiz.vendor_count = len(wiz.vendor_ids)

    @api.onchange('vehicle_type_id')
    def _onchange_vehicle_type(self):
        if self.vehicle_type_id and self.plan_id:
            self.vendor_ids = self.plan_id._cp_eligible_vendors(self.vehicle_type_id)

    def action_launch(self):
        self.ensure_one()
        auction = self.plan_id.cp_launch_bid(
            self.vendor_ids, self.duration_minutes, mode=self.bidding_mode, target_price=self.target_price,
            vehicle_type=self.vehicle_type_id, pickup_slot=self.pickup_slot,
            ceiling=self.ceiling_price if self.bidding_mode == 'reverse_auction' else 0.0)
        return {'type': 'ir.actions.act_window', 'res_model': 'cp.dispatch.plan', 'res_id': self.plan_id.id,
                'view_mode': 'form', 'target': 'current', 'context': {'cp_auction': auction.id}}


class CpBidAwardWizard(models.TransientModel):
    """FR-04: approve the proposed winner, or pick another offer with a justification."""
    _name = 'cp.bid.award.wizard'
    _description = 'Approve 3PL Winner'

    plan_id = fields.Many2one('cp.dispatch.plan', required=True)
    auction_id = fields.Many2one(related='plan_id.auction_id')
    proposed_bid_id = fields.Many2one(related='plan_id.auction_id.proposed_bid_id')
    bid_id = fields.Many2one('cp.auction.bid', string='Award To', required=True,
                             domain="[('auction_id', '=', auction_id), ('is_latest', '=', True)]")
    is_override = fields.Boolean(compute='_compute_is_override')
    justification = fields.Text(help='Required when awarding to someone other than the proposed winner, '
                                     'for example a faster reporting time.')

    @api.depends('bid_id', 'proposed_bid_id')
    def _compute_is_override(self):
        for wiz in self:
            wiz.is_override = bool(wiz.bid_id and wiz.proposed_bid_id and wiz.bid_id != wiz.proposed_bid_id)

    def action_award(self):
        self.ensure_one()
        self.plan_id.cp_award(self.bid_id, justification=self.justification)
        return {'type': 'ir.actions.act_window_close'}


class CpBidRecordWizard(models.TransientModel):
    """Enter an offer a vendor gave by phone, WhatsApp or e-mail into the live bid, on their behalf."""
    _name = 'cp.bid.record.wizard'
    _description = 'Record Bid for Vendor'

    plan_id = fields.Many2one('cp.dispatch.plan', required=True)
    auction_id = fields.Many2one(related='plan_id.auction_id')
    bidding_mode = fields.Selection(related='plan_id.auction_id.bidding_mode')
    target_price = fields.Monetary(related='plan_id.auction_id.target_price')
    invited_ids = fields.Many2many(related='plan_id.auction_id.transporter_ids')
    vendor_id = fields.Many2one('res.partner', string='Vendor', domain="[('id', 'in', invited_ids)]")
    rate = fields.Monetary(string='Quoted Rate')
    currency_id = fields.Many2one(related='plan_id.currency_id')
    channel = fields.Selection([('phone', 'Phone call'), ('whatsapp', 'WhatsApp'), ('email', 'E-mail')],
                               default='phone', required=True, string='Received By')
    note = fields.Char(help='For example who you spoke to and any conditions the vendor gave.')

    def action_record(self):
        self.ensure_one()
        auction = self.auction_id
        if not auction:
            raise UserError(_('%s has no live bid.', self.plan_id.name))
        if not self.vendor_id:
            raise UserError(_('Choose the vendor who gave the offer.'))
        if self.bidding_mode != 'target_price' and self.rate <= 0:
            raise UserError(_('Enter the rate the vendor quoted.'))
        how = dict(self._fields['channel'].selection)[self.channel]
        note = _('%(how)s, recorded by %(user)s', how=how, user=self.env.user.name)
        if self.note:
            note += ': ' + self.note
        bid = auction.cp_place_bid(self.vendor_id, self.rate or auction.target_price,
                                   channel='whatsapp' if self.channel == 'whatsapp' else 'backend', note=note[:200])
        self.plan_id.message_post(body=_('Offer from %(v)s recorded (%(how)s): %(r)s. %(n)s', v=self.vendor_id.name,
                                         how=how, r=self.plan_id._cp_money(bid.rate), n=self.note or ''))
        return {'type': 'ir.actions.act_window_close'}
