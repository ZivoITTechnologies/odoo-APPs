# -*- coding: utf-8 -*-
from odoo import _, fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    # Reconciliation / exception tolerances (percentages)
    jw_shortage_tolerance = fields.Float(
        'Material Shortage Tolerance (%)', default=0.5,
        help="Unaccounted material above this percentage of the issued quantity raises a shortage exception "
             "once the job is completed.")
    jw_scrap_tolerance = fields.Float(
        'Scrap Tolerance (%)', default=2.0,
        help="Scrap above this percentage of the issued quantity raises an excess scrap exception.")
    jw_yield_tolerance = fields.Float(
        'Yield Tolerance (points)', default=1.0,
        help="Actual yield lower than the expected yield minus this tolerance raises a low yield exception.")
    jw_quality_tolerance = fields.Float(
        'Minimum Acceptance Rate (%)', default=98.0,
        help="Inspections with an acceptance rate below this value fail and can create an NCR.")
    jw_cost_variance_tolerance = fields.Float(
        'Cost Variance Tolerance (%)', default=5.0,
        help="Actual job cost above expected cost by more than this percentage raises a cost variance exception.")
    jw_bill_qty_tolerance = fields.Float(
        'Bill Quantity Tolerance (%)', default=0.0,
        help="Billed quantity may exceed the accepted quantity by this percentage without being flagged.")
    jw_bill_price_tolerance = fields.Float(
        'Bill Price Tolerance (%)', default=0.0,
        help="Billed rate may exceed the approved job rate by this percentage without being flagged.")
    jw_block_bill_variance = fields.Boolean(
        'Block Bills With Unapproved Variance', default=True,
        help="Prevent posting vendor bills for job work charges that exceed the accepted quantity or approved "
             "rate until a Job Work Administrator approves the variance.")
    jw_auto_vendor_location = fields.Boolean(
        'Dedicated Location per Job Worker', default=True,
        help="Automatically create a dedicated subcontracting stock location for each job worker so material "
             "custody can be tracked per vendor.")
    jw_auto_ncr = fields.Boolean(
        'Create NCR on Failed Inspection', default=True)

    # Vendor scorecard weights
    jw_score_period_days = fields.Integer('Scorecard Period (days)', default=90)
    jw_weight_on_time = fields.Float('On-time Delivery Weight', default=25.0)
    jw_weight_quality = fields.Float('Quality Acceptance Weight', default=25.0)
    jw_weight_yield = fields.Float('Yield Weight', default=15.0)
    jw_weight_scrap = fields.Float('Scrap Weight', default=10.0)
    jw_weight_material = fields.Float('Material Variance Weight', default=10.0)
    jw_weight_cost = fields.Float('Cost Variance Weight', default=10.0)
    jw_weight_response = fields.Float('Responsiveness Weight', default=5.0)

    jw_processing_location_id = fields.Many2one(
        'stock.location', 'Job Work Processing Location', readonly=True,
        help="Virtual production location where material consumed by job workers is transformed into output.")

    def _jw_get_processing_location(self):
        """Return (and lazily create) the virtual production location used for job work transformations."""
        self.ensure_one()
        location = self.sudo().jw_processing_location_id
        if not location:
            parent = self.env.ref('stock.stock_location_locations_virtual', raise_if_not_found=False)
            location = self.env['stock.location'].sudo().create({
                'name': _('Job Work Processing'),
                'usage': 'production',
                'location_id': parent.id if parent else False,
                'company_id': self.id,
            })
            self.sudo().jw_processing_location_id = location
        return location

    def _jw_get_scrap_location(self):
        self.ensure_one()
        location = self.env['stock.location'].sudo().search(
            [('company_id', '=', self.id), ('usage', '=', 'inventory')], order='id', limit=1)
        if not location:
            location = self.env['stock.location'].sudo().search(
                [('company_id', '=', False), ('usage', '=', 'inventory')], order='id', limit=1)
        return location
