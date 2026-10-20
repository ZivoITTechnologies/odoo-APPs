# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    jw_shortage_tolerance = fields.Float(related='company_id.jw_shortage_tolerance', readonly=False)
    jw_scrap_tolerance = fields.Float(related='company_id.jw_scrap_tolerance', readonly=False)
    jw_yield_tolerance = fields.Float(related='company_id.jw_yield_tolerance', readonly=False)
    jw_quality_tolerance = fields.Float(related='company_id.jw_quality_tolerance', readonly=False)
    jw_cost_variance_tolerance = fields.Float(related='company_id.jw_cost_variance_tolerance', readonly=False)
    jw_bill_qty_tolerance = fields.Float(related='company_id.jw_bill_qty_tolerance', readonly=False)
    jw_bill_price_tolerance = fields.Float(related='company_id.jw_bill_price_tolerance', readonly=False)
    jw_block_bill_variance = fields.Boolean(related='company_id.jw_block_bill_variance', readonly=False)
    jw_auto_vendor_location = fields.Boolean(related='company_id.jw_auto_vendor_location', readonly=False)
    jw_auto_ncr = fields.Boolean(related='company_id.jw_auto_ncr', readonly=False)
    jw_score_period_days = fields.Integer(related='company_id.jw_score_period_days', readonly=False)
    jw_weight_on_time = fields.Float(related='company_id.jw_weight_on_time', readonly=False)
    jw_weight_quality = fields.Float(related='company_id.jw_weight_quality', readonly=False)
    jw_weight_yield = fields.Float(related='company_id.jw_weight_yield', readonly=False)
    jw_weight_scrap = fields.Float(related='company_id.jw_weight_scrap', readonly=False)
    jw_weight_material = fields.Float(related='company_id.jw_weight_material', readonly=False)
    jw_weight_cost = fields.Float(related='company_id.jw_weight_cost', readonly=False)
    jw_weight_response = fields.Float(related='company_id.jw_weight_response', readonly=False)
