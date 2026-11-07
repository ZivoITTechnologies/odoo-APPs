# -*- coding: utf-8 -*-
from odoo import api, fields, models

from .cp_easyecom import pick, to_float


class CpChannelStock(models.Model):
    """Stock that EasyEcom reports per SKU and warehouse (marketplace, FBA, retail DC...)."""
    _name = 'cp.channel.stock'
    _description = 'Channel Stock'
    _order = 'sku, location_key'

    account_id = fields.Many2one('cp.easyecom.account', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='account_id.company_id', store=True)
    sku = fields.Char(string='SKU', required=True, index=True)
    product_id = fields.Many2one('product.product', compute='_compute_product', store=True)
    product_name = fields.Char()
    location_key = fields.Char(string='Location Key', index=True)
    warehouse_name = fields.Char(string='Warehouse')
    available_qty = fields.Float(string='Available')
    reserved_qty = fields.Float(string='Reserved')
    virtual_qty = fields.Float(string='Virtual')
    updated_at = fields.Datetime(default=fields.Datetime.now)

    _sku_location_unique = models.Constraint('unique(account_id, sku, location_key)',
                                             'One stock line per SKU and warehouse.')

    @api.depends('sku')
    def _compute_product(self):
        Product = self.env['product.product']
        for rec in self:
            rec.product_id = Product.search([('default_code', '=', rec.sku)], limit=1) if rec.sku else Product

    @api.model
    def cp_apply(self, account, rows):
        """Upsert stock rows from the inventory API or webhook. Returns how many rows were applied."""
        if isinstance(rows, dict):
            rows = pick(rows, 'inventoryData', 'inventory', 'data', default=[rows])
            rows = rows if isinstance(rows, list) else [rows]
        count = 0
        now = fields.Datetime.now()
        for row in rows or []:
            sku = str(pick(row, 'sku', 'SKU', 'skuCode', default='') or '').strip()
            if not sku:
                continue
            locations = pick(row, 'locations', 'warehouses', default=None)
            entries = locations if isinstance(locations, list) else [row]
            for entry in entries:
                key = str(pick(entry, 'location_key', 'locationKey', 'warehouseId', 'warehouse_id',
                               default=pick(row, 'location_key', 'warehouseId', default='')) or '')
                vals = {
                    'product_name': pick(row, 'productName', 'product_name', 'name', default=False),
                    'warehouse_name': pick(entry, 'location_name', 'warehouseName', 'companyName', default=False),
                    'available_qty': to_float(pick(entry, 'availableInventory', 'available_inventory', 'inventory',
                                                   'quantity', 'qty')),
                    'reserved_qty': to_float(pick(entry, 'reservedInventory', 'reserved_inventory', 'reserved')),
                    'virtual_qty': to_float(pick(entry, 'virtualInventory', 'virtual_inventory')),
                    'updated_at': now,
                }
                line = self.search([('account_id', '=', account.id), ('sku', '=', sku), ('location_key', '=', key)],
                                   limit=1)
                if line:
                    line.write(vals)
                else:
                    self.create(dict(vals, account_id=account.id, sku=sku, location_key=key))
                count += 1
        return count
