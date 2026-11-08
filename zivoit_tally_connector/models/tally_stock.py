# -*- coding: utf-8 -*-
"""Inventory side of the Tally connector: godowns, batches and stock moves."""
import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, float_round

from ..lib import matching
from ..lib import tally_xml as TX
from .tally_connection import SYNC_DIR

_logger = logging.getLogger(__name__)

MAIN_GODOWN = 'Main Location'
INVOICE_KINDS = ('sale', 'purchase', 'credit_note', 'debit_note')


class TallyMapping(models.Model):
    _inherit = 'tally.mapping'

    kind = fields.Selection(selection_add=[('godown', 'Godown / Location')],
                            ondelete={'godown': 'cascade'})
    location_id = fields.Many2one('stock.location', index=True, ondelete='set null',
                                  domain=[('usage', '=', 'internal')])

    @api.model
    def _target_fields(self):
        res = super()._target_fields()
        res['godown'] = 'location_id'
        return res

    @api.depends('location_id')
    def _compute_odoo_record(self):
        return super()._compute_odoo_record()


class TallyConnection(models.Model):
    _inherit = 'tally.connection'

    warehouse_id = fields.Many2one(
        'stock.warehouse', 'Warehouse',
        default=lambda s: s.env['stock.warehouse'].search(
            [('company_id', '=', s.env.company.id)], limit=1),
        help="Its stock location is Tally's 'Main Location'; new godowns become locations "
             "inside it.")
    sync_godowns = fields.Selection(SYNC_DIR, 'Godowns', default='both', required=True)
    sync_batches = fields.Boolean('Sync batches as lots/serials', default=True)
    create_stock_moves = fields.Boolean(
        'Create stock moves for Tally invoices', default=True,
        help='Item invoices imported from Tally create and validate the matching '
             'delivery / receipt / return in the godown locations and lots.')

    def _tally_warehouse(self):
        wh = self.warehouse_id or self.env['stock.warehouse'].search(
            [('company_id', '=', self.company_id.id)], limit=1)
        if not wh:
            raise UserError(_('No warehouse found for company %s.') % self.company_id.name)
        return wh

    # ------------------------------------------------------------------ pulls
    def _pulls_wanted(self):
        wants = super()._pulls_wanted()
        if self.sync_godowns != 'none':
            wants.insert(1, 'pull_godowns')
        return wants

    def _on_pull_godowns(self, req, text):
        rows = [TX.normalize_simple_master(r) for r in TX.parse_collection(text, 'GODOWN')]
        Map = self.env['tally.mapping']
        wh = self._tally_warehouse()
        Location = self.env['stock.location']
        locations = Location.search([('usage', '=', 'internal'), ('warehouse_id', '=', wh.id)])
        top = self.last_alter_godown
        for g in rows:
            if not g['name']:
                continue
            m = Map._find(self, 'godown', tally_name=g['name'])
            vals = {'tally_guid': g['guid'], 'tally_master_id': g['master_id'],
                    'tally_parent': g['parent'], 'synced_to_tally': True,
                    'tally_data': json.dumps(g, default=str)}
            if m:
                m.write(vals)
            else:
                m = Map.create(dict(vals, connection_id=self.id, kind='godown',
                                    tally_name=g['name'], origin='tally'))
            m.tally_alter_id = max(g['alter_id'], m.tally_alter_id)
            top = max(top, g['alter_id'])
            if m.location_id or m.state == 'ignored':
                continue
            if g['name'].lower() == MAIN_GODOWN.lower():
                m.write({'location_id': wh.lot_stock_id.id, 'state': 'confirmed',
                         'match_reason': _('Tally default godown')})
                continue
            best, score = Location, 0.0
            for loc in locations:
                sc = max(matching.name_similarity(g['name'], loc.name),
                         matching.name_similarity(g['name'], loc.complete_name))
                if sc > score:
                    best, score = loc, sc
            if best and score >= (self.ai_match_threshold or 0.92):
                m.write({'location_id': best.id, 'state': 'confirmed', 'origin': 'ai',
                         'confidence': score, 'match_reason': 'name %.0f%%' % (score * 100)})
            elif best and score >= (self.ai_suggest_threshold or 0.6):
                m.write({'location_id': best.id, 'state': 'suggested', 'origin': 'ai',
                         'confidence': score, 'match_reason': 'name %.0f%%' % (score * 100)})
            elif self._dir_allows('sync_godowns', 'pull'):
                parent = Map._find(self, 'godown', tally_name=g['parent']).location_id \
                    if g['parent'] else Location
                loc = Location.create({'name': g['name'], 'usage': 'internal',
                                       'location_id': (parent or wh.lot_stock_id).id,
                                       'company_id': self.company_id.id})
                locations |= loc
                m.write({'location_id': loc.id, 'state': 'confirmed'})
        req.records_in = len(rows)
        self.last_alter_godown = top

    def _godown_location(self, godown):
        wh = self._tally_warehouse()
        if not godown or godown.lower() == MAIN_GODOWN.lower():
            m = self.env['tally.mapping']._find(self, 'godown', tally_name=MAIN_GODOWN)
            return (m and m.location_id) or wh.lot_stock_id
        m = self.env['tally.mapping']._find(self, 'godown', tally_name=godown)
        if not m or not m.location_id or m.state == 'ignored':
            raise UserError(_('Tally godown "%s" is not mapped to an Odoo location.') % godown)
        return m.location_id

    # ------------------------------------------------------------------ pushes
    def _ensure_godown(self, location, col):
        wh = self._tally_warehouse()
        Map = self.env['tally.mapping']
        if location == wh.lot_stock_id:
            m = Map._find(self, 'godown', record=location) or Map._upsert(
                self, 'godown', MAIN_GODOWN, {'location_id': location.id, 'state': 'confirmed',
                                              'origin': 'tally', 'synced_to_tally': True})
            return m.tally_name
        m = self._map_record('godown', location, location.name, 'location_id')
        if self._dir_allows('sync_godowns', 'push') and self._needs_push(m, location) \
                and m not in col.godowns:
            col.godowns[m] = TX.build_godown_xml(
                m.tally_name, action='Alter' if m.synced_to_tally else 'Create',
                old_name=m.tally_name if m.synced_to_tally else None)
        return m.tally_name

    def _stock_item_extra(self, product):
        res = super()._stock_item_extra(product)
        if self.sync_batches and product.tracking in ('lot', 'serial'):
            res['batchwise'] = True
            if getattr(product, 'use_expiration_date', False):
                res['expiry'] = True
        return res

    @api.model
    def _invoice_line_stock_moves(self, line, kind):
        moves = self.env['stock.move']
        if 'sale_line_ids' in line._fields:
            moves |= line.sale_line_ids.move_ids
        if 'purchase_line_id' in line._fields:
            moves |= line.purchase_line_id.move_ids
        moves = moves.filtered(lambda m: m.state == 'done' and m.product_id == line.product_id)
        usage = {'sale': ('location_dest_id', 'customer'), 'credit_note': ('location_id', 'customer'),
                 'purchase': ('location_id', 'supplier'),
                 'debit_note': ('location_dest_id', 'supplier')}[kind]
        return moves.filtered(lambda m: m[usage[0]].usage == usage[1])

    def _inventory_batches(self, move, line, qty, amount, kind, col):
        res = super()._inventory_batches(move, line, qty, amount, kind, col)
        if res or self.sync_godowns == 'none' or kind not in INVOICE_KINDS:
            return res
        product = line.product_id
        outgoing = kind in ('sale', 'debit_note')
        groups = {}
        for ml in self._invoice_line_stock_moves(line, kind).move_line_ids:
            loc = ml.location_id if outgoing else ml.location_dest_id
            if loc.usage != 'internal':
                continue
            key = (loc, ml.lot_id)
            groups[key] = groups.get(key, 0.0) + ml.quantity_product_uom
        if not groups:
            loc = self._tally_warehouse().lot_stock_id
            return [{'godown': self._ensure_godown(loc, col), 'batch': '', 'qty': qty,
                     'amount': amount}]
        total = sum(groups.values()) or 1.0
        batches, qty_left, amt_left = [], abs(qty), amount
        items = list(groups.items())
        for i, ((loc, lot), q) in enumerate(items):
            last = i == len(items) - 1
            bq = qty_left if last else float_round(abs(qty) * q / total, precision_digits=4)
            ba = round(amt_left, 2) if last else round(amount * q / total, 2)
            qty_left -= bq
            amt_left -= ba
            expiry = getattr(lot, 'expiration_date', False) if lot else False
            batches.append({
                'godown': self._ensure_godown(loc, col),
                'batch': lot.name if lot and self.sync_batches else '',
                'qty': bq, 'amount': ba,
                'expiry': expiry.date() if expiry else None})
        if not self.sync_batches and product.tracking != 'none':
            for b in batches:
                b['batch'] = ''
        return batches

    def _auto_heal_extra(self, code, arg, col):
        if code == 'missing_godown':
            m = self.env['tally.mapping'].search([('connection_id', '=', self.id),
                                                  ('kind', '=', 'godown'),
                                                  ('tally_name', '=ilike', arg)], limit=1)
            if m and m.location_id:
                m.write({'synced_to_tally': False, 'origin': 'odoo', 'tally_guid': False})
                col.godowns[m] = TX.build_godown_xml(m.tally_name)
                return True
        return super()._auto_heal_extra(code, arg, col)


class AccountMove(models.Model):
    _inherit = 'account.move'

    tally_picking_ids = fields.Many2many('stock.picking', 'account_move_tally_picking_rel',
                                         'move_id', 'picking_id', string='Tally stock moves',
                                         copy=False)


class TallyVoucher(models.Model):
    _inherit = 'tally.voucher'

    picking_ids = fields.Many2many('stock.picking', 'tally_voucher_picking_rel', 'voucher_id',
                                   'picking_id', string='Transfers', readonly=True, copy=False)
    stock_message = fields.Text(readonly=True)

    # ------------------------------------------------------------------
    def _tally_transfer(self, picking_type, src, dest, specs, origin):
        """Create a transfer with explicit move lines and validate it.

        specs: [{'product', 'uom', 'qty', 'lines': [{'src', 'dest', 'lot', 'qty'}]}]
        Returns (picking, warnings).  Warnings leave the transfer unvalidated.
        """
        Picking = self.env['stock.picking']
        Move = self.env['stock.move']
        company = self.company_id
        picking = Picking.create({
            'picking_type_id': picking_type.id, 'partner_id': self.partner_id.id or False,
            'location_id': src.id, 'location_dest_id': dest.id, 'origin': origin,
            'company_id': company.id})
        moves = Move
        for spec in specs:
            moves |= Move.create({
                'picking_id': picking.id, 'product_id': spec['product'].id,
                'product_uom_qty': spec['qty'], 'product_uom': spec['uom'].id,
                'location_id': src.id, 'location_dest_id': dest.id, 'company_id': company.id,
                'origin': origin})
        picking.action_confirm()
        picking.move_line_ids.unlink()   # drop automatic reservations; Tally decides
        warnings = []
        MoveLine = self.env['stock.move.line']
        for move, spec in zip(moves, specs):
            for ln in spec['lines']:
                MoveLine.create({
                    'move_id': move.id, 'picking_id': picking.id, 'product_id': move.product_id.id,
                    'product_uom_id': spec['uom'].id, 'quantity': ln['qty'],
                    'location_id': ln['src'].id, 'location_dest_id': ln['dest'].id,
                    'lot_id': ln['lot'].id if ln.get('lot') else False, 'picked': True,
                    'company_id': company.id})
            if move.product_id.tracking != 'none' and any(not ln.get('lot')
                                                          for ln in spec['lines']):
                warnings.append(_('%s is tracked by lot/serial but Tally sent no batch.')
                                % move.product_id.display_name)
            if move.product_id.tracking == 'serial' and any(
                    float_compare(ln['qty'], 1.0, precision_digits=4) > 0 for ln in spec['lines']):
                warnings.append(_('%s is tracked by serial number: each Tally batch must hold '
                                  'quantity 1.') % move.product_id.display_name)
        moves.write({'picked': True})
        if not warnings:
            picking.with_context(cancel_backorder=True)._action_done()
        return picking, warnings

    def _find_or_create_lot(self, product, name, expiry=None):
        Lot = self.env['stock.lot']
        lot = Lot.search([('name', '=', name), ('product_id', '=', product.id),
                          ('company_id', 'in', (self.company_id.id, False))], limit=1)
        if not lot:
            vals = {'name': name, 'product_id': product.id, 'company_id': self.company_id.id}
            if expiry and 'expiration_date' in Lot._fields:
                vals['expiration_date'] = fields.Datetime.to_datetime(expiry)
            lot = Lot.create(vals)
        return lot

    def _after_import(self, move, data):
        res = super()._after_import(move, data)
        conn = self.connection_id
        ies = data.get('inventory_entries') or []
        if not (conn.create_stock_moves and self.kind in INVOICE_KINDS and ies):
            return res
        wh = conn._tally_warehouse()
        outgoing = self.kind in ('sale', 'debit_note')
        partner_loc = self.env.ref('stock.stock_location_customers') \
            if self.kind in ('sale', 'credit_note') else self.env.ref('stock.stock_location_suppliers')
        ptype = wh.out_type_id if outgoing else wh.in_type_id
        Map = self.env['tally.mapping']
        specs, problems = [], []
        try:
            with self.env.cr.savepoint():
                for ie in ies:
                    pm = Map._find(conn, 'item', tally_name=ie['item'])
                    product = pm.product_id
                    if not product or not product.is_storable:
                        continue
                    uom = conn._uom_for_unit(ie.get('unit')) or product.uom_id
                    batches = ie.get('batches') or [{'godown': '', 'batch': '', 'qty': ie['qty']}]
                    lines = []
                    for b in batches:
                        loc = conn._godown_location(b.get('godown'))
                        lot = False
                        if product.tracking != 'none' and b.get('batch') and conn.sync_batches:
                            lot = self._find_or_create_lot(product, b['batch'], b.get('expiry'))
                        lines.append({'src': loc if outgoing else partner_loc,
                                      'dest': partner_loc if outgoing else loc,
                                      'lot': lot, 'qty': abs(b['qty'])})
                    specs.append({'product': product, 'uom': uom, 'qty': abs(ie['qty']),
                                  'lines': lines})
                if not specs:
                    return res
                picking, problems = self._tally_transfer(
                    ptype, wh.lot_stock_id if outgoing else partner_loc,
                    partner_loc if outgoing else wh.lot_stock_id, specs,
                    '%s %s' % (self.vch_type, self.number))
        except (UserError, ValidationError) as e:
            self.stock_message = _('Stock not updated: %s') % e
            return res
        move.tally_picking_ids = [(4, picking.id)]
        self.picking_ids = [(4, picking.id)]
        self.stock_message = ('\n'.join(problems) + '\n' + _(
            'Transfer %s left to validate manually.') % picking.name) if problems else False
        return res

    def _reverse_stock(self, move):
        res = super()._reverse_stock(move)
        for picking in move.tally_picking_ids:
            if picking.state == 'done':
                specs = []
                for sm in picking.move_ids.filtered(lambda m: m.state == 'done'):
                    specs.append({'product': sm.product_id, 'uom': sm.product_uom,
                                  'qty': sm.quantity, 'lines': [
                                      {'src': ml.location_dest_id, 'dest': ml.location_id,
                                       'lot': ml.lot_id, 'qty': ml.quantity}
                                      for ml in sm.move_line_ids]})
                rtype = picking.picking_type_id.return_picking_type_id or (
                    picking.picking_type_id.warehouse_id.in_type_id
                    if picking.picking_type_code == 'outgoing'
                    else picking.picking_type_id.warehouse_id.out_type_id)
                rev, _w = self._tally_transfer(rtype, picking.location_dest_id,
                                               picking.location_id, specs,
                                               _('Reversal of %s') % picking.name)
                move.tally_picking_ids = [(4, rev.id)]
                self.picking_ids = [(4, rev.id)]
            elif picking.state not in ('cancel',):
                picking.action_cancel()
        return res

    def action_view_pickings(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'stock.picking',
                'name': _('Transfers'), 'view_mode': 'list,form',
                'domain': [('id', 'in', self.picking_ids.ids)]}
