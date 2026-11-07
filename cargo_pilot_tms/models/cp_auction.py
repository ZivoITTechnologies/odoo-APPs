# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import format_amount


class CpAuction(models.Model):
    _name = 'cp.auction'
    _description = 'Spot Freight Auction'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'portal.mixin']
    _order = 'date_start desc, id desc'

    name = fields.Char(default=lambda self: _('New'), copy=False, readonly=True)
    indent_id = fields.Many2one('cp.indent', required=True, ondelete='restrict', tracking=True)
    lane_id = fields.Many2one(related='indent_id.lane_id', store=True)
    vehicle_type_id = fields.Many2one(related='indent_id.vehicle_type_id', store=True)
    required_date = fields.Datetime(related='indent_id.required_date')
    total_weight_ton = fields.Float(related='indent_id.total_weight_ton')
    vehicle_count = fields.Integer(string='Vehicles', required=True, default=1)
    date_start = fields.Datetime(string='Starts', required=True, default=fields.Datetime.now, tracking=True)
    date_end = fields.Datetime(string='Ends', required=True, tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('scheduled', 'Scheduled (Pre-bids)'),
        ('live', 'Live'),
        ('closed', 'Closed'),
        ('awarded', 'Awarded'),
        ('cancel', 'Cancelled'),
    ], default='draft', tracking=True)
    bid_mode = fields.Selection([
        ('open', 'Open (rank and best price visible, names hidden)'),
        ('sealed', 'Sealed (bidders see only their own bid)'),
    ], default='open', required=True)
    ceiling_price = fields.Monetary(string='Ceiling / Vehicle', tracking=True,
                                    help='Bids above this price are rejected. Leave 0 for no ceiling.')
    allow_pre_bid = fields.Boolean(string='Allow Pre-bids', default=True,
                                   help='Each transporter may place one indicative bid before the auction opens.')
    allow_part_bid = fields.Boolean(string='Allow Part-bids', default=True,
                                    help='Transporters may bid for fewer vehicles than requested.')
    min_decrement_pct = fields.Float(string='Min. Decrement (%)', default=0.5,
                                     help='A new bid must undercut the bidder\'s previous bid by at least this much.')
    extension_minutes = fields.Integer(default=lambda self: self.env.company.cp_auction_extension_minutes)
    extension_count = fields.Integer(readonly=True)
    transporter_ids = fields.Many2many('res.partner', string='Invited Transporters',
                                       domain=[('cp_is_transporter', '=', True)])
    open_to_all = fields.Boolean(string='Open to All Transporters', default=False)
    bid_ids = fields.One2many('cp.auction.bid', 'auction_id', string='Bids')
    bid_count = fields.Integer(compute='_compute_bid_stats')
    participant_count = fields.Integer(compute='_compute_bid_stats')
    best_rate = fields.Monetary(compute='_compute_bid_stats', string='Best Rate')
    suggested_ceiling = fields.Monetary(compute='_compute_bid_stats',
                                        help='Lowest pre-bid, or the contract benchmark when there is none.')
    benchmark_rate = fields.Monetary(compute='_compute_benchmark', string='Contract Benchmark')
    awarded_amount = fields.Monetary(compute='_compute_award', store=True)
    savings_amount = fields.Monetary(compute='_compute_award', store=True, string='Savings',
                                     help='Reference price (ceiling, else benchmark) minus awarded price, for all vehicles.')
    savings_pct = fields.Float(compute='_compute_award', store=True, string='Savings %')
    winner_ids = fields.Many2many('res.partner', 'cp_auction_winner_rel', 'auction_id', 'partner_id',
                                  string='Winners', readonly=True)
    trip_ids = fields.One2many('cp.trip', 'auction_id', string='Trips')
    time_left = fields.Char(compute='_compute_time_left')
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('cp.auction') or _('New')
            if not vals.get('date_end'):
                start = fields.Datetime.to_datetime(vals.get('date_start')) or fields.Datetime.now()
                vals['date_end'] = start + timedelta(minutes=self.env.company.cp_auction_default_duration or 60)
        return super().create(vals_list)

    def _compute_access_url(self):
        super()._compute_access_url()
        for rec in self:
            rec.access_url = '/my/cp/auctions/%s' % rec.id

    @api.constrains('date_start', 'date_end', 'vehicle_count')
    def _check_dates(self):
        for rec in self:
            if rec.date_end <= rec.date_start:
                raise ValidationError(_('The auction must end after it starts.'))
            if rec.vehicle_count < 1:
                raise ValidationError(_('An auction needs at least one vehicle.'))

    @api.depends('bid_ids.rate', 'bid_ids.is_latest', 'bid_ids.is_pre_bid')
    def _compute_bid_stats(self):
        for rec in self:
            latest = rec.bid_ids.filtered('is_latest')
            rec.bid_count = len(rec.bid_ids)
            rec.participant_count = len(latest.mapped('transporter_id'))
            rec.best_rate = min(latest.mapped('rate')) if latest else 0.0
            pre = rec.bid_ids.filtered('is_pre_bid')
            rec.suggested_ceiling = min(pre.mapped('rate')) if pre else rec.benchmark_rate

    @api.depends('lane_id', 'vehicle_type_id', 'total_weight_ton', 'vehicle_count')
    def _compute_benchmark(self):
        Contract = self.env['cp.rate.contract']
        for rec in self:
            line = Contract.cp_find_rate(False, rec.lane_id, rec.vehicle_type_id) if rec.lane_id else False
            if line:
                weight = rec.indent_id.total_weight_ton / (rec.indent_id.vehicle_count or 1)
                rec.benchmark_rate = line.cp_compute_amount(weight_ton=weight)
            else:
                rec.benchmark_rate = 0.0

    @api.depends('trip_ids.agreed_freight', 'trip_ids.state', 'state')
    def _compute_award(self):
        for rec in self:
            trips = rec.trip_ids.filtered(lambda t: t.state != 'cancel')
            awarded = sum(trips.mapped('agreed_freight'))
            reference = rec.ceiling_price or rec.benchmark_rate
            baseline = reference * len(trips)
            rec.awarded_amount = awarded
            rec.savings_amount = (baseline - awarded) if (trips and reference) else 0.0
            rec.savings_pct = (rec.savings_amount / baseline * 100.0) if baseline else 0.0

    def _compute_time_left(self):
        now = fields.Datetime.now()
        for rec in self:
            target = rec.date_start if rec.state == 'scheduled' else rec.date_end
            if rec.state not in ('scheduled', 'live') or not target or target <= now:
                rec.time_left = ''
                continue
            secs = int((target - now).total_seconds())
            hours, rem = divmod(secs, 3600)
            rec.time_left = '%dh %02dm' % (hours, rem // 60)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def action_publish(self):
        template = self.env.ref('cargo_pilot_tms.mail_template_cp_auction_invite', raise_if_not_found=False)
        for rec in self:
            if not rec.transporter_ids and not rec.open_to_all:
                raise UserError(_('Invite at least one transporter or open the auction to all.'))
            now = fields.Datetime.now()
            rec.state = 'live' if rec.date_start <= now else 'scheduled'
            rec._portal_ensure_token()
            rec.message_subscribe(partner_ids=rec.transporter_ids.ids)
            if template:
                for partner in rec.transporter_ids.filtered('email'):
                    template.with_context(cp_partner=partner).send_mail(
                        rec.id, email_values={'recipient_ids': [(6, 0, partner.ids)]})
            rec.message_post(body=_('Auction published to %s transporter(s).', len(rec.transporter_ids)))

    def action_start(self):
        self.filtered(lambda a: a.state in ('draft', 'scheduled')).write({
            'state': 'live', 'date_start': fields.Datetime.now()})

    def action_close(self):
        self.filtered(lambda a: a.state == 'live').write({'state': 'closed'})

    def action_cancel(self):
        self.write({'state': 'cancel'})

    @api.model
    def _cron_auction_lifecycle(self):
        now = fields.Datetime.now()
        self.search([('state', '=', 'scheduled'), ('date_start', '<=', now)]).write({'state': 'live'})
        ended = self.search([('state', '=', 'live'), ('date_end', '<=', now)])
        ended.write({'state': 'closed'})
        for auction in ended:
            if not auction.bid_ids:
                self.env['cp.exception'].cp_raise(
                    'no_bids', auction, _('No bids on %s', auction.lane_id.name), severity='high',
                    suggestion=_('Raise the ceiling, invite more transporters or extend the auction.'))
            auction.activity_schedule(
                'mail.mail_activity_data_todo', user_id=auction.indent_id.user_id.id or self.env.uid,
                summary=_('Auction closed: award vehicles'),
                note=_('%s bid(s) received. Best rate: %s.', auction.bid_count, auction.best_rate))

    # ------------------------------------------------------------------
    # Bidding
    # ------------------------------------------------------------------
    def cp_can_bid(self, transporter):
        self.ensure_one()
        return bool(transporter) and (self.open_to_all or transporter in self.transporter_ids)

    def cp_place_bid(self, transporter, rate, vehicles=None, channel='backend', note=False):
        """Single entry point for bids from backend, portal, API and messaging channels."""
        self.ensure_one()
        vehicles = int(vehicles or self.vehicle_count)
        now = fields.Datetime.now()
        if not self.cp_can_bid(transporter):
            raise UserError(_('%s is not invited to this auction.', transporter.display_name))
        if rate <= 0:
            raise UserError(_('Enter a rate greater than zero.'))
        if self.ceiling_price and rate > self.ceiling_price:
            raise UserError(_('Your rate is above the ceiling of %s.',
                                format_amount(self.env, self.ceiling_price, self.currency_id)))
        if vehicles < 1 or vehicles > self.vehicle_count:
            raise UserError(_('Offer between 1 and %s vehicles.', self.vehicle_count))
        if not self.allow_part_bid and vehicles != self.vehicle_count:
            raise UserError(_('This auction requires bids for all %s vehicles.', self.vehicle_count))

        Bid = self.env['cp.auction.bid'].sudo()
        previous = self.bid_ids.filtered(lambda b: b.transporter_id == transporter and b.is_latest)
        is_pre_bid = False
        if self.state == 'scheduled':
            if not self.allow_pre_bid:
                raise UserError(_('This auction has not opened yet.'))
            if self.bid_ids.filtered(lambda b: b.transporter_id == transporter and b.is_pre_bid):
                raise UserError(_('Only one pre-bid is allowed before the auction opens.'))
            is_pre_bid = True
        elif self.state == 'live':
            if now > self.date_end:
                raise UserError(_('This auction has ended.'))
            if previous and not previous.is_pre_bid:
                floor = previous.rate * (1 - (self.min_decrement_pct or 0.0) / 100.0)
                if rate > floor:
                    raise UserError(_('Your new bid must be at most %.2f (%.2f%% below your last bid).',
                                      floor, self.min_decrement_pct))
        else:
            raise UserError(_('Bidding is closed for %s.', self.name))

        previous.write({'is_latest': False})
        bid = Bid.create({
            'auction_id': self.id,
            'transporter_id': transporter.id,
            'rate': rate,
            'vehicle_count': vehicles,
            'is_pre_bid': is_pre_bid,
            'channel': channel,
            'note': note,
        })
        # Anti-sniping: a bid in the closing window pushes the end time out.
        if self.state == 'live' and self.extension_minutes:
            window = timedelta(minutes=self.extension_minutes)
            if self.date_end - now <= window:
                self.sudo().write({'date_end': self.date_end + window,
                                   'extension_count': self.extension_count + 1})
                self.sudo().message_post(body=_('Late bid received: auction extended by %s minutes.',
                                                self.extension_minutes))
        return bid

    def cp_rank_for(self, transporter):
        self.ensure_one()
        mine = self.bid_ids.filtered(lambda b: b.transporter_id == transporter and b.is_latest)
        return mine.rank if mine else 0

    # ------------------------------------------------------------------
    # Award
    # ------------------------------------------------------------------
    def _cp_auto_allocation(self):
        """Lowest rate first; part-bids fill the remaining vehicles."""
        self.ensure_one()
        remaining = self.vehicle_count
        plan = []
        for bid in self.bid_ids.filtered('is_latest').sorted(lambda b: (b.rate, b.bid_time)):
            if remaining <= 0:
                break
            qty = min(bid.vehicle_count, remaining)
            plan.append((bid, qty))
            remaining -= qty
        return plan

    def action_open_award_wizard(self):
        self.ensure_one()
        if self.state == 'live':
            self.action_close()
        if self.state != 'closed':
            raise UserError(_('Only closed auctions can be awarded.'))
        wizard = self.env['cp.auction.award.wizard'].create({
            'auction_id': self.id,
            'line_ids': [(0, 0, {'bid_id': bid.id, 'vehicle_count': qty})
                         for bid, qty in self._cp_auto_allocation()],
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Award Auction'),
            'res_model': 'cp.auction.award.wizard',
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def cp_award(self, allocation):
        """allocation: list of (bid, vehicle_count)."""
        self.ensure_one()
        total = sum(qty for _bid, qty in allocation)
        if not total:
            raise UserError(_('Allocate at least one vehicle.'))
        if total > self.vehicle_count:
            raise UserError(_('You allocated %s vehicles but the auction is for %s.', total, self.vehicle_count))
        Trip = self.env['cp.trip']
        winners = self.env['res.partner']
        for bid, qty in allocation:
            if qty <= 0:
                continue
            if qty > bid.vehicle_count:
                raise UserError(_('%s offered only %s vehicle(s).', bid.transporter_id.name, bid.vehicle_count))
            Trip.create([self.indent_id._cp_trip_vals(bid.transporter_id, bid.rate, auction=self)
                         for _i in range(qty)])
            bid.write({'state': 'won', 'awarded_vehicles': qty})
            winners |= bid.transporter_id
        self.bid_ids.filtered(lambda b: b.is_latest and b.state != 'won').write({'state': 'lost'})
        self.write({'state': 'awarded', 'winner_ids': [(6, 0, winners.ids)]})
        indent = self.indent_id
        indent.state = 'allocated' if not indent.pending_count else 'sourcing'
        self.message_post(body=_('Awarded %(n)s vehicle(s) to %(w)s.', n=total, w=', '.join(winners.mapped('name'))))
        return True

    def action_view_trips(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('cargo_pilot_tms.action_cp_trip')
        action['domain'] = [('auction_id', '=', self.id)]
        return action


class CpAuctionBid(models.Model):
    _name = 'cp.auction.bid'
    _description = 'Auction Bid'
    _order = 'auction_id, is_latest desc, rate, bid_time'

    auction_id = fields.Many2one('cp.auction', required=True, ondelete='cascade', index=True)
    transporter_id = fields.Many2one('res.partner', required=True)
    rate = fields.Monetary(string='Rate / Vehicle', required=True)
    vehicle_count = fields.Integer(string='Vehicles Offered', default=1)
    awarded_vehicles = fields.Integer(readonly=True)
    bid_time = fields.Datetime(default=fields.Datetime.now, readonly=True)
    is_pre_bid = fields.Boolean(string='Pre-bid', readonly=True)
    is_latest = fields.Boolean(default=True, readonly=True)
    channel = fields.Selection([
        ('backend', 'Backend'), ('portal', 'Transporter Portal'), ('api', 'API'),
        ('whatsapp', 'WhatsApp / Messaging'),
    ], default='backend', readonly=True)
    state = fields.Selection([('active', 'Active'), ('won', 'Won'), ('lost', 'Lost')], default='active')
    rank = fields.Integer(compute='_compute_rank')
    note = fields.Char()
    currency_id = fields.Many2one(related='auction_id.currency_id')

    def _compute_rank(self):
        for auction in self.mapped('auction_id'):
            ordered = auction.bid_ids.filtered('is_latest').sorted(lambda b: (b.rate, b.bid_time))
            ranks = {bid.id: i for i, bid in enumerate(ordered, start=1)}
            for bid in self.filtered(lambda b: b.auction_id == auction):
                bid.rank = ranks.get(bid.id, 0)

    def _compute_display_name(self):
        for bid in self:
            bid.display_name = '%s @ %s' % (bid.transporter_id.name or '',
                                            format_amount(bid.env, bid.rate, bid.currency_id or bid.env.company.currency_id))
