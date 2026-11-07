# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    cp_is_transporter = fields.Boolean(string='Transporter')
    cp_is_driver = fields.Boolean(string='Driver')
    cp_is_vehicle_owner = fields.Boolean(string='Market Vehicle Owner / Broker')
    cp_transporter_code = fields.Char(string='Transporter Code', copy=False)
    cp_license_number = fields.Char(string='Driving License No.')
    cp_license_expiry = fields.Date(string='License Expiry')
    cp_vehicle_type_ids = fields.Many2many('cp.vehicle.type', string='Vehicle Types Offered')
    cp_lane_ids = fields.Many2many('cp.lane', string='Preferred Lanes')

    cp_trip_count = fields.Integer(compute='_compute_cp_stats', string='Trips')
    cp_on_time_rate = fields.Float(compute='_compute_cp_stats', string='On-time %')
    cp_epod_compliance = fields.Float(compute='_compute_cp_stats', string='ePOD Compliance %')
    cp_auction_win_count = fields.Integer(compute='_compute_cp_stats', string='Auctions Won')
    cp_score = fields.Float(compute='_compute_cp_stats', string='Performance Score',
                            help='Weighted score: 60% on-time delivery, 40% ePOD compliance.')
    cp_safety_score = fields.Float(compute='_compute_cp_safety', string='Driver Safety Score')

    def _compute_cp_stats(self):
        Trip = self.env['cp.trip'].sudo()
        Auction = self.env['cp.auction'].sudo()
        for partner in self:
            if not partner.cp_is_transporter or not partner.id:
                partner.update({'cp_trip_count': 0, 'cp_on_time_rate': 0.0, 'cp_epod_compliance': 0.0,
                                'cp_auction_win_count': 0, 'cp_score': 0.0})
                continue
            trips = Trip.search([('transporter_id', '=', partner.id), ('state', '!=', 'cancel')])
            done = trips.filtered(lambda t: t.state in ('delivered', 'closed'))
            on_time = done.filtered(lambda t: not t.is_late)
            with_epod = done.filtered(lambda t: t.epod_state == 'verified')
            on_time_rate = (len(on_time) / len(done) * 100.0) if done else 0.0
            epod_rate = (len(with_epod) / len(done) * 100.0) if done else 0.0
            partner.cp_trip_count = len(trips)
            partner.cp_on_time_rate = on_time_rate
            partner.cp_epod_compliance = epod_rate
            partner.cp_auction_win_count = Auction.search_count([('winner_ids', 'in', partner.id)])
            partner.cp_score = round(0.6 * on_time_rate + 0.4 * epod_rate, 1) if done else 0.0

    def _compute_cp_safety(self):
        Event = self.env['cp.driving.event'].sudo()
        for partner in self:
            if not partner.cp_is_driver or not partner.id:
                partner.cp_safety_score = 0.0
                continue
            events = Event.search([('driver_id', '=', partner.id)])
            penalty = sum(events.mapped('penalty'))
            partner.cp_safety_score = max(0.0, 100.0 - penalty)

    def action_cp_view_trips(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('cargo_pilot_tms.action_cp_trip')
        action['domain'] = [('transporter_id', '=', self.id)]
        action['context'] = {'default_transporter_id': self.id}
        return action

    @api.model
    def _cp_portal_transporter(self):
        """Commercial partner of the current portal user, if it is a transporter."""
        partner = self.env.user.partner_id.commercial_partner_id
        return partner if partner.cp_is_transporter else self.browse()
