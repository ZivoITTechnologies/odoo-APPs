# -*- coding: utf-8 -*-
"""Knowledge base that turns cryptic Tally LINEERRORs into fixes.

Each rule may carry an `auto_fix` code the sync engine knows how to perform
(e.g. create the missing ledger, then retry the voucher).
"""
import re

RULES = [
    (re.compile(r"ledger\s+'([^']+)'\s+does not exist", re.I), 'missing_ledger',
     "Tally has no ledger named '{0}'. The connector can create it automatically "
     "(Auto-heal) or you can map it to an existing Tally ledger in Mappings."),
    (re.compile(r"could not find ledger\s+'?([^'!]+)'?", re.I), 'missing_ledger',
     "Tally has no ledger named '{0}'. Create it or fix the mapping."),
    (re.compile(r"stock item\s+'([^']+)'\s+does not exist", re.I), 'missing_item',
     "Stock item '{0}' is missing in Tally. Push the product first (Auto-heal does this)."),
    (re.compile(r"(?:unit|units)\s+'([^']+)'\s+does not exist", re.I), 'missing_unit',
     "Unit of measure '{0}' is not defined in Tally. Auto-heal creates it as a simple unit."),
    (re.compile(r"godown\s+'([^']+)'\s+does not exist", re.I), 'missing_godown',
     "Godown '{0}' is missing in Tally. Auto-heal creates it from the mapped Odoo location."),
    (re.compile(r"cost cent(?:re|er)\s+'([^']+)'\s+does not exist", re.I), 'missing_costcentre',
     "Cost centre '{0}' is missing in Tally. Auto-heal creates it from the mapped analytic account."),
    (re.compile(r"cost category\s+'([^']+)'\s+does not exist", re.I), 'missing_costcategory',
     "Cost category '{0}' is missing in Tally. Auto-heal creates it."),
    (re.compile(r"batch(?:es)?.*(?:required|mandatory|not allowed)|'([^']+)'.*maintain(?:ed)? in batches",
                re.I), 'batch_required',
     "Tally and Odoo disagree on batch tracking for this item. Align 'Maintain in batches' in Tally "
     "with the product's lot tracking in Odoo."),
    (re.compile(r"voucher type\s+'([^']+)'\s+does not exist", re.I), 'missing_vchtype',
     "Voucher type '{0}' does not exist in this Tally company. Fix the voucher type names "
     "on the connection (Configuration tab)."),
    (re.compile(r"(?:ledger|stock item|unit)?\s*'?([^']*)'?\s*already exists", re.I), 'exists',
     "'{0}' already exists in Tally. The connector will switch to Alter and retry."),
    (re.compile(r"voucher totals do not match", re.I), 'unbalanced',
     "Debit and credit totals differ. Usually a rounding line or an unmapped tax ledger "
     "was dropped. Enable 'Round-off ledger' on the connection."),
    (re.compile(r"date.*(?:out of range|outside|before books)", re.I), 'date_range',
     "The voucher date is outside the Tally company's books period. Open the correct "
     "financial year in Tally or change 'Books beginning from'."),
    (re.compile(r"no (?:company|companies) (?:is )?(?:open|loaded)|company.*not (?:open|loaded)",
                re.I), 'no_company',
     "No company is open in TallyPrime. Open '{company}' in Tally and retry."),
    (re.compile(r"duplicate.*(?:voucher number|entry)", re.I), 'duplicate_number',
     "Tally rejected a duplicate voucher number. Set the voucher type numbering to "
     "'Manual' or 'Automatic (Manual Override)' in Tally."),
    (re.compile(r"(?:could not|cannot) set .*gst", re.I), 'gst_config',
     "GST details could not be applied - enable GST in F11 Company Features and set the "
     "ledger/stock item GST rates."),
    (re.compile(r"unknown request|cannot be processed", re.I), 'bad_request',
     "Tally did not understand the request. Check Tally version compatibility (Tally.ERP 9 6.x "
     "or TallyPrime) and the connection's XML schema setting."),
    (re.compile(r"cannot reach tally|connection refused|timed out", re.I), 'offline',
     "Tally is not reachable. Make sure TallyPrime is running with the company open, the "
     "Connectivity port matches, and (agent mode) the ZivoIT Tally Agent is running."),
]


def diagnose(message, company=''):
    """Return (code, explanation, argument) for a Tally error message."""
    for regex, code, text in RULES:
        m = regex.search(message or '')
        if m:
            arg = (m.group(1) or '').strip() if m.groups() else ''
            return code, text.format(arg, company=company or 'your company'), arg
    return 'unknown', '', ''
