# -*- coding: utf-8 -*-
from odoo import fields, models

from .india_gst_utils import UQC_BY_XMLID


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    jw_hsn_code = fields.Char(
        'HSN/SAC (Job Work)', help="HSN code printed on job work challans, E-Way Bill data and ITC-04. "
                                   "If empty, the Indian localization HSN code is used when available.")

    def _jw_get_hsn(self):
        self.ensure_one()
        return self.jw_hsn_code or ('l10n_in_hsn_code' in self._fields and self.l10n_in_hsn_code) or ''


class UomUom(models.Model):
    _inherit = 'uom.uom'

    jw_uqc = fields.Char('GST UQC', help="Unique Quantity Code used for GST reporting (e.g. NOS, KGS, MTR).")

    def _jw_get_uqc(self):
        self.ensure_one()
        if self.jw_uqc:
            return self.jw_uqc.upper()
        if 'l10n_in_code' in self._fields and self.l10n_in_code:
            return self.l10n_in_code.split('-')[0].upper()
        xmlid = self.get_external_id().get(self.id)
        return UQC_BY_XMLID.get(xmlid, 'OTH')
