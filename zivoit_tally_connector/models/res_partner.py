# -*- coding: utf-8 -*-
from odoo import fields, models

PARTNER_SYNC_FIELDS = {'name', 'vat', 'street', 'street2', 'city', 'zip', 'state_id',
                       'country_id', 'email', 'phone'}


class ResPartner(models.Model):
    _inherit = 'res.partner'

    tally_mapping_ids = fields.One2many('tally.mapping', 'partner_id')
    tally_ledger = fields.Char(compute='_compute_tally_ledger')

    def _compute_tally_ledger(self):
        for p in self:
            p.tally_ledger = ', '.join(p.tally_mapping_ids.filtered(
                lambda m: m.kind == 'party' and m.state == 'confirmed').mapped('tally_name'))

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get('tally_no_push') and PARTNER_SYNC_FIELDS & set(vals):
            self._tally_push_changed()
        return res

    def _tally_push_changed(self):
        maps = self.env['tally.mapping'].sudo().search([
            ('partner_id', 'in', self.commercial_partner_id.ids), ('kind', '=', 'party'),
            ('synced_to_tally', '=', True), ('state', '=', 'confirmed')])
        for conn in maps.mapped('connection_id'):
            if conn._dir_allows('sync_parties', 'push'):
                conn._push_masters(partners=maps.filtered(
                    lambda m: m.connection_id == conn).mapped('partner_id'))

    def action_tally_push(self):
        for conn in self.env['tally.connection'].sudo().search([]):
            if conn._dir_allows('sync_parties', 'push'):
                conn._push_masters(partners=self.commercial_partner_id)
                self.env['tally.request'].sudo()._process_queue(conn)
        return True
