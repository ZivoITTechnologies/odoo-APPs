# -*- coding: utf-8 -*-
from odoo import fields, models

REASON_TYPES = [
    ('shortage', 'Shortage / Unaccounted'),
    ('scrap', 'Scrap'),
    ('loss', 'Loss / Waste'),
    ('quality', 'Quality'),
    ('other', 'Other'),
]

# exception type -> reason types that may explain it
EXCEPTION_REASON_TYPES = {
    'shortage': ['shortage', 'loss', 'other'],
    'excess_scrap': ['scrap', 'loss', 'other'],
    'material_variance': ['scrap', 'loss', 'shortage', 'other'],
    'low_yield': ['scrap', 'quality', 'other'],
    'quality': ['quality', 'other'],
    'aging': ['shortage', 'other'],
}


class ZivoJobWorkReason(models.Model):
    _name = 'zivo.job.work.reason'
    _description = 'Job Work Reason Code'
    _order = 'sequence, name'

    name = fields.Char('Reason', required=True, translate=True)
    code = fields.Char('Code', required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    reason_type = fields.Selection(REASON_TYPES, 'Applies To', required=True, default='other')
    requires_approval = fields.Boolean(
        'Requires Approval', default=True,
        help="Resolutions and write-offs using this reason must be approved by a Job Work Administrator.")
    recover_from_vendor = fields.Boolean(
        'Recover From Job Worker', help="Default for write-offs: the value is to be debited to the job worker.")
    company_id = fields.Many2one('res.company', 'Company')
    description = fields.Text('Guidance', translate=True)

    _code_company_uniq = models.Constraint(
        'unique(code, company_id)', 'The reason code must be unique per company.')

    def _compute_display_name(self):
        for reason in self:
            reason.display_name = '[%s] %s' % (reason.code, reason.name) if reason.code else reason.name
