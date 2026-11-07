# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    cp_audit_distance_tolerance = fields.Float(related='company_id.cp_audit_distance_tolerance', readonly=False)
    cp_audit_weight_tolerance = fields.Float(related='company_id.cp_audit_weight_tolerance', readonly=False)
    cp_auction_extension_minutes = fields.Integer(related='company_id.cp_auction_extension_minutes', readonly=False)
    cp_auction_default_duration = fields.Integer(related='company_id.cp_auction_default_duration', readonly=False)
    cp_stoppage_alert_hours = fields.Float(related='company_id.cp_stoppage_alert_hours', readonly=False)
    cp_document_alert_days = fields.Integer(related='company_id.cp_document_alert_days', readonly=False)
    cp_fuel_anomaly_percent = fields.Float(related='company_id.cp_fuel_anomaly_percent', readonly=False)
    cp_default_speed_kmph = fields.Float(related='company_id.cp_default_speed_kmph', readonly=False)
    cp_epod_geofence_m = fields.Integer(related='company_id.cp_epod_geofence_m', readonly=False)
    cp_api_key = fields.Char(related='company_id.cp_api_key', readonly=False)
    cp_require_acceptance = fields.Boolean(related='company_id.cp_require_acceptance', readonly=False)
    cp_acceptance_hours = fields.Float(related='company_id.cp_acceptance_hours', readonly=False)
    cp_auction_on_decline = fields.Boolean(related='company_id.cp_auction_on_decline', readonly=False)
    cp_placement_grace_hours = fields.Float(related='company_id.cp_placement_grace_hours', readonly=False)
    cp_scorecard_days = fields.Integer(related='company_id.cp_scorecard_days', readonly=False)

    def action_cp_generate_api_key(self):
        self.company_id.cp_generate_api_key()
        return {'type': 'ir.actions.client', 'tag': 'reload'}
