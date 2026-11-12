from odoo import api, fields, models


class StockLocation(models.Model):
    _inherit = 'stock.location'

    moowr_licence_id = fields.Many2one('moowr.licence', 'MOOWR Licence', index='btree_not_null',
                                       help="Mark this location (and its sub-locations) as a customs bonded location.")
    moowr_bonded = fields.Boolean('Bonded', compute='_compute_moowr_bonded')

    @api.depends('moowr_licence_id', 'location_id')
    def _compute_moowr_bonded(self):
        for loc in self:
            loc.moowr_bonded = bool(loc._moowr_licence())

    def _moowr_licence(self):
        """Return the licence of the nearest bonded location in the hierarchy (self included)."""
        self.ensure_one()
        if not self.id or self.usage not in ('internal', 'transit'):
            return self.env['moowr.licence']
        ids = [int(x) for x in (self.parent_path or '').split('/') if x]
        if not ids:
            return self.moowr_licence_id
        locs = self.browse(ids).filtered('moowr_licence_id')
        if not locs:
            return self.env['moowr.licence']
        deepest = max(locs, key=lambda l: len(l.parent_path or ''))
        return deepest.moowr_licence_id
