from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_is_zero

MOVE_TYPES = [
    ('receipt', 'Received into Bond'),
    ('consumption', 'Consumed in Manufacture'),
    ('production', 'Produced in Bond'),
    ('home', 'Cleared for Home Consumption'),
    ('export', 'Exported'),
    ('scrap', 'Scrap / Write-off'),
    ('return', 'Returned to Supplier'),
]
IN_TYPES = ('receipt', 'production')


class MoowrLedger(models.Model):
    _name = 'moowr.ledger'
    _description = 'Bonded Register Entry'
    _order = 'date desc, id desc'
    _check_company_auto = True

    date = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    licence_id = fields.Many2one('moowr.licence', required=True, index=True, check_company=True)
    company_id = fields.Many2one(related='licence_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    move_type = fields.Selection(MOVE_TYPES, 'Movement', required=True, index=True)
    direction = fields.Selection([('in', 'In'), ('out', 'Out')], compute='_compute_direction', store=True)
    goods_kind = fields.Selection([('input', 'Imported Input'), ('finished', 'Bonded Product')],
                                  compute='_compute_direction', store=True, string='Goods')
    product_id = fields.Many2one('product.product', required=True, index=True)
    uom_id = fields.Many2one(related='product_id.uom_id', string='Unit')
    quantity = fields.Float(digits='Product Unit', required=True)
    signed_quantity = fields.Float('Quantity +/-', digits='Product Unit', compute='_compute_direction', store=True)
    boe_line_id = fields.Many2one('moowr.boe.line', 'Bill of Entry Line', index='btree_not_null', ondelete='restrict')
    boe_id = fields.Many2one(related='boe_line_id.boe_id', store=True, string='Bill of Entry', index='btree_not_null')
    batch_id = fields.Many2one('moowr.batch', 'Production Batch', index='btree_not_null', ondelete='restrict')
    output_batch_id = fields.Many2one('moowr.batch', 'Used in Batch', index='btree_not_null')
    production_id = fields.Many2one('mrp.production', 'Manufacturing Order', index='btree_not_null')
    picking_id = fields.Many2one('stock.picking', 'Transfer', index='btree_not_null')
    stock_move_id = fields.Many2one('stock.move', 'Stock Move', index='btree_not_null')
    clearance_id = fields.Many2one('moowr.clearance', 'Clearance', index='btree_not_null', ondelete='set null')
    partner_id = fields.Many2one('res.partner', 'Partner')
    reference = fields.Char()
    value = fields.Monetary('Assessable Value')
    bcd_amount = fields.Monetary('BCD')
    sws_amount = fields.Monetary('SWS')
    igst_amount = fields.Monetary('IGST')
    duty_amount = fields.Monetary('Duty')
    interest_days = fields.Integer('Days in Bond')
    interest_amount = fields.Monetary('Interest')

    @api.depends('move_type', 'quantity', 'batch_id', 'boe_line_id')
    def _compute_direction(self):
        for led in self:
            led.direction = 'in' if led.move_type in IN_TYPES else 'out'
            led.signed_quantity = led.quantity if led.direction == 'in' else -led.quantity
            led.goods_kind = 'finished' if led.batch_id else 'input'

    # ------------------------------------------------------------------
    # Allocation engine (first in, first out)
    # ------------------------------------------------------------------
    @api.model
    def _moowr_sources(self, licence, product):
        """Return available bonded sources for a product, oldest first: production batches, then bill of entry lines."""
        batches = self.env['moowr.batch'].search(
            [('licence_id', '=', licence.id), ('product_id', '=', product.id), ('qty_balance', '>', 0)],
            order='date, id')
        lines = self.env['moowr.boe.line'].search(
            [('licence_id', '=', licence.id), ('product_id', '=', product.id), ('boe_state', '=', 'confirmed'),
             ('qty_balance', '>', 0)], order='boe_date, boe_id, sequence, id')
        return list(batches) + list(lines)

    @api.model
    def _moowr_allocate(self, licence, product, qty, inputs_only=False):
        """Split a quantity over bonded sources. Returns [(source, qty)] and the quantity not covered
        (treated as duty-paid or domestic goods)."""
        rounding = product.uom_id.rounding
        result = []
        remaining = qty
        for src in self._moowr_sources(licence, product):
            if inputs_only and src._name == 'moowr.batch':
                continue
            if float_compare(remaining, 0.0, precision_rounding=rounding) <= 0:
                break
            take = min(remaining, src.qty_balance)
            if float_compare(take, 0.0, precision_rounding=rounding) <= 0:
                continue
            result.append((src, take))
            remaining -= take
        if float_is_zero(remaining, precision_rounding=rounding):
            remaining = 0.0
        return result, remaining

    @api.model
    def _moowr_entry_vals(self, licence, move_type, src, qty, date, **extra):
        cur = licence.currency_id
        if src._name == 'moowr.batch':
            ratio = src.qty_produced and qty / src.qty_produced or 0.0
            vals = {
                'batch_id': src.id,
                'value': cur.round(src.value_content * ratio),
                'bcd_amount': cur.round(src.bcd_content * ratio),
                'sws_amount': cur.round(src.sws_content * ratio),
                'igst_amount': cur.round(src.igst_content * ratio),
            }
        else:
            ratio = src.quantity and qty / src.quantity or 0.0
            defer_igst = licence.defer_igst
            vals = {
                'boe_line_id': src.id,
                'value': cur.round(src.assessable_value * ratio),
                'bcd_amount': cur.round(src.bcd_amount * ratio),
                'sws_amount': cur.round(src.sws_amount * ratio),
                'igst_amount': cur.round(src.igst_amount * ratio) if defer_igst else 0.0,
            }
        vals['duty_amount'] = vals['bcd_amount'] + vals['sws_amount'] + vals['igst_amount']
        vals.update({
            'licence_id': licence.id, 'move_type': move_type, 'product_id': src.product_id.id,
            'quantity': qty, 'date': date,
        })
        if move_type == 'home' and src._name == 'moowr.boe.line':
            interest, days = src._interest_for(vals['duty_amount'], fields.Date.to_date(date))
            vals.update({'interest_amount': interest, 'interest_days': days})
        elif src._name == 'moowr.boe.line' and src.date_first_receipt:
            vals['interest_days'] = (fields.Date.to_date(date) - src.date_first_receipt).days
        vals.update(extra)
        return vals

    def unlink(self):
        if not self.env.context.get('moowr_force_unlink'):
            raise UserError(self.env._("Bonded register entries cannot be deleted. Record a correcting movement instead."))
        return super().unlink()

    def write(self, vals):
        protected = {'quantity', 'move_type', 'product_id', 'boe_line_id', 'batch_id', 'licence_id',
                     'value', 'duty_amount', 'bcd_amount', 'sws_amount', 'igst_amount'}
        if protected & set(vals) and not self.env.context.get('moowr_force_write'):
            raise UserError(self.env._("Bonded register entries cannot be changed. Record a correcting movement instead."))
        return super().write(vals)
