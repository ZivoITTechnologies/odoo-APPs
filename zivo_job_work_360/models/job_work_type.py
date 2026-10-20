# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ZivoJobWorkType(models.Model):
    _name = 'zivo.job.work.type'
    _description = 'Job Work Type'
    _order = 'sequence, name'

    name = fields.Char('Job Type', required=True, translate=True)
    code = fields.Char('Code')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    color = fields.Integer('Color')
    company_id = fields.Many2one('res.company', 'Company', default=lambda self: self.env.company)
    service_product_id = fields.Many2one(
        'product.product', 'Processing Service',
        domain="[('type', '=', 'service'), ('purchase_ok', '=', True)]",
        help="Service product used on purchase orders and vendor bills for the processing charges.")
    quality_required = fields.Boolean(
        'Inspection Required', default=True,
        help="Received output must be inspected before it counts as accepted quantity.")
    expected_yield = fields.Float(
        'Expected Yield (%)', default=100.0,
        help="Standard yield for this process: accepted output compared to the output the consumed material "
             "should produce.")
    expected_scrap_pct = fields.Float(
        'Expected Process Scrap (%)', default=0.0,
        help="Default share of the issued material expected to be scrapped during the process.")
    lead_days = fields.Integer('Standard Lead Time (days)', default=7)
    scrap_product_id = fields.Many2one(
        'product.product', 'Returnable Scrap Product',
        domain="[('type', '=', 'consu')]",
        help="Optional by-product received back when the job worker returns process scrap "
             "(for example brass turnings).")
    is_rework = fields.Boolean('Rework Type', help="Used for rework job work orders created from inspections.")
    instructions = fields.Html('Standard Instructions', translate=True)
    job_count = fields.Integer(compute='_compute_job_count')

    _code_company_uniq = models.Constraint(
        'unique(code, company_id)', 'The job type code must be unique per company.')

    @api.constrains('expected_yield', 'expected_scrap_pct')
    def _check_percentages(self):
        for jtype in self:
            if not 0 <= jtype.expected_yield <= 100 or not 0 <= jtype.expected_scrap_pct < 100:
                raise ValidationError(_("Yield must be between 0 and 100% and scrap between 0 and 99.99%."))

    @api.constrains('scrap_product_id')
    def _check_scrap_product(self):
        for jtype in self:
            if jtype.scrap_product_id and jtype.scrap_product_id.tracking != 'none':
                raise ValidationError(_("The returnable scrap product cannot be tracked by lots or serial numbers."))

    def _compute_job_count(self):
        groups = dict(self.env['zivo.job.work']._read_group(
            [('job_type_id', 'in', self.ids)], ['job_type_id'], ['__count']))
        for jtype in self:
            jtype.job_count = groups.get(jtype, 0)
