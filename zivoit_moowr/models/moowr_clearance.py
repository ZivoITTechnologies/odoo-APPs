from odoo import api, fields, models
from odoo.exceptions import UserError


class MoowrClearance(models.Model):
    _name = 'moowr.clearance'
    _description = 'MOOWR Removal / Ex-bond Clearance'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc, id desc'
    _check_company_auto = True

    name = fields.Char('Reference', required=True, copy=False, readonly=True, default=lambda self: self.env._('New'))
    licence_id = fields.Many2one('moowr.licence', required=True, index=True, check_company=True, tracking=True)
    company_id = fields.Many2one(related='licence_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    clearance_type = fields.Selection([
        ('home', 'Home Consumption'), ('export', 'Export'), ('scrap', 'Scrap / Write-off'),
        ('return', 'Return to Supplier'),
    ], required=True, tracking=True)
    date = fields.Date(required=True, default=fields.Date.context_today, tracking=True)
    partner_id = fields.Many2one('res.partner', tracking=True)
    picking_id = fields.Many2one('stock.picking', 'Transfer', check_company=True)
    production_id = fields.Many2one('mrp.production', 'Manufacturing Order', check_company=True)
    document_number = fields.Char('Customs Document No.', tracking=True,
                                  help="Ex-bond bill of entry for home consumption, shipping bill or bill of export number.")
    document_date = fields.Date('Customs Document Date', tracking=True)
    payment_reference = fields.Char('Duty Payment Reference', tracking=True, help="Challan or e-payment reference.")
    payment_date = fields.Date('Duty Paid On', tracking=True)
    destroyed = fields.Boolean('Destroyed under Supervision', tracking=True,
                               help="Scrap destroyed in the presence of the proper officer: no duty is payable.")
    ledger_ids = fields.One2many('moowr.ledger', 'clearance_id', 'Goods Removed')
    state = fields.Selection([
        ('draft', 'To File'), ('filed', 'Filed'), ('paid', 'Duty Paid'), ('done', 'Done'), ('cancel', 'Cancelled'),
    ], default='draft', required=True, tracking=True, copy=False)
    note = fields.Text()

    amount_value = fields.Monetary('Assessable Value', compute='_compute_amounts', store=True)
    amount_bcd = fields.Monetary('BCD', compute='_compute_amounts', store=True)
    amount_sws = fields.Monetary('SWS', compute='_compute_amounts', store=True)
    amount_igst = fields.Monetary('IGST', compute='_compute_amounts', store=True)
    amount_duty = fields.Monetary('Duty', compute='_compute_amounts', store=True)
    amount_interest = fields.Monetary('Interest', compute='_compute_amounts', store=True)
    amount_payable = fields.Monetary('Amount Payable', compute='_compute_amounts', store=True, tracking=True)
    duty_forgone = fields.Monetary('Duty Not Payable', compute='_compute_amounts', store=True,
                                   help="Duty that does not become payable because the goods were exported, returned or destroyed.")

    @api.depends('ledger_ids.value', 'ledger_ids.duty_amount', 'ledger_ids.interest_amount', 'clearance_type', 'destroyed',
                 'ledger_ids.bcd_amount', 'ledger_ids.sws_amount', 'ledger_ids.igst_amount')
    def _compute_amounts(self):
        for cl in self:
            leds = cl.ledger_ids
            cl.amount_value = sum(leds.mapped('value'))
            cl.amount_bcd = sum(leds.mapped('bcd_amount'))
            cl.amount_sws = sum(leds.mapped('sws_amount'))
            cl.amount_igst = sum(leds.mapped('igst_amount'))
            cl.amount_duty = sum(leds.mapped('duty_amount'))
            cl.amount_interest = sum(leds.mapped('interest_amount'))
            if cl._is_duty_payable():
                cl.amount_payable = cl.amount_duty + cl.amount_interest
                cl.duty_forgone = 0.0
            else:
                cl.amount_payable = 0.0
                cl.duty_forgone = cl.amount_duty

    def _is_duty_payable(self):
        self.ensure_one()
        return self.clearance_type == 'home' or (self.clearance_type == 'scrap' and not self.destroyed)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', self.env._('New')) == self.env._('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('moowr.clearance') or self.env._('New')
        return super().create(vals_list)

    def action_file(self):
        for cl in self:
            if not cl.document_number:
                raise UserError(self.env._("Enter the customs document number for %s before filing.", cl.name))
            if cl._is_duty_payable():
                cl.state = 'filed'
            else:
                cl.state = 'done'

    def action_mark_paid(self):
        for cl in self:
            if not cl._is_duty_payable():
                raise UserError(self.env._("No duty is payable on %s.", cl.name))
            if not cl.payment_reference:
                raise UserError(self.env._("Enter the duty payment reference for %s.", cl.name))
            cl.write({'state': 'paid', 'payment_date': cl.payment_date or fields.Date.context_today(cl)})

    def action_mark_destroyed(self):
        for cl in self:
            if cl.clearance_type != 'scrap':
                raise UserError(self.env._("Only scrap removals can be marked as destroyed."))
            cl.write({'destroyed': True, 'state': 'done'})

    def action_reset_draft(self):
        self.filtered(lambda c: c.state in ('filed', 'cancel')).write({'state': 'draft'})

    def unlink(self):
        if self.ledger_ids:
            raise UserError(self.env._("A clearance linked to bonded register entries cannot be deleted."))
        return super().unlink()

    def action_print(self):
        return self.env.ref('zivoit_moowr.action_report_moowr_clearance').report_action(self)
