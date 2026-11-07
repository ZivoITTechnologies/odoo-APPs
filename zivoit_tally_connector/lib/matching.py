# -*- coding: utf-8 -*-
"""Deterministic 'AI' matching engine (no external calls).

Scores candidate pairs between Tally masters and Odoo records using strong
identifiers (GSTIN, PAN, HSN, e-mail, phone) and normalised fuzzy names.  The
LLM layer is only consulted for the ambiguous middle band, which keeps cost
low and makes the connector fully functional offline.
"""
import re
from difflib import SequenceMatcher

GSTIN_RE = re.compile(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$')

_NOISE = {
    'm/s', 'ms', 'messrs', 'the', 'pvt', 'private', 'ltd', 'limited', 'llp', 'inc', 'co',
    'company', 'corp', 'corporation', 'and', '&', 'a/c', 'ac', 'account', 'india', 'pvtltd',
    'enterprises', 'enterprise',
}
_ABBR = {'intl': 'international', 'mfg': 'manufacturing', 'engg': 'engineering',
         'tech': 'technologies', 'techno': 'technologies', 'svcs': 'services', 'sol': 'solutions',
         'distr': 'distributors', 'trdg': 'trading', 'bros': 'brothers'}

# GST state codes (first two digits of a GSTIN)
GST_STATE_CODES = {
    '01': 'Jammu & Kashmir', '02': 'Himachal Pradesh', '03': 'Punjab', '04': 'Chandigarh',
    '05': 'Uttarakhand', '06': 'Haryana', '07': 'Delhi', '08': 'Rajasthan', '09': 'Uttar Pradesh',
    '10': 'Bihar', '11': 'Sikkim', '12': 'Arunachal Pradesh', '13': 'Nagaland', '14': 'Manipur',
    '15': 'Mizoram', '16': 'Tripura', '17': 'Meghalaya', '18': 'Assam', '19': 'West Bengal',
    '20': 'Jharkhand', '21': 'Odisha', '22': 'Chhattisgarh', '23': 'Madhya Pradesh',
    '24': 'Gujarat', '26': 'Dadra & Nagar Haveli and Daman & Diu', '27': 'Maharashtra',
    '29': 'Karnataka', '30': 'Goa', '31': 'Lakshadweep', '32': 'Kerala', '33': 'Tamil Nadu',
    '34': 'Puducherry', '35': 'Andaman & Nicobar Islands', '36': 'Telangana',
    '37': 'Andhra Pradesh', '38': 'Ladakh', '97': 'Other Territory',
}


def normalize_name(name):
    name = (name or '').lower()
    name = re.sub(r'[\.\,\(\)\-_/\\\'"]', ' ', name)
    tokens = []
    for t in name.split():
        t = _ABBR.get(t, t)
        if t not in _NOISE:
            tokens.append(t)
    return ' '.join(tokens)


def _digits(s):
    return re.sub(r'\D', '', s or '')[-10:]


def name_similarity(a, b):
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jac = len(ta & tb) / float(len(ta | tb)) if ta | tb else 0.0
    contain = 0.9 if (na in nb or nb in na) and min(len(na), len(nb)) >= 4 else 0.0
    return max(seq * 0.6 + jac * 0.4, contain)


def valid_gstin(gstin):
    """Structural + checksum validation of an Indian GSTIN."""
    g = (gstin or '').strip().upper()
    if not GSTIN_RE.match(g):
        return False
    chars = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    total = 0
    for i, c in enumerate(g[:14]):
        v = chars.index(c) * (1 if i % 2 == 0 else 2)
        total += v // 36 + v % 36
    check = chars[(36 - total % 36) % 36]
    return check == g[14]


def state_from_gstin(gstin):
    return GST_STATE_CODES.get((gstin or '')[:2], '')


def score_party(tally, odoo):
    """tally/odoo: dicts with name, gstin, pan, email, phone, mobile.  Returns (score, reason)."""
    tg, og = (tally.get('gstin') or '').upper(), (odoo.get('gstin') or '').upper()
    if tg and og:
        if tg == og:
            return 1.0, 'GSTIN match'
        if tg[2:12] == og[2:12]:
            sim = name_similarity(tally.get('name'), odoo.get('name'))
            return 0.80 + 0.15 * sim, 'Same PAN, different state GSTIN'
        # Different GSTINs are a strong negative signal
        return 0.2 * name_similarity(tally.get('name'), odoo.get('name')), 'GSTIN differs'
    score = name_similarity(tally.get('name'), odoo.get('name'))
    reasons = ['name %.0f%%' % (score * 100)]
    te, oe = (tally.get('email') or '').lower(), (odoo.get('email') or '').lower()
    if te and oe and te == oe:
        score = min(1.0, score + 0.25)
        reasons.append('email')
    tp = {_digits(tally.get('phone')), _digits(tally.get('mobile'))} - {''}
    op = {_digits(odoo.get('phone')), _digits(odoo.get('mobile'))} - {''}
    if tp & op:
        score = min(1.0, score + 0.2)
        reasons.append('phone')
    if tally.get('pan') and tally.get('pan') == odoo.get('pan'):
        score = min(1.0, score + 0.3)
        reasons.append('PAN')
    return score, ', '.join(reasons)


def score_product(tally, odoo):
    """tally/odoo: name, code/part_no, hsn."""
    tc, oc = (tally.get('part_no') or '').strip().lower(), (odoo.get('code') or '').strip().lower()
    if tc and oc and tc == oc:
        return 1.0, 'part number / internal reference'
    score = name_similarity(tally.get('name'), odoo.get('name'))
    reasons = ['name %.0f%%' % (score * 100)]
    if oc and normalize_name(oc) == normalize_name(tally.get('name')):
        return 0.97, 'name equals internal reference'
    th, oh = (tally.get('hsn') or '').strip(), (odoo.get('hsn') or '').strip()
    if th and oh:
        if th == oh:
            score = min(1.0, score + 0.1)
            reasons.append('HSN')
        else:
            score *= 0.8
            reasons.append('HSN differs')
    return score, ', '.join(reasons)


def score_account(tally_ledger, odoo_account):
    """Match a Tally P&L / balance-sheet ledger to an Odoo account."""
    score = name_similarity(tally_ledger.get('name'), odoo_account.get('name'))
    code = (odoo_account.get('code') or '').lower()
    if code and code in (tally_ledger.get('name') or '').lower():
        score = max(score, 0.9)
    role = tally_ledger.get('role')
    atype = odoo_account.get('account_type') or ''
    compatible = {
        'income': ('income', 'income_other'),
        'expense': ('expense', 'expense_direct_cost', 'expense_depreciation'),
        'bank': ('asset_cash',), 'cash': ('asset_cash',),
        'asset': ('asset_current', 'asset_non_current', 'asset_fixed', 'asset_prepayments',
                  'asset_receivable'),
        'liability': ('liability_current', 'liability_non_current', 'liability_payable',
                      'liability_credit_card'),
        'equity': ('equity', 'equity_unaffected'),
        'tax': ('liability_current', 'asset_current'),
    }.get(role)
    if compatible:
        score = score * (1.0 if atype in compatible else 0.55)
    return score, 'name %.0f%% / type %s' % (score * 100, atype or '?')


def best_matches(source, candidates, scorer, limit=3):
    scored = []
    for c in candidates:
        s, why = scorer(source, c)
        if s > 0:
            scored.append((round(s, 4), why, c))
    scored.sort(key=lambda t: -t[0])
    return scored[:limit]


def classify_ledger_by_name(name):
    """Heuristic role guess when the primary group is unavailable."""
    n = (name or '').lower()
    rules = [
        (('cgst', 'sgst', 'igst', 'utgst', 'cess', 'gst', 'tds', 'tcs', 'vat'), 'tax'),
        (('bank', 'hdfc', 'icici', 'sbi', 'axis', 'kotak', 'a/c no'), 'bank'),
        (('cash',), 'cash'),
        (('sales', 'revenue', 'income', 'commission received', 'interest received'), 'income'),
        (('purchase', 'expense', 'rent', 'salary', 'wages', 'freight', 'electricity',
          'telephone', 'printing', 'travel', 'repair', 'discount allowed'), 'expense'),
        (('capital', 'reserve', 'drawings'), 'equity'),
        (('loan', 'payable', 'provision', 'outstanding'), 'liability'),
        (('furniture', 'machinery', 'computer', 'vehicle', 'building', 'deposit', 'advance',
          'prepaid', 'receivable', 'stock'), 'asset'),
    ]
    for keys, role in rules:
        if any(k in n for k in keys):
            return role
    return ''


SPECIAL_TAX_MARKERS = ('rc', 'reverse', 'exp', 'export', 'sez', 'lut', 'imp', 'import',
                       'deemed', 'composition', 'exempt', 'nil')


def special_tax_markers(name):
    """Markers like RC / EXP / SEZ found in a tax or ledger name (word-level)."""
    words = set(re.findall(r'[a-z]+', (name or '').lower()))
    return {m for m in SPECIAL_TAX_MARKERS if m in words}


def tax_rank(tax_name, wanted_name=''):
    """Lower is better: prefer taxes whose special markers equal the Tally ledger's."""
    have, want = special_tax_markers(tax_name), special_tax_markers(wanted_name)
    return len(have ^ want)


def gst_head_and_rate(ledger_name):
    """'Output CGST @9%' -> ('CGST', 9.0).  Returns (head, rate or None)."""
    n = (ledger_name or '').upper()
    head = ''
    for h in ('IGST', 'CGST', 'SGST', 'UTGST', 'CESS'):
        if h in n:
            head = h
            break
    m = re.search(r'(\d+(?:\.\d+)?)\s*%', n)
    return head, (float(m.group(1)) if m else None)
