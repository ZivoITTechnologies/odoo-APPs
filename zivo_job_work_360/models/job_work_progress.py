# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ZivoJobWorkProgress(models.Model):
    _name = 'zivo.job.work.progress'
    _description = 'Job Work Progress Report'
    _order = 'date desc, id desc'

    job_id = fields.Many2one('zivo.job.work', 'Job Work', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='job_id.company_id', store=True)
    partner_id = fields.Many2one(related='job_id.partner_id', store=True, string='Job Worker')
    product_uom_id = fields.Many2one(related='job_id.product_uom_id', string='Unit')
    date = fields.Datetime(default=fields.Datetime.now, required=True)
    qty_completed = fields.Float('Completed', digits='Product Unit')
    qty_scrap = fields.Float('Scrap', digits='Product Unit', help="Material scrapped during processing.")
    qty_rejected = fields.Float('Rejected', digits='Product Unit')
    qty_rework = fields.Float('Rework', digits='Product Unit')
    note = fields.Text('Comment')
    source = fields.Selection([('portal', 'Vendor Portal'), ('backend', 'Internal')], default='backend',
                              required=True)
    reported_by_id = fields.Many2one('res.users', 'Reported By', default=lambda self: self.env.user)

    @api.constrains('qty_completed', 'qty_scrap', 'qty_rejected', 'qty_rework')
    def _check_qty(self):
        for rec in self:
            if min(rec.qty_completed, rec.qty_scrap, rec.qty_rejected, rec.qty_rework) < 0:
                raise ValidationError(_("Reported quantities cannot be negative."))
