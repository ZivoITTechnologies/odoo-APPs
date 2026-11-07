# -*- coding: utf-8 -*-
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    cp_dispatch_bid_minutes = fields.Integer(string='Default Bid Duration (min)', default=15)
    cp_dispatch_reporting_hours = fields.Float(string='Vehicle Reporting Window (h)', default=2.0)
    cp_dispatch_require_eway = fields.Boolean(string='E-way Bill Required at Gate-Out', default=True)
    cp_eway_threshold = fields.Monetary(string='E-way Bill Value Threshold', default=50000.0,
                                        currency_field='currency_id')
    cp_dispatch_sms = fields.Boolean(string='Send SMS to Customers', default=True)
    cp_detention_free_hours = fields.Float(string='Default Free Waiting (h)', default=2.0)
    cp_detention_hourly_rate = fields.Monetary(string='Default Detention per Hour', currency_field='currency_id')
    cp_detention_daily_rate = fields.Monetary(string='Default Halting per Day', currency_field='currency_id')
    cp_regular_share_target = fields.Float(string='Regular Partner Share Target (%)', default=75.0)
    cp_dispatch_transit_days = fields.Float(
        string='Default Transit Time (days)', default=2.0,
        help='Used for the target delivery date when the lane has no distance and the delivery has no deadline.')


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    cp_dispatch_bid_minutes = fields.Integer(related='company_id.cp_dispatch_bid_minutes', readonly=False)
    cp_dispatch_reporting_hours = fields.Float(related='company_id.cp_dispatch_reporting_hours', readonly=False)
    cp_dispatch_require_eway = fields.Boolean(related='company_id.cp_dispatch_require_eway', readonly=False)
    cp_eway_threshold = fields.Monetary(related='company_id.cp_eway_threshold', readonly=False)
    cp_dispatch_sms = fields.Boolean(related='company_id.cp_dispatch_sms', readonly=False)
    cp_detention_free_hours = fields.Float(related='company_id.cp_detention_free_hours', readonly=False)
    cp_detention_hourly_rate = fields.Monetary(related='company_id.cp_detention_hourly_rate', readonly=False)
    cp_detention_daily_rate = fields.Monetary(related='company_id.cp_detention_daily_rate', readonly=False)
    cp_regular_share_target = fields.Float(related='company_id.cp_regular_share_target', readonly=False)
    cp_dispatch_transit_days = fields.Float(related='company_id.cp_dispatch_transit_days', readonly=False)
