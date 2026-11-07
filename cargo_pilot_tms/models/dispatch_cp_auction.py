# -*- coding: utf-8 -*-
"""FR-03 / FR-04 on top of Cargo Pilot's auction engine: 10-20 minute bids for dispatch plans,
reverse auction (L1) or fixed target price (first to accept), automatic lock at the deadline."""
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class CpAuction(models.Model):
    _inherit = 'cp.auction'

    dispatch_plan_id = fields.Many2one('cp.dispatch.plan', string='Dispatch Plan', index=True, ondelete='set null')
    bidding_mode = fields.Selection([('reverse_auction', 'Lowest quote (reverse auction)'),
                                     ('target_price', 'Fixed target price (first to accept)')],
                                    default='reverse_auction', required=True)
    target_price = fields.Monetary()
    duration_minutes = fields.Integer(string='Duration (min)')
    pickup_slot = fields.Datetime()
    reporting_cutoff = fields.Datetime(string='Report Vehicle By')
    proposed_bid_id = fields.Many2one('cp.auction.bid', string='Proposed Winner', readonly=True, copy=False)
    override_reason = fields.Text(string='Reason for Another Winner', readonly=True, copy=False)
    seconds_left = fields.Integer(compute='_compute_seconds_left')
    participation_rate = fields.Float(compute='_compute_participation', string='Participation (%)')

    def _compute_seconds_left(self):
        now = fields.Datetime.now()
        for rec in self:
            rec.seconds_left = max(int((rec.date_end - now).total_seconds()), 0) \
                if rec.state == 'live' and rec.date_end else 0

    @api.depends('bid_ids', 'transporter_ids')
    def _compute_participation(self):
        for rec in self:
            invited = len(rec.transporter_ids)
            rec.participation_rate = (len(rec.bid_ids.transporter_id) / invited * 100.0) if invited else 0.0

    @api.depends('trip_ids.agreed_freight', 'trip_ids.state', 'state', 'target_price', 'bidding_mode',
                 'dispatch_plan_id')
    def _compute_award(self):
        """Dispatch bids measure savings against the planner's target rate (Mode A reference / Mode B price),
        the same baseline the dispatch dashboard uses; other auctions keep the ceiling / benchmark baseline."""
        super()._compute_award()
        for rec in self.filtered('dispatch_plan_id'):
            trips = rec.trip_ids.filtered(lambda t: t.state != 'cancel')
            reference = rec.target_price or rec.ceiling_price or rec.benchmark_rate
            baseline = reference * len(trips)
            rec.savings_amount = (baseline - rec.awarded_amount) if (trips and reference) else 0.0
            rec.savings_pct = (rec.savings_amount / baseline * 100.0) if baseline else 0.0

    def _cp_dispatch_winner(self):
        """L1 for reverse auctions; first acceptance for target-price bids."""
        self.ensure_one()
        bids = self.bid_ids.filtered(lambda b: b.is_latest and b.state != 'lost')
        if self.bidding_mode == 'target_price':
            return bids.sorted(lambda b: (b.bid_time, b.id))[:1]
        return bids.sorted(lambda b: (b.rate, b.bid_time, b.id))[:1]

    def cp_place_bid(self, transporter, rate, vehicles=None, channel='backend', note=False):
        if not self.dispatch_plan_id:
            return super().cp_place_bid(transporter, rate, vehicles=vehicles, channel=channel, note=note)
        self.ensure_one()
        self.cp_close_if_expired()
        if self.state != 'live':
            raise UserError(_('Bidding is closed for %s.', self.name))
        if self.bidding_mode == 'target_price':
            rate = self.target_price
        bid = super().cp_place_bid(transporter, rate, vehicles=1, channel=channel, note=note)
        if self.bidding_mode == 'target_price':
            bid.accepted_target = True
            self.sudo().write({'state': 'closed'})
            self.sudo().message_post(body=_('%s accepted the target price; bidding locked.', transporter.name))
            self.dispatch_plan_id.sudo().cp_after_bid_close()
        return bid

    def cp_close_if_expired(self):
        """Lock bids whose deadline passed, without waiting for the minute cron."""
        now = fields.Datetime.now()
        expired = self.filtered(lambda a: a.dispatch_plan_id and a.state == 'live' and a.date_end <= now)
        if expired:
            expired.sudo().write({'state': 'closed'})
            expired.mapped('dispatch_plan_id').sudo().cp_after_bid_close()
        return expired

    @api.model
    def _cron_auction_lifecycle(self):
        res = super()._cron_auction_lifecycle()
        plans = self.search([('state', '=', 'closed'), ('dispatch_plan_id.state', '=', 'bidding'),
                             ('proposed_bid_id', '=', False)]).mapped('dispatch_plan_id')
        plans.cp_after_bid_close()
        return res

    def cp_live_status(self, transporter=None):
        """Data for the vendor portal's live timer."""
        self.ensure_one()
        self.cp_close_if_expired()
        status = {'state': self.state, 'seconds_left': self.seconds_left,
                  'ends_at': fields.Datetime.to_string(self.date_end), 'bids': len(self.bid_ids.filtered('is_latest')),
                  'mode': self.bidding_mode}
        if self.bid_mode == 'open' and self.bidding_mode == 'reverse_auction':
            status['best_rate'] = self.best_rate
        if transporter:
            status['my_rank'] = self.cp_rank_for(transporter)
            status['won'] = bool(self.bid_ids.filtered(lambda b: b.transporter_id == transporter and b.state == 'won'))
        return status


class CpAuctionBid(models.Model):
    _inherit = 'cp.auction.bid'

    accepted_target = fields.Boolean(string='Accepted Target Price', readonly=True)
