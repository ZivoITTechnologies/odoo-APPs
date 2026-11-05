# -*- coding: utf-8 -*-
"""Pure-Python TallyPrime XML protocol layer.

No Odoo imports: this module is shared by the Odoo addon, the on-premise
bridge agent and the test simulator.  It builds request envelopes
(collection exports with inline TDL, master/voucher imports, cancellations)
and parses every response shape TallyPrime / Tally.ERP 9 is known to emit.
"""
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from xml.sax.saxutils import escape

# Characters illegal in XML 1.0 (Tally happily emits &#4; and friends).
_ILLEGAL_CHARREF = re.compile(r'&#(x?[0-9A-Fa-f]+);')
_ILLEGAL_RAW = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f]')

MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

# Tally primary groups -> functional role used by the mapping engine.
PRIMARY_GROUP_ROLE = {
    'sundry debtors': 'customer',
    'sundry creditors': 'vendor',
    'duties & taxes': 'tax',
    'bank accounts': 'bank',
    'bank od a/c': 'bank',
    'bank occ a/c': 'bank',
    'cash-in-hand': 'cash',
    'sales accounts': 'income',
    'direct incomes': 'income',
    'indirect incomes': 'income',
    'purchase accounts': 'expense',
    'direct expenses': 'expense',
    'indirect expenses': 'expense',
    'fixed assets': 'asset',
    'current assets': 'asset',
    'loans & advances (asset)': 'asset',
    'deposits (asset)': 'asset',
    'investments': 'asset',
    'stock-in-hand': 'asset',
    'misc. expenses (asset)': 'asset',
    'capital account': 'equity',
    'reserves & surplus': 'equity',
    'current liabilities': 'liability',
    'provisions': 'liability',
    'loans (liability)': 'liability',
    'secured loans': 'liability',
    'unsecured loans': 'liability',
    'suspense a/c': 'liability',
    'branch / divisions': 'liability',
}


class TallyProtocolError(Exception):
    """Raised when Tally answers with something we cannot understand."""


# --------------------------------------------------------------------------
# Value helpers
# --------------------------------------------------------------------------
def sanitize_response(raw):
    """Turn raw Tally bytes/str into XML ElementTree can parse."""
    if isinstance(raw, bytes):
        if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
            raw = raw.decode('utf-16')
        else:
            try:
                raw = raw.decode('utf-8')
            except UnicodeDecodeError:
                raw = raw.decode('latin-1')
    raw = raw.lstrip('﻿')

    def _charref(m):
        txt = m.group(1)
        try:
            code = int(txt[1:], 16) if txt.lower().startswith('x') else int(txt)
        except ValueError:
            return ''
        if code in (9, 10, 13) or code >= 32:
            return m.group(0)
        return ''
    raw = _ILLEGAL_CHARREF.sub(_charref, raw)
    raw = _ILLEGAL_RAW.sub('', raw)
    # Tally sometimes prepends an XML declaration claiming UTF-16 on UTF-8 data
    raw = re.sub(r'^<\?xml[^>]*\?>', '', raw.strip())
    return raw


def clean_text(value):
    if value is None:
        return ''
    return _ILLEGAL_RAW.sub('', str(value)).strip()


def x(value):
    """Escape a value for inclusion in Tally XML."""
    return escape(clean_text(value), {'"': '&quot;'})


def fmt_date(d):
    if isinstance(d, str):
        d = datetime.strptime(d[:10], '%Y-%m-%d').date()
    if isinstance(d, datetime):
        d = d.date()
    return d.strftime('%Y%m%d')


def fmt_date_human(d):
    """d-Mon-YYYY form, used by Delete/Cancel actions."""
    if isinstance(d, str):
        d = datetime.strptime(d[:10], '%Y-%m-%d').date()
    return '%d-%s-%d' % (d.day, MONTHS[d.month - 1], d.year)


def parse_date(value):
    value = clean_text(value)
    if not value:
        return None
    for fmt in ('%Y%m%d', '%d-%b-%Y', '%d-%b-%y', '%Y-%m-%d', '%d/%m/%Y'):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def fmt_amount(value, digits=2):
    q = Decimal(1).scaleb(-digits)
    return str(Decimal(str(value or 0)).quantize(q, rounding=ROUND_HALF_UP))


_NUM = re.compile(r'-?[\d,]*\.?\d+')


def parse_amount(value):
    """Parse '1,180.00', '-1180', '1180.00 Dr', '₹ 1,180.00 Cr', '$ 10 = ₹ 830'."""
    value = clean_text(value)
    if not value:
        return 0.0
    if '=' in value:  # forex: "$ 10 @ ₹ 83/$ = ₹ 830" -> base currency part
        value = value.split('=')[-1]
    m = _NUM.search(value.replace(' ', ''))
    if not m:
        return 0.0
    amount = float(m.group(0).replace(',', ''))
    low = value.lower()
    if low.endswith('dr') and amount > 0:
        amount = -amount
    return amount


def parse_qty(value):
    """' 5 Nos' -> (5.0, 'Nos');  '2.5 kg = 2500 g' -> (2.5, 'kg')."""
    value = clean_text(value)
    if not value:
        return 0.0, ''
    value = value.split('=')[0].strip()
    m = re.match(r'(-?[\d,]*\.?\d+)\s*(.*)', value)
    if not m:
        return 0.0, ''
    return float(m.group(1).replace(',', '')), m.group(2).strip()


def parse_rate(value):
    """'100.00/Nos' -> (100.0, 'Nos')."""
    value = clean_text(value)
    if not value:
        return 0.0, ''
    parts = value.split('/')
    return abs(parse_amount(parts[0])), (parts[1].strip() if len(parts) > 1 else '')


def yes(value):
    return clean_text(value).lower() in ('yes', 'true', '1')


def yn(flag):
    return 'Yes' if flag else 'No'


# --------------------------------------------------------------------------
# Request builders
# --------------------------------------------------------------------------
def _static_vars(company, extra=None):
    parts = ['<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>']
    if company:
        parts.append('<SVCURRENTCOMPANY>%s</SVCURRENTCOMPANY>' % x(company))
    for k, v in (extra or {}).items():
        parts.append('<%s>%s</%s>' % (k, x(v), k))
    return '<STATICVARIABLES>%s</STATICVARIABLES>' % ''.join(parts)


COLLECTION_FETCH = {
    'Group': ['GUID', 'MasterID', 'AlterID', 'Name', 'Parent'],
    'Ledger': ['GUID', 'MasterID', 'AlterID', 'Name', 'Parent', 'PartyGSTIN', 'LedStateName',
               'Address', 'Email', 'LedgerPhone', 'LedgerMobile', 'PinCode', 'CountryName',
               'GSTRegistrationType', 'LedMailingDetails', 'LedGSTRegDetails', 'IncomeTaxNumber',
               'OpeningBalance', 'ClosingBalance', 'IsBillWiseOn', 'TaxType', 'GSTDutyHead',
               'RateOfTaxCalculation', 'BankDetails', 'IFSCode', 'BankAccHolderName'],
    'StockItem': ['GUID', 'MasterID', 'AlterID', 'Name', 'Parent', 'BaseUnits', 'GSTDetails',
                  'HSNDetails', 'Description', 'PartNo', 'OpeningBalance', 'ClosingBalance',
                  'ClosingRate', 'StandardCostList', 'StandardPriceList', 'GSTTypeOfSupply'],
    'Unit': ['GUID', 'MasterID', 'AlterID', 'Name', 'IsSimpleUnit', 'DecimalPlaces'],
    'Godown': ['GUID', 'MasterID', 'AlterID', 'Name', 'Parent'],
    'CostCentre': ['GUID', 'MasterID', 'AlterID', 'Name', 'Parent', 'Category'],
    'CostCategory': ['GUID', 'MasterID', 'AlterID', 'Name'],
    'VoucherType': ['GUID', 'MasterID', 'AlterID', 'Name', 'Parent'],
    'Voucher': ['GUID', 'MasterID', 'AlterID', 'Date', 'EffectiveDate', 'VoucherTypeName',
                'VoucherNumber', 'Reference', 'ReferenceDate', 'PartyLedgerName', 'PartyGSTIN',
                'PlaceOfSupply', 'Narration', 'IsCancelled', 'IsOptional', 'IsInvoice',
                'PersistedView', 'IRN', 'AllLedgerEntries', 'LedgerEntries',
                'AllInventoryEntries', 'InventoryEntries', 'BasicBuyerName',
                'AllLedgerEntries.BillAllocations', 'AllLedgerEntries.CategoryAllocations',
                'LedgerEntries.CategoryAllocations', 'AllInventoryEntries.BatchAllocations',
                'AllInventoryEntries.AccountingAllocations'],
}
COLLECTION_COMPUTE = {
    'Ledger': ['ZPrimaryGroup:$_PrimaryGroup'],
    'Group': ['ZPrimaryGroup:$_PrimaryGroup'],
}


def build_collection_request(object_type, company=None, alter_id_gt=None, from_date=None,
                             to_date=None, name=None, extra_filters=None, fetch=None):
    """Export request for a TDL collection, with incremental AlterID filtering.

    Works on Tally.ERP 9 (6.x) and every TallyPrime release including 7.x.
    """
    coll = name or ('Zivo%sColl' % object_type)
    filters, formulae = [], []
    if alter_id_gt:
        filters.append('ZivoAlterFlt')
        formulae.append('<SYSTEM TYPE="Formulae" NAME="ZivoAlterFlt">$AlterID &gt; %d</SYSTEM>'
                        % int(alter_id_gt))
    for i, expr in enumerate(extra_filters or []):
        fname = 'ZivoXFlt%d' % i
        filters.append(fname)
        formulae.append('<SYSTEM TYPE="Formulae" NAME="%s">%s</SYSTEM>' % (fname, x(expr)))
    extra = {}
    if from_date:
        extra['SVFROMDATE'] = fmt_date(from_date)
    if to_date:
        extra['SVTODATE'] = fmt_date(to_date)
    fetch = ', '.join(fetch or COLLECTION_FETCH.get(object_type,
                                                    ['GUID', 'MasterID', 'AlterID', 'Name']))
    compute = '' if name and name.startswith('ZivoCensus') else ''.join(
        '<COMPUTE>%s</COMPUTE>' % c for c in COLLECTION_COMPUTE.get(object_type, []))
    filt = ('<FILTER>%s</FILTER>' % ','.join(filters)) if filters else ''
    return (
        '<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>'
        '<TYPE>Collection</TYPE><ID>%(coll)s</ID></HEADER><BODY><DESC>%(sv)s'
        '<TDL><TDLMESSAGE><COLLECTION NAME="%(coll)s" ISMODIFY="No" ISFIXED="No" '
        'ISINITIALIZE="No" ISOPTION="No" ISINTERNAL="No"><TYPE>%(type)s</TYPE>'
        '<FETCH>%(fetch)s</FETCH>%(compute)s%(filt)s</COLLECTION>%(formulae)s'
        '</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>'
    ) % {'coll': coll, 'sv': _static_vars(company, extra), 'type': object_type,
         'fetch': fetch, 'compute': compute, 'filt': filt, 'formulae': ''.join(formulae)}


def build_company_list_request():
    return ('<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>'
            '<TYPE>Collection</TYPE><ID>ZivoCompanyColl</ID></HEADER><BODY><DESC>'
            '<STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>'
            '<TDL><TDLMESSAGE><COLLECTION NAME="ZivoCompanyColl" ISMODIFY="No"><TYPE>Company</TYPE>'
            '<FETCH>GUID, Name, StartingFrom, BooksFrom, StateName, GSTIN, BaseCurrencySymbol</FETCH>'
            '</COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>')


def build_import_envelope(company, report, messages_xml):
    """Wrap one or more <TALLYMESSAGE> payloads into an Import Data envelope."""
    return (
        '<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY>'
        '<IMPORTDATA><REQUESTDESC><REPORTNAME>%s</REPORTNAME>%s</REQUESTDESC>'
        '<REQUESTDATA>%s</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>'
    ) % (x(report), ('<STATICVARIABLES><SVCURRENTCOMPANY>%s</SVCURRENTCOMPANY></STATICVARIABLES>'
                     % x(company)) if company else '', ''.join(messages_xml))


def tally_message(inner):
    return '<TALLYMESSAGE xmlns:UDF="TallyUDF">%s</TALLYMESSAGE>' % inner


def _tag(name, value):
    if value in (None, False, ''):
        return ''
    return '<%s>%s</%s>' % (name, x(value), name)


def build_ledger_xml(vals, schema='prime3', action='Create'):
    """vals: name, parent, gstin, state, country, pincode, address(list), email, phone,
    mobile, pan, registration_type, billwise, opening_balance, old_name,
    tax_type, duty_head, rate."""
    name = vals['name']
    attr_name = vals.get('old_name') or name
    out = ['<LEDGER NAME="%s" ACTION="%s">' % (x(attr_name), action)]
    out.append('<NAME.LIST TYPE="String"><NAME>%s</NAME></NAME.LIST>' % x(name))
    out.append(_tag('PARENT', vals.get('parent') or 'Sundry Debtors'))
    out.append(_tag('ISBILLWISEON', yn(vals.get('billwise', True))))
    out.append(_tag('AFFECTSSTOCK', 'No'))
    if vals.get('cost_centres_on'):
        out.append(_tag('ISCOSTCENTRESON', 'Yes'))
    out.append(_tag('EMAIL', vals.get('email')))
    out.append(_tag('LEDGERPHONE', vals.get('phone')))
    out.append(_tag('LEDGERMOBILE', vals.get('mobile')))
    out.append(_tag('INCOMETAXNUMBER', vals.get('pan')))
    if vals.get('tax_type'):
        out.append(_tag('TAXTYPE', vals['tax_type']))
        out.append(_tag('GSTDUTYHEAD', vals.get('duty_head')))
        if vals.get('rate'):
            out.append(_tag('RATEOFTAXCALCULATION', fmt_amount(vals['rate'])))
    if vals.get('opening_balance'):
        out.append(_tag('OPENINGBALANCE', fmt_amount(vals['opening_balance'])))
    addr = [a for a in (vals.get('address') or []) if a]
    reg_type = vals.get('registration_type') or ('Regular' if vals.get('gstin') else 'Unregistered/Consumer')
    if schema == 'prime3':
        applicable = fmt_date(vals.get('applicable_from') or date(2017, 7, 1))
        out.append('<LEDMAILINGDETAILS.LIST>')
        if addr:
            out.append('<ADDRESS.LIST TYPE="String">%s</ADDRESS.LIST>'
                       % ''.join('<ADDRESS>%s</ADDRESS>' % x(a) for a in addr))
        out.append(_tag('APPLICABLEFROM', applicable))
        out.append(_tag('MAILINGNAME', name))
        out.append(_tag('STATE', vals.get('state')))
        out.append(_tag('COUNTRY', vals.get('country') or 'India'))
        out.append(_tag('PINCODE', vals.get('pincode')))
        out.append('</LEDMAILINGDETAILS.LIST>')
        if vals.get('gstin') or vals.get('state'):
            out.append('<LEDGSTREGDETAILS.LIST>')
            out.append(_tag('APPLICABLEFROM', applicable))
            out.append(_tag('GSTREGISTRATIONTYPE', reg_type))
            out.append(_tag('PLACEOFSUPPLY', vals.get('state')))
            out.append(_tag('GSTIN', vals.get('gstin')))
            out.append('</LEDGSTREGDETAILS.LIST>')
    else:
        if addr:
            out.append('<ADDRESS.LIST TYPE="String">%s</ADDRESS.LIST>'
                       % ''.join('<ADDRESS>%s</ADDRESS>' % x(a) for a in addr))
        out.append(_tag('MAILINGNAME', name))
        out.append(_tag('LEDSTATENAME', vals.get('state')))
        out.append(_tag('COUNTRYNAME', vals.get('country') or 'India'))
        out.append(_tag('PINCODE', vals.get('pincode')))
        out.append(_tag('GSTREGISTRATIONTYPE', reg_type))
        out.append(_tag('PARTYGSTIN', vals.get('gstin')))
    out.append('</LEDGER>')
    return ''.join(out)


def build_unit_xml(name):
    return ('<UNIT NAME="%s" ACTION="Create"><NAME>%s</NAME><ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>'
            '<DECIMALPLACES>2</DECIMALPLACES></UNIT>') % (x(name), x(name))


def build_stock_item_xml(vals, schema='prime3', action='Create'):
    """vals: name, old_name, parent, unit, hsn, gst_rate, description, part_no, rate."""
    name = vals['name']
    out = ['<STOCKITEM NAME="%s" ACTION="%s">' % (x(vals.get('old_name') or name), action)]
    out.append('<NAME.LIST TYPE="String"><NAME>%s</NAME></NAME.LIST>' % x(name))
    out.append(_tag('PARENT', vals.get('parent')))
    out.append(_tag('BASEUNITS', vals.get('unit') or 'Nos'))
    out.append(_tag('DESCRIPTION', vals.get('description')))
    out.append(_tag('MAILINGNAME', vals.get('part_no')))
    out.append(_tag('GSTAPPLICABLE', 'Applicable'))
    out.append(_tag('GSTTYPEOFSUPPLY', vals.get('supply_type') or 'Goods'))
    if vals.get('batchwise'):
        out.append(_tag('ISBATCHWISEON', 'Yes'))
    if vals.get('expiry'):
        out.append(_tag('ISPERISHABLEON', 'Yes'))
        out.append(_tag('HASMFGDATE', 'Yes'))
    applicable = fmt_date(vals.get('applicable_from') or date(2017, 7, 1))
    if schema == 'prime3':
        if vals.get('hsn'):
            out.append('<HSNDETAILS.LIST>%s%s%s</HSNDETAILS.LIST>' % (
                _tag('APPLICABLEFROM', applicable), _tag('HSNCODE', vals['hsn']),
                _tag('SRCOFHSNDETAILS', 'Specify Details Here')))
        if vals.get('gst_rate') is not None:
            out.append('<GSTDETAILS.LIST>%s%s%s</GSTDETAILS.LIST>' % (
                _tag('APPLICABLEFROM', applicable), _tag('TAXABILITY', 'Taxable'),
                _gst_rate_details(vals['gst_rate'])))
    else:
        out.append('<GSTDETAILS.LIST>%s%s%s%s</GSTDETAILS.LIST>' % (
            _tag('APPLICABLEFROM', applicable), _tag('HSNCODE', vals.get('hsn')),
            _tag('TAXABILITY', 'Taxable'),
            _gst_rate_details(vals.get('gst_rate')) if vals.get('gst_rate') is not None else ''))
    if vals.get('rate'):
        out.append('<STANDARDPRICELIST.LIST>%s%s</STANDARDPRICELIST.LIST>' % (
            _tag('DATE', applicable), _tag('RATE', '%s/%s' % (fmt_amount(vals['rate']),
                                                              vals.get('unit') or 'Nos'))))
    out.append('</STOCKITEM>')
    return ''.join(out)


def _gst_rate_details(rate):
    rate = float(rate or 0)
    half = rate / 2.0
    heads = (('IGST', rate), ('CGST', half), ('SGST/UTGST', half))
    rates = ''.join(
        '<RATEDETAILS.LIST><GSTRATEDUTYHEAD>%s</GSTRATEDUTYHEAD><GSTRATEVALUATIONTYPE>'
        'Based on Value</GSTRATEVALUATIONTYPE><GSTRATE>%s</GSTRATE></RATEDETAILS.LIST>'
        % (head, fmt_amount(r)) for head, r in heads)
    return ('<STATEWISEDETAILS.LIST><STATENAME>&#4; Any</STATENAME>%s</STATEWISEDETAILS.LIST>'
            % rates)


def build_voucher_xml(v, action='Create'):
    """Build a <VOUCHER>.

    v keys: remote_id, vch_type, date, number, reference, reference_date, party, narration,
    place_of_supply, party_gstin, invoice_mode (bool),
    ledger_entries: [{ledger, amount (+credit / -debit), is_party, bills:[{name,type,amount}],
                      cost_centre}]
    inventory_entries: [{item, qty, unit, rate, amount, godown,
                         ledger (accounting allocation), discount}]
    Amount sign convention follows Tally: debit = negative & ISDEEMEDPOSITIVE=Yes.
    """
    invoice_mode = bool(v.get('invoice_mode') and v.get('inventory_entries'))
    view = 'Invoice Voucher View' if invoice_mode else 'Accounting Voucher View'
    out = ['<VOUCHER REMOTEID="%s" VCHTYPE="%s" ACTION="%s" OBJVIEW="%s">' % (
        x(v['remote_id']), x(v['vch_type']), action, view)]
    out.append(_tag('DATE', fmt_date(v['date'])))
    out.append(_tag('EFFECTIVEDATE', fmt_date(v['date'])))
    out.append(_tag('GUID', v['remote_id']))
    out.append(_tag('VOUCHERTYPENAME', v['vch_type']))
    out.append(_tag('VOUCHERNUMBER', v.get('number')))
    out.append(_tag('REFERENCE', v.get('reference')))
    if v.get('reference_date'):
        out.append(_tag('REFERENCEDATE', fmt_date(v['reference_date'])))
    out.append(_tag('PARTYLEDGERNAME', v.get('party')))
    out.append(_tag('PARTYNAME', v.get('party')))
    out.append(_tag('PARTYGSTIN', v.get('party_gstin')))
    out.append(_tag('PLACEOFSUPPLY', v.get('place_of_supply')))
    out.append(_tag('NARRATION', v.get('narration')))
    out.append(_tag('PERSISTEDVIEW', view))
    out.append(_tag('ISINVOICE', yn(invoice_mode)))
    out.append(_tag('VCHENTRYMODE', 'Item Invoice' if invoice_mode else 'Accounting Invoice'
                    if v.get('is_invoice_doc') else None))
    ledger_tag = 'LEDGERENTRIES.LIST' if invoice_mode else 'ALLLEDGERENTRIES.LIST'
    for le in v.get('ledger_entries', []):
        out.append(_ledger_entry_xml(ledger_tag, le))
    if invoice_mode:
        for ie in v['inventory_entries']:
            out.append(_inventory_entry_xml(ie))
    out.append('</VOUCHER>')
    return ''.join(out)


def _ledger_entry_xml(tag, le):
    amount = float(le['amount'])
    debit = amount < 0
    parts = ['<%s>' % tag, _tag('LEDGERNAME', le['ledger']),
             _tag('ISDEEMEDPOSITIVE', yn(debit)),
             _tag('ISPARTYLEDGER', yn(le.get('is_party'))),
             _tag('AMOUNT', fmt_amount(amount))]
    for b in le.get('bills') or []:
        parts.append('<BILLALLOCATIONS.LIST>%s%s%s</BILLALLOCATIONS.LIST>' % (
            _tag('NAME', b['name']), _tag('BILLTYPE', b.get('type') or 'New Ref'),
            _tag('AMOUNT', fmt_amount(b.get('amount', amount)))))
    parts.append(_cost_centres_xml(le.get('cost_centres')))
    parts.append('</%s>' % tag)
    return ''.join(parts)


def _cost_centres_xml(allocs):
    """allocs: [{'category', 'name', 'amount'}] -> CATEGORYALLOCATIONS.LIST blocks."""
    if not allocs:
        return ''
    by_cat = {}
    for a in allocs:
        by_cat.setdefault(a.get('category') or 'Primary Cost Category', []).append(a)
    out = []
    for cat, items in by_cat.items():
        out.append('<CATEGORYALLOCATIONS.LIST>%s%s%s</CATEGORYALLOCATIONS.LIST>' % (
            _tag('CATEGORY', cat), _tag('ISDEEMEDPOSITIVE', yn(float(items[0]['amount']) < 0)),
            ''.join('<COSTCENTREALLOCATIONS.LIST>%s%s</COSTCENTREALLOCATIONS.LIST>' % (
                _tag('NAME', a['name']), _tag('AMOUNT', fmt_amount(a['amount']))) for a in items)))
    return ''.join(out)


def _inventory_entry_xml(ie):
    amount = float(ie['amount'])
    debit = amount < 0  # purchase side
    unit = ie.get('unit') or 'Nos'
    qty = '%s %s' % (fmt_amount(abs(ie['qty'])), unit)
    parts = ['<ALLINVENTORYENTRIES.LIST>', _tag('STOCKITEMNAME', ie['item']),
             _tag('ISDEEMEDPOSITIVE', yn(debit)),
             _tag('RATE', '%s/%s' % (fmt_amount(ie['rate']), unit)),
             _tag('DISCOUNT', fmt_amount(ie['discount']) if ie.get('discount') else None),
             _tag('AMOUNT', fmt_amount(amount)),
             _tag('ACTUALQTY', qty), _tag('BILLEDQTY', qty)]
    batches = ie.get('batches') or ([{'godown': ie['godown'], 'qty': ie['qty'],
                                       'amount': amount}] if ie.get('godown') else [])
    for b in batches:
        bq = '%s %s' % (fmt_amount(abs(b['qty'])), unit)
        parts.append('<BATCHALLOCATIONS.LIST>%s%s%s%s%s%s</BATCHALLOCATIONS.LIST>' % (
            _tag('GODOWNNAME', b.get('godown') or 'Main Location'),
            _tag('BATCHNAME', b.get('batch') or 'Primary Batch'),
            _tag('EXPIRYPERIOD', fmt_date(b['expiry']) if b.get('expiry') else None),
            _tag('AMOUNT', fmt_amount(b.get('amount', amount))),
            _tag('ACTUALQTY', bq), _tag('BILLEDQTY', bq)))
    parts.append('<ACCOUNTINGALLOCATIONS.LIST>%s%s%s%s</ACCOUNTINGALLOCATIONS.LIST>' % (
        _tag('LEDGERNAME', ie['ledger']), _tag('ISDEEMEDPOSITIVE', yn(debit)),
        _tag('AMOUNT', fmt_amount(amount)), _cost_centres_xml(ie.get('cost_centres'))))
    parts.append('</ALLINVENTORYENTRIES.LIST>')
    return ''.join(parts)


def build_godown_xml(name, parent=None, action='Create', old_name=None):
    return ('<GODOWN NAME="%s" ACTION="%s"><NAME.LIST TYPE="String"><NAME>%s</NAME></NAME.LIST>'
            '%s<HASNOSPACE>No</HASNOSPACE></GODOWN>') % (
        x(old_name or name), action, x(name), _tag('PARENT', parent))


def build_cost_category_xml(name):
    return ('<COSTCATEGORY NAME="%s" ACTION="Create"><NAME.LIST TYPE="String"><NAME>%s</NAME>'
            '</NAME.LIST><ALLOCATEREVENUE>Yes</ALLOCATEREVENUE><ALLOCATENONREVENUE>Yes'
            '</ALLOCATENONREVENUE></COSTCATEGORY>') % (x(name), x(name))


def build_cost_centre_xml(name, category=None, parent=None, action='Create', old_name=None):
    return ('<COSTCENTRE NAME="%s" ACTION="%s"><NAME.LIST TYPE="String"><NAME>%s</NAME>'
            '</NAME.LIST>%s%s</COSTCENTRE>') % (
        x(old_name or name), action, x(name),
        _tag('CATEGORY', category or 'Primary Cost Category'), _tag('PARENT', parent))


def build_voucher_cancel_xml(vch_type, vdate, number, action='Cancel'):
    return ('<VOUCHER DATE="%s" TAGNAME="Voucher Number" TAGVALUE="%s" VCHTYPE="%s" ACTION="%s">'
            '<NARRATION>Cancelled from Odoo</NARRATION></VOUCHER>') % (
        fmt_date_human(vdate), x(number), x(vch_type), action)


# --------------------------------------------------------------------------
# Response parsers
# --------------------------------------------------------------------------
def parse_xml(raw):
    text = sanitize_response(raw)
    if not text:
        raise TallyProtocolError('Empty response from Tally')
    if not text.startswith('<'):
        raise TallyProtocolError(text[:300])
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        try:  # some builds return several root elements
            return ET.fromstring('<ROOT>%s</ROOT>' % text)
        except ET.ParseError:
            raise TallyProtocolError('Malformed XML from Tally: %s' % e)


def parse_import_response(raw):
    """Return dict(created, altered, deleted, cancelled, ignored, errors, combined,
    last_vch_id, last_mid, line_errors=[...], ok=bool)."""
    root = parse_xml(raw)
    if root.tag == 'RESPONSE' and not len(root):
        msg = clean_text(root.text)
        return {'created': 0, 'altered': 0, 'deleted': 0, 'cancelled': 0, 'ignored': 0,
                'errors': 1, 'combined': 0, 'last_vch_id': 0, 'last_mid': 0,
                'line_errors': [msg or 'Unknown response'], 'ok': False}
    res = {}
    for key, tag in (('created', 'CREATED'), ('altered', 'ALTERED'), ('deleted', 'DELETED'),
                     ('cancelled', 'CANCELLED'), ('ignored', 'IGNORED'), ('errors', 'ERRORS'),
                     ('combined', 'COMBINED'), ('last_vch_id', 'LASTVCHID'),
                     ('last_mid', 'LASTMID')):
        el = root.find('.//' + tag)
        try:
            res[key] = int(float(clean_text(el.text))) if el is not None and el.text else 0
        except ValueError:
            res[key] = 0
    res['line_errors'] = [clean_text(e.text) for e in root.iter('LINEERROR') if clean_text(e.text)]
    status = root.find('.//HEADER/STATUS')
    if status is not None and clean_text(status.text) == '0' and not res['line_errors']:
        res['line_errors'] = [clean_text(''.join(root.itertext()))[:500] or 'Tally returned status 0']
    if res['line_errors'] and not res['errors']:
        res['errors'] = len(res['line_errors'])
    res['ok'] = res['errors'] == 0 and not res['line_errors']
    return res


def _el_to_dict(el):
    """Recursively convert a Tally object element to python structures.

    Repeated '.LIST' children become lists; leaf values are cleaned strings.
    """
    data = dict(('@' + k.lower(), clean_text(v)) for k, v in el.attrib.items() if k not in ('TYPE',))
    for child in el:
        tag = child.tag
        key = tag.lower()
        if tag.endswith('.LIST'):
            key = key[:-5]
            if len(child) and all(len(g) == 0 and g.tag + '.LIST' == tag for g in child):
                # simple string list e.g. <ADDRESS.LIST><ADDRESS>l1</ADDRESS><ADDRESS>l2</ADDRESS>
                lst = data.setdefault(key, [])
                lst.extend({'value': clean_text(g.text)} for g in child if clean_text(g.text))
            elif len(child):
                data.setdefault(key, []).append(_el_to_dict(child))
            else:
                txt = clean_text(child.text)
                if txt:
                    data.setdefault(key, []).append({'value': txt})
        elif len(child):
            data[key] = _el_to_dict(child)
        else:
            data[key] = clean_text(child.text)
    return data


def parse_collection(raw, object_tag):
    """Return list of dicts for every <OBJECT_TAG> in a collection export."""
    root = parse_xml(raw)
    errors = [clean_text(e.text) for e in root.iter('LINEERROR') if clean_text(e.text)]
    if errors:
        raise TallyProtocolError('; '.join(errors))
    if root.tag == 'RESPONSE' and not len(root):
        raise TallyProtocolError(clean_text(root.text) or 'Unknown response')
    object_tag = object_tag.upper()
    return [_el_to_dict(el) for el in root.iter(object_tag)]


def _first(d, *keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, list):
            if v and isinstance(v[0], dict) and 'value' in v[0]:
                return v[0]['value']
            continue
        if v:
            return v
    return ''


def _list_name(d):
    for n in d.get('name', []) if isinstance(d.get('name'), list) else []:
        if isinstance(n, dict) and (n.get('value') or n.get('name')):
            return n.get('value') or n.get('name')
    return d.get('name', '') if isinstance(d.get('name'), str) else ''


def _addr_lines(d):
    lines = []
    for a in d.get('address', []) or []:
        if isinstance(a, dict):
            if a.get('value'):
                lines.append(a['value'])
            elif a.get('address'):
                lines.append(a['address'])
    return [l for l in lines if l]


def normalize_ledger(d):
    """Flatten a parsed ledger into a schema-independent dict (legacy + Prime 3+)."""
    mailing = (d.get('ledmailingdetails') or [{}])[-1]
    gstreg = (d.get('ledgstregdetails') or [{}])[-1]
    name = d.get('@name') or _list_name(d)
    address = _addr_lines(mailing) or _addr_lines(d)
    banks = d.get('bankdetails') or []
    return {
        'guid': d.get('guid', ''),
        'master_id': int(float(d.get('masterid') or 0)),
        'alter_id': int(float(d.get('alterid') or 0)),
        'name': name,
        'parent': d.get('parent', ''),
        'primary_group': d.get('zprimarygroup') or '',
        'gstin': (gstreg.get('gstin') or d.get('partygstin') or '').upper(),
        'registration_type': gstreg.get('gstregistrationtype') or d.get('gstregistrationtype') or '',
        'state': mailing.get('state') or d.get('ledstatename') or gstreg.get('placeofsupply') or '',
        'country': mailing.get('country') or d.get('countryname') or '',
        'pincode': mailing.get('pincode') or d.get('pincode') or '',
        'address': address,
        'email': d.get('email', ''),
        'phone': d.get('ledgerphone', ''),
        'mobile': d.get('ledgermobile', ''),
        'pan': d.get('incometaxnumber', ''),
        'opening_balance': parse_amount(d.get('openingbalance')),
        'closing_balance': parse_amount(d.get('closingbalance')),
        'billwise': yes(d.get('isbillwiseon')),
        'tax_type': d.get('taxtype', ''),
        'duty_head': d.get('gstdutyhead', ''),
        'rate': parse_amount(d.get('rateoftaxcalculation')),
        'ifsc': d.get('ifscode') or (banks[0].get('ifscode') if banks else ''),
    }


def normalize_stock_item(d):
    hsn = ''
    rate = None
    for h in d.get('hsndetails', []) or []:
        hsn = h.get('hsncode') or hsn
    for g in d.get('gstdetails', []) or []:
        hsn = hsn or g.get('hsncode', '')
        for sw in g.get('statewisedetails', []) or []:
            for rd in sw.get('ratedetails', []) or []:
                if rd.get('gstratedutyhead', '').upper() == 'IGST':
                    rate = parse_amount(rd.get('gstrate'))
    std_price = 0.0
    for sp in d.get('standardpricelist', []) or []:
        std_price = parse_rate(sp.get('rate'))[0] or std_price
    std_cost = 0.0
    for sc in d.get('standardcostlist', []) or []:
        std_cost = parse_rate(sc.get('rate'))[0] or std_cost
    name = d.get('@name') or _list_name(d)
    return {
        'guid': d.get('guid', ''),
        'master_id': int(float(d.get('masterid') or 0)),
        'alter_id': int(float(d.get('alterid') or 0)),
        'name': name,
        'parent': d.get('parent', ''),
        'unit': d.get('baseunits', ''),
        'hsn': hsn,
        'gst_rate': rate,
        'description': d.get('description', ''),
        'part_no': _first(d, 'partno'),
        'price': std_price,
        'cost': std_cost,
        'closing_qty': parse_qty(d.get('closingbalance'))[0],
        'closing_rate': parse_rate(d.get('closingrate'))[0],
    }


def _parse_cost_centres(entry):
    out = []
    for cat in entry.get('categoryallocations') or []:
        category = cat.get('category') or 'Primary Cost Category'
        for cc in cat.get('costcentreallocations') or []:
            if cc.get('name'):
                out.append({'category': category, 'name': cc['name'],
                            'amount': parse_amount(cc.get('amount'))})
    return out


def normalize_simple_master(d):
    """Godown / cost centre / cost category."""
    return {
        'guid': d.get('guid', ''),
        'master_id': int(float(d.get('masterid') or 0)),
        'alter_id': int(float(d.get('alterid') or 0)),
        'name': d.get('@name') or _list_name(d),
        'parent': d.get('parent', ''),
        'category': d.get('category', ''),
    }


def normalize_voucher(d):
    ledger_entries = []
    for key in ('allledgerentries', 'ledgerentries'):
        for le in d.get(key, []) or []:
            if not le.get('ledgername'):
                continue
            bills = [{'name': b.get('name', ''), 'type': b.get('billtype', ''),
                      'amount': parse_amount(b.get('amount'))}
                     for b in le.get('billallocations', []) or [] if b.get('name')]
            ledger_entries.append({
                'ledger': le['ledgername'],
                'amount': parse_amount(le.get('amount')),
                'is_party': yes(le.get('ispartyledger')),
                'deemed_positive': yes(le.get('isdeemedpositive')),
                'bills': bills,
                'cost_centres': _parse_cost_centres(le),
            })
        if ledger_entries and key == 'allledgerentries':
            break
    inventory_entries = []
    for key in ('allinventoryentries', 'inventoryentries'):
        for ie in d.get(key, []) or []:
            if not ie.get('stockitemname'):
                continue
            qty, unit = parse_qty(ie.get('billedqty') or ie.get('actualqty'))
            rate, runit = parse_rate(ie.get('rate'))
            alloc = (ie.get('accountingallocations') or [{}])[0]
            batches = []
            for b in ie.get('batchallocations') or []:
                bqty, _bunit = parse_qty(b.get('billedqty') or b.get('actualqty'))
                batch = b.get('batchname') or ''
                batches.append({
                    'godown': b.get('godownname') or '',
                    'batch': '' if batch.lower() in ('primary batch', 'any') else batch,
                    'qty': bqty, 'amount': parse_amount(b.get('amount')),
                    'expiry': parse_date(b.get('expiryperiod')),
                })
            inventory_entries.append({
                'item': ie['stockitemname'],
                'qty': qty, 'unit': unit or runit, 'rate': rate,
                'discount': parse_amount(ie.get('discount')),
                'amount': parse_amount(ie.get('amount')),
                'ledger': alloc.get('ledgername', ''),
                'godown': batches[0]['godown'] if batches else '',
                'batches': batches,
                'cost_centres': _parse_cost_centres(alloc),
            })
        if inventory_entries:
            break
    return {
        'guid': d.get('guid', ''),
        'remote_id': d.get('@remoteid', ''),
        'master_id': int(float(d.get('masterid') or 0)),
        'alter_id': int(float(d.get('alterid') or 0)),
        'date': parse_date(d.get('date')),
        'vch_type': d.get('vouchertypename') or d.get('@vchtype', ''),
        'number': d.get('vouchernumber', ''),
        'reference': d.get('reference', ''),
        'reference_date': parse_date(d.get('referencedate')),
        'party': d.get('partyledgername', ''),
        'party_gstin': d.get('partygstin', ''),
        'place_of_supply': d.get('placeofsupply', ''),
        'narration': d.get('narration', ''),
        'cancelled': yes(d.get('iscancelled')),
        'optional': yes(d.get('isoptional')),
        'is_invoice': yes(d.get('isinvoice')),
        'irn': d.get('irn', ''),
        'ledger_entries': ledger_entries,
        'inventory_entries': inventory_entries,
    }


def primary_role(primary_group):
    return PRIMARY_GROUP_ROLE.get((primary_group or '').strip().lower(), '')


def voucher_balance(v):
    """Sum of ledger entry amounts (and inventory amounts when in invoice mode).

    A balanced Tally voucher sums to zero.
    """
    total = sum(le['amount'] for le in v.get('ledger_entries', []))
    if v.get('inventory_entries') and not any(
            le['ledger'] == ie['ledger'] for ie in v['inventory_entries']
            for le in v.get('ledger_entries', [])):
        total += sum(ie['amount'] for ie in v['inventory_entries'])
    return round(total, 2)
