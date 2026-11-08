# -*- coding: utf-8 -*-
"""Explainable anomaly detection for vouchers flowing between Odoo and Tally.

Every check returns findings with a severity, a code and a human sentence so
the accountant sees *why* a voucher was held back.  Statistical checks use a
robust median/MAD z-score so a few huge invoices do not mask real outliers.
"""
import statistics

from . import matching

SEV_INFO, SEV_WARN, SEV_BLOCK = 'info', 'warning', 'block'


def _finding(sev, code, msg):
    return {'severity': sev, 'code': code, 'message': msg}


def robust_z(value, history):
    hist = [h for h in history if h]
    if len(hist) < 5:
        return 0.0
    med = statistics.median(hist)
    mad = statistics.median([abs(h - med) for h in hist]) or (med * 0.1) or 1.0
    return 0.6745 * (value - med) / mad


def analyse(v, ctx):
    """v: normalised voucher dict with keys
        date, vch_type, kind ('sale','purchase','receipt','payment','journal','contra',
        'credit_note','debit_note'), party, party_gstin, number, total, taxable,
        tax_lines: [{head, rate, amount}], lines:[{name, hsn}]
    ctx: company_state, company_gstin, partner_history (list of past totals),
        duplicates (list of {number,date,total}), lock_date, weekend_check (bool),
        z_threshold (float), large_amount (float)
    """
    out = []
    total = abs(v.get('total') or 0.0)

    # 1. Balanced?
    if abs(v.get('imbalance') or 0.0) > 0.01:
        out.append(_finding(SEV_BLOCK, 'unbalanced',
                            'Voucher is not balanced (difference %.2f).' % v['imbalance']))

    # 2. Duplicates
    for d in ctx.get('duplicates') or []:
        out.append(_finding(SEV_WARN, 'duplicate',
                            'Possible duplicate of %s dated %s for the same party and amount %.2f.'
                            % (d.get('number') or '?', d.get('date'), d.get('total') or 0.0)))

    # 3. Statistical outlier vs. party history
    z = robust_z(total, ctx.get('partner_history') or [])
    thr = ctx.get('z_threshold') or 4.0
    if z > thr:
        out.append(_finding(SEV_WARN, 'outlier',
                            'Amount %.2f is unusually high for %s (robust z-score %.1f vs '
                            'threshold %.1f).' % (total, v.get('party') or 'this party', z, thr)))

    # 4. Large round amount (common in manual/fraudulent entries)
    large = ctx.get('large_amount') or 100000.0
    if total >= large and total % 1000 == 0 and v.get('kind') in ('journal', 'payment', 'contra'):
        out.append(_finding(SEV_INFO, 'round_amount',
                            'Large round-figure %s of %.0f - please confirm supporting documents.'
                            % (v.get('kind'), total)))

    # 5. GST place-of-supply sanity (intra-state => CGST+SGST, inter-state => IGST)
    heads = {t.get('head') for t in v.get('tax_lines') or [] if t.get('head')}
    comp_state = (ctx.get('company_state') or '').strip().lower()
    party_state = (matching.state_from_gstin(v.get('party_gstin')) or v.get('party_state') or '')
    party_state = party_state.strip().lower()
    if heads and comp_state and party_state and v.get('kind') in (
            'sale', 'purchase', 'credit_note', 'debit_note'):
        intra = comp_state == party_state
        if intra and 'IGST' in heads:
            out.append(_finding(SEV_BLOCK, 'gst_pos',
                                'IGST charged on an intra-state supply (%s to %s). CGST+SGST expected.'
                                % (ctx.get('company_state'), party_state.title())))
        if not intra and heads & {'CGST', 'SGST', 'UTGST'}:
            out.append(_finding(SEV_BLOCK, 'gst_pos',
                                'CGST/SGST charged on an inter-state supply (%s to %s). IGST expected.'
                                % (ctx.get('company_state'), party_state.title())))
    if 'CGST' in heads and not heads & {'SGST', 'UTGST'}:
        out.append(_finding(SEV_WARN, 'gst_pair', 'CGST present without matching SGST/UTGST.'))

    # 6. GST arithmetic: tax approx. taxable x rate
    taxable = abs(v.get('taxable') or 0.0)
    for t in v.get('tax_lines') or []:
        if t.get('rate') and taxable:
            expected = round(taxable * t['rate'] / 100.0, 2)
            if abs(abs(t.get('amount') or 0) - expected) > max(1.0, expected * 0.01):
                out.append(_finding(SEV_WARN, 'gst_math',
                                    '%s @%s%% is %.2f but %.2f expected on taxable value %.2f.'
                                    % (t.get('head') or 'Tax', t['rate'], abs(t.get('amount') or 0),
                                       expected, taxable)))

    # 7. GSTIN validity
    if v.get('party_gstin') and not matching.valid_gstin(v['party_gstin']):
        out.append(_finding(SEV_WARN, 'gstin', 'Party GSTIN %s fails checksum validation.'
                            % v['party_gstin']))

    # 8. Missing HSN on goods lines of a GST invoice
    if heads and v.get('kind') in ('sale', 'credit_note'):
        missing = [l['name'] for l in v.get('lines') or [] if l.get('is_product') and not l.get('hsn')]
        if missing:
            out.append(_finding(SEV_INFO, 'hsn', 'HSN/SAC missing on: %s.' % ', '.join(missing[:5])))

    # 9. Lock date / weekend
    lock = ctx.get('lock_date')
    if lock and v.get('date') and v['date'] <= lock:
        out.append(_finding(SEV_BLOCK, 'locked',
                            'Date %s falls in a locked period (lock date %s).' % (v['date'], lock)))
    if ctx.get('weekend_check') and v.get('date') and v['date'].weekday() == 6:
        out.append(_finding(SEV_INFO, 'sunday', 'Voucher dated on a Sunday.'))
    return out


def risk_score(findings):
    weights = {SEV_INFO: 10, SEV_WARN: 30, SEV_BLOCK: 60}
    return min(100, sum(weights.get(f['severity'], 0) for f in findings))
