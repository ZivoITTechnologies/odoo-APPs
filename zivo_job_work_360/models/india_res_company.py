# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    jw_in_return_days_inputs = fields.Integer(
        'Return Period for Inputs (days)', default=365,
        help="Configurable period within which inputs sent for job work are expected back. Verify the period "
             "against the current GST rules.")
    jw_in_return_days_capital = fields.Integer(
        'Return Period for Capital Goods (days)', default=1095,
        help="Configurable period within which capital goods sent for job work are expected back.")
    jw_in_alert_days = fields.Integer('Alert Before Due (days)', default=30)
    jw_in_auto_challan = fields.Boolean('Create Challan on Dispatch', default=True,
                                        help="Create the job work challan automatically when material is "
                                             "dispatched to a job worker.")
    @api.model
    def _jw_in_enabled(self):
        """India compliance features are switched on in the settings (implied group of internal users)."""
        group = self.env.ref('zivo_job_work_360.group_jw_india', raise_if_not_found=False)
        return bool(group) and group in self.env.ref('base.group_user').sudo().all_implied_ids

    @api.model
    def _jw_in_enable(self):
        group = self.env.ref('zivo_job_work_360.group_jw_india')
        self.env.ref('base.group_user').sudo()._apply_group(group)

    jw_in_default_gst_rate = fields.Float('Default GST Rate (%)', default=18.0,
                                          help="Rate printed on challans and E-Way Bill data when the product has "
                                               "no purchase tax.")
