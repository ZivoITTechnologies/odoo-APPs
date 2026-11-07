# -*- coding: utf-8 -*-
"""Small helpers for Indian GST data (state codes, UQC)."""
import re

GSTIN_RE = re.compile(r'^[0-9]{2}[A-Z0-9]{10}[0-9A-Z]Z[0-9A-Z]$')

# GST state codes by ISO 3166-2:IN subdivision code used in Odoo's res.country.state
STATE_CODES = {
    'JK': 1, 'HP': 2, 'PB': 3, 'CH': 4, 'UK': 5, 'UT': 5, 'HR': 6, 'DL': 7, 'RJ': 8, 'UP': 9, 'BR': 10,
    'SK': 11, 'AR': 12, 'NL': 13, 'MN': 14, 'MZ': 15, 'TR': 16, 'ML': 17, 'AS': 18, 'WB': 19, 'JH': 20,
    'OD': 21, 'OR': 21, 'CT': 22, 'CG': 22, 'MP': 23, 'GJ': 24, 'DD': 26, 'DN': 26, 'DH': 26, 'MH': 27,
    'AP': 37, 'KA': 29, 'GA': 30, 'LD': 31, 'KL': 32, 'TN': 33, 'PY': 34, 'AN': 35, 'TS': 36, 'TG': 36,
    'LA': 38,
}

# GST Unique Quantity Codes by uom xml id
UQC_BY_XMLID = {
    'uom.product_uom_unit': 'NOS',
    'uom.product_uom_dozen': 'DOZ',
    'uom.product_uom_pack_6': 'PAC',
    'uom.product_uom_gram': 'GMS',
    'uom.product_uom_kgm': 'KGS',
    'uom.product_uom_ton': 'TON',
    'uom.product_uom_millimeter': 'MTR',
    'uom.product_uom_cm': 'CMS',
    'uom.product_uom_meter': 'MTR',
    'uom.product_uom_km': 'KME',
    'uom.product_uom_square_meter': 'SQM',
    'uom.product_uom_milliliter': 'MLT',
    'uom.product_uom_litre': 'LTR',
    'uom.product_uom_cubic_meter': 'CBM',
    'uom.product_uom_square_foot': 'SQF',
    'uom.product_uom_yard': 'YDS',
    'uom.product_uom_lb': 'OTH',
    'uom.product_uom_oz': 'OTH',
}


def gstin_state_code(gstin):
    if gstin and len(gstin) >= 2 and gstin[:2].isdigit():
        return int(gstin[:2])
    return 0


def partner_state_code(partner):
    """GST state code of a partner: from its GSTIN, else from its state."""
    code = gstin_state_code(partner.vat)
    if code:
        return code
    state = partner.state_id
    if state and state.country_id.code == 'IN':
        return STATE_CODES.get((state.code or '').upper(), 0)
    return 0


def is_valid_gstin(gstin):
    return bool(gstin and GSTIN_RE.match(gstin.strip().upper()))
