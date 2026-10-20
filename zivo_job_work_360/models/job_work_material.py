# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


def jw_uom_root(uom):
    root = uom
    seen = set()
    while root.relative_uom_id and root.id not in seen:
        seen.add(root.id)
        root = root.relative_uom_id
    return root


def jw_allowed_uoms(product):
    """Units in which a product can be counted: same reference unit family plus its packagings."""
    if not product:
        return product.env['uom.uom']
    root = jw_uom_root(product.uom_id)
    family = product.env['uom.uom'].search([('id', 'child_of', root.id)])
    return family | product.uom_id | product.uom_ids


class ZivoJobWorkMaterial(models.Model):
    _name = 'zivo.job.work.material'
    _description = 'Job Work Material (Custody Line)'
    _order = 'job_id desc, sequence, id'
    _rec_name = 'product_id'
    _check_company_auto = True

    job_id = fields.Many2one('zivo.job.work', 'Job Work', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(related='job_id.company_id', store=True, index=True)
    company_currency_id = fields.Many2one(related='company_id.currency_id')
    partner_id = fields.Many2one(related='job_id.partner_id', store=True, string='Job Worker', index=True)
    job_state = fields.Selection(related='job_id.state', string='Job Status')
    product_id = fields.Many2one('product.product', 'Material', required=True, check_company=True,
                                 domain="[('type', '=', 'consu')]")
    allowed_uom_ids = fields.Many2many('uom.uom', compute='_compute_allowed_uom_ids')
    product_uom_id = fields.Many2one(
        'uom.uom', 'Unit', required=True, compute='_compute_product_uom_id', store=True, readonly=False,
        precompute=True, domain="[('id', 'in', allowed_uom_ids)]")
    planned_qty = fields.Float('Required', digits='Product Unit', required=True, default=1.0)
    scrap_pct = fields.Float(
        'Process Scrap (%)', compute='_compute_scrap_pct', store=True, readonly=False, precompute=True,
        help="Share of the required quantity expected to be scrapped during processing.")
    std_qty_per_unit = fields.Float(
        'Std. Consumption / Output Unit', compute='_compute_std_qty', store=True, digits='Product Unit')
    unit_cost = fields.Float('Unit Cost', digits='Product Price',
                             help="Material cost reference used for costing (company currency).")
    scrap_product_id = fields.Many2one(
        'product.product', 'Returnable Scrap', compute='_compute_scrap_product', store=True, readonly=False,
        precompute=True, domain="[('type', '=', 'consu'), ('tracking', '=', 'none')]")
    is_capital_goods = fields.Boolean('Capital Goods', help="Used by statutory return-period alerts.")
    move_ids = fields.One2many('stock.move', 'jw_material_id', 'Stock Moves', groups='stock.group_stock_user')

    qty_issued = fields.Float('Sent', compute='_compute_quantities', store=True, digits='Product Unit')
    qty_consumed = fields.Float('Consumed', compute='_compute_quantities', store=True, digits='Product Unit')
    qty_returned = fields.Float('Returned', compute='_compute_quantities', store=True, digits='Product Unit')
    qty_scrap = fields.Float('Scrap', compute='_compute_quantities', store=True, digits='Product Unit')
    qty_loss = fields.Float('Approved Loss', compute='_compute_quantities', store=True, digits='Product Unit')
    qty_in_transit = fields.Float('In Open Transfers', compute='_compute_in_transit', digits='Product Unit',
                                  compute_sudo=True)
    balance_qty = fields.Float('Balance at Job Worker', compute='_compute_quantities', store=True,
                               digits='Product Unit',
                               help="Unaccounted quantity = Sent - Consumed - Returned - Scrap - Approved Loss")
    balance_value = fields.Monetary('Balance Value', compute='_compute_quantities', store=True,
                                    currency_field='company_currency_id')
    expected_consumption = fields.Float('Std. Consumption', compute='_compute_expected', digits='Product Unit',
                                        help="Standard consumption for the output received so far.")
    consumption_variance = fields.Float('Consumption Variance', compute='_compute_expected',
                                        digits='Product Unit')
    date_first_issue = fields.Datetime('First Issued', compute='_compute_quantities', store=True)
    age_days = fields.Integer('Age (days)', compute='_compute_age')

    _planned_positive = models.Constraint('CHECK(planned_qty >= 0)', 'Required quantity cannot be negative.')

    @api.depends('product_id')
    def _compute_allowed_uom_ids(self):
        for mat in self:
            mat.allowed_uom_ids = jw_allowed_uoms(mat.product_id)

    @api.constrains('product_id', 'product_uom_id')
    def _check_uom(self):
        for mat in self:
            if mat.product_id and mat.product_uom_id not in jw_allowed_uoms(mat.product_id):
                raise ValidationError(_(
                    "The unit %(uom)s cannot be used for %(product)s (its unit is %(ref)s).",
                    uom=mat.product_uom_id.name, product=mat.product_id.display_name, ref=mat.product_id.uom_id.name))

    @api.depends('product_id')
    def _compute_product_uom_id(self):
        for mat in self:
            if mat.product_id and (not mat.product_uom_id or not mat.move_ids):
                mat.product_uom_id = mat.product_id.uom_id

    @api.depends('job_id.job_type_id')
    def _compute_scrap_pct(self):
        for mat in self:
            if not mat.move_ids:
                mat.scrap_pct = mat.job_id.job_type_id.expected_scrap_pct

    @api.depends('job_id.job_type_id')
    def _compute_scrap_product(self):
        for mat in self:
            if not mat.scrap_product_id:
                mat.scrap_product_id = mat.job_id.job_type_id.scrap_product_id

    @api.depends('planned_qty', 'scrap_pct', 'job_id.product_qty')
    def _compute_std_qty(self):
        for mat in self:
            output = mat.job_id.product_qty
            mat.std_qty_per_unit = output and mat.planned_qty * (1 - mat.scrap_pct / 100.0) / output or 0.0

    @api.depends('move_ids.state', 'move_ids.quantity', 'move_ids.jw_move_type', 'move_ids.product_uom',
                 'move_ids.date', 'unit_cost', 'product_uom_id')
    def _compute_quantities(self):
        for mat in self:
            totals = {'issue': 0.0, 'consume': 0.0, 'return': 0.0, 'scrap': 0.0, 'loss': 0.0}
            first_issue = False
            for move in mat.move_ids:
                if move.state != 'done' or move.jw_move_type not in totals:
                    continue
                qty = move.product_uom._compute_quantity(move.quantity, mat.product_uom_id, round=False) \
                    if mat.product_uom_id else move.quantity
                totals[move.jw_move_type] += qty
                if move.jw_move_type == 'issue' and (not first_issue or move.date < first_issue):
                    first_issue = move.date
            mat.qty_issued = totals['issue']
            mat.qty_consumed = totals['consume']
            mat.qty_returned = totals['return']
            mat.qty_scrap = totals['scrap']
            mat.qty_loss = totals['loss']
            balance = totals['issue'] - totals['consume'] - totals['return'] - totals['scrap'] - totals['loss']
            if mat.product_uom_id and abs(balance) < mat.product_uom_id.rounding / 2.0:
                balance = 0.0
            mat.balance_qty = balance
            mat.balance_value = balance * mat.unit_cost
            mat.date_first_issue = first_issue

    def _compute_in_transit(self):
        for mat in self:
            mat.qty_in_transit = mat.job_id._jw_open_transfer_qty(mat) if mat.job_id else 0.0

    @api.depends('std_qty_per_unit', 'job_id.qty_received', 'qty_consumed')
    def _compute_expected(self):
        for mat in self:
            mat.expected_consumption = mat.std_qty_per_unit * mat.job_id.qty_received
            mat.consumption_variance = mat.qty_consumed - mat.expected_consumption

    def _compute_age(self):
        now = fields.Datetime.now()
        for mat in self:
            mat.age_days = (now - mat.date_first_issue).days if mat.date_first_issue and mat.balance_qty > 0 else 0

    @api.onchange('product_id')
    def _onchange_product_cost(self):
        if self.product_id:
            company = self.job_id.company_id or self.env.company
            self.unit_cost = self.product_id.with_company(company).standard_price

    @api.constrains('scrap_pct')
    def _check_scrap_pct(self):
        for mat in self:
            if not 0 <= mat.scrap_pct < 100:
                raise ValidationError(_("Process scrap must be between 0 and 99.99%."))

    @api.model_create_multi
    def create(self, vals_list):
        mats = super().create(vals_list)
        for mat in mats.filtered(lambda m: not m.unit_cost and m.product_id):
            mat.unit_cost = mat.product_id.with_company(mat.job_id.company_id).standard_price
        return mats

    def write(self, vals):
        if 'product_id' in vals or 'product_uom_id' in vals:
            for mat in self:
                if mat.move_ids.filtered(lambda m: m.state != 'cancel') and (
                        vals.get('product_id', mat.product_id.id) != mat.product_id.id
                        or vals.get('product_uom_id', mat.product_uom_id.id) != mat.product_uom_id.id):
                    raise UserError(_("You cannot change the product or unit of a material that already has "
                                      "stock movements."))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_moves(self):
        for mat in self:
            if mat.move_ids.filtered(lambda m: m.state != 'cancel'):
                raise UserError(_("You cannot delete material %s: it already has stock movements.",
                                  mat.product_id.display_name))

    def action_view_moves(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('stock.stock_move_action')
        action['domain'] = [('jw_material_id', '=', self.id)]
        action['context'] = {'create': False, 'search_default_done': 0}
        return action

    def action_open_job(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'zivo.job.work',
            'res_id': self.job_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }
