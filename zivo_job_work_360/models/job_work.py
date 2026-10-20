# -*- coding: utf-8 -*-
from collections import defaultdict
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, float_is_zero, float_round
from odoo.tools.misc import format_date, formatLang

from .job_work_material import jw_allowed_uoms

JOB_STATES = [
    ('draft', 'Draft'),
    ('planned', 'Planned'),
    ('ready', 'Material Ready'),
    ('issued', 'Material Issued / In Transit'),
    ('at_vendor', 'At Subcontractor'),
    ('processing', 'Processing'),
    ('qc_pending', 'QC Pending'),
    ('completed', 'Completed'),
    ('reconciled', 'Reconciled'),
    ('closed', 'Closed'),
    ('hold', 'On Hold'),
    ('cancel', 'Cancelled'),
]
# States in which the job worker is (or will be) working on the job
ACTIVE_STATES = ('planned', 'ready', 'issued', 'at_vendor', 'processing', 'qc_pending')
# States in which output can be received
RECEIVABLE_STATES = ('issued', 'at_vendor', 'processing', 'qc_pending', 'completed')

VENDOR_STATES = [
    ('new', 'Awaiting Acceptance'),
    ('accepted', 'Accepted'),
    ('rejected', 'Rejected'),
    ('received', 'Material Receipt Confirmed'),
    ('processing', 'Processing'),
    ('completed', 'Completion Submitted'),
]


class ZivoJobWork(models.Model):
    _name = 'zivo.job.work'
    _description = 'Job Work Order'
    _inherit = ['portal.mixin', 'mail.thread', 'mail.activity.mixin']
    _order = 'priority desc, date_expected asc, id desc'
    _check_company_auto = True

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    name = fields.Char(
        'Job Work Number', required=True, copy=False, readonly=True, index='trigram',
        default=lambda self: _('New'))
    company_id = fields.Many2one(
        'res.company', 'Company', required=True, index=True, default=lambda self: self.env.company)
    principal_id = fields.Many2one(
        'res.partner', 'Principal / Customer', required=True, tracking=True,
        default=lambda self: self.env.company.partner_id,
        help="Owner of the material. Defaults to your company; set the customer for customer-owned material.")
    partner_id = fields.Many2one(
        'res.partner', 'Job Worker', required=True, tracking=True, index=True,
        domain="[('is_company', '=', True)]", check_company=True)
    job_type_id = fields.Many2one('zivo.job.work.type', 'Job Type', required=True, tracking=True, check_company=True)
    is_rework = fields.Boolean(related='job_type_id.is_rework', store=True)
    client_ref = fields.Char('Reference', help="Sales order, manufacturing order, project or customer reference.")
    priority = fields.Selection(
        [('0', 'Normal'), ('1', 'High'), ('2', 'Urgent')], 'Priority', default='0', index=True)
    date_planned_start = fields.Datetime(
        'Planned Start', required=True, default=fields.Datetime.now, tracking=True)
    date_expected = fields.Datetime(
        'Expected Completion', required=True, tracking=True, index=True,
        default=lambda self: fields.Datetime.now() + timedelta(days=7))
    date_issued = fields.Datetime('First Issue', compute='_compute_material_summary', store=True)
    date_completed = fields.Datetime('Completed On', readonly=True, copy=False)
    date_closed = fields.Datetime('Closed On', readonly=True, copy=False)
    user_id = fields.Many2one(
        'res.users', 'Responsible', required=True, tracking=True, default=lambda self: self.env.user,
        domain="[('share', '=', False)]")
    state = fields.Selection(JOB_STATES, 'Status', default='draft', required=True, copy=False,
                             tracking=True, index=True)
    hold_state = fields.Char(copy=False)
    notes = fields.Html('Instructions')
    shared_attachment_ids = fields.Many2many(
        'ir.attachment', 'zivo_job_work_shared_attachment_rel', 'job_id', 'attachment_id',
        string='Documents for Job Worker', help="Drawings and documents visible to the job worker on the portal.")
    internal_note = fields.Html('Internal Notes')

    # Output
    product_id = fields.Many2one(
        'product.product', 'Output Product', required=True, tracking=True, check_company=True,
        domain="[('type', '=', 'consu')]")
    product_tracking = fields.Selection(related='product_id.tracking')
    product_qty = fields.Float('Planned Output', required=True, default=1.0, digits='Product Unit', tracking=True)
    allowed_uom_ids = fields.Many2many('uom.uom', compute='_compute_allowed_uom_ids')
    product_uom_id = fields.Many2one(
        'uom.uom', 'Unit', required=True, compute='_compute_product_uom_id', store=True, readonly=False,
        precompute=True, domain="[('id', 'in', allowed_uom_ids)]")

    # Locations
    warehouse_id = fields.Many2one(
        'stock.warehouse', 'Warehouse', required=True, check_company=True,
        default=lambda self: self.env['stock.warehouse'].search(
            [('company_id', '=', self.env.company.id)], limit=1))
    location_src_id = fields.Many2one(
        'stock.location', 'Issue From', compute='_compute_locations', store=True, readonly=False,
        precompute=True, check_company=True, domain="[('usage', '=', 'internal')]")
    location_dest_id = fields.Many2one(
        'stock.location', 'Receive Into', compute='_compute_locations', store=True, readonly=False,
        precompute=True, check_company=True, domain="[('usage', '=', 'internal')]")
    vendor_location_id = fields.Many2one(
        'stock.location', 'Job Worker Location', readonly=True, copy=False,
        help="Custody location representing the material physically at the job worker.")

    # Commercial
    currency_id = fields.Many2one(
        'res.currency', 'Currency', required=True, compute='_compute_currency_id', store=True,
        readonly=False, precompute=True)
    company_currency_id = fields.Many2one(related='company_id.currency_id', string='Company Currency')
    service_product_id = fields.Many2one(
        'product.product', 'Processing Service', compute='_compute_service_product_id', store=True,
        readonly=False, precompute=True, domain="[('type', '=', 'service')]")
    rate = fields.Monetary('Job Work Rate', currency_field='currency_id', tracking=True,
                           help="Approved processing charge per unit of output.")
    purchase_line_ids = fields.One2many('purchase.order.line', 'jw_order_id', string='Purchase Lines',
                                        groups='purchase.group_purchase_user')
    purchase_order_ids = fields.Many2many('purchase.order', compute='_compute_purchase', string='Purchase Orders',
                                          compute_sudo=True, groups='purchase.group_purchase_user')
    purchase_count = fields.Integer(compute='_compute_purchase', compute_sudo=True)
    bill_count = fields.Integer(compute='_compute_purchase', compute_sudo=True)

    # Links
    material_ids = fields.One2many('zivo.job.work.material', 'job_id', 'Materials', copy=True)
    production_ids = fields.One2many('zivo.job.work.production', 'job_id', 'Receipts')
    progress_ids = fields.One2many('zivo.job.work.progress', 'job_id', 'Progress Reports')
    qc_ids = fields.One2many('zivo.job.work.qc', 'job_id', 'Inspections')
    ncr_ids = fields.One2many('zivo.job.work.ncr', 'job_id', 'NCRs')
    exception_ids = fields.One2many('zivo.job.work.exception', 'job_id', 'Exceptions')
    request_ids = fields.One2many('zivo.job.work.request', 'job_id', 'Material Requests')
    loss_ids = fields.One2many('zivo.job.work.loss', 'job_id', 'Loss Requests')
    cost_line_ids = fields.One2many('zivo.job.work.cost', 'job_id', 'Additional Costs', copy=True)
    picking_ids = fields.One2many('stock.picking', 'jw_order_id', 'Transfers', groups='stock.group_stock_user')
    picking_count = fields.Integer(compute='_compute_picking_count', compute_sudo=True)
    qc_count = fields.Integer(compute='_compute_counts')
    ncr_count = fields.Integer(compute='_compute_counts')
    request_count = fields.Integer(compute='_compute_counts')
    child_count = fields.Integer(compute='_compute_counts')
    open_exception_count = fields.Integer(compute='_compute_exception_flags', store=True)
    has_blocking_exception = fields.Boolean(compute='_compute_exception_flags', store=True)

    # Multi-stage
    parent_id = fields.Many2one('zivo.job.work', 'Previous Stage', index=True, copy=False, check_company=True)
    child_ids = fields.One2many('zivo.job.work', 'parent_id', 'Next Stages / Rework')
    root_id = fields.Many2one('zivo.job.work', 'Job Chain', compute='_compute_root', store=True, index=True,
                              recursive=True)
    stage_sequence = fields.Integer('Stage No.', compute='_compute_root', store=True, recursive=True)
    mrp_production_id = fields.Many2one('mrp.production', 'Manufacturing Order', copy=False, check_company=True)

    # Vendor collaboration
    vendor_state = fields.Selection(VENDOR_STATES, 'Job Worker Status', default='new', copy=False, tracking=True)
    vendor_accept_date = fields.Datetime('Accepted On', copy=False, readonly=True)
    vendor_receipt_date = fields.Datetime('Material Received On', copy=False, readonly=True)
    vendor_start_date = fields.Datetime('Processing Started On', copy=False, readonly=True)
    vendor_complete_date = fields.Datetime('Completion Submitted On', copy=False, readonly=True)
    vendor_reject_reason = fields.Text('Rejection Reason', copy=False, readonly=True)
    date_sent = fields.Datetime('Sent To Job Worker', copy=False, readonly=True)
    vendor_reported_qty = fields.Float('Reported Completed', compute='_compute_vendor_reported',
                                       digits='Product Unit')
    vendor_reported_scrap = fields.Float('Reported Scrap', compute='_compute_vendor_reported',
                                         digits='Product Unit')

    # Output quantities
    qty_received = fields.Float('Received', compute='_compute_output_qty', store=True, digits='Product Unit')
    qty_accepted = fields.Float('Accepted', compute='_compute_output_qty', store=True, digits='Product Unit')
    qty_rejected = fields.Float('Rejected', compute='_compute_output_qty', store=True, digits='Product Unit')
    qty_rework = fields.Float('Rework', compute='_compute_output_qty', store=True, digits='Product Unit')
    qty_qc_scrap = fields.Float('Output Scrap', compute='_compute_output_qty', store=True, digits='Product Unit')
    qty_pending_qc = fields.Float('Pending QC', compute='_compute_output_qty', store=True, digits='Product Unit')
    qty_remaining = fields.Float('Remaining', compute='_compute_output_qty', store=True, digits='Product Unit')
    acceptance_rate = fields.Float('Acceptance Rate (%)', compute='_compute_output_qty', store=True,
                                   aggregator='avg')
    expected_yield = fields.Float('Expected Yield (%)', compute='_compute_expected_yield', store=True,
                                  readonly=False, precompute=True, aggregator='avg')
    actual_yield = fields.Float('Actual Yield (%)', compute='_compute_yield', store=True, aggregator='avg')
    yield_variance = fields.Float('Yield Variance (pts)', compute='_compute_yield', store=True, aggregator='avg')
    progress = fields.Float('Progress (%)', compute='_compute_output_qty', store=True, aggregator='avg')

    # Material summary
    has_material_to_issue = fields.Boolean(compute='_compute_material_flags', compute_sudo=True)
    has_material_balance = fields.Boolean(compute='_compute_material_flags', compute_sudo=True)
    material_issued_value = fields.Monetary('Material Issued Value', compute='_compute_material_summary',
                                            store=True, currency_field='company_currency_id')
    material_balance_value = fields.Monetary('Material at Job Worker', compute='_compute_material_summary',
                                             store=True, currency_field='company_currency_id')
    material_scrap_value = fields.Monetary('Scrap & Loss Value', compute='_compute_material_summary',
                                           store=True, currency_field='company_currency_id')
    scrap_rate = fields.Float('Scrap Rate (%)', compute='_compute_material_summary', store=True, aggregator='avg')
    reconciliation_state = fields.Selection(
        [('pending', 'Pending'), ('balanced', 'Balanced'), ('exception', 'Exception')],
        'Reconciliation', default='pending', copy=False, tracking=True)
    is_overdue = fields.Boolean('Overdue', compute='_compute_is_overdue', search='_search_is_overdue')
    is_disputed = fields.Boolean('Disputed', copy=False, tracking=True)
    cycle_time_days = fields.Float('Cycle Time (days)', compute='_compute_cycle_time', store=True,
                                   aggregator='avg')

    # Costing (company currency)
    cost_exp_material = fields.Monetary(compute_sudo=True, string='Expected Raw Material', compute='_compute_costs',
                                        currency_field='company_currency_id')
    cost_act_material = fields.Monetary(compute_sudo=True, string='Actual Raw Material', compute='_compute_costs',
                                        currency_field='company_currency_id')
    cost_exp_processing = fields.Monetary(compute_sudo=True, string='Expected Job Work', compute='_compute_costs',
                                          currency_field='company_currency_id')
    cost_act_processing = fields.Monetary(compute_sudo=True, string='Actual Job Work', compute='_compute_costs',
                                          currency_field='company_currency_id')
    cost_exp_scrap = fields.Monetary(compute_sudo=True, string='Expected Scrap Impact', compute='_compute_costs',
                                     currency_field='company_currency_id')
    cost_act_scrap = fields.Monetary(compute_sudo=True, string='Actual Scrap Impact', compute='_compute_costs',
                                     currency_field='company_currency_id')
    cost_exp_freight = fields.Monetary(compute_sudo=True, string='Expected Freight', compute='_compute_costs',
                                       currency_field='company_currency_id')
    cost_act_freight = fields.Monetary(compute_sudo=True, string='Actual Freight', compute='_compute_costs',
                                       currency_field='company_currency_id')
    cost_exp_inspection = fields.Monetary(compute_sudo=True, string='Expected Inspection', compute='_compute_costs',
                                          currency_field='company_currency_id')
    cost_act_inspection = fields.Monetary(compute_sudo=True, string='Actual Inspection', compute='_compute_costs',
                                          currency_field='company_currency_id')
    cost_exp_other = fields.Monetary(compute_sudo=True, string='Expected Other', compute='_compute_costs',
                                     currency_field='company_currency_id')
    cost_act_other = fields.Monetary(compute_sudo=True, string='Actual Other', compute='_compute_costs',
                                     currency_field='company_currency_id')
    cost_exp_total = fields.Monetary(compute_sudo=True, string='Expected Total', compute='_compute_costs',
                                     currency_field='company_currency_id')
    cost_act_total = fields.Monetary(compute_sudo=True, string='Actual Total', compute='_compute_costs',
                                     currency_field='company_currency_id')
    cost_variance = fields.Monetary(compute_sudo=True, string='Cost Variance', compute='_compute_costs',
                                    currency_field='company_currency_id')
    cost_variance_pct = fields.Float('Cost Variance (%)', compute='_compute_costs', compute_sudo=True)
    cost_per_unit = fields.Monetary(compute_sudo=True, string='Actual Cost / Accepted Unit', compute='_compute_costs',
                                    currency_field='company_currency_id')
    cost_processing_source = fields.Selection(
        [('plan', 'Plan'), ('accrual', 'Accrued at job rate'), ('bill', 'Vendor bills')],
        compute='_compute_costs', string='Job Work Actual Source', compute_sudo=True)
    cost_snapshot_expected = fields.Monetary('Expected Cost (at reconciliation)', readonly=True, copy=False,
                                             currency_field='company_currency_id')
    cost_snapshot_actual = fields.Monetary('Actual Cost (at reconciliation)', readonly=True, copy=False,
                                           currency_field='company_currency_id')
    cost_snapshot_variance_pct = fields.Float('Cost Variance % (at reconciliation)', readonly=True, copy=False,
                                              aggregator='avg')

    _qty_positive = models.Constraint('CHECK(product_qty > 0)', 'The planned output must be positive.')

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('product_id')
    def _compute_allowed_uom_ids(self):
        for job in self:
            job.allowed_uom_ids = jw_allowed_uoms(job.product_id)

    @api.constrains('product_id', 'product_uom_id')
    def _check_product_uom(self):
        for job in self:
            if job.product_id and job.product_uom_id not in jw_allowed_uoms(job.product_id):
                raise ValidationError(_("The unit %(uom)s cannot be used for %(product)s.",
                                        uom=job.product_uom_id.name, product=job.product_id.display_name))

    @api.depends('product_id')
    def _compute_product_uom_id(self):
        for job in self:
            if job.product_id and (not job.product_uom_id or job.state == 'draft'):
                job.product_uom_id = job.product_id.uom_id

    @api.depends('warehouse_id')
    def _compute_locations(self):
        for job in self:
            stock = job.warehouse_id.lot_stock_id
            if not job.location_src_id or job.location_src_id.warehouse_id != job.warehouse_id:
                job.location_src_id = stock
            if not job.location_dest_id or job.location_dest_id.warehouse_id != job.warehouse_id:
                job.location_dest_id = stock

    @api.depends('partner_id', 'company_id')
    def _compute_currency_id(self):
        for job in self:
            currency = job.partner_id.with_company(job.company_id).property_purchase_currency_id
            job.currency_id = currency or job.company_id.currency_id or self.env.company.currency_id

    @api.depends('job_type_id')
    def _compute_service_product_id(self):
        default = self.env.ref('zivo_job_work_360.product_job_work_service', raise_if_not_found=False)
        for job in self:
            job.service_product_id = job.job_type_id.service_product_id or job.service_product_id or default

    @api.depends('job_type_id')
    def _compute_expected_yield(self):
        for job in self:
            if job.job_type_id and job.state in ('draft', 'planned', False):
                job.expected_yield = job.job_type_id.expected_yield
            elif not job.expected_yield:
                job.expected_yield = 100.0

    @api.depends('parent_id', 'parent_id.root_id', 'parent_id.stage_sequence')
    def _compute_root(self):
        for job in self:
            if job.parent_id:
                job.root_id = job.parent_id.root_id or job.parent_id
                job.stage_sequence = (job.parent_id.stage_sequence or 1) + 1
            else:
                job.root_id = job if isinstance(job.id, int) else False
                job.stage_sequence = 1

    @api.depends('purchase_line_ids.order_id.state', 'purchase_line_ids.invoice_lines.move_id.state')
    def _compute_purchase(self):
        for job in self:
            orders = job.purchase_line_ids.order_id.filtered(lambda o: o.state != 'cancel')
            job.purchase_order_ids = orders
            job.purchase_count = len(orders)
            job.bill_count = len(job.purchase_line_ids.invoice_lines.move_id)

    def _compute_picking_count(self):
        for job in self:
            job.picking_count = len(job.picking_ids)

    def _compute_counts(self):
        for job in self:
            job.qc_count = len(job.qc_ids)
            job.ncr_count = len(job.ncr_ids)
            job.request_count = len(job.request_ids)
            job.child_count = len(job.child_ids)

    @api.depends('exception_ids.state', 'exception_ids.severity')
    def _compute_exception_flags(self):
        for job in self:
            open_exc = job.exception_ids.filtered(lambda e: e.state == 'open')
            job.open_exception_count = len(open_exc)
            job.has_blocking_exception = any(e.severity == 'high' for e in open_exc)

    @api.depends('progress_ids.qty_completed', 'progress_ids.qty_scrap')
    def _compute_vendor_reported(self):
        for job in self:
            job.vendor_reported_qty = sum(job.progress_ids.mapped('qty_completed'))
            job.vendor_reported_scrap = sum(job.progress_ids.mapped('qty_scrap'))

    @api.depends('production_ids.state', 'production_ids.qty_received', 'production_ids.qty_accepted',
                 'production_ids.qty_rejected', 'production_ids.qty_rework', 'production_ids.qty_scrap',
                 'product_qty')
    def _compute_output_qty(self):
        for job in self:
            prods = job.production_ids.filtered(lambda p: p.state in ('qc_pending', 'done'))
            done = prods.filtered(lambda p: p.state == 'done')
            job.qty_received = sum(prods.mapped('qty_received'))
            job.qty_accepted = sum(done.mapped('qty_accepted'))
            job.qty_rejected = sum(done.mapped('qty_rejected'))
            job.qty_rework = sum(done.mapped('qty_rework'))
            job.qty_qc_scrap = sum(done.mapped('qty_scrap'))
            job.qty_pending_qc = sum((prods - done).mapped('qty_received'))
            job.qty_remaining = max(job.product_qty - job.qty_received, 0.0)
            inspected = sum(done.mapped('qty_received'))
            job.acceptance_rate = inspected and job.qty_accepted / inspected * 100.0 or 0.0
            job.progress = job.product_qty and min(job.qty_received / job.product_qty * 100.0, 100.0) or 0.0

    @api.depends('material_ids.qty_issued', 'material_ids.qty_consumed', 'material_ids.qty_scrap',
                 'material_ids.qty_loss', 'material_ids.qty_returned', 'material_ids.unit_cost',
                 'material_ids.date_first_issue', 'material_ids.balance_value')
    def _compute_material_summary(self):
        for job in self:
            mats = job.material_ids
            issued_value = sum(m.qty_issued * m.unit_cost for m in mats)
            scrap_value = sum((m.qty_scrap + m.qty_loss) * m.unit_cost for m in mats)
            job.material_issued_value = issued_value
            job.material_balance_value = sum(mats.mapped('balance_value'))
            job.material_scrap_value = scrap_value
            job.scrap_rate = issued_value and scrap_value / issued_value * 100.0 or 0.0
            dates = [d for d in mats.mapped('date_first_issue') if d]
            job.date_issued = min(dates) if dates else False

    @api.depends('material_ids.planned_qty', 'material_ids.qty_issued', 'material_ids.balance_qty')
    def _compute_material_flags(self):
        for job in self:
            job.has_material_balance = any(m.balance_qty > 0 for m in job.material_ids)
            job.has_material_to_issue = any(
                m.planned_qty - m.qty_issued - job._jw_open_transfer_qty(m) > m.product_uom_id.rounding / 2.0
                for m in job.material_ids)

    @api.depends('qty_accepted', 'material_ids.qty_consumed', 'material_ids.qty_scrap',
                 'material_ids.qty_loss', 'material_ids.std_qty_per_unit', 'expected_yield')
    def _compute_yield(self):
        for job in self:
            equivalents = [
                (m.qty_consumed + m.qty_scrap + m.qty_loss) / m.std_qty_per_unit
                for m in job.material_ids if m.std_qty_per_unit > 0
            ]
            input_equivalent = max(equivalents) if equivalents else 0.0
            if input_equivalent > 0:
                job.actual_yield = min(job.qty_accepted / input_equivalent * 100.0, 999.0)
                job.yield_variance = job.actual_yield - job.expected_yield
            else:
                job.actual_yield = 0.0
                job.yield_variance = 0.0

    @api.depends('date_issued', 'date_completed')
    def _compute_cycle_time(self):
        for job in self:
            if job.date_issued and job.date_completed:
                delta = job.date_completed - job.date_issued
                job.cycle_time_days = max(delta.total_seconds() / 86400.0, 0.0)
            else:
                job.cycle_time_days = 0.0

    def _compute_is_overdue(self):
        now = fields.Datetime.now()
        for job in self:
            job.is_overdue = bool(job.date_expected and job.date_expected < now and job.state in ACTIVE_STATES)

    def _search_is_overdue(self, operator, value):
        if operator not in ('=', '!=', 'in', 'not in'):
            return NotImplemented
        if operator in ('in', 'not in'):
            positive = (True in value) == (operator == 'in')
        else:
            positive = bool(value) == (operator == '=')
        domain = [('date_expected', '<', fields.Datetime.now()), ('state', 'in', list(ACTIVE_STATES))]
        if positive:
            return domain
        return ['|', ('date_expected', '>=', fields.Datetime.now()), ('state', 'not in', list(ACTIVE_STATES))]

    def _jw_to_company_currency(self, amount, currency, date=None):
        self.ensure_one()
        company_currency = self.company_id.currency_id
        if not currency or currency == company_currency:
            return amount
        return currency._convert(amount, company_currency, self.company_id, date or fields.Date.context_today(self))

    @api.depends('material_ids', 'material_ids.planned_qty', 'material_ids.unit_cost',
                 'material_ids.qty_consumed', 'material_ids.qty_scrap', 'material_ids.qty_loss',
                 'material_ids.scrap_pct', 'rate', 'product_qty', 'qty_accepted', 'qty_received',
                 'cost_line_ids.expected_amount', 'cost_line_ids.actual_amount', 'cost_line_ids.cost_type',
                 'purchase_line_ids.invoice_lines.parent_state', 'currency_id')
    def _compute_costs(self):
        for job in self:
            # Scale the standard to the received output once output is known
            basis = job.qty_received if job.qty_received > 0 else job.product_qty
            factor = job.product_qty and basis / job.product_qty or 1.0
            exp_mat = exp_scrap = act_mat = act_scrap = 0.0
            for mat in job.material_ids:
                exp_mat += mat.planned_qty * (1 - mat.scrap_pct / 100.0) * factor * mat.unit_cost
                exp_scrap += mat.planned_qty * (mat.scrap_pct / 100.0) * factor * mat.unit_cost
                act_mat += mat.qty_consumed * mat.unit_cost
                act_scrap += (mat.qty_scrap + mat.qty_loss) * mat.unit_cost
            exp_proc = job._jw_to_company_currency(job.rate * basis, job.currency_id)
            bill_lines = job.purchase_line_ids.invoice_lines.filtered(
                lambda l: l.parent_state == 'posted' and l.display_type == 'product')
            if bill_lines:
                act_proc = sum(
                    l.balance for l in bill_lines)  # vendor bills are debit (positive), refunds negative
                source = 'bill'
            elif job.qty_accepted:
                act_proc = job._jw_to_company_currency(job.rate * job.qty_accepted, job.currency_id)
                source = 'accrual'
            else:
                act_proc = 0.0
                source = 'plan'
            sums = defaultdict(lambda: [0.0, 0.0])
            for line in job.cost_line_ids:
                sums[line.cost_type][0] += line.expected_amount_company
                sums[line.cost_type][1] += line.actual_amount_company
            job.cost_exp_material = exp_mat
            job.cost_act_material = act_mat
            job.cost_exp_scrap = exp_scrap
            job.cost_act_scrap = act_scrap
            job.cost_exp_processing = exp_proc
            job.cost_act_processing = act_proc
            job.cost_processing_source = source
            job.cost_exp_freight, job.cost_act_freight = sums['freight']
            job.cost_exp_inspection, job.cost_act_inspection = sums['inspection']
            other_exp = sums['other'][0] + sums['rework'][0]
            other_act = sums['other'][1] + sums['rework'][1]
            job.cost_exp_other = other_exp
            job.cost_act_other = other_act
            job.cost_exp_total = exp_mat + exp_scrap + exp_proc + sums['freight'][0] + sums['inspection'][0] + other_exp
            job.cost_act_total = act_mat + act_scrap + act_proc + sums['freight'][1] + sums['inspection'][1] + other_act
            job.cost_variance = job.cost_act_total - job.cost_exp_total
            job.cost_variance_pct = job.cost_exp_total and job.cost_variance / job.cost_exp_total * 100.0 or 0.0
            job.cost_per_unit = job.qty_accepted and job.cost_act_total / job.qty_accepted or 0.0

    def _compute_access_url(self):
        super()._compute_access_url()
        for job in self:
            job.access_url = '/my/job-works/%s' % job.id

    def action_preview(self):
        """Open the job as the job worker sees it on the portal."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'target': 'new',
            'url': self.get_portal_url(),
        }

    def _get_report_base_filename(self):
        self.ensure_one()
        return 'Job Work - %s' % self.name.replace('/', '-')

    # ------------------------------------------------------------------
    # Onchange / constraints
    # ------------------------------------------------------------------
    @api.onchange('job_type_id', 'date_planned_start')
    def _onchange_job_type_dates(self):
        if self.job_type_id and self.date_planned_start and self.state == 'draft':
            self.date_expected = self.date_planned_start + timedelta(days=self.job_type_id.lead_days or 0)
            if self.job_type_id.instructions and not self.notes:
                self.notes = self.job_type_id.instructions
            for mat in self.material_ids:
                mat.scrap_pct = self.job_type_id.expected_scrap_pct

    @api.onchange('partner_id', 'service_product_id', 'product_qty')
    def _onchange_rate(self):
        if self.partner_id and self.service_product_id and not self.rate:
            seller = self.service_product_id.with_company(self.company_id)._select_seller(
                partner_id=self.partner_id, quantity=self.product_qty or 1.0,
                date=fields.Date.context_today(self), uom_id=self.service_product_id.uom_id)
            if seller:
                price = seller.price
                if seller.currency_id and seller.currency_id != self.currency_id:
                    price = seller.currency_id._convert(
                        price, self.currency_id, self.company_id, fields.Date.context_today(self))
                self.rate = price

    @api.constrains('date_planned_start', 'date_expected')
    def _check_dates(self):
        for job in self:
            if job.date_expected and job.date_planned_start and job.date_expected < job.date_planned_start:
                raise ValidationError(_("The expected completion cannot be before the planned start."))

    @api.constrains('parent_id')
    def _check_parent(self):
        if self._has_cycle():
            raise ValidationError(_("A job work chain cannot loop on itself."))

    # ------------------------------------------------------------------
    # ORM
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals.get('name') == _('New'):
                company = self.env['res.company'].browse(vals.get('company_id')) or self.env.company
                vals['name'] = self.env['ir.sequence'].with_company(company).next_by_code(
                    'zivo.job.work') or _('New')
        jobs = super().create(vals_list)
        for job in jobs.filtered(lambda j: not j.root_id):
            job.root_id = job
        return jobs

    def write(self, vals):
        locked_fields = {'partner_id', 'product_id', 'company_id', 'warehouse_id'}
        if locked_fields & set(vals):
            for job in self:
                if job.state not in ('draft', 'planned') and any(
                        job[f].id != vals[f] for f in locked_fields & set(vals) if f in vals):
                    raise UserError(_(
                        "You cannot change the job worker, output product, warehouse or company of %s once "
                        "material movements have started.", job.name))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_started(self):
        for job in self:
            if job.state not in ('draft', 'cancel'):
                raise UserError(_("Only draft or cancelled job work orders can be deleted (%s).", job.name))

    def copy_data(self, default=None):
        vals_list = super().copy_data(default=default)
        for vals in vals_list:
            vals.setdefault('date_planned_start', fields.Datetime.now())
            vals.setdefault('date_expected', fields.Datetime.now() + timedelta(days=7))
        return vals_list

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _jw_ensure_state(self, states, action_label):
        for job in self:
            if job.state not in states:
                raise UserError(_(
                    "%(action)s is not possible on %(job)s in status '%(state)s'.",
                    action=action_label, job=job.name,
                    state=dict(self._fields['state']._description_selection(self.env)).get(job.state)))

    def _jw_get_vendor_location(self):
        self.ensure_one()
        if not self.vendor_location_id:
            self.vendor_location_id = self.partner_id._jw_get_vendor_location(self.company_id)
        return self.vendor_location_id

    def _jw_issue_picking_type(self):
        self.ensure_one()
        wh = self.warehouse_id
        return wh.subcontracting_resupply_type_id or wh.int_type_id

    def _jw_receipt_picking_type(self):
        self.ensure_one()
        return self.warehouse_id.in_type_id

    def _jw_post_move(self, product, qty, uom, location, location_dest, move_type,
                      material=False, production=False, description=False):
        """Create and validate a stock move used for job work postings (consumption, scrap, loss...).

        Quantities taken from internal locations must be available there; lots/serials are picked by
        the standard reservation mechanism.
        """
        self.ensure_one()
        if float_is_zero(qty, precision_rounding=uom.rounding):
            return self.env['stock.move']
        move = self.env['stock.move'].sudo().create({
            'description_picking': description or '%s - %s' % (self.name, product.display_name),
            'origin': self.name,
            'company_id': self.company_id.id,
            'product_id': product.id,
            'product_uom': uom.id,
            'product_uom_qty': qty,
            'location_id': location.id,
            'location_dest_id': location_dest.id,
            'partner_id': self.partner_id.id,
            'jw_material_id': material and material.id or False,
            'jw_production_id': production and production.id or False,
            'jw_move_type': move_type,
        })
        move._action_confirm()
        if location.usage == 'internal':
            move._action_assign()
            if float_compare(move.quantity, qty, precision_rounding=uom.rounding) < 0:
                raise UserError(_(
                    "Only %(available)s %(uom)s of %(product)s is available at %(location)s; %(qty)s %(uom)s "
                    "is required for job work %(job)s.",
                    available=float_round(move.quantity, precision_rounding=uom.rounding), uom=uom.name,
                    product=product.display_name, location=location.display_name, qty=qty, job=self.name))
        else:
            move.quantity = qty
        move.picked = True
        move._action_done()
        if move.state != 'done':
            raise UserError(_("The stock move for %s could not be validated.", product.display_name))
        return move

    def _jw_create_picking(self, picking_type, location, location_dest, lines, move_type):
        """Create a picking. ``lines`` is a list of (material_or_False, product, qty, uom)."""
        self.ensure_one()
        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'location_id': location.id,
            'location_dest_id': location_dest.id,
            'partner_id': self.partner_id.id,
            'origin': self.name,
            'company_id': self.company_id.id,
            'jw_order_id': self.id,
            'scheduled_date': fields.Datetime.now(),
            'move_ids': [(0, 0, {
                'product_id': product.id,
                'product_uom': uom.id,
                'product_uom_qty': qty,
                'location_id': location.id,
                'location_dest_id': location_dest.id,
                'company_id': self.company_id.id,
                'partner_id': self.partner_id.id,
                'jw_material_id': material and material.id or False,
                'jw_move_type': move_type,
                'origin': self.name,
            }) for material, product, qty, uom in lines],
        })
        picking.action_confirm()
        picking.action_assign()
        return picking

    def _jw_validate_picking(self, picking):
        """Validate a picking created by Job Work when everything is reserved."""
        for move in picking.move_ids:
            if float_compare(move.quantity, move.product_uom_qty, precision_rounding=move.product_uom.rounding) < 0:
                if move.location_id.usage != 'internal' and move.product_id.tracking == 'none':
                    move.quantity = move.product_uom_qty
                else:
                    raise UserError(_(
                        "Not enough %(product)s available at %(location)s to validate %(picking)s.",
                        product=move.product_id.display_name, location=move.location_id.display_name,
                        picking=picking.name))
        picking.move_ids.picked = True
        res = picking.with_context(skip_backorder=True, cancel_backorder=True,
                                   skip_sms=True, skip_expired=True).button_validate()
        if picking.state != 'done':
            raise UserError(_("Transfer %s could not be validated automatically.", picking.name))
        return res

    def _jw_open_transfer_qty(self, material):
        """Quantity of a material in issue transfers that are not done yet."""
        moves = material.move_ids.filtered(
            lambda m: m.jw_move_type == 'issue' and m.state not in ('done', 'cancel'))
        return sum(m.product_uom._compute_quantity(m.product_uom_qty, material.product_uom_id) for m in moves)

    def _jw_update_state(self):
        """Recompute the operational state after receipts and inspections."""
        for job in self:
            if job.state in ('draft', 'planned', 'reconciled', 'closed', 'cancel', 'hold'):
                continue
            rounding = job.product_uom_id.rounding
            processed = job.qty_accepted + job.qty_rejected + job.qty_rework + job.qty_qc_scrap
            if job.qty_pending_qc > 0 or job.production_ids.filtered(lambda p: p.state == 'waiting'):
                new_state = 'qc_pending' if job.qty_pending_qc > 0 else job.state
            elif float_compare(processed, job.product_qty, precision_rounding=rounding) >= 0 or (
                    job.production_ids.filtered(lambda p: p.is_final) and processed > 0):
                new_state = 'completed'
            elif job.production_ids:
                new_state = 'processing'
            else:
                new_state = job.state
            if new_state != job.state:
                vals = {'state': new_state}
                if new_state == 'completed' and not job.date_completed:
                    vals['date_completed'] = fields.Datetime.now()
                job.write(vals)
                if new_state == 'completed':
                    job._jw_schedule_activity(_("Reconcile job work %s", job.name),
                                              _("All output has been received and inspected. Reconcile the material "
                                                "and costs to close the job."))

    def _jw_schedule_activity(self, summary, note, user=None):
        activity_type = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        for job in self:
            if not activity_type:
                continue
            job.sudo().activity_schedule(
                activity_type_id=activity_type.id, summary=summary, note=note,
                user_id=(user or job.user_id).id or self.env.user.id)

    def _jw_update_po_received(self):
        for job in self:
            lines = job.purchase_line_ids.filtered(
                lambda l: l.state == 'purchase' and l.qty_received_method == 'manual')
            # Job work purchase lines are expressed in the job's output unit (rate per output unit)
            for line in lines:
                line.sudo().qty_received_manual = job.qty_accepted

    # ------------------------------------------------------------------
    # Workflow actions
    # ------------------------------------------------------------------
    def action_plan(self):
        self._jw_ensure_state(('draft',), _("Planning"))
        for job in self:
            if not job.material_ids:
                raise UserError(_("Add at least one material to issue before planning %s.", job.name))
            if any(m.planned_qty <= 0 for m in job.material_ids):
                raise UserError(_("All planned material quantities must be positive on %s.", job.name))
            job._jw_get_vendor_location()
            for mat in job.material_ids.filtered(lambda m: not m.unit_cost):
                mat.unit_cost = mat.product_id.with_company(job.company_id).standard_price
            job.state = 'planned'
        return True

    def action_send_to_vendor(self):
        """Open the composer to send the job to the job worker with a portal link."""
        self.ensure_one()
        template = self.env.ref('zivo_job_work_360.mail_template_job_work_sent', raise_if_not_found=False)
        ctx = {
            'default_model': 'zivo.job.work',
            'default_res_ids': self.ids,
            'default_template_id': template.id if template else False,
            'default_composition_mode': 'comment',
            'default_email_layout_xmlid': 'mail.mail_notification_light',
            'mark_jw_as_sent': True,
            'force_email': True,
        }
        return {
            'type': 'ir.actions.act_window',
            'view_mode': 'form',
            'res_model': 'mail.compose.message',
            'views': [(False, 'form')],
            'target': 'new',
            'context': ctx,
        }

    def message_post(self, **kwargs):
        if self.env.context.get('mark_jw_as_sent'):
            self.filtered(lambda j: not j.date_sent).sudo().write({'date_sent': fields.Datetime.now()})
        return super().message_post(**kwargs)

    def action_issue_material(self):
        """Create the dispatch transfer for all materials not yet issued."""
        self.ensure_one()
        self._jw_ensure_state(('planned', 'ready', 'issued', 'at_vendor', 'processing', 'qc_pending'),
                              _("Issuing material"))
        if self.vendor_state == 'rejected':
            raise UserError(_("The job worker rejected this job. Change the job worker or reset the status "
                              "before issuing material."))
        vendor_loc = self._jw_get_vendor_location()
        lines = []
        for mat in self.material_ids:
            to_issue = mat.planned_qty - mat.qty_issued - self._jw_open_transfer_qty(mat)
            if float_compare(to_issue, 0.0, precision_rounding=mat.product_uom_id.rounding) > 0:
                lines.append((mat, mat.product_id, to_issue, mat.product_uom_id))
        if not lines:
            raise UserError(_("All planned material is already issued or in an open transfer."))
        picking = self._jw_create_picking(
            self._jw_issue_picking_type(), self.location_src_id, vendor_loc, lines, 'issue')
        if self.state == 'planned':
            self.state = 'ready'
        return self._jw_action_open_picking(picking)

    def _jw_action_open_picking(self, picking):
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'res_id': picking.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'current',
        }

    def _jw_on_issue_done(self):
        for job in self:
            if job.state == 'hold' and job.hold_state in ('planned', 'ready'):
                job.hold_state = 'issued'
            if job.state in ('planned', 'ready'):
                job.state = 'issued'
                if job.vendor_state in ('received', 'processing'):
                    job.state = 'at_vendor' if job.vendor_state == 'received' else 'processing'

    def action_vendor_accept(self):
        for job in self:
            if job.vendor_state not in ('new', 'rejected'):
                continue
            job.write({'vendor_state': 'accepted', 'vendor_accept_date': fields.Datetime.now(),
                       'vendor_reject_reason': False})
            job.message_post(body=_("Job accepted by the job worker."))
        return True

    def action_vendor_reject(self, reason=None):
        for job in self:
            if job.state not in ('draft', 'planned', 'ready'):
                raise UserError(_("A job can only be rejected before material is issued."))
            job.write({'vendor_state': 'rejected', 'vendor_reject_reason': reason or _('No reason given')})
            job.message_post(body=_("Job rejected by the job worker: %s", reason or _('No reason given')))
            job._jw_schedule_activity(_("Job rejected by %s", job.partner_id.name),
                                      reason or _("The job worker rejected the job."))
        return True

    def action_reset_vendor_state(self):
        self.write({'vendor_state': 'new', 'vendor_reject_reason': False})
        return True

    def action_vendor_confirm_receipt(self):
        for job in self:
            if job.state not in ('issued', 'at_vendor', 'processing', 'qc_pending'):
                raise UserError(_("Material receipt can be confirmed once material has been issued."))
            vals = {'vendor_receipt_date': job.vendor_receipt_date or fields.Datetime.now()}
            if job.vendor_state in ('new', 'accepted', 'rejected'):
                vals['vendor_state'] = 'received'
                vals['vendor_accept_date'] = job.vendor_accept_date or fields.Datetime.now()
            if job.state == 'issued':
                vals['state'] = 'at_vendor'
            job.write(vals)
            job.message_post(body=_("The job worker confirmed receipt of the material."))
        return True

    def action_vendor_start(self):
        for job in self:
            if job.state not in ('issued', 'at_vendor', 'processing'):
                raise UserError(_("Processing can start once material has been issued."))
            vals = {'vendor_state': 'processing', 'vendor_start_date': job.vendor_start_date or fields.Datetime.now()}
            if not job.vendor_receipt_date:
                vals['vendor_receipt_date'] = fields.Datetime.now()
            if not job.vendor_accept_date:
                vals['vendor_accept_date'] = fields.Datetime.now()
            if job.state in ('issued', 'at_vendor'):
                vals['state'] = 'processing'
            job.write(vals)
            job.message_post(body=_("Processing started."))
        return True

    def action_vendor_complete(self):
        for job in self:
            if job.state not in ('issued', 'at_vendor', 'processing', 'qc_pending', 'completed'):
                raise UserError(_("The job cannot be submitted as completed in its current status."))
            job.write({'vendor_state': 'completed', 'vendor_complete_date': fields.Datetime.now()})
            job.message_post(body=_("The job worker submitted the job as completed."))
            job._jw_schedule_activity(_("Receive output of %s", job.name),
                                      _("The job worker reported the job as completed. Receive and inspect "
                                        "the output."))
        return True

    def action_receive(self):
        self.ensure_one()
        self._jw_ensure_state(RECEIVABLE_STATES, _("Receiving output"))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Receive Job Work Output'),
            'res_model': 'zivo.job.work.receive.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_job_id': self.id},
        }

    def action_return_material(self):
        self.ensure_one()
        self._jw_ensure_state(RECEIVABLE_STATES, _("Returning material"))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Return Unprocessed Material'),
            'res_model': 'zivo.job.work.return.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_job_id': self.id},
        }

    def action_request_loss(self):
        self.ensure_one()
        self._jw_ensure_state(RECEIVABLE_STATES, _("Recording a loss"))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Record Material Loss'),
            'res_model': 'zivo.job.work.loss.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_job_id': self.id},
        }

    def action_mark_completed(self):
        """Short-close: the job worker will not deliver more output."""
        for job in self:
            job._jw_ensure_state(('issued', 'at_vendor', 'processing', 'qc_pending'), _("Completing"))
            if job.qty_pending_qc > 0 or job.production_ids.filtered(lambda p: p.state in ('waiting', 'qc_pending')):
                raise UserError(_("Finish pending receipts and inspections of %s first.", job.name))
            job.write({'state': 'completed', 'date_completed': fields.Datetime.now()})
            job.message_post(body=_("Job marked as completed with %(qty)s %(uom)s received out of %(planned)s.",
                                    qty=job.qty_received, uom=job.product_uom_id.name, planned=job.product_qty))
        return True

    def action_hold(self):
        for job in self:
            if job.state in ('draft', 'closed', 'cancel', 'hold'):
                raise UserError(_("%s cannot be put on hold in its current status.", job.name))
            job.write({'hold_state': job.state, 'state': 'hold'})
        return True

    def action_resume(self):
        for job in self.filtered(lambda j: j.state == 'hold'):
            job.write({'state': job.hold_state or 'planned', 'hold_state': False})
            job._jw_update_state()
        return True

    def action_cancel(self):
        for job in self:
            if job.state in ('closed', 'cancel'):
                continue
            if any(m.qty_issued > 0 for m in job.material_ids) or job.production_ids.filtered(
                    lambda p: p.state != 'cancel'):
                raise UserError(_(
                    "%s has posted material movements. Return the material or record the loss and complete "
                    "the reconciliation instead of cancelling.", job.name))
            job.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')).action_cancel()
            job.purchase_line_ids.order_id.filtered(lambda o: o.state in ('draft', 'sent')).button_cancel()
            job.state = 'cancel'
        return True

    def action_draft(self):
        for job in self:
            if job.state not in ('cancel', 'planned'):
                raise UserError(_("Only planned or cancelled job work orders can be reset to draft."))
            if job.picking_ids.filtered(lambda p: p.state not in ('cancel',)):
                raise UserError(_("Cancel the transfers of %s before resetting it to draft.", job.name))
            job.state = 'draft'
        return True

    def action_reconcile(self):
        """Run the reconciliation engine; move to Reconciled when nothing blocks."""
        self._jw_ensure_state(('completed', 'reconciled'), _("Reconciliation"))
        blocked = self.env['zivo.job.work']
        for job in self:
            job._jw_run_checks(final=True)
            if job.has_blocking_exception or job._jw_reconciliation_blockers():
                job.reconciliation_state = 'exception'
                blocked |= job
                continue
            job.write({
                'state': 'reconciled',
                'reconciliation_state': 'balanced',
                'cost_snapshot_expected': job.cost_exp_total,
                'cost_snapshot_actual': job.cost_act_total,
                'cost_snapshot_variance_pct': job.cost_variance_pct,
            })
            job.message_post(body=_("Material and cost reconciliation completed."))
            job.activity_feedback(['mail.mail_activity_data_todo'])
        if blocked:
            messages = []
            for job in blocked:
                reasons = job._jw_reconciliation_blockers() + [
                    e.name for e in job.exception_ids if e.state == 'open' and e.severity == 'high']
                messages.append('%s: %s' % (job.name, '; '.join(reasons)))
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Reconciliation blocked'),
                    'message': '\n'.join(messages),
                    'type': 'warning',
                    'sticky': True,
                    'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
                },
            }
        return True

    def _jw_reconciliation_blockers(self):
        self.ensure_one()
        reasons = []
        for mat in self.material_ids:
            if float_compare(mat.balance_qty, 0.0, precision_rounding=mat.product_uom_id.rounding) > 0:
                reasons.append(_("%(qty)s %(uom)s of %(product)s is still unaccounted at the job worker",
                                 qty=float_round(mat.balance_qty, precision_rounding=mat.product_uom_id.rounding),
                                 uom=mat.product_uom_id.name, product=mat.product_id.display_name))
        if self.loss_ids.filtered(lambda l: l.state == 'to_approve'):
            reasons.append(_("loss requests are waiting for approval"))
        if self.request_ids.filtered(lambda r: r.state == 'to_approve'):
            reasons.append(_("material requests are waiting for approval"))
        if self.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')):
            reasons.append(_("transfers are still open"))
        if self.qc_ids.filtered(lambda q: q.state == 'draft'):
            reasons.append(_("inspections are still pending"))
        return reasons

    def action_close(self):
        self._jw_ensure_state(('reconciled',), _("Closing"))
        for job in self:
            job.write({'state': 'closed', 'date_closed': fields.Datetime.now()})
        return True

    def action_reopen(self):
        """Re-open a reconciled job (e.g. a late bill)."""
        self._jw_ensure_state(('reconciled',), _("Re-opening"))
        self.write({'state': 'completed', 'reconciliation_state': 'pending'})
        return True

    def action_create_purchase_order(self):
        self.ensure_one()
        if self.state in ('draft', 'cancel', 'closed'):
            raise UserError(_("Plan the job before creating its purchase order."))
        if not self.service_product_id:
            raise UserError(_("Set a processing service product on the job or its job type."))
        if self.purchase_order_ids:
            raise UserError(_("A purchase order already exists for %s.", self.name))
        service = self.service_product_id
        qty = self.product_qty
        po = self.env['purchase.order'].create({
            'partner_id': self.partner_id.id,
            'company_id': self.company_id.id,
            'currency_id': self.currency_id.id,
            'origin': self.name,
            'partner_ref': self.client_ref or False,
            'picking_type_id': self.warehouse_id.in_type_id.id,
            'order_line': [(0, 0, {
                'product_id': service.id,
                'name': _("Job work %(type)s on %(product)s (%(job)s), rate per %(uom)s",
                          type=self.job_type_id.name, product=self.product_id.display_name,
                          job=self.name, uom=self.product_uom_id.name),
                'product_qty': qty,
                'product_uom_id': service.uom_id.id,
                'price_unit': self.rate,
                'date_planned': self.date_expected,
                'jw_order_id': self.id,
            })],
        })
        po.order_line.price_unit = self.rate  # keep the approved job rate over pricelist recomputation
        self.message_post(body=_("Purchase order %s created for the processing charges.", po._get_html_link()))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.order',
            'res_id': po.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }

    def action_create_next_stage(self):
        """Prepare the next processing stage using the accepted output as material."""
        self.ensure_one()
        if not self.qty_accepted:
            raise UserError(_("The next stage can be created once output has been accepted."))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Next Stage'),
            'res_model': 'zivo.job.work',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'current',
            'context': {
                'default_parent_id': self.id,
                'default_product_id': self.product_id.id,
                'default_product_qty': self.qty_accepted,
                'default_principal_id': self.principal_id.id,
                'default_client_ref': self.client_ref,
                'default_warehouse_id': self.warehouse_id.id,
                'default_mrp_production_id': self.mrp_production_id.id,
                'default_material_ids': [(0, 0, {
                    'product_id': self.product_id.id,
                    'planned_qty': self.qty_accepted,
                    'product_uom_id': self.product_uom_id.id,
                })],
            },
        }

    # ------------------------------------------------------------------
    # Exceptions / reconciliation engine
    # ------------------------------------------------------------------
    def _jw_set_exception(self, exc_type, active, description='', severity='high', material=False, auto=True):
        """Open an exception when ``active`` and none is open; auto-resolve it when no longer active."""
        self.ensure_one()
        Exception_ = self.env['zivo.job.work.exception'].sudo()
        domain = [('job_id', '=', self.id), ('exception_type', '=', exc_type), ('state', '=', 'open')]
        if material:
            domain.append(('material_id', '=', material.id))
        existing = Exception_.search(domain)
        if active and not existing and Exception_.search_count(
                [d for d in domain if d[0] != 'state'] + [('state', '=', 'waived')], limit=1):
            return  # a manager already waived this exception
        if active and not existing:
            Exception_.create({
                'job_id': self.id,
                'exception_type': exc_type,
                'severity': severity,
                'description': description,
                'material_id': material and material.id or False,
            })
        elif active and existing:
            existing.description = description
        elif not active and existing and auto:
            existing.write({'state': 'resolved', 'resolution': _('Resolved automatically: condition cleared.'),
                            'resolved_by_id': self.env.user.id, 'date_resolved': fields.Datetime.now()})

    def _jw_run_checks(self, final=False):
        """Evaluate the exception rules for each job."""
        for job in self:
            company = job.company_id
            # Overdue
            job._jw_set_exception(
                'overdue', job.is_overdue,
                _("Expected completion %(date)s has passed and the job is still %(state)s.",
                  date=format_date(job.env, job.date_expected),
                  state=dict(job._fields['state']._description_selection(job.env)).get(job.state)),
                severity='high')
            for mat in job.material_ids:
                rounding = mat.product_uom_id.rounding
                issued = mat.qty_issued
                if final:
                    unaccounted = mat.balance_qty
                    tolerance_qty = issued * company.jw_shortage_tolerance / 100.0
                    if float_compare(unaccounted, 0.0, precision_rounding=rounding) > 0 and \
                            float_compare(unaccounted, tolerance_qty, precision_rounding=rounding) <= 0:
                        # Within tolerance: write off as an approved process loss
                        loss = self.env['zivo.job.work.loss'].sudo().create({
                            'job_id': job.id,
                            'material_id': mat.id,
                            'qty': unaccounted,
                            'reason': 'tolerance',
                            'note': _("Automatic write-off within the %s%% shortage tolerance.",
                                      company.jw_shortage_tolerance),
                        })
                        loss._jw_post()
                        unaccounted = mat.balance_qty
                    job._jw_set_exception(
                        'shortage', float_compare(unaccounted, 0.0, precision_rounding=rounding) > 0,
                        _("Unaccounted %(qty)s %(uom)s of %(product)s (sent %(sent)s - consumed %(cons)s - returned "
                          "%(ret)s - scrap %(scrap)s - approved loss %(loss)s).",
                          qty=float_round(unaccounted, precision_rounding=rounding), uom=mat.product_uom_id.name,
                          product=mat.product_id.display_name, sent=issued, cons=mat.qty_consumed,
                          ret=mat.qty_returned, scrap=mat.qty_scrap, loss=mat.qty_loss),
                        severity='high', material=mat)
                if issued > 0:
                    scrap_pct = mat.qty_scrap / issued * 100.0
                    allowed = mat.scrap_pct + company.jw_scrap_tolerance
                    job._jw_set_exception(
                        'excess_scrap', scrap_pct > allowed + 1e-6,
                        _("Scrap of %(product)s is %(pct).2f%% of the issued quantity (allowed %(allowed).2f%%).",
                          product=mat.product_id.display_name, pct=scrap_pct, allowed=allowed),
                        severity='high', material=mat)
                    expected = mat.expected_consumption
                    over = expected > 0 and (mat.qty_consumed - expected) / expected * 100.0 or 0.0
                    job._jw_set_exception(
                        'material_variance', expected > 0 and over > company.jw_shortage_tolerance + 1e-6,
                        _("Consumption of %(product)s is %(pct).2f%% above standard (%(act)s vs %(exp)s %(uom)s).",
                          product=mat.product_id.display_name, pct=over, act=mat.qty_consumed,
                          exp=float_round(expected, precision_rounding=rounding), uom=mat.product_uom_id.name),
                        severity='medium', material=mat)
            if job.qty_accepted > 0 or final:
                low = job.actual_yield > 0 and job.actual_yield < job.expected_yield - company.jw_yield_tolerance
                job._jw_set_exception(
                    'low_yield', low,
                    _("Actual yield %(act).2f%% is below the expected %(exp).2f%%.",
                      act=job.actual_yield, exp=job.expected_yield),
                    severity='medium')
            if final:
                cost_over = job.cost_variance_pct > company.jw_cost_variance_tolerance + 1e-6
                job._jw_set_exception(
                    'cost_variance', cost_over,
                    _("Actual cost exceeds the expected cost by %(pct).2f%% (%(amount)s).",
                      pct=job.cost_variance_pct,
                      amount=job.company_currency_id.format(job.cost_variance)),
                    severity='medium')
            job._jw_check_bill_variance()

    def _jw_check_bill_variance(self):
        for job in self:
            moves = job.purchase_line_ids.invoice_lines.move_id.filtered(
                lambda m: m.move_type == 'in_invoice' and m.state != 'cancel')
            variance_moves = moves.filtered(lambda m: m.jw_has_variance and m.jw_review_state != 'approved')
            job._jw_set_exception(
                'bill_variance', bool(variance_moves),
                _("Vendor bill(s) %s exceed the accepted quantity or approved rate.",
                  ', '.join(m.name or m.ref or job.env._('Draft') for m in variance_moves)),
                severity='high')

    @api.model
    def _cron_check_exceptions(self):
        jobs = self.search([('state', 'in', list(ACTIVE_STATES) + ['completed'])])
        for job in jobs:
            job._jw_run_checks(final=False)
            if job.is_overdue and not job.activity_ids.filtered(
                    lambda a: a.summary and job.name in a.summary and 'overdue' in a.summary.lower()):
                job._jw_schedule_activity(_("Job %s is overdue", job.name),
                                          _("Expected completion has passed. Follow up with %s.",
                                            job.partner_id.name))

    # ------------------------------------------------------------------
    # Smart buttons
    # ------------------------------------------------------------------
    def _jw_action(self, xmlid, domain, context=None):
        action = self.env['ir.actions.act_window']._for_xml_id(xmlid)
        action['domain'] = domain
        action['context'] = context or {}
        return action

    def action_view_pickings(self):
        self.ensure_one()
        return self._jw_action('stock.action_picking_tree_all', [('jw_order_id', '=', self.id)],
                               {'create': False})

    def action_view_purchase(self):
        self.ensure_one()
        action = self._jw_action('purchase.purchase_rfq', [('id', 'in', self.purchase_order_ids.ids)],
                                 {'create': False})
        if len(self.purchase_order_ids) == 1:
            action.update({'res_id': self.purchase_order_ids.id, 'view_mode': 'form',
                           'views': [(False, 'form')]})
        return action

    def action_view_bills(self):
        self.ensure_one()
        bills = self.purchase_line_ids.invoice_lines.move_id
        action = self._jw_action('account.action_move_in_invoice_type', [('id', 'in', bills.ids)],
                                 {'create': False, 'default_move_type': 'in_invoice'})
        return action

    def action_view_qc(self):
        self.ensure_one()
        return self._jw_action('zivo_job_work_360.action_zivo_job_work_qc', [('job_id', '=', self.id)],
                               {'create': False})

    def action_inspect(self):
        self.ensure_one()
        pending = self.qc_ids.filtered(lambda q: q.state == 'draft')
        if len(pending) == 1:
            return {'type': 'ir.actions.act_window', 'res_model': 'zivo.job.work.qc', 'res_id': pending.id,
                    'view_mode': 'form', 'views': [(False, 'form')]}
        return self.action_view_qc()

    def _jw_fmt_qty(self, value, uom=None):
        """Human friendly quantity for portal and reports (thousand separators, unit precision)."""
        uom = uom or self.product_uom_id
        digits = max(0, len(str(uom.rounding).rstrip('0').split('.')[-1])) if uom and uom.rounding < 1 else 0
        if float_is_zero(value - round(value), precision_digits=6):
            digits = 0
        return formatLang(self.env, value, digits=digits)

    def action_view_ncr(self):
        self.ensure_one()
        return self._jw_action('zivo_job_work_360.action_zivo_job_work_ncr', [('job_id', '=', self.id)],
                               {'default_job_id': self.id})

    def action_view_exceptions(self):
        self.ensure_one()
        return self._jw_action('zivo_job_work_360.action_zivo_job_work_exception', [('job_id', '=', self.id)],
                               {'create': False, 'search_default_open': 1})

    def action_view_requests(self):
        self.ensure_one()
        return self._jw_action('zivo_job_work_360.action_zivo_job_work_request', [('job_id', '=', self.id)],
                               {'default_job_id': self.id})

    def action_view_children(self):
        self.ensure_one()
        return self._jw_action('zivo_job_work_360.action_zivo_job_work', [('parent_id', '=', self.id)],
                               {'default_parent_id': self.id})

    def action_view_chain(self):
        self.ensure_one()
        root = self.root_id or self
        return self._jw_action('zivo_job_work_360.action_zivo_job_work', [('root_id', '=', root.id)],
                               {'create': False})

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------
    @api.model
    def get_jw_dashboard_data(self):
        """Aggregated KPIs for the Job Work control tower (respects the user's access rights)."""
        companies = self.env.companies
        company = self.env.company
        currency = company.currency_id
        Job = self.with_context(active_test=False)
        open_domain = [('state', 'in', list(ACTIVE_STATES) + ['completed', 'hold']),
                       ('company_id', 'in', companies.ids)]
        open_jobs = Job.search(open_domain)
        overdue = open_jobs.filtered('is_overdue')
        since = fields.Datetime.now() - timedelta(days=company.jw_score_period_days or 90)
        finished = Job.search([('company_id', 'in', companies.ids),
                               ('state', 'in', ('completed', 'reconciled', 'closed')),
                               ('date_completed', '>=', since)])
        materials = self.env['zivo.job.work.material'].search([
            ('company_id', 'in', companies.ids), ('balance_qty', '>', 0),
            ('job_id.state', 'not in', ('cancel', 'closed'))])
        value_at_vendors = sum(materials.mapped('balance_value'))
        by_vendor = defaultdict(lambda: {'value': 0.0, 'lines': 0, 'age': 0})
        for mat in materials:
            entry = by_vendor[mat.partner_id]
            entry['value'] += mat.balance_value
            entry['lines'] += 1
            entry['age'] = max(entry['age'], mat.age_days)
        vendor_rows = sorted(
            ({'partner_id': p.id, 'name': p.display_name, 'value': v['value'],
              'value_fmt': currency.format(v['value']), 'lines': v['lines'], 'age': v['age']}
             for p, v in by_vendor.items()), key=lambda r: -r['value'])[:8]
        cycle = [j.cycle_time_days for j in finished if j.cycle_time_days]
        yields = [j.actual_yield for j in finished if j.actual_yield]
        exp_total = sum(finished.mapped('cost_exp_total'))
        act_total = sum(finished.mapped('cost_act_total'))
        issued_value = sum(finished.mapped('material_issued_value'))
        scrap_value = sum(finished.mapped('material_scrap_value'))
        exceptions = self.env['zivo.job.work.exception'].search(
            [('state', '=', 'open'), ('company_id', 'in', companies.ids)], order='severity_rank desc, id desc')
        scores = self.env['zivo.job.work.vendor.score'].search(
            [('company_id', 'in', companies.ids), ('is_latest', '=', True)], order='score desc', limit=8)
        state_labels = dict(self._fields['state']._description_selection(self.env))
        pipeline = []
        for state in ('planned', 'ready', 'issued', 'at_vendor', 'processing', 'qc_pending', 'completed', 'hold'):
            count = len(open_jobs.filtered(lambda j, s=state: j.state == s))
            pipeline.append({'state': state, 'label': state_labels[state], 'count': count})
        return {
            'currency_symbol': currency.symbol,
            'kpis': {
                'open_jobs': len(open_jobs),
                'overdue_jobs': len(overdue),
                'value_at_vendors': currency.format(value_at_vendors),
                'value_at_vendors_raw': value_at_vendors,
                'avg_cycle_time': round(sum(cycle) / len(cycle), 1) if cycle else 0.0,
                'avg_yield': round(sum(yields) / len(yields), 1) if yields else 0.0,
                'scrap_rate': round(issued_value and scrap_value / issued_value * 100.0 or 0.0, 2),
                'cost_variance_pct': round(exp_total and (act_total - exp_total) / exp_total * 100.0 or 0.0, 2),
                'cost_variance': currency.format(act_total - exp_total),
                'open_exceptions': len(exceptions),
                'high_exceptions': len(exceptions.filtered(lambda e: e.severity == 'high')),
                'pending_qc': len(open_jobs.filtered(lambda j: j.state == 'qc_pending')),
                'period_days': company.jw_score_period_days or 90,
            },
            'pipeline': pipeline,
            'vendors_custody': vendor_rows,
            'exceptions': [{
                'id': e.id, 'job': e.job_id.name, 'job_id': e.job_id.id,
                'type': dict(e._fields['exception_type']._description_selection(self.env)).get(e.exception_type),
                'severity': e.severity, 'description': e.description or '',
                'partner': e.partner_id.display_name or '',
            } for e in exceptions[:8]],
            'scorecards': [{
                'id': s.id, 'partner': s.partner_id.display_name, 'score': round(s.score, 1), 'grade': s.grade,
                'on_time': round(s.on_time_rate, 1), 'quality': round(s.quality_rate, 1),
                'yield': round(s.yield_rate, 1), 'scrap': round(s.scrap_rate, 2),
            } for s in scores],
            'overdue': [{
                'id': j.id, 'name': j.name, 'partner': j.partner_id.display_name,
                'product': j.product_id.display_name,
                'days': max((fields.Datetime.now() - j.date_expected).days, 0),
            } for j in overdue.sorted('date_expected')[:8]],
        }
