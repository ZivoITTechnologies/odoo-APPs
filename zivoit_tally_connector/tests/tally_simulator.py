# -*- coding: utf-8 -*-
"""In-memory TallyPrime simulator speaking the TallyPrime XML-over-HTTP protocol.

Used by the automated tests and for demos (``python tally_simulator.py`` serves on
:9000).  It mimics TallyPrime behaviour that matters for integration:

* Collection exports with TDL FETCH, $_PrimaryGroup COMPUTE, `$AlterID > n` filters,
  SVFROMDATE/SVTODATE periods, company selection (SVCURRENTCOMPANY).
* Prime 3.0+ export shapes (LEDMAILINGDETAILS.LIST, LEDGSTREGDETAILS.LIST,
  HSNDETAILS.LIST) and the infamous ``&#4;`` control characters.
* Import of ledgers, units, stock items and vouchers with Tally's validations:
  unknown ledger / item / unit / voucher type / group, unbalanced voucher,
  date before books, duplicate master, REMOTEID idempotency (alter), Cancel.
* IMPORTRESULT counters, LINEERROR messages, LASTVCHID, AlterID/MasterID sequencing.
"""
import itertools
import threading
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

PRIMARY_GROUPS = [
    'Capital Account', 'Current Assets', 'Current Liabilities', 'Fixed Assets',
    'Investments', 'Loans (Liability)', 'Misc. Expenses (ASSET)', 'Suspense A/c',
    'Sales Accounts', 'Purchase Accounts', 'Direct Incomes', 'Indirect Incomes',
    'Direct Expenses', 'Indirect Expenses', 'Branch / Divisions',
]
SUB_GROUPS = {
    'Sundry Debtors': 'Current Assets', 'Bank Accounts': 'Current Assets',
    'Cash-in-Hand': 'Current Assets', 'Deposits (Asset)': 'Current Assets',
    'Loans & Advances (Asset)': 'Current Assets', 'Stock-in-Hand': 'Current Assets',
    'Sundry Creditors': 'Current Liabilities', 'Duties & Taxes': 'Current Liabilities',
    'Provisions': 'Current Liabilities', 'Reserves & Surplus': 'Capital Account',
    'Secured Loans': 'Loans (Liability)', 'Unsecured Loans': 'Loans (Liability)',
    'Bank OD A/c': 'Loans (Liability)',
}
VOUCHER_TYPES = ['Sales', 'Purchase', 'Credit Note', 'Debit Note', 'Receipt', 'Payment',
                 'Journal', 'Contra']


def e(v):
    return escape(str(v if v is not None else ''))


def amt(v):
    return '%.2f' % float(v)


def yn(b):
    return 'Yes' if b else 'No'


class Company:
    def __init__(self, name, books_from=date(2020, 4, 1), state='Telangana'):
        self.name = name
        self.books_from = books_from
        self.state = state
        self.guid = str(uuid.uuid4())
        self._alter = itertools.count(1)
        self._master = itertools.count(1)
        self._vmaster = itertools.count(1)
        self.groups = {g: '' for g in PRIMARY_GROUPS}
        self.groups.update(SUB_GROUPS)
        self.ledgers = {}
        self.units = {}
        self.items = {}
        self.vouchers = {}          # guid -> voucher dict
        self.vchtypes = {v: v for v in VOUCHER_TYPES}
        self.group_meta = {g: self._meta() for g in self.groups}
        self.vchtype_meta = {v: self._meta() for v in self.vchtypes}
        self.godowns = {'Main Location': dict(self._meta(), name='Main Location', parent='')}
        self.costcats = {'Primary Cost Category': dict(self._meta(), name='Primary Cost Category')}
        self.costcentres = {}
        self.ledgers['Cash'] = dict(self._meta(), name='Cash', parent='Cash-in-Hand',
                                    opening=0.0)
        self.ledgers['Profit & Loss A/c'] = dict(self._meta(), name='Profit & Loss A/c',
                                                 parent='Capital Account', opening=0.0)

    def _meta(self):
        return {'guid': str(uuid.uuid4()), 'master_id': next(self._master),
                'alter_id': next(self._alter)}

    def touch(self, obj):
        obj['alter_id'] = next(self._alter)

    def primary(self, group):
        seen = 0
        while group and self.groups.get(group) and seen < 20:
            group = self.groups[group]
            seen += 1
        return group

    def group_of_ledger(self, name):
        return self.primary(self.ledgers[name]['parent'])

    def closing(self, name):
        led = self.ledgers[name]
        bal = led.get('opening', 0.0)
        for v in self.vouchers.values():
            if v.get('cancelled'):
                continue
            for le in v['ledger_entries']:
                if le['ledger'] == name:
                    bal += le['amount']
            for ie in v['inventory_entries']:
                if ie.get('ledger') == name and not any(
                        l['ledger'] == name for l in v['ledger_entries']):
                    bal += ie['amount']
        return round(bal, 2)


class TallySimulator:
    def __init__(self, company_name='Zivo Demo Pvt Ltd'):
        self.companies = {company_name: Company(company_name)}
        self.open_companies = {company_name}
        self.requests = []
        self.lock = threading.Lock()
        self.fail_next = 0          # simulate Tally being busy/offline (HTTP-level)

    @property
    def company(self):
        return next(iter(self.companies.values()))

    # ------------------------------------------------------------------ helpers for tests
    def add_ledger(self, name, parent, gstin='', state='', address=None, opening=0.0, **kw):
        c = self.company
        led = dict(c._meta(), name=name, parent=parent, gstin=gstin, state=state,
                   address=address or [], opening=opening, **kw)
        c.ledgers[name] = led
        return led

    def add_unit(self, name):
        c = self.company
        c.units[name] = dict(c._meta(), name=name)

    def add_item(self, name, unit='Nos', hsn='', gst_rate=None, price=0.0, part_no=''):
        c = self.company
        if unit not in c.units:
            self.add_unit(unit)
        c.items[name] = dict(c._meta(), name=name, unit=unit, hsn=hsn, gst_rate=gst_rate,
                             price=price, part_no=part_no, parent='')
        return c.items[name]

    def add_voucher(self, vch_type, vdate, number, ledger_entries, inventory_entries=None,
                    party='', narration='', reference='', remote_id=''):
        c = self.company
        v = dict(c._meta(), guid=remote_id or str(uuid.uuid4()), remote_id=remote_id,
                 vch_type=vch_type, date=vdate, number=number, party=party,
                 narration=narration, reference=reference, cancelled=False,
                 ledger_entries=ledger_entries, inventory_entries=inventory_entries or [])
        v['master_id'] = next(c._vmaster)
        c.vouchers[v['guid']] = v
        return v

    def add_godown(self, name, parent=''):
        c = self.company
        c.godowns[name] = dict(c._meta(), name=name, parent=parent)
        return c.godowns[name]

    def add_cost_centre(self, name, category='Primary Cost Category', parent=''):
        c = self.company
        if category not in c.costcats:
            c.costcats[category] = dict(c._meta(), name=category)
        c.costcentres[name] = dict(c._meta(), name=name, category=category, parent=parent)
        return c.costcentres[name]

    def delete_voucher(self, guid):
        del self.company.vouchers[guid]

    def delete_ledger(self, name):
        del self.company.ledgers[name]

    def delete_item(self, name):
        del self.company.items[name]

    def alter_voucher(self, guid, **changes):
        c = self.company
        v = c.vouchers[guid]
        v.update(changes)
        c.touch(v)
        return v

    # ------------------------------------------------------------------ protocol
    def handle(self, raw):
        with self.lock:
            text = raw.decode('utf-8') if isinstance(raw, bytes) else raw
            self.requests.append(text)
            try:
                # Tally itself accepts the &#4; marker it uses for reserved values
                root = ET.fromstring(text.replace('&#4;', ''))
            except ET.ParseError:
                return '<RESPONSE>Unknown Request, cannot be processed</RESPONSE>'
            req = (root.findtext('HEADER/TALLYREQUEST') or '').strip().lower()
            if req == 'export':
                return self._export(root)
            if req == 'import data':
                return self._import(root)
            return '<RESPONSE>Unknown Request, cannot be processed</RESPONSE>'

    def _error_envelope(self, msg):
        return ('<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>0</STATUS></HEADER><BODY><DATA>'
                '<LINEERROR>%s</LINEERROR></DATA></BODY></ENVELOPE>' % e(msg))

    def _company_for(self, root):
        name = root.findtext('.//SVCURRENTCOMPANY')
        if name:
            if name not in self.companies or name not in self.open_companies:
                return None, "Could not find Company '%s'" % name
            return self.companies[name], None
        if not self.open_companies:
            return None, 'No company is open'
        return self.companies[next(iter(self.open_companies))], None

    # ------------------------------------------------------------------ export
    def _export(self, root):
        coll = root.find('.//TDLMESSAGE/COLLECTION')
        if coll is None:
            return self._error_envelope("Could not find Report '%s'!" % root.findtext('HEADER/ID'))
        otype = (coll.findtext('TYPE') or '').strip()
        if otype == 'Company':
            body = ''.join('<COMPANY NAME="%s"><NAME>%s</NAME><GUID>%s</GUID><STARTINGFROM>%s'
                           '</STARTINGFROM><STATENAME>%s</STATENAME></COMPANY>'
                           % (e(c.name), e(c.name), c.guid, c.books_from.strftime('%Y%m%d'),
                              e(c.state)) for n, c in self.companies.items()
                           if n in self.open_companies)
            return self._wrap(body)
        comp, err = self._company_for(root)
        if err:
            return self._error_envelope(err)
        alter_gt = 0
        for sysf in root.iter('SYSTEM'):
            if (sysf.text or '').strip().startswith('$AlterID'):
                alter_gt = int((sysf.text or '').split('>')[-1].strip())
        dfrom = root.findtext('.//SVFROMDATE')
        dto = root.findtext('.//SVTODATE')
        dfrom = datetime.strptime(dfrom, '%Y%m%d').date() if dfrom else comp.books_from
        dto = datetime.strptime(dto, '%Y%m%d').date() if dto else date(2099, 1, 1)
        ser = {'Ledger': self._ser_ledger, 'StockItem': self._ser_item,
               'VoucherType': self._ser_vchtype, 'Group': self._ser_group,
               'Unit': self._ser_unit, 'Voucher': self._ser_voucher,
               'Godown': self._ser_godown, 'CostCentre': self._ser_costcentre,
               'CostCategory': self._ser_costcat}.get(otype)
        if not ser:
            return self._error_envelope("Unknown object type '%s'" % otype)
        source = {'Ledger': comp.ledgers, 'StockItem': comp.items, 'Unit': comp.units,
                  'Voucher': comp.vouchers, 'Godown': comp.godowns,
                  'CostCentre': comp.costcentres, 'CostCategory': comp.costcats,
                  'VoucherType': {k: dict(comp.vchtype_meta[k], name=k, parent=p)
                                  for k, p in comp.vchtypes.items()},
                  'Group': {k: dict(comp.group_meta[k], name=k, parent=p)
                            for k, p in comp.groups.items()}}[otype]
        out = []
        for obj in source.values():
            if obj['alter_id'] <= alter_gt:
                continue
            if otype == 'Voucher' and not (dfrom <= obj['date'] <= dto):
                continue
            out.append(ser(comp, obj))
        return self._wrap(''.join(out))

    def _wrap(self, body):
        return ('<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DESC>'
                '</DESC><DATA><COLLECTION>%s</COLLECTION></DATA></BODY></ENVELOPE>' % body)

    def _common(self, o):
        return ('<GUID TYPE="String">%s</GUID><MASTERID TYPE="Number"> %s</MASTERID>'
                '<ALTERID TYPE="Number"> %s</ALTERID>' % (o['guid'], o['master_id'], o['alter_id']))

    def _ser_godown(self, comp, g):
        return '<GODOWN NAME="%s" RESERVEDNAME="">%s<PARENT>%s</PARENT></GODOWN>' % (
            e(g['name']), self._common(g), e(g.get('parent', '')))

    def _ser_costcentre(self, comp, c):
        return ('<COSTCENTRE NAME="%s" RESERVEDNAME="">%s<PARENT>%s</PARENT><CATEGORY>%s'
                '</CATEGORY></COSTCENTRE>') % (e(c['name']), self._common(c),
                                               e(c.get('parent', '')), e(c['category']))

    def _ser_costcat(self, comp, c):
        return '<COSTCATEGORY NAME="%s" RESERVEDNAME="">%s</COSTCATEGORY>' % (
            e(c['name']), self._common(c))

    @staticmethod
    def _ser_cc(allocs):
        by_cat = {}
        for a in allocs or []:
            by_cat.setdefault(a['category'], []).append(a)
        return ''.join(
            '<CATEGORYALLOCATIONS.LIST><CATEGORY>%s</CATEGORY>%s</CATEGORYALLOCATIONS.LIST>' % (
                e(cat), ''.join('<COSTCENTREALLOCATIONS.LIST><NAME>%s</NAME><AMOUNT>%s</AMOUNT>'
                                '</COSTCENTREALLOCATIONS.LIST>' % (e(a['name']), amt(a['amount']))
                                for a in items)) for cat, items in by_cat.items())

    def _ser_group(self, comp, g):
        return '<GROUP NAME="%s" RESERVEDNAME="">%s<PARENT>%s</PARENT><ZPRIMARYGROUP>%s' \
               '</ZPRIMARYGROUP></GROUP>' % (e(g['name']), self._common(g), e(g['parent']),
                                             e(comp.primary(g['name'])))

    def _ser_vchtype(self, comp, v):
        return '<VOUCHERTYPE NAME="%s" RESERVEDNAME="">%s<PARENT>%s</PARENT></VOUCHERTYPE>' % (
            e(v['name']), self._common(v), e(v['parent']))

    def _ser_unit(self, comp, u):
        return '<UNIT NAME="%s">%s<NAME>%s</NAME><ISSIMPLEUNIT>Yes</ISSIMPLEUNIT></UNIT>' % (
            e(u['name']), self._common(u), e(u['name']))

    def _ser_ledger(self, comp, l):
        addr = ''.join('<ADDRESS>%s</ADDRESS>' % e(a) for a in l.get('address') or [])
        closing = comp.closing(l['name'])
        reg = l.get('registration_type') or ('Regular' if l.get('gstin') else '')
        return (
            '<LEDGER NAME="%(n)s" RESERVEDNAME="">%(c)s<PARENT TYPE="String">%(p)s</PARENT>'
            '<ZPRIMARYGROUP>%(pg)s</ZPRIMARYGROUP>'
            '<EMAIL>%(email)s</EMAIL><LEDGERPHONE>%(ph)s</LEDGERPHONE>'
            '<INCOMETAXNUMBER>%(pan)s</INCOMETAXNUMBER>'
            '<ISBILLWISEON>%(bw)s</ISBILLWISEON><TAXTYPE>%(tt)s</TAXTYPE>'
            '<GSTDUTYHEAD>%(dh)s</GSTDUTYHEAD><RATEOFTAXCALCULATION>%(rate)s</RATEOFTAXCALCULATION>'
            '<OPENINGBALANCE>%(ob)s</OPENINGBALANCE><CLOSINGBALANCE>%(cb)s</CLOSINGBALANCE>'
            '<LEDMAILINGDETAILS.LIST><ADDRESS.LIST TYPE="String">%(addr)s</ADDRESS.LIST>'
            '<APPLICABLEFROM>20170701</APPLICABLEFROM><MAILINGNAME>%(n)s</MAILINGNAME>'
            '<STATE>%(st)s</STATE><COUNTRY>India</COUNTRY><PINCODE>%(pin)s</PINCODE>'
            '</LEDMAILINGDETAILS.LIST>'
            '<LEDGSTREGDETAILS.LIST><APPLICABLEFROM>20170701</APPLICABLEFROM>'
            '<GSTREGISTRATIONTYPE>%(reg)s</GSTREGISTRATIONTYPE><PLACEOFSUPPLY>%(st)s'
            '</PLACEOFSUPPLY><GSTIN>%(gstin)s</GSTIN></LEDGSTREGDETAILS.LIST>'
            '<NAME.LIST TYPE="String"><NAME>%(n)s</NAME></NAME.LIST></LEDGER>'
        ) % {'n': e(l['name']), 'c': self._common(l), 'p': e(l['parent']),
             'pg': e(comp.primary(l['parent'])), 'email': e(l.get('email', '')),
             'ph': e(l.get('phone', '')), 'pan': e(l.get('pan', '')),
             'bw': yn(l.get('billwise', l['parent'] in ('Sundry Debtors', 'Sundry Creditors'))),
             'tt': e(l.get('tax_type', '')), 'dh': e(l.get('duty_head', '')),
             'rate': e(l.get('rate', '')), 'ob': amt(l.get('opening', 0)), 'cb': amt(closing),
             'addr': addr, 'st': e(l.get('state', '')), 'pin': e(l.get('pincode', '')),
             'reg': ('&#4; ' + e(reg)) if reg == 'Unknown' else e(reg),
             'gstin': e(l.get('gstin', ''))}

    def _ser_item(self, comp, it):
        gst = ''
        if it.get('gst_rate') is not None:
            r = float(it['gst_rate'])
            gst = ('<GSTDETAILS.LIST><APPLICABLEFROM>20170701</APPLICABLEFROM><TAXABILITY>Taxable'
                   '</TAXABILITY><STATEWISEDETAILS.LIST><STATENAME>&#4; Any</STATENAME>'
                   '<RATEDETAILS.LIST><GSTRATEDUTYHEAD>IGST</GSTRATEDUTYHEAD><GSTRATE> %s</GSTRATE>'
                   '</RATEDETAILS.LIST><RATEDETAILS.LIST><GSTRATEDUTYHEAD>CGST</GSTRATEDUTYHEAD>'
                   '<GSTRATE> %s</GSTRATE></RATEDETAILS.LIST></STATEWISEDETAILS.LIST>'
                   '</GSTDETAILS.LIST>' % (amt(r), amt(r / 2)))
        hsn = ('<HSNDETAILS.LIST><APPLICABLEFROM>20170701</APPLICABLEFROM><HSNCODE>%s</HSNCODE>'
               '</HSNDETAILS.LIST>' % e(it['hsn'])) if it.get('hsn') else ''
        price = ('<STANDARDPRICELIST.LIST><DATE>20200401</DATE><RATE>%s/%s</RATE>'
                 '</STANDARDPRICELIST.LIST>' % (amt(it['price']), e(it['unit']))) \
            if it.get('price') else ''
        return ('<STOCKITEM NAME="%s" RESERVEDNAME="">%s<PARENT>%s</PARENT><BASEUNITS>%s'
                '</BASEUNITS><PARTNO>%s</PARTNO>%s%s%s</STOCKITEM>') % (
            e(it['name']), self._common(it), e(it.get('parent', '')), e(it['unit']),
            e(it.get('part_no', '')), hsn, gst, price)

    def _ser_voucher(self, comp, v):
        inv_mode = bool(v['inventory_entries'])
        tag = 'LEDGERENTRIES.LIST' if inv_mode else 'ALLLEDGERENTRIES.LIST'
        les = ''.join(
            '<%(t)s><LEDGERNAME>%(l)s</LEDGERNAME><ISDEEMEDPOSITIVE>%(dp)s</ISDEEMEDPOSITIVE>'
            '<ISPARTYLEDGER>%(ip)s</ISPARTYLEDGER><AMOUNT>%(a)s</AMOUNT>%(b)s%(cc)s</%(t)s>' % {
                't': tag, 'l': e(le['ledger']), 'dp': yn(le['amount'] < 0),
                'ip': yn(le['ledger'] == v['party']), 'a': amt(le['amount']),
                'b': ''.join('<BILLALLOCATIONS.LIST><NAME>%s</NAME><BILLTYPE>%s</BILLTYPE>'
                             '<AMOUNT>%s</AMOUNT></BILLALLOCATIONS.LIST>' % (
                                 e(b['name']), e(b['type']), amt(b['amount']))
                             for b in le.get('bills') or []),
                'cc': self._ser_cc(le.get('cost_centres'))}
            for le in v['ledger_entries'])
        ies = ''.join(
            '<ALLINVENTORYENTRIES.LIST><STOCKITEMNAME>%(i)s</STOCKITEMNAME><ISDEEMEDPOSITIVE>%(dp)s'
            '</ISDEEMEDPOSITIVE><RATE>%(r)s/%(u)s</RATE><AMOUNT>%(a)s</AMOUNT><ACTUALQTY> %(q)s %(u)s'
            '</ACTUALQTY><BILLEDQTY> %(q)s %(u)s</BILLEDQTY>%(bat)s<ACCOUNTINGALLOCATIONS.LIST>'
            '<LEDGERNAME>%(l)s</LEDGERNAME><AMOUNT>%(a)s</AMOUNT>%(cc)s'
            '</ACCOUNTINGALLOCATIONS.LIST></ALLINVENTORYENTRIES.LIST>' % {
                'i': e(ie['item']), 'dp': yn(ie['amount'] < 0), 'r': amt(ie['rate']),
                'u': e(ie.get('unit') or 'Nos'), 'a': amt(ie['amount']), 'q': amt(ie['qty']),
                'l': e(ie.get('ledger', '')), 'cc': self._ser_cc(ie.get('cost_centres')),
                'bat': ''.join(
                    '<BATCHALLOCATIONS.LIST><GODOWNNAME>%s</GODOWNNAME><BATCHNAME>%s</BATCHNAME>'
                    '%s<AMOUNT>%s</AMOUNT><ACTUALQTY> %s %s</ACTUALQTY><BILLEDQTY> %s %s'
                    '</BILLEDQTY></BATCHALLOCATIONS.LIST>' % (
                        e(b.get('godown') or 'Main Location'), e(b.get('batch') or 'Primary Batch'),
                        ('<EXPIRYPERIOD>%s</EXPIRYPERIOD>' % b['expiry'].strftime('%Y%m%d'))
                        if b.get('expiry') else '', amt(b['amount']), amt(b['qty']),
                        e(ie.get('unit') or 'Nos'), amt(b['qty']), e(ie.get('unit') or 'Nos'))
                    for b in ie.get('batches') or [])}
            for ie in v['inventory_entries'])
        remote = (' REMOTEID="%s"' % e(v['remote_id'])) if v.get('remote_id') else ''
        return (
            '<VOUCHER%(rid)s VCHKEY="%(guid)s:00000008" VCHTYPE="%(vt)s" ACTION="Create" '
            'OBJVIEW="%(view)s">%(c)s<DATE TYPE="Date">%(d)s</DATE><VOUCHERTYPENAME>%(vt)s'
            '</VOUCHERTYPENAME><VOUCHERNUMBER>%(num)s</VOUCHERNUMBER><REFERENCE>%(ref)s</REFERENCE>'
            '<PARTYLEDGERNAME>%(party)s</PARTYLEDGERNAME><NARRATION>%(nar)s</NARRATION>'
            '<ISCANCELLED>%(canc)s</ISCANCELLED><ISOPTIONAL>No</ISOPTIONAL>'
            '<ISINVOICE>%(inv)s</ISINVOICE><PERSISTEDVIEW>%(view)s</PERSISTEDVIEW>'
            '%(ies)s%(les)s</VOUCHER>'
        ) % {'rid': remote, 'guid': v['guid'], 'vt': e(v['vch_type']),
             'view': 'Invoice Voucher View' if inv_mode else 'Accounting Voucher View',
             'c': self._common(v), 'd': v['date'].strftime('%Y%m%d'), 'num': e(v['number']),
             'ref': e(v.get('reference', '')), 'party': e(v.get('party', '')),
             'nar': e(v.get('narration', '')), 'canc': yn(v.get('cancelled')),
             'inv': yn(inv_mode), 'ies': ies, 'les': les}

    # ------------------------------------------------------------------ import
    def _import(self, root):
        comp, err = self._company_for(root)
        if err:
            return self._error_envelope(err)
        res = {'CREATED': 0, 'ALTERED': 0, 'DELETED': 0, 'LASTVCHID': 0, 'LASTMID': 0,
               'COMBINED': 0, 'IGNORED': 0, 'ERRORS': 0, 'CANCELLED': 0}
        errors = []
        for msg in root.iter('TALLYMESSAGE'):
            for obj in msg:
                try:
                    handler = {'LEDGER': self._imp_ledger, 'UNIT': self._imp_unit,
                               'STOCKITEM': self._imp_item, 'VOUCHER': self._imp_voucher,
                               'GODOWN': self._imp_godown, 'COSTCENTRE': self._imp_costcentre,
                               'COSTCATEGORY': self._imp_costcat}[obj.tag]
                    handler(comp, obj, res)
                except KeyError:
                    errors.append("Unknown object '%s'" % obj.tag)
                    res['ERRORS'] += 1
                except ValueError as ex:
                    errors.append(str(ex))
                    res['ERRORS'] += 1
        body = ''.join('<%s>%s</%s>' % (k, v, k) for k, v in res.items())
        lines = ''.join('<LINEERROR>%s</LINEERROR>' % e(x) for x in errors)
        return ('<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DATA>'
                '<IMPORTRESULT>%s<EXCEPTIONS>0</EXCEPTIONS></IMPORTRESULT>%s</DATA></BODY>'
                '</ENVELOPE>' % (body, lines))

    def _imp_ledger(self, comp, el, res):
        attr_name = el.get('NAME')
        action = (el.get('ACTION') or 'Create').lower()
        new_name = el.findtext('NAME.LIST/NAME') or attr_name
        parent = (el.findtext('PARENT') or '').strip()
        if parent and parent not in comp.groups:
            raise ValueError("Group '%s' does not exist!" % parent)
        mail = el.find('LEDMAILINGDETAILS.LIST')
        gst = el.find('LEDGSTREGDETAILS.LIST')
        vals = {
            'parent': parent, 'email': el.findtext('EMAIL') or '',
            'phone': el.findtext('LEDGERPHONE') or '',
            'gstin': (gst.findtext('GSTIN') if gst is not None else el.findtext('PARTYGSTIN')) or '',
            'state': (mail.findtext('STATE') if mail is not None else el.findtext('LEDSTATENAME'))
            or '',
            'pincode': (mail.findtext('PINCODE') if mail is not None else el.findtext('PINCODE'))
            or '',
            'address': [a.text for a in el.iter('ADDRESS') if a.text],
            'tax_type': el.findtext('TAXTYPE') or '', 'duty_head': el.findtext('GSTDUTYHEAD') or '',
            'rate': el.findtext('RATEOFTAXCALCULATION') or '',
        }
        if action == 'create':
            if attr_name in comp.ledgers:
                raise ValueError("Ledger '%s' already exists!" % attr_name)
            if not parent:
                raise ValueError("Ledger '%s': Parent is mandatory" % attr_name)
            comp.ledgers[new_name] = dict(comp._meta(), name=new_name, opening=0.0,
                                          cost_centres_on=el.findtext('ISCOSTCENTRESON') == 'Yes',
                                          **vals)
            res['CREATED'] += 1
        else:
            if attr_name not in comp.ledgers:
                raise ValueError("Ledger '%s' does not exist!" % attr_name)
            led = comp.ledgers.pop(attr_name)
            led.update({k: v for k, v in vals.items() if v})
            led['name'] = new_name
            comp.touch(led)
            comp.ledgers[new_name] = led
            res['ALTERED'] += 1

    def _imp_godown(self, comp, el, res):
        attr = el.get('NAME')
        new = el.findtext('NAME.LIST/NAME') or attr
        if (el.get('ACTION') or 'Create').lower() == 'create':
            if attr in comp.godowns:
                raise ValueError("Godown '%s' already exists!" % attr)
            comp.godowns[new] = dict(comp._meta(), name=new, parent=el.findtext('PARENT') or '')
            res['CREATED'] += 1
        else:
            if attr not in comp.godowns:
                raise ValueError("Godown '%s' does not exist!" % attr)
            g = comp.godowns.pop(attr)
            g['name'] = new
            comp.touch(g)
            comp.godowns[new] = g
            res['ALTERED'] += 1

    def _imp_costcat(self, comp, el, res):
        name = el.get('NAME')
        if name in comp.costcats:
            res['ALTERED'] += 1
            return
        comp.costcats[name] = dict(comp._meta(), name=name)
        res['CREATED'] += 1

    def _imp_costcentre(self, comp, el, res):
        attr = el.get('NAME')
        new = el.findtext('NAME.LIST/NAME') or attr
        cat = el.findtext('CATEGORY') or 'Primary Cost Category'
        if cat not in comp.costcats:
            raise ValueError("Cost Category '%s' does not exist!" % cat)
        if (el.get('ACTION') or 'Create').lower() == 'create':
            if attr in comp.costcentres:
                raise ValueError("Cost Centre '%s' already exists!" % attr)
            comp.costcentres[new] = dict(comp._meta(), name=new, category=cat,
                                         parent=el.findtext('PARENT') or '')
            res['CREATED'] += 1
        else:
            if attr not in comp.costcentres:
                raise ValueError("Cost Centre '%s' does not exist!" % attr)
            c = comp.costcentres.pop(attr)
            c.update(name=new, category=cat)
            comp.touch(c)
            comp.costcentres[new] = c
            res['ALTERED'] += 1

    @staticmethod
    def _imp_cc(comp, el):
        out = []
        for cat in el.findall('CATEGORYALLOCATIONS.LIST'):
            category = cat.findtext('CATEGORY') or 'Primary Cost Category'
            if category not in comp.costcats:
                raise ValueError("Cost Category '%s' does not exist!" % category)
            for cc in cat.findall('COSTCENTREALLOCATIONS.LIST'):
                name = cc.findtext('NAME')
                if name not in comp.costcentres:
                    raise ValueError("Cost Centre '%s' does not exist!" % name)
                out.append({'category': category, 'name': name,
                            'amount': float(cc.findtext('AMOUNT') or 0)})
        return out

    def _imp_unit(self, comp, el, res):
        name = el.get('NAME')
        if name in comp.units:
            res['ALTERED'] += 1
            return
        comp.units[name] = dict(comp._meta(), name=name)
        res['CREATED'] += 1

    def _imp_item(self, comp, el, res):
        attr_name = el.get('NAME')
        action = (el.get('ACTION') or 'Create').lower()
        new_name = el.findtext('NAME.LIST/NAME') or attr_name
        unit = el.findtext('BASEUNITS') or ''
        if unit and unit not in comp.units:
            raise ValueError("Unit '%s' does not exist!" % unit)
        hsn = el.findtext('.//HSNCODE') or ''
        rate = None
        for rd in el.iter('RATEDETAILS.LIST'):
            if rd.findtext('GSTRATEDUTYHEAD') == 'IGST':
                rate = float(rd.findtext('GSTRATE') or 0)
        if action == 'create':
            if attr_name in comp.items:
                raise ValueError("Stock Item '%s' already exists!" % attr_name)
            comp.items[new_name] = dict(comp._meta(), name=new_name, unit=unit, hsn=hsn,
                                        gst_rate=rate, price=0.0, parent='',
                                        part_no=el.findtext('MAILINGNAME') or '',
                                        batchwise=el.findtext('ISBATCHWISEON') == 'Yes')
            res['CREATED'] += 1
        else:
            if attr_name not in comp.items:
                raise ValueError("Stock Item '%s' does not exist!" % attr_name)
            it = comp.items.pop(attr_name)
            it.update(name=new_name, unit=unit or it['unit'], hsn=hsn or it['hsn'],
                      gst_rate=rate if rate is not None else it['gst_rate'])
            if el.findtext('ISBATCHWISEON'):
                it['batchwise'] = el.findtext('ISBATCHWISEON') == 'Yes'
            comp.touch(it)
            comp.items[new_name] = it
            res['ALTERED'] += 1

    def _imp_voucher(self, comp, el, res):
        action = (el.get('ACTION') or 'Create').lower()
        vtype = el.get('VCHTYPE') or el.findtext('VOUCHERTYPENAME')
        if action in ('cancel', 'delete'):
            number = el.get('TAGVALUE')
            for v in comp.vouchers.values():
                if v['number'] == number and v['vch_type'] == vtype:
                    if action == 'cancel':
                        v['cancelled'] = True
                        comp.touch(v)
                        res['CANCELLED'] += 1
                    else:
                        del comp.vouchers[v['guid']]
                        res['DELETED'] += 1
                    return
            raise ValueError("Voucher '%s' not found!" % number)
        if vtype not in comp.vchtypes:
            raise ValueError("Voucher Type '%s' does not exist!" % vtype)
        vdate = datetime.strptime(el.findtext('DATE'), '%Y%m%d').date()
        if vdate < comp.books_from:
            raise ValueError('Date %s is out of range (before books beginning)' % vdate)
        les, ies = [], []
        for tag in ('ALLLEDGERENTRIES.LIST', 'LEDGERENTRIES.LIST'):
            for le in el.findall(tag):
                name = le.findtext('LEDGERNAME')
                if name not in comp.ledgers:
                    raise ValueError("Ledger '%s' does not exist!" % name)
                les.append({'ledger': name, 'amount': float(le.findtext('AMOUNT') or 0),
                            'cost_centres': self._imp_cc(comp, le),
                            'bills': [{'name': b.findtext('NAME'), 'type': b.findtext('BILLTYPE'),
                                       'amount': float(b.findtext('AMOUNT') or 0)}
                                      for b in le.findall('BILLALLOCATIONS.LIST')]})
        for ie in el.findall('ALLINVENTORYENTRIES.LIST'):
            item = ie.findtext('STOCKITEMNAME')
            if item not in comp.items:
                raise ValueError("Stock Item '%s' does not exist!" % item)
            alloc = ie.find('ACCOUNTINGALLOCATIONS.LIST')
            ledger = alloc.findtext('LEDGERNAME') if alloc is not None else ''
            if ledger and ledger not in comp.ledgers:
                raise ValueError("Ledger '%s' does not exist!" % ledger)
            qty = float((ie.findtext('BILLEDQTY') or '0').split()[0])
            rate_txt = ie.findtext('RATE') or '0/Nos'
            batches = []
            for b in ie.findall('BATCHALLOCATIONS.LIST'):
                gd = b.findtext('GODOWNNAME') or 'Main Location'
                if gd not in comp.godowns:
                    raise ValueError("Godown '%s' does not exist!" % gd)
                bname = b.findtext('BATCHNAME') or 'Primary Batch'
                if comp.items[item].get('batchwise') and bname == 'Primary Batch':
                    raise ValueError("Batches are required for '%s' (maintained in batches)" % item)
                exp = b.findtext('EXPIRYPERIOD')
                batches.append({'godown': gd, 'batch': '' if bname == 'Primary Batch' else bname,
                                'qty': float((b.findtext('BILLEDQTY') or '0').split()[0]),
                                'amount': float(b.findtext('AMOUNT') or 0),
                                'expiry': datetime.strptime(exp, '%Y%m%d').date() if exp else None})
            if batches and abs(sum(b['qty'] for b in batches) - qty) > 0.0001:
                raise ValueError("Batch quantities of '%s' do not add up to %s" % (item, qty))
            ies.append({'item': item, 'qty': qty, 'rate': float(rate_txt.split('/')[0]),
                        'unit': rate_txt.split('/')[-1], 'amount': float(ie.findtext('AMOUNT') or 0),
                        'ledger': ledger, 'batches': batches,
                        'cost_centres': self._imp_cc(comp, alloc) if alloc is not None else []})
        total = sum(l['amount'] for l in les) + sum(i['amount'] for i in ies)
        if abs(total) > 0.005:
            dr = -sum(x for x in [l['amount'] for l in les] + [i['amount'] for i in ies] if x < 0)
            cr = sum(x for x in [l['amount'] for l in les] + [i['amount'] for i in ies] if x > 0)
            raise ValueError('Voucher totals do not match! Dr: %.2f Dr Cr: %.2f Cr Diff: %.2f'
                             % (dr, cr, abs(total)))
        remote = el.get('REMOTEID') or ''
        existing = comp.vouchers.get(remote) if remote else None
        data = {'vch_type': vtype, 'date': vdate, 'number': el.findtext('VOUCHERNUMBER') or '',
                'party': el.findtext('PARTYLEDGERNAME') or '',
                'narration': el.findtext('NARRATION') or '',
                'reference': el.findtext('REFERENCE') or '', 'ledger_entries': les,
                'inventory_entries': ies, 'cancelled': False, 'remote_id': remote}
        if existing:
            existing.update(data)
            comp.touch(existing)
            res['ALTERED'] += 1
            res['LASTVCHID'] = existing['master_id']
        else:
            v = dict(comp._meta(), guid=remote or str(uuid.uuid4()), **data)
            v['master_id'] = next(comp._vmaster)
            comp.vouchers[v['guid']] = v
            res['CREATED'] += 1
            res['LASTVCHID'] = v['master_id']


class _Handler(BaseHTTPRequestHandler):
    sim = None

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n)
        if self.sim.fail_next > 0:
            self.sim.fail_next -= 1
            self.close_connection = True
            self.connection.shutdown(2)
            return
        out = self.sim.handle(body).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/xml; charset=utf-8')
        self.send_header('Content-Length', str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def serve(sim, host='127.0.0.1', port=0):
    handler = type('H', (_Handler,), {'sim': sim})
    httpd = ThreadingHTTPServer((host, port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='TallyPrime protocol simulator')
    p.add_argument('--port', type=int, default=9000)
    p.add_argument('--company', default='Zivo Demo Pvt Ltd')
    a = p.parse_args()
    s = TallySimulator(a.company)
    s.add_ledger('Sales GST 18%', 'Sales Accounts')
    s.add_ledger('Output CGST 9%', 'Duties & Taxes', tax_type='GST', duty_head='Central Tax', rate='9')
    s.add_ledger('Output SGST 9%', 'Duties & Taxes', tax_type='GST', duty_head='State Tax', rate='9')
    s.add_ledger('HDFC Bank', 'Bank Accounts')
    s.add_ledger('Sri Lakshmi Traders', 'Sundry Debtors', gstin='36AAACZ1234F1Z5',
                 state='Telangana', address=['Plot 12, IDA', 'Cherlapally', 'Hyderabad'])
    httpd = serve(s, '0.0.0.0', a.port)
    print('TallyPrime simulator for "%s" on :%s (Ctrl+C to stop)' % (a.company, a.port))
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        httpd.shutdown()
