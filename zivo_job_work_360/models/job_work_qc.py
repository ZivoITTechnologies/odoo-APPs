# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare


class ZivoJobWorkQC(models.Model):
    _name = 'zivo.job.work.qc'
    _description = 'Job Work Quality Inspection'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'
    _check_company_auto = True

    name = fields.Char('Inspection', required=True, readonly=True, copy=False, default=lambda self: _('New'))
    job_id = fields.Many2one('zivo.job.work', 'Job Work', required=True, ondelete='cascade', index=True,
                             check_company=True)
    production_id = fields.Many2one('zivo.job.work.production', 'Receipt', ondelete='cascade',
                                    domain="[('job_id', '=', job_id)]")
    company_id = fields.Many2one(related='job_id.company_id', store=True)
    partner_id = fields.Many2one(related='job_id.partner_id', store=True, string='Job Worker')
    product_id = fields.Many2one(related='job_id.product_id', store=True, string='Product')
    product_uom_id = fields.Many2one(related='job_id.product_uom_id', string='Unit')
    job_type_id = fields.Many2one(related='job_id.job_type_id', store=True)
    date = fields.Datetime('Inspection Date', default=fields.Datetime.now)
    inspector_id = fields.Many2one('res.users', 'Inspector', default=lambda self: self.env.user)
    qty_inspected = fields.Float('Inspected', digits='Product Unit', required=True)
    qty_accepted = fields.Float('Accepted', digits='Product Unit')
    qty_rejected = fields.Float('Rejected', digits='Product Unit')
    qty_rework = fields.Float('Rework', digits='Product Unit')
    qty_scrap = fields.Float('Scrap', digits='Product Unit')
    accepted_under_deviation = fields.Boolean(
        'Accepted Under Deviation', help="Output does not meet the specification but is accepted.")
    acceptance_rate = fields.Float('Acceptance Rate (%)', compute='_compute_rate', store=True, aggregator='avg')
    tolerance = fields.Float('Minimum Acceptance (%)', default=lambda self: self.env.company.jw_quality_tolerance)
    result = fields.Selection([('pass', 'Pass'), ('fail', 'Exception')], compute='_compute_rate', store=True)
    state = fields.Selection([('draft', 'To Inspect'), ('done', 'Done')],
                             default='draft', required=True, tracking=True, index=True)
    notes = fields.Html('Findings')
    attachment_ids = fields.Many2many('ir.attachment', string='Certificates / Reports')
    ncr_id = fields.Many2one('zivo.job.work.ncr', 'NCR', readonly=True)
    rework_job_id = fields.Many2one('zivo.job.work', 'Rework Job', readonly=True)

    @api.depends('qty_inspected', 'qty_accepted', 'tolerance')
    def _compute_rate(self):
        for qc in self:
            qc.acceptance_rate = qc.qty_inspected and qc.qty_accepted / qc.qty_inspected * 100.0 or 0.0
            qc.result = 'pass' if qc.acceptance_rate + 1e-6 >= qc.tolerance else 'fail'

    @api.constrains('qty_accepted', 'qty_rejected', 'qty_rework', 'qty_scrap', 'qty_inspected')
    def _check_qty(self):
        for qc in self:
            if min(qc.qty_accepted, qc.qty_rejected, qc.qty_rework, qc.qty_scrap, qc.qty_inspected) < 0:
                raise ValidationError(_("Inspection quantities cannot be negative."))

    @api.onchange('qty_rejected', 'qty_rework', 'qty_scrap')
    def _onchange_split(self):
        self.qty_accepted = max(self.qty_inspected - self.qty_rejected - self.qty_rework - self.qty_scrap, 0.0)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('zivo.job.work.qc') or _('New')
            if 'tolerance' not in vals and vals.get('job_id'):
                job = self.env['zivo.job.work'].browse(vals['job_id'])
                vals['tolerance'] = job.company_id.jw_quality_tolerance
        return super().create(vals_list)

    def action_done(self):
        for qc in self:
            if qc.state != 'draft':
                continue
            rounding = qc.product_uom_id.rounding
            total = qc.qty_accepted + qc.qty_rejected + qc.qty_rework + qc.qty_scrap
            if float_compare(total, qc.qty_inspected, precision_rounding=rounding) != 0:
                raise UserError(_(
                    "Accepted + rejected + rework + scrap (%(total)s) must equal the inspected quantity "
                    "(%(inspected)s).", total=total, inspected=qc.qty_inspected))
            job = qc.job_id
            scrap_location = job.company_id._jw_get_scrap_location()
            if qc.qty_rejected > 0:
                job._jw_post_move(job.product_id, qc.qty_rejected, job.product_uom_id, job.location_dest_id,
                                  scrap_location, 'reject', production=qc.production_id,
                                  description=_('%s - rejected output', job.name))
            if qc.qty_scrap > 0:
                job._jw_post_move(job.product_id, qc.qty_scrap, job.product_uom_id, job.location_dest_id,
                                  scrap_location, 'qc_scrap', production=qc.production_id,
                                  description=_('%s - scrapped output', job.name))
            vals = {'state': 'done', 'date': fields.Datetime.now()}
            if qc.qty_rework > 0:
                vals['rework_job_id'] = qc._create_rework_job().id
            qc.write(vals)
            if qc.production_id:
                qc.production_id.write({
                    'state': 'done',
                    'qty_accepted': qc.qty_accepted,
                    'qty_rejected': qc.qty_rejected,
                    'qty_rework': qc.qty_rework,
                    'qty_scrap': qc.qty_scrap,
                })
            if qc.result == 'fail':
                if job.company_id.jw_auto_ncr and not qc.ncr_id:
                    qc.ncr_id = self.env['zivo.job.work.ncr'].create({
                        'job_id': job.id,
                        'qc_id': qc.id,
                        'description': _(
                            "Acceptance rate %(rate).2f%% below the %(tol).2f%% tolerance: %(rej)s rejected, "
                            "%(rew)s rework, %(scr)s scrap out of %(insp)s inspected.",
                            rate=qc.acceptance_rate, tol=qc.tolerance, rej=qc.qty_rejected, rew=qc.qty_rework,
                            scr=qc.qty_scrap, insp=qc.qty_inspected),
                    })
                exc = self.env['zivo.job.work.exception'].sudo().create({
                    'job_id': job.id,
                    'exception_type': 'quality',
                    'severity': 'high',
                    'description': _("Inspection %(qc)s: acceptance %(rate).2f%% below %(tol).2f%%.",
                                     qc=qc.name, rate=qc.acceptance_rate, tol=qc.tolerance),
                    'ncr_id': qc.ncr_id.id,
                })
                if qc.ncr_id:
                    qc.ncr_id.exception_id = exc
            job.message_post(body=_(
                "Inspection %(qc)s done: %(acc)s accepted, %(rej)s rejected, %(rew)s rework, %(scr)s scrap.",
                qc=qc.name, acc=qc.qty_accepted, rej=qc.qty_rejected, rew=qc.qty_rework, scr=qc.qty_scrap))
        jobs = self.job_id
        jobs._jw_update_po_received()
        jobs._jw_update_state()
        return True

    def _create_rework_job(self):
        self.ensure_one()
        job = self.job_id
        rework_type = self.env['zivo.job.work.type'].search(
            [('is_rework', '=', True), ('company_id', 'in', (job.company_id.id, False))], limit=1)
        if not rework_type:
            raise UserError(_("Configure a job type flagged as 'Rework Type' to send output back for rework."))
        rework = self.env['zivo.job.work'].create({
            'parent_id': job.id,
            'company_id': job.company_id.id,
            'partner_id': job.partner_id.id,
            'principal_id': job.principal_id.id,
            'job_type_id': rework_type.id,
            'client_ref': job.client_ref,
            'product_id': job.product_id.id,
            'product_qty': self.qty_rework,
            'product_uom_id': job.product_uom_id.id,
            'warehouse_id': job.warehouse_id.id,
            'location_src_id': job.location_dest_id.id,
            'location_dest_id': job.location_dest_id.id,
            'user_id': job.user_id.id,
            'priority': '1',
            'rate': 0.0,
            'date_planned_start': fields.Datetime.now(),
            'date_expected': fields.Datetime.now() + timedelta(days=rework_type.lead_days or 3),
            'notes': _('<p>Rework of %(qty)s %(uom)s rejected at inspection %(qc)s.</p>',
                       qty=self.qty_rework, uom=job.product_uom_id.name, qc=self.name),
            'material_ids': [(0, 0, {
                'product_id': job.product_id.id,
                'product_uom_id': job.product_uom_id.id,
                'planned_qty': self.qty_rework,
                'scrap_pct': 0.0,
            })],
        })
        rework.action_plan()
        job.message_post(body=_("Rework job %s created.", rework._get_html_link()))
        return rework

    def action_open_job(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'res_model': 'zivo.job.work', 'res_id': self.job_id.id,
            'view_mode': 'form', 'views': [(False, 'form')]}
