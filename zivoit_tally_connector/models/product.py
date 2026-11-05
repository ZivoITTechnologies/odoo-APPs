# -*- coding: utf-8 -*-
from odoo import fields, models

PRODUCT_SYNC_FIELDS = {'name', 'default_code', 'list_price', 'uom_id', 'taxes_id',
                       'l10n_in_hsn_code', 'description_sale'}


class ProductProduct(models.Model):
    _inherit = 'product.product'

    tally_mapping_ids = fields.One2many('tally.mapping', 'product_id')

    def action_tally_push(self):
        for conn in self.env['tally.connection'].sudo().search([]):
            if conn._dir_allows('sync_items', 'push'):
                conn._push_masters(products=self)
                self.env['tally.request'].sudo()._process_queue(conn)
        return True


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get('tally_no_push') and PRODUCT_SYNC_FIELDS & set(vals):
            maps = self.env['tally.mapping'].sudo().search([
                ('product_id', 'in', self.product_variant_ids.ids), ('kind', '=', 'item'),
                ('synced_to_tally', '=', True), ('state', '=', 'confirmed')])
            for conn in maps.mapped('connection_id'):
                if conn._dir_allows('sync_items', 'push'):
                    conn._push_masters(products=maps.filtered(
                        lambda m: m.connection_id == conn).mapped('product_id'))
        return res

    def action_tally_push(self):
        return self.product_variant_ids.action_tally_push()
