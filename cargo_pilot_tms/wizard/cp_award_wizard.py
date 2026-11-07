# -*- coding: utf-8 -*-
from odoo import api, fields, models


class CpAuctionAwardWizard(models.TransientModel):
    _name = 'cp.auction.award.wizard'
    _description = 'Award Auction'

    auction_id = fields.Many2one('cp.auction', required=True)
    vehicle_count = fields.Integer(related='auction_id.vehicle_count')
    line_ids = fields.One2many('cp.auction.award.wizard.line', 'wizard_id')
    allocated = fields.Integer(compute='_compute_allocated')
    currency_id = fields.Many2one(related='auction_id.currency_id')

    @api.depends('line_ids.vehicle_count')
    def _compute_allocated(self):
        for wiz in self:
            wiz.allocated = sum(wiz.line_ids.mapped('vehicle_count'))

    def action_award(self):
        self.ensure_one()
        self.auction_id.cp_award([(line.bid_id, line.vehicle_count) for line in self.line_ids])
        return self.auction_id.action_view_trips()


class CpAuctionAwardWizardLine(models.TransientModel):
    _name = 'cp.auction.award.wizard.line'
    _description = 'Award Line'

    wizard_id = fields.Many2one('cp.auction.award.wizard', required=True, ondelete='cascade')
    bid_id = fields.Many2one('cp.auction.bid', required=True,
                             domain="[('auction_id', '=', parent.auction_id), ('is_latest', '=', True)]")
    transporter_id = fields.Many2one(related='bid_id.transporter_id')
    rate = fields.Monetary(related='bid_id.rate')
    offered = fields.Integer(related='bid_id.vehicle_count', string='Offered')
    vehicle_count = fields.Integer(string='Award Vehicles', default=1)
    currency_id = fields.Many2one(related='bid_id.currency_id')
