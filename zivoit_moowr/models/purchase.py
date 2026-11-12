from odoo import api, fields, models
from odoo.exceptions import UserError


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    moowr_bonded = fields.Boolean('Bonded', compute='_compute_moowr_bonded', store=True, readonly=False,
                                  help="Goods on this line are imported into the MOOWR bonded warehouse under a bill of entry.")

    @api.depends('product_id', 'order_id.partner_id', 'order_id.picking_type_id')
    def _compute_moowr_bonded(self):
        for line in self:
            tmpl = line.product_id.product_tmpl_id
            partner = line.order_id.partner_id.commercial_partner_id
            company_country = line.order_id.company_id.country_id
            foreign = bool(partner.country_id and company_country and partner.country_id != company_country)
            dest = line.order_id.picking_type_id.default_location_dest_id
            line.moowr_bonded = bool(tmpl.moowr_goods_type in ('input', 'capital') and foreign
                                     and dest and dest.moowr_bonded)


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    moowr_boe_ids = fields.One2many('moowr.boe', 'purchase_id', 'Bills of Entry')
    moowr_boe_count = fields.Integer(compute='_compute_moowr_boe_count')
    moowr_has_bonded = fields.Boolean(compute='_compute_moowr_boe_count')

    @api.depends('moowr_boe_ids', 'order_line.moowr_bonded')
    def _compute_moowr_boe_count(self):
        for po in self:
            po.moowr_boe_count = len(po.moowr_boe_ids)
            po.moowr_has_bonded = any(po.order_line.mapped('moowr_bonded'))

    def action_create_moowr_boe(self):
        self.ensure_one()
        lines = self.order_line.filtered('moowr_bonded')
        if not lines:
            raise UserError(self.env._("No bonded lines on %s. Tick 'Bonded' on the lines imported under MOOWR.", self.name))
        dest = self.picking_type_id.default_location_dest_id
        licence = dest._moowr_licence() if dest else self.env['moowr.licence']
        if not licence:
            licence = self.env['moowr.licence'].search([('company_id', '=', self.company_id.id)], limit=1)
        if not licence:
            raise UserError(self.env._("Create a MOOWR licence for %s first.", self.company_id.name))
        date = fields.Date.context_today(self)
        boe_lines = []
        for line in lines:
            tmpl = line.product_id.product_tmpl_id
            qty = line.product_uom_id._compute_quantity(line.product_qty, line.product_id.uom_id)
            value = self.currency_id._convert(line.price_subtotal, self.company_id.currency_id, self.company_id, date)
            boe_lines.append((0, 0, {
                'product_id': line.product_id.id, 'purchase_line_id': line.id, 'quantity': qty,
                'assessable_value': value, 'bcd_rate': tmpl.moowr_bcd_rate, 'sws_rate': tmpl.moowr_sws_rate,
                'igst_rate': tmpl.moowr_igst_rate, 'tariff_code': tmpl.moowr_tariff_code,
            }))
        boe = self.env['moowr.boe'].create({
            'name': self.env._("Draft BoE for %s", self.name), 'licence_id': licence.id, 'partner_id': self.partner_id.id,
            'purchase_id': self.id, 'line_ids': boe_lines,
        })
        return {'type': 'ir.actions.act_window', 'res_model': 'moowr.boe', 'res_id': boe.id, 'view_mode': 'form'}

    def action_view_moowr_boe(self):
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_moowr.action_moowr_boe')
        action['domain'] = [('purchase_id', 'in', self.ids)]
        action['context'] = {'default_purchase_id': self.id, 'default_partner_id': self.partner_id.id}
        return action
