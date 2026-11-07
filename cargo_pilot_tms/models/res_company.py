# -*- coding: utf-8 -*-
import secrets

from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    cp_audit_distance_tolerance = fields.Float(
        string='Distance Tolerance (%)', default=5.0,
        help='Billed distance may exceed GPS distance by this percentage before the excess is deducted.')
    cp_audit_weight_tolerance = fields.Float(
        string='Weight Tolerance (%)', default=1.0,
        help='Allowed gap between billed, loaded and delivered weight.')
    cp_auction_extension_minutes = fields.Integer(
        string='Auction Auto-Extension (min)', default=5,
        help='A bid placed in the last N minutes pushes the auction end by N minutes.')
    cp_auction_default_duration = fields.Integer(string='Default Auction Duration (min)', default=60)
    cp_stoppage_alert_hours = fields.Float(string='Stoppage Alert (hours)', default=3.0)
    cp_document_alert_days = fields.Integer(string='Document Expiry Alert (days)', default=15)
    cp_fuel_anomaly_percent = fields.Float(
        string='Fuel Anomaly Threshold (%)', default=15.0,
        help='Flag a fill when mileage falls this far below the vehicle benchmark.')
    cp_default_speed_kmph = fields.Float(string='Planning Speed (km/h)', default=40.0)
    cp_epod_geofence_m = fields.Integer(string='ePOD Geofence Radius (m)', default=1000)
    cp_require_acceptance = fields.Boolean(
        string='Transporters Must Accept Bookings', default=True,
        help='Each booking is offered to the transporter, who accepts or declines it before a deadline.')
    cp_acceptance_hours = fields.Float(string='Time to Accept (hours)', default=2.0)
    cp_auction_on_decline = fields.Boolean(
        string='Start Bidding When All Decline',
        help='When no contracted transporter accepts, open the load for spot bidding automatically.')
    cp_placement_grace_hours = fields.Float(
        string='Placement Grace (hours)', default=2.0,
        help='A truck reported within this time after the planned placement counts as on time.')
    cp_scorecard_days = fields.Integer(string='Scorecard Period (days)', default=90)
    cp_api_key = fields.Char(string='Integration API Key', copy=False, groups='base.group_system')

    def cp_generate_api_key(self):
        for company in self:
            company.cp_api_key = secrets.token_urlsafe(32)
