from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    moowr_goods_type = fields.Selection([
        ('input', 'Imported Input'),
        ('capital', 'Capital Goods'),
        ('finished', 'Bonded Product'),
    ], string='MOOWR Goods Type', help="How this product is treated in a MOOWR bonded warehouse.")
    moowr_tariff_code = fields.Char('Customs Tariff Code (CTH)')
    moowr_bcd_rate = fields.Float('BCD %', digits=(6, 2), help="Default basic customs duty rate for bills of entry.")
    moowr_sws_rate = fields.Float('SWS % of BCD', digits=(6, 2), default=10.0,
                                  help="Social welfare surcharge, as a percentage of basic customs duty.")
    moowr_igst_rate = fields.Float('IGST %', digits=(6, 2), default=18.0)
