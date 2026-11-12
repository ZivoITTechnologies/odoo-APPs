from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_is_zero


class StockMove(models.Model):
    _inherit = 'stock.move'

    moowr_boe_line_id = fields.Many2one(
        'moowr.boe.line', 'Bill of Entry Line', copy=False, index='btree_not_null',
        domain="[('product_id', '=', product_id), ('boe_state', '=', 'confirmed')]",
        help="Bill of entry line that covers this bonded receipt. Leave empty to match automatically.")
    moowr_ledger_ids = fields.One2many('moowr.ledger', 'stock_move_id', 'MOOWR Register Entries')

    def _action_done(self, cancel_backorder=False):
        moves = super()._action_done(cancel_backorder=cancel_backorder)
        if not self.env.context.get('moowr_skip'):
            done = moves.filtered(lambda m: m.state == 'done' and not m.moowr_ledger_ids)
            # inputs first so that finished goods receive the duty content of what was consumed
            ordered = done.filtered('raw_material_production_id') + done.filtered(
                lambda m: not m.raw_material_production_id and m.production_id) + done.filtered(
                lambda m: not m.raw_material_production_id and not m.production_id)
            for move in ordered:
                move.sudo()._moowr_process()
            boes = ordered.sudo().moowr_ledger_ids.boe_id
            if boes:
                boes._auto_close()
        return moves

    # ------------------------------------------------------------------
    def _moowr_qty(self):
        self.ensure_one()
        return self.product_uom._compute_quantity(self.quantity, self.product_id.uom_id)

    def _moowr_process(self):
        self.ensure_one()
        if not self.product_id.is_storable:
            return
        qty = self._moowr_qty()
        if float_is_zero(qty, precision_rounding=self.product_id.uom_id.rounding):
            return
        src_lic = self.location_id._moowr_licence()
        dst_lic = self.location_dest_id._moowr_licence()
        if src_lic and dst_lic:
            if src_lic != dst_lic:
                raise UserError(self.env._(
                    "Moving %s directly between two MOOWR licences is not supported. "
                    "Clear the goods from %s first.", self.product_id.display_name, src_lic.name))
            return
        if dst_lic:
            if self.production_id:
                self._moowr_production_in(dst_lic, qty)
            elif self.location_id.usage in ('supplier', 'transit') or self.purchase_line_id:
                self._moowr_receipt(dst_lic, qty)
            return
        if src_lic:
            if self.raw_material_production_id or self.location_dest_id.usage == 'production':
                self._moowr_consumption(src_lic, qty)
            elif self.location_dest_id.usage == 'supplier':
                self._moowr_removal(src_lic, qty, 'return')
            elif self.scrap_id or self.location_dest_id.usage == 'inventory':
                self._moowr_removal(src_lic, qty, 'scrap')
            else:
                removal = self.picking_id._moowr_removal_type() if self.picking_id else 'home'
                self._moowr_removal(src_lic, qty, removal)
            return
        # finished goods produced from bonded inputs but stored outside the bond: produce and clear at once
        if self.production_id:
            pending = self.env['moowr.ledger'].search([
                ('production_id', '=', self.production_id.id), ('move_type', '=', 'consumption'),
                ('output_batch_id', '=', False)])
            lic = pending[:1].licence_id
            if lic and self.product_id == self.production_id.product_id:
                self._moowr_production_in(lic, qty)
                self._moowr_removal(lic, qty, 'home')

    def _moowr_common(self, licence):
        return {
            'licence_id': licence.id, 'stock_move_id': self.id, 'picking_id': self.picking_id.id,
            'partner_id': (self.picking_id.partner_id or self.partner_id).id,
            'reference': self.picking_id.name or self.reference or self.production_id.name
                         or self.raw_material_production_id.name or self.scrap_id.name or '',
        }

    def _moowr_receipt(self, licence, qty):
        Ledger = self.env['moowr.ledger']
        rounding = self.product_id.uom_id.rounding
        remaining = qty
        candidates = self.env['moowr.boe.line']
        if self.moowr_boe_line_id:
            candidates = self.moowr_boe_line_id
        else:
            base = [('licence_id', '=', licence.id), ('product_id', '=', self.product_id.id), ('boe_state', '=', 'confirmed')]
            if self.purchase_line_id:
                candidates = self.env['moowr.boe.line'].search(base + [('purchase_line_id', '=', self.purchase_line_id.id)],
                                                                order='boe_date, id')
                candidates |= self.env['moowr.boe.line'].search(base + [('boe_id.purchase_id', '=', self.purchase_line_id.order_id.id)],
                                                                 order='boe_date, id')
            if not candidates and not self.purchase_line_id:
                candidates = self.env['moowr.boe.line'].search(base, order='boe_date, id')
        vals_list = []
        for line in candidates:
            if line.licence_id != licence or line.boe_state != 'confirmed' or line.product_id != self.product_id:
                continue
            open_qty = line.quantity - line.qty_received
            if float_compare(open_qty, 0.0, precision_rounding=rounding) <= 0:
                continue
            take = min(open_qty, remaining)
            vals = Ledger._moowr_entry_vals(licence, 'receipt', line, take, self.date, **self._moowr_common(licence))
            vals.pop('interest_days', None)
            vals_list.append(vals)
            remaining -= take
            if float_compare(remaining, 0.0, precision_rounding=rounding) <= 0:
                break
        bonded_line = self.purchase_line_id.moowr_bonded or bool(self.moowr_boe_line_id)
        if float_compare(remaining, 0.0, precision_rounding=rounding) > 0 and bonded_line and licence.require_boe:
            raise UserError(self.env._(
                "%(qty)s %(uom)s of %(product)s received into bonded location %(loc)s are not covered by a confirmed "
                "bill of entry. Confirm the bill of entry for this import (or increase its declared quantity) "
                "before validating the receipt.",
                qty=round(remaining, 4), uom=self.product_id.uom_id.name, product=self.product_id.display_name,
                loc=self.location_dest_id.display_name))
        if vals_list:
            Ledger.create(vals_list)

    def _moowr_consumption(self, licence, qty):
        Ledger = self.env['moowr.ledger']
        allocations, _rest = Ledger._moowr_allocate(licence, self.product_id, qty)
        production = self.raw_material_production_id
        vals_list = []
        for src, take in allocations:
            vals = Ledger._moowr_entry_vals(licence, 'consumption', src, take, self.date, **self._moowr_common(licence))
            vals['production_id'] = production.id
            vals.pop('interest_amount', None)
            vals_list.append(vals)
        if vals_list:
            Ledger.create(vals_list)

    def _moowr_production_in(self, licence, qty):
        Ledger = self.env['moowr.ledger']
        production = self.production_id
        is_main = self.product_id == production.product_id
        inputs = Ledger.browse()
        if is_main:
            inputs = Ledger.search([('production_id', '=', production.id), ('move_type', '=', 'consumption'),
                                    ('output_batch_id', '=', False)])
        batch = self.env['moowr.batch'].create({
            'name': production.name if is_main else f"{production.name} / {self.product_id.display_name}",
            'licence_id': licence.id, 'production_id': production.id, 'product_id': self.product_id.id,
            'date': self.date, 'qty_produced': qty,
            'value_content': sum(inputs.mapped('value')), 'bcd_content': sum(inputs.mapped('bcd_amount')),
            'sws_content': sum(inputs.mapped('sws_amount')), 'igst_content': sum(inputs.mapped('igst_amount')),
            'duty_content': sum(inputs.mapped('duty_amount')),
        })
        if inputs:
            inputs.with_context(moowr_force_write=True).write({'output_batch_id': batch.id})
        common = self._moowr_common(licence)
        Ledger.create({
            **common, 'move_type': 'production', 'product_id': self.product_id.id, 'quantity': qty,
            'batch_id': batch.id, 'production_id': production.id, 'date': self.date,
            'value': batch.value_content, 'bcd_amount': batch.bcd_content, 'sws_amount': batch.sws_content,
            'igst_amount': batch.igst_content, 'duty_amount': batch.duty_content,
        })
        return batch

    def _moowr_removal(self, licence, qty, removal_type):
        Ledger = self.env['moowr.ledger']
        inputs_only = removal_type == 'return'
        allocations, _rest = Ledger._moowr_allocate(licence, self.product_id, qty, inputs_only=inputs_only)
        if not allocations:
            return
        clearance = self._moowr_get_clearance(licence, removal_type)
        common = self._moowr_common(licence)
        common['clearance_id'] = clearance.id
        vals_list = []
        for src, take in allocations:
            vals = Ledger._moowr_entry_vals(licence, removal_type, src, take, self.date, **common)
            if removal_type != 'home':
                vals['interest_amount'] = 0.0
            vals_list.append(vals)
        Ledger.create(vals_list)

    def _moowr_get_clearance(self, licence, removal_type):
        Clearance = self.env['moowr.clearance']
        domain = [('licence_id', '=', licence.id), ('clearance_type', '=', removal_type), ('state', '=', 'draft')]
        if self.picking_id:
            clearance = Clearance.search(domain + [('picking_id', '=', self.picking_id.id)], limit=1)
            if clearance:
                return clearance
        production = self.production_id or self.raw_material_production_id
        return Clearance.create({
            'licence_id': licence.id, 'clearance_type': removal_type,
            'date': fields.Date.to_date(self.date) or fields.Date.context_today(self),
            'partner_id': (self.picking_id.partner_id or self.partner_id).id,
            'picking_id': self.picking_id.id, 'production_id': production.id,
            'document_number': self.picking_id.moowr_document_number or False,
        })
