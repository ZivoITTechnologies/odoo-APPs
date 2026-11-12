from odoo import api, fields, models
from odoo.tools import float_round


class MoowrBatch(models.Model):
    _name = 'moowr.batch'
    _description = 'Bonded Production Batch'
    _order = 'date, id'
    _check_company_auto = True

    name = fields.Char('Reference', required=True)
    licence_id = fields.Many2one('moowr.licence', required=True, index=True, check_company=True)
    company_id = fields.Many2one(related='licence_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    production_id = fields.Many2one('mrp.production', 'Manufacturing Order', index='btree_not_null', check_company=True)
    product_id = fields.Many2one('product.product', required=True, index=True)
    uom_id = fields.Many2one(related='product_id.uom_id', string='Unit')
    date = fields.Datetime(required=True, default=fields.Datetime.now)
    qty_produced = fields.Float('Produced', digits='Product Unit', required=True)
    value_content = fields.Monetary('Imported Input Value')
    bcd_content = fields.Monetary('BCD Content')
    sws_content = fields.Monetary('SWS Content')
    igst_content = fields.Monetary('IGST Content')
    duty_content = fields.Monetary('Duty Content')
    unit_duty = fields.Monetary('Duty per Unit', compute='_compute_unit', store=True)
    unit_value = fields.Monetary('Input Value per Unit', compute='_compute_unit', store=True)
    input_ids = fields.One2many('moowr.ledger', 'output_batch_id', 'Inputs Consumed')
    ledger_ids = fields.One2many('moowr.ledger', 'batch_id', 'Register Entries')
    qty_out = fields.Float('Removed', digits='Product Unit', compute='_compute_balance', store=True)
    qty_balance = fields.Float('Balance in Bond', digits='Product Unit', compute='_compute_balance', store=True)
    duty_balance = fields.Monetary('Duty in Bond', compute='_compute_balance', store=True)
    value_balance = fields.Monetary('Value in Bond', compute='_compute_balance', store=True)

    @api.depends('qty_produced', 'duty_content', 'value_content')
    def _compute_unit(self):
        for batch in self:
            batch.unit_duty = batch.qty_produced and batch.duty_content / batch.qty_produced or 0.0
            batch.unit_value = batch.qty_produced and batch.value_content / batch.qty_produced or 0.0

    @api.depends('ledger_ids.quantity', 'ledger_ids.move_type', 'qty_produced', 'unit_duty', 'unit_value')
    def _compute_balance(self):
        for batch in self:
            out = sum(l.quantity for l in batch.ledger_ids if l.move_type != 'production')
            batch.qty_out = out
            rounding = batch.product_id.uom_id.rounding or 0.0001
            bal = float_round(batch.qty_produced - out, precision_rounding=rounding)
            batch.qty_balance = bal
            cur = batch.currency_id or self.env.company.currency_id
            batch.duty_balance = cur.round(bal * batch.unit_duty)
            batch.value_balance = cur.round(bal * batch.unit_value)
