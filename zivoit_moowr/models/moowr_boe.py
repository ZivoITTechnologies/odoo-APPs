from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, float_is_zero, float_round


class MoowrBoe(models.Model):
    _name = 'moowr.boe'
    _description = 'Into-bond Bill of Entry'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'boe_date desc, id desc'
    _check_company_auto = True

    name = fields.Char('Bill of Entry Number', required=True, tracking=True, copy=False)
    boe_date = fields.Date('Bill of Entry Date', required=True, default=fields.Date.context_today, tracking=True)
    licence_id = fields.Many2one('moowr.licence', required=True, tracking=True, check_company=True,
                                 default=lambda self: self.env['moowr.licence'].search(
                                     [('company_id', '=', self.env.company.id)], limit=1))
    company_id = fields.Many2one('res.company', related='licence_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    partner_id = fields.Many2one('res.partner', 'Supplier', tracking=True)
    purchase_id = fields.Many2one('purchase.order', 'Purchase Order', check_company=True, tracking=True)
    port_code = fields.Char('Port Code')
    igm_number = fields.Char('IGM Number')
    exchange_rate = fields.Float('Exchange Rate', digits=(12, 4), help="Customs exchange rate used on the bill of entry.")
    line_ids = fields.One2many('moowr.boe.line', 'boe_id', 'Lines', copy=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('confirmed', 'In Bond'), ('closed', 'Closed'), ('cancel', 'Cancelled'),
    ], default='draft', required=True, tracking=True, copy=False)
    note = fields.Html('Notes')

    amount_assessable = fields.Monetary('Assessable Value', compute='_compute_amounts', store=True)
    amount_bcd = fields.Monetary('Basic Customs Duty', compute='_compute_amounts', store=True)
    amount_sws = fields.Monetary('Social Welfare Surcharge', compute='_compute_amounts', store=True)
    amount_igst = fields.Monetary('IGST', compute='_compute_amounts', store=True)
    amount_duty = fields.Monetary('Duty Deferred', compute='_compute_amounts', store=True)
    duty_balance = fields.Monetary('Duty Still in Bond', compute='_compute_balance')
    receipt_progress = fields.Float('Received %', compute='_compute_balance')
    ledger_count = fields.Integer(compute='_compute_balance')

    _name_licence_uniq = models.Constraint(
        'UNIQUE(name, licence_id)', 'This bill of entry number already exists for the licence.')

    @api.depends('line_ids.assessable_value', 'line_ids.bcd_amount', 'line_ids.sws_amount',
                 'line_ids.igst_amount', 'line_ids.duty_amount')
    def _compute_amounts(self):
        for boe in self:
            boe.amount_assessable = sum(boe.line_ids.mapped('assessable_value'))
            boe.amount_bcd = sum(boe.line_ids.mapped('bcd_amount'))
            boe.amount_sws = sum(boe.line_ids.mapped('sws_amount'))
            boe.amount_igst = sum(boe.line_ids.mapped('igst_amount'))
            boe.amount_duty = sum(boe.line_ids.mapped('duty_amount'))

    def _compute_balance(self):
        for boe in self:
            boe.duty_balance = sum(boe.line_ids.mapped('duty_balance'))
            declared = sum(boe.line_ids.mapped('quantity'))
            boe.receipt_progress = declared and 100.0 * sum(boe.line_ids.mapped('qty_received')) / declared or 0.0
            boe.ledger_count = len(boe.line_ids.ledger_ids)

    @api.onchange('purchase_id')
    def _onchange_purchase_id(self):
        if self.purchase_id and not self.partner_id:
            self.partner_id = self.purchase_id.partner_id

    def action_confirm(self):
        for boe in self:
            if not boe.line_ids:
                raise UserError(self.env._("Add at least one line to bill of entry %s.", boe.name))
            for line in boe.line_ids:
                if line.quantity <= 0:
                    raise UserError(self.env._("Line %s on bill of entry %s must have a positive quantity.",
                                               line.product_id.display_name, boe.name))
                if line.assessable_value < 0:
                    raise UserError(self.env._("The assessable value cannot be negative."))
            boe.state = 'confirmed'

    def action_draft(self):
        for boe in self:
            if boe.line_ids.ledger_ids:
                raise UserError(self.env._("Bill of entry %s already has movements in the bonded register "
                                           "and cannot be reset to draft.", boe.name))
            boe.state = 'draft'

    def action_cancel(self):
        for boe in self:
            if boe.line_ids.ledger_ids:
                raise UserError(self.env._("Bill of entry %s already has movements and cannot be cancelled.", boe.name))
            boe.state = 'cancel'

    def action_close(self):
        for boe in self:
            if any(float_compare(l.qty_balance, 0.0, precision_rounding=l.uom_id.rounding) > 0 for l in boe.line_ids):
                raise UserError(self.env._("Bill of entry %s still has goods in bond.", boe.name))
            boe.state = 'closed'

    def action_reopen(self):
        self.filtered(lambda b: b.state == 'closed').write({'state': 'confirmed'})

    def _auto_close(self):
        for boe in self.filtered(lambda b: b.state == 'confirmed'):
            lines = boe.line_ids
            if lines and all(float_compare(l.qty_received, l.quantity, precision_rounding=l.uom_id.rounding) >= 0
                             and float_is_zero(l.qty_balance, precision_rounding=l.uom_id.rounding) for l in lines):
                boe.state = 'closed'

    def action_view_ledger(self):
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_moowr.action_moowr_ledger')
        action['domain'] = [('boe_id', 'in', self.ids)]
        action['context'] = {}
        return action

    def unlink(self):
        if any(b.state not in ('draft', 'cancel') for b in self):
            raise UserError(self.env._("Only draft or cancelled bills of entry can be deleted."))
        return super().unlink()


class MoowrBoeLine(models.Model):
    _name = 'moowr.boe.line'
    _description = 'Bill of Entry Line'
    _order = 'boe_date, boe_id, sequence, id'
    _rec_names_search = ['product_id', 'boe_id']

    boe_id = fields.Many2one('moowr.boe', 'Bill of Entry', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    boe_date = fields.Date(related='boe_id.boe_date', store=True)
    boe_state = fields.Selection(related='boe_id.state', store=True, string='Status')
    licence_id = fields.Many2one(related='boe_id.licence_id', store=True, index=True)
    company_id = fields.Many2one(related='boe_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='boe_id.currency_id')
    purchase_line_id = fields.Many2one('purchase.order.line', 'Purchase Line', index='btree_not_null')
    product_id = fields.Many2one('product.product', required=True, index=True)
    uom_id = fields.Many2one(related='product_id.uom_id', string='Unit')
    tariff_code = fields.Char('Tariff Code (CTH)')
    quantity = fields.Float('Declared Quantity', digits='Product Unit', required=True, default=1.0)
    assessable_value = fields.Monetary('Assessable Value', required=True, default=0.0)
    bcd_rate = fields.Float('BCD %', digits=(6, 2))
    bcd_amount = fields.Monetary('BCD', compute='_compute_duty', store=True)
    sws_rate = fields.Float('SWS % of BCD', digits=(6, 2), default=10.0)
    sws_amount = fields.Monetary('SWS', compute='_compute_duty', store=True)
    igst_rate = fields.Float('IGST %', digits=(6, 2), default=18.0)
    igst_amount = fields.Monetary('IGST', compute='_compute_duty', store=True)
    duty_amount = fields.Monetary('Duty Deferred', compute='_compute_duty', store=True)
    unit_duty = fields.Monetary('Duty per Unit', compute='_compute_duty', store=True)
    unit_value = fields.Monetary('Value per Unit', compute='_compute_duty', store=True)

    ledger_ids = fields.One2many('moowr.ledger', 'boe_line_id', 'Register Entries')
    qty_received = fields.Float('Received', digits='Product Unit', compute='_compute_quantities', store=True)
    qty_consumed = fields.Float('Consumed', digits='Product Unit', compute='_compute_quantities', store=True)
    qty_cleared = fields.Float('Cleared as Such', digits='Product Unit', compute='_compute_quantities', store=True)
    qty_exported = fields.Float('Exported as Such', digits='Product Unit', compute='_compute_quantities', store=True)
    qty_scrapped = fields.Float('Scrapped', digits='Product Unit', compute='_compute_quantities', store=True)
    qty_returned = fields.Float('Returned', digits='Product Unit', compute='_compute_quantities', store=True)
    qty_balance = fields.Float('Balance in Bond', digits='Product Unit', compute='_compute_quantities', store=True)
    duty_balance = fields.Monetary('Duty in Bond', compute='_compute_quantities', store=True)
    value_balance = fields.Monetary('Value in Bond', compute='_compute_quantities', store=True)
    date_first_receipt = fields.Date('First Receipt', compute='_compute_quantities', store=True)
    age_days = fields.Integer('Age (Days)', compute='_compute_age')
    interest_exposure = fields.Monetary('Interest if Cleared as Such Today', compute='_compute_age')

    @api.depends('assessable_value', 'bcd_rate', 'sws_rate', 'igst_rate', 'quantity', 'boe_id.licence_id.defer_igst')
    def _compute_duty(self):
        for line in self:
            cur = line.currency_id or self.env.company.currency_id
            bcd = cur.round(line.assessable_value * line.bcd_rate / 100.0)
            sws = cur.round(bcd * line.sws_rate / 100.0)
            igst = cur.round((line.assessable_value + bcd + sws) * line.igst_rate / 100.0)
            line.bcd_amount = bcd
            line.sws_amount = sws
            line.igst_amount = igst
            defer_igst = line.boe_id.licence_id.defer_igst if line.boe_id.licence_id else True
            line.duty_amount = bcd + sws + (igst if defer_igst else 0.0)
            line.unit_duty = line.quantity and line.duty_amount / line.quantity or 0.0
            line.unit_value = line.quantity and line.assessable_value / line.quantity or 0.0

    @api.depends('ledger_ids.quantity', 'ledger_ids.move_type', 'ledger_ids.date', 'quantity', 'unit_duty', 'unit_value')
    def _compute_quantities(self):
        out_map = {'consumption': 'qty_consumed', 'home': 'qty_cleared', 'export': 'qty_exported',
                   'scrap': 'qty_scrapped', 'return': 'qty_returned'}
        for line in self:
            vals = dict.fromkeys(['qty_received'] + list(out_map.values()), 0.0)
            first = False
            for led in line.ledger_ids:
                if led.move_type == 'receipt':
                    vals['qty_received'] += led.quantity
                    d = fields.Date.to_date(led.date)
                    first = min(first, d) if first else d
                elif led.move_type in out_map:
                    vals[out_map[led.move_type]] += led.quantity
            line.update(vals)
            balance = vals['qty_received'] - sum(vals[k] for k in out_map.values())
            rounding = line.product_id.uom_id.rounding or 0.0001
            balance = float_round(balance, precision_rounding=rounding)
            line.qty_balance = balance
            line.duty_balance = (line.currency_id or self.env.company.currency_id).round(balance * line.unit_duty)
            line.value_balance = (line.currency_id or self.env.company.currency_id).round(balance * line.unit_value)
            line.date_first_receipt = first

    def _compute_age(self):
        today = fields.Date.context_today(self)
        for line in self:
            line.age_days = (today - line.date_first_receipt).days if line.date_first_receipt else 0
            lic = line.licence_id
            line.interest_exposure = line._interest_for(line.duty_balance, today)[0] if lic else 0.0

    def _interest_for(self, duty, on_date):
        self.ensure_one()
        lic = self.licence_id
        if not self.date_first_receipt or not duty:
            return 0.0, 0
        days = (on_date - self.date_first_receipt).days
        extra = days - lic.interest_free_days
        if extra <= 0:
            return 0.0, days
        amount = duty * lic.interest_rate / 100.0 * extra / 365.0
        return (self.currency_id or self.env.company.currency_id).round(amount), days

    @api.onchange('product_id')
    def _onchange_product_id(self):
        tmpl = self.product_id.product_tmpl_id
        if tmpl:
            self.bcd_rate = tmpl.moowr_bcd_rate
            self.sws_rate = tmpl.moowr_sws_rate
            self.igst_rate = tmpl.moowr_igst_rate
            self.tariff_code = tmpl.moowr_tariff_code

    @api.constrains('quantity', 'assessable_value')
    def _check_values(self):
        for line in self:
            if line.quantity <= 0:
                raise ValidationError(self.env._("The declared quantity must be positive."))
            if line.assessable_value < 0:
                raise ValidationError(self.env._("The assessable value cannot be negative."))

    @api.depends('boe_id.name', 'product_id')
    def _compute_display_name(self):
        for line in self:
            line.display_name = f"{line.boe_id.name or ''} / {line.product_id.display_name or ''}"
