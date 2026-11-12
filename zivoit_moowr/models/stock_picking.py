from odoo import api, fields, models


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    moowr_clearance_type = fields.Selection([
        ('auto', 'Automatic'), ('home', 'Home Consumption'), ('export', 'Export'),
    ], string='MOOWR Removal', default='auto',
        help="Automatic: export when the delivery partner is outside the company country, otherwise home consumption.")
    moowr_document_number = fields.Char('Customs Document No.',
                                        help="Shipping bill, bill of export or ex-bond bill of entry number.")
    moowr_is_bonded = fields.Boolean(compute='_compute_moowr_is_bonded')
    moowr_clearance_ids = fields.One2many('moowr.clearance', 'picking_id', 'MOOWR Clearances')
    moowr_clearance_count = fields.Integer(compute='_compute_moowr_is_bonded')

    @api.depends('location_id', 'location_dest_id', 'moowr_clearance_ids')
    def _compute_moowr_is_bonded(self):
        for pick in self:
            pick.moowr_is_bonded = bool(pick.location_id.moowr_bonded or pick.location_dest_id.moowr_bonded)
            pick.moowr_clearance_count = len(pick.moowr_clearance_ids)

    def _moowr_removal_type(self):
        self.ensure_one()
        if self.moowr_clearance_type in ('home', 'export'):
            return self.moowr_clearance_type
        partner = self.partner_id.commercial_partner_id
        company_country = self.company_id.country_id
        if partner.country_id and company_country and partner.country_id != company_country:
            return 'export'
        return 'home'

    def action_view_moowr_clearances(self):
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_moowr.action_moowr_clearance')
        action['domain'] = [('picking_id', 'in', self.ids)]
        action['context'] = {}
        return action
