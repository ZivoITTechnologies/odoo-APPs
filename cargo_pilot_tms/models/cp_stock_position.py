# -*- coding: utf-8 -*-
"""Stock 360: one view of where every product is - at the supplier / OEM (incoming), in our
warehouses, on trucks, and at sales channels (marketplace warehouses, FBA, retail DCs)."""
from collections import defaultdict

from odoo import api, fields, models, _

BUCKETS = [('incoming', 'At Supplier / Incoming'), ('warehouse', 'In Warehouse'), ('reserved', 'Reserved'),
           ('transit', 'On Trucks'), ('channel', 'At Channels')]


class CpStockPosition(models.Model):
    _name = 'cp.stock.position'
    _description = 'Stock 360 Position'
    _order = 'product_id, bucket, place'

    company_id = fields.Many2one('res.company', required=True, index=True)
    product_id = fields.Many2one('product.product', index=True)
    sku = fields.Char(string='SKU')
    product_name = fields.Char(string='Product Name')
    categ_id = fields.Many2one('product.category', related='product_id.categ_id', store=True)
    bucket = fields.Selection(BUCKETS, required=True, index=True)
    place = fields.Char(required=True, help='Warehouse, supplier, shipment or channel location.')
    warehouse_id = fields.Many2one('stock.warehouse')
    trip_id = fields.Many2one('cp.trip', string='Shipment')
    quantity = fields.Float()
    value = fields.Monetary()
    currency_id = fields.Many2one(related='company_id.currency_id')
    refreshed_at = fields.Datetime()

    @api.model
    def cp_refresh(self, company=None):
        companies = company or self.env['res.company'].search([])
        now = fields.Datetime.now()
        for comp in companies:
            self.sudo().search([('company_id', '=', comp.id)]).unlink()
            rows = self._cp_rows(comp)
            for row in rows:
                row.update(company_id=comp.id, refreshed_at=now)
                product = row.get('product_id') and self.env['product.product'].browse(row['product_id'])
                if product:
                    row.setdefault('sku', product.default_code)
                    row.setdefault('product_name', product.display_name)
                    row.setdefault('value', row['quantity'] * product.standard_price)
            self.sudo().create(rows)
        return True

    @api.model
    def _cp_rows(self, company):
        rows = []
        # In warehouse (on hand) and reserved
        Quant = self.env['stock.quant'].sudo()
        grouped = Quant._read_group(
            [('company_id', '=', company.id), ('location_id.usage', '=', 'internal'),
             ('location_id.warehouse_id', '!=', False)],
            ['product_id', 'location_id'], ['quantity:sum', 'reserved_quantity:sum'])
        on_hand = defaultdict(lambda: [0.0, 0.0])
        for product, location, qty, reserved in grouped:
            key = (product, location.warehouse_id)
            on_hand[key][0] += qty
            on_hand[key][1] += reserved
        for (product, warehouse), (qty, reserved) in on_hand.items():
            if qty - reserved:
                rows.append({'product_id': product.id, 'bucket': 'warehouse', 'place': warehouse.name,
                             'warehouse_id': warehouse.id, 'quantity': qty - reserved})
            if reserved:
                rows.append({'product_id': product.id, 'bucket': 'reserved', 'place': warehouse.name,
                             'warehouse_id': warehouse.id, 'quantity': reserved})
        # Incoming from suppliers / OEM: open receipts
        Move = self.env['stock.move'].sudo()
        incoming = defaultdict(float)
        for move in Move.search([('company_id', '=', company.id), ('state', 'not in', ('done', 'cancel', 'draft')),
                                 ('picking_code', '=', 'incoming')]):
            key = (move.product_id.id, move.picking_id.partner_id.display_name or _('Supplier'),
                   move.picking_type_id.warehouse_id.id)
            incoming[key] += move.product_qty
        for (product_id, supplier, warehouse_id), qty in incoming.items():
            rows.append({'product_id': product_id, 'bucket': 'incoming', 'place': supplier,
                         'warehouse_id': warehouse_id or False, 'quantity': qty})
        # On trucks: deliveries linked to shipments on the road
        transit = defaultdict(float)
        live = ('assigned', 'reported', 'loading', 'in_transit', 'arrived')
        trips = self.env['cp.trip'].sudo().search([('company_id', '=', company.id), ('state', 'in', live)])
        for trip in trips:
            for picking in trip.order_ids.picking_id:
                for move in picking.move_ids.filtered(lambda m: m.state != 'cancel'):
                    transit[(move.product_id.id, trip.id)] += move.quantity or move.product_qty
            for line in trip.order_ids.filtered(lambda l: not l.picking_id and l.product_id):
                transit[(line.product_id.id, trip.id)] += line.quantity
        for (product_id, trip_id), qty in transit.items():
            trip = self.env['cp.trip'].browse(trip_id)
            rows.append({'product_id': product_id, 'bucket': 'transit', 'trip_id': trip_id,
                         'place': '%s (%s)' % (trip.name, trip.lane_id.name or ''), 'quantity': qty})
        # At channels (EasyEcom or any add-on that fills cp.channel.stock)
        if 'cp.channel.stock' in self.env:
            for line in self.env['cp.channel.stock'].sudo().search([('company_id', '=', company.id)]):
                if line.available_qty:
                    rows.append({'product_id': line.product_id.id or False, 'sku': line.sku,
                                 'product_name': line.product_name or line.sku, 'bucket': 'channel',
                                 'place': line.warehouse_name or line.location_key or _('Channel'),
                                 'quantity': line.available_qty, 'value': 0.0 if not line.product_id else
                                 line.available_qty * line.product_id.standard_price})
        return rows

    @api.model
    def action_refresh(self):
        self.cp_refresh(self.env.company)
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    @api.model
    def _cron_refresh(self):
        return self.cp_refresh()
