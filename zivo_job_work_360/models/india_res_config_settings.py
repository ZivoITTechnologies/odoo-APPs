# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    group_jw_india = fields.Boolean(
        'India Compliance', implied_group='zivo_job_work_360.group_jw_india',
        help="Job work challans, E-Way Bill data, ITC-04 preparation and statutory return-period alerts.")
    jw_in_return_days_inputs = fields.Integer(related='company_id.jw_in_return_days_inputs', readonly=False)
    jw_in_return_days_capital = fields.Integer(related='company_id.jw_in_return_days_capital', readonly=False)
    jw_in_alert_days = fields.Integer(related='company_id.jw_in_alert_days', readonly=False)
    jw_in_auto_challan = fields.Boolean(related='company_id.jw_in_auto_challan', readonly=False)
    jw_in_default_gst_rate = fields.Float(related='company_id.jw_in_default_gst_rate', readonly=False)
