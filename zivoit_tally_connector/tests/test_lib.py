# -*- coding: utf-8 -*-
"""Protocol & AI-engine unit tests (no database work needed)."""
import json
from datetime import date
from unittest.mock import patch

from odoo.tests import BaseCase, tagged

from ..lib import anomaly, error_kb, llm, matching
from ..lib import tally_xml as TX
from .common import make_gstin

LEGACY_LEDGER_EXPORT = """<ENVELOPE>
 <HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER>
 <BODY><DESC></DESC><DATA><COLLECTION>
  <LEDGER NAME="Ravi &amp; Sons" RESERVEDNAME="">
   <ADDRESS.LIST TYPE="String"><ADDRESS>12 MG Road</ADDRESS><ADDRESS>Secunderabad</ADDRESS></ADDRESS.LIST>
   <MAILINGNAME.LIST TYPE="String"><MAILINGNAME>Ravi &amp; Sons</MAILINGNAME></MAILINGNAME.LIST>
   <GUID TYPE="String">a1b2-0001</GUID><MASTERID TYPE="Number"> 77</MASTERID><ALTERID TYPE="Number"> 1290</ALTERID>
   <PARENT TYPE="String">Sundry Debtors</PARENT>
   <LEDSTATENAME>Telangana</LEDSTATENAME><COUNTRYNAME>India</COUNTRYNAME>
   <GSTREGISTRATIONTYPE>&#4; Unknown</GSTREGISTRATIONTYPE>
   <PARTYGSTIN>36AAACZ1234F1Z5</PARTYGSTIN>
   <CLOSINGBALANCE>-15,340.00</CLOSINGBALANCE>
   <NAME.LIST TYPE="String"><NAME>Ravi &amp; Sons</NAME><NAME>RAVI</NAME></NAME.LIST>
  </LEDGER>
 </COLLECTION></DATA></BODY></ENVELOPE>"""

DAYBOOK_INVOICE = """<ENVELOPE><BODY><IMPORTDATA><REQUESTDATA><TALLYMESSAGE xmlns:UDF="TallyUDF">
<VOUCHER REMOTEID="9f1c-77" VCHKEY="9f1c-77:00000008" VCHTYPE="GST Sales" ACTION="Create" OBJVIEW="Invoice Voucher View">
 <DATE>20261012</DATE><GUID>9f1c-77</GUID><VOUCHERTYPENAME>GST Sales</VOUCHERTYPENAME>
 <VOUCHERNUMBER>GS/112</VOUCHERNUMBER><PARTYLEDGERNAME>Ravi &amp; Sons</PARTYLEDGERNAME>
 <ISCANCELLED>No</ISCANCELLED><ISOPTIONAL>No</ISOPTIONAL><ISINVOICE>Yes</ISINVOICE>
 <ALTERID> 5501</ALTERID><MASTERID> 812</MASTERID>
 <ALLINVENTORYENTRIES.LIST><STOCKITEMNAME>Steel Bracket</STOCKITEMNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
  <RATE>250.00/Nos</RATE><AMOUNT>1000.00</AMOUNT><ACTUALQTY> 4 Nos</ACTUALQTY><BILLEDQTY> 4 Nos</BILLEDQTY>
  <BATCHALLOCATIONS.LIST><GODOWNNAME>Main Location</GODOWNNAME></BATCHALLOCATIONS.LIST>
  <ACCOUNTINGALLOCATIONS.LIST><LEDGERNAME>Sales GST 18%</LEDGERNAME><AMOUNT>1000.00</AMOUNT></ACCOUNTINGALLOCATIONS.LIST>
 </ALLINVENTORYENTRIES.LIST>
 <LEDGERENTRIES.LIST><LEDGERNAME>Ravi &amp; Sons</LEDGERNAME><ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><ISPARTYLEDGER>Yes</ISPARTYLEDGER><AMOUNT>-1180.00</AMOUNT>
  <BILLALLOCATIONS.LIST><NAME>GS/112</NAME><BILLTYPE>New Ref</BILLTYPE><AMOUNT>-1180.00</AMOUNT></BILLALLOCATIONS.LIST></LEDGERENTRIES.LIST>
 <LEDGERENTRIES.LIST><LEDGERNAME>Output CGST 9%</LEDGERNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>90.00</AMOUNT></LEDGERENTRIES.LIST>
 <LEDGERENTRIES.LIST><LEDGERNAME>Output SGST 9%</LEDGERNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>90.00</AMOUNT></LEDGERENTRIES.LIST>
</VOUCHER></TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"""


@tagged('post_install', '-at_install', 'zivoit_tally')
class TestTallyProtocol(BaseCase):

    def test_legacy_ledger_utf16_and_control_chars(self):
        raw = LEGACY_LEDGER_EXPORT.encode('utf-16')  # Tally may answer in UTF-16 with BOM
        rows = TX.parse_collection(raw, 'LEDGER')
        self.assertEqual(len(rows), 1)
        led = TX.normalize_ledger(rows[0])
        self.assertEqual(led['name'], 'Ravi & Sons')
        self.assertEqual(led['address'], ['12 MG Road', 'Secunderabad'])
        self.assertEqual(led['gstin'], '36AAACZ1234F1Z5')
        self.assertEqual(led['state'], 'Telangana')
        self.assertEqual(led['registration_type'], 'Unknown')  # &#4; stripped
        self.assertEqual(led['alter_id'], 1290)
        self.assertEqual(led['closing_balance'], -15340.0)

    def test_daybook_item_invoice(self):
        v = TX.normalize_voucher(TX.parse_collection(DAYBOOK_INVOICE, 'VOUCHER')[0])
        self.assertEqual(v['vch_type'], 'GST Sales')
        self.assertEqual(v['date'], date(2026, 10, 12))
        self.assertEqual(v['remote_id'], '9f1c-77')
        self.assertEqual(len(v['inventory_entries']), 1)
        ie = v['inventory_entries'][0]
        self.assertEqual((ie['qty'], ie['unit'], ie['rate'], ie['ledger'], ie['godown']),
                         (4.0, 'Nos', 250.0, 'Sales GST 18%', 'Main Location'))
        party = [le for le in v['ledger_entries'] if le['is_party']][0]
        self.assertEqual(party['amount'], -1180.0)
        self.assertEqual(party['bills'][0]['type'], 'New Ref')
        self.assertEqual(TX.voucher_balance(v), 0.0)

    def test_voucher_round_trip_balanced(self):
        vals = {'remote_id': 'ZIVO-X-MV-1', 'vch_type': 'Sales', 'date': date(2026, 10, 1),
                'number': 'INV/1', 'party': 'A <&> B', 'invoice_mode': True,
                'ledger_entries': [{'ledger': 'A <&> B', 'amount': -1180, 'is_party': True,
                                    'bills': [{'name': 'INV/1', 'type': 'New Ref',
                                               'amount': -1180}]},
                                   {'ledger': 'Output CGST 9%', 'amount': 90},
                                   {'ledger': 'Output SGST 9%', 'amount': 90}],
                'inventory_entries': [{'item': 'Steel Bracket', 'qty': 4, 'unit': 'Nos',
                                       'rate': 250, 'amount': 1000, 'ledger': 'Sales'}]}
        xml = TX.build_import_envelope('Zivo', 'Vouchers',
                                       [TX.tally_message(TX.build_voucher_xml(vals))])
        self.assertIn('A &lt;&amp;&gt; B', xml)
        v = TX.normalize_voucher(TX.parse_collection(xml, 'VOUCHER')[0])
        self.assertEqual(v['party'], 'A <&> B')
        self.assertEqual(TX.voucher_balance(v), 0.0)
        self.assertEqual(v['remote_id'], 'ZIVO-X-MV-1')

    def test_collection_request_filters(self):
        xml = TX.build_collection_request('Voucher', company='Zivo & Co', alter_id_gt=41,
                                          from_date=date(2026, 4, 1), to_date=date(2027, 3, 31))
        self.assertIn('$AlterID &gt; 41', xml)
        self.assertIn('<SVFROMDATE>20260401</SVFROMDATE>', xml)
        self.assertIn('<SVCURRENTCOMPANY>Zivo &amp; Co</SVCURRENTCOMPANY>', xml)
        self.assertIn('AllLedgerEntries', xml)

    def test_import_responses(self):
        ok = TX.parse_import_response('<RESPONSE><CREATED>2</CREATED><ALTERED>1</ALTERED>'
                                      '<LASTVCHID>55</LASTVCHID><ERRORS>0</ERRORS></RESPONSE>')
        self.assertTrue(ok['ok'])
        self.assertEqual((ok['created'], ok['altered'], ok['last_vch_id']), (2, 1, 55))
        bad = TX.parse_import_response('<RESPONSE>Unknown Request, cannot be processed</RESPONSE>')
        self.assertFalse(bad['ok'])
        st0 = TX.parse_import_response('<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>0</STATUS>'
                                       '</HEADER><BODY><DATA>Company not open</DATA></BODY></ENVELOPE>')
        self.assertFalse(st0['ok'])
        with self.assertRaises(TX.TallyProtocolError):
            TX.parse_collection('Could not connect', 'LEDGER')

    def test_amount_qty_parsing(self):
        self.assertEqual(TX.parse_amount('\u20b9 1,23,456.50 Dr'), -123456.5)
        self.assertEqual(TX.parse_amount('1180.00 Cr'), 1180.0)
        self.assertEqual(TX.parse_amount('$ 10.00 @ \u20b9 83.00/$ = \u20b9 830.00'), 830.0)
        self.assertEqual(TX.parse_qty('2.5 kg = 2500 g'), (2.5, 'kg'))
        self.assertEqual(TX.fmt_date_human(date(2026, 3, 5)), '5-Mar-2026')


@tagged('post_install', '-at_install', 'zivoit_tally')
class TestTallyAIEngine(BaseCase):

    def test_gstin_checksum(self):
        g = make_gstin('36', 'AAACZ1234F')
        self.assertTrue(matching.valid_gstin(g))
        self.assertFalse(matching.valid_gstin(g[:-1] + ('A' if g[-1] != 'A' else 'B')))
        self.assertEqual(matching.state_from_gstin(g), 'Telangana')

    def test_party_scoring(self):
        g = make_gstin('36', 'AAACZ1234F')
        self.assertEqual(matching.score_party({'name': 'X', 'gstin': g},
                                              {'name': 'Y', 'gstin': g})[0], 1.0)
        s, _w = matching.score_party({'name': 'M/s. Sri Lakshmi Traders'},
                                     {'name': 'Sri Lakshmi Traders Pvt Ltd'})
        self.assertGreaterEqual(s, 0.9)
        s2, _w = matching.score_party({'name': 'Sri Lakshmi Traders'},
                                      {'name': 'Lakshmi Steel Industries'})
        self.assertLess(s2, 0.6)
        other = make_gstin('29', 'AAACZ1234F')
        s3, why = matching.score_party({'name': 'Sri Lakshmi', 'gstin': g},
                                       {'name': 'Sri Lakshmi', 'gstin': other})
        self.assertIn('Same PAN', why)
        self.assertGreater(s3, 0.9)

    def test_tax_ledger_parsing(self):
        self.assertEqual(matching.gst_head_and_rate('Output CGST @ 9%'), ('CGST', 9.0))
        self.assertEqual(matching.gst_head_and_rate('Input IGST 18 %'), ('IGST', 18.0))
        self.assertEqual(matching.classify_ledger_by_name('HDFC Bank OD'), 'bank')

    def test_anomalies(self):
        v = {'date': date(2026, 10, 11), 'kind': 'sale', 'party': 'Ravi',
             'party_gstin': make_gstin('36', 'AAACZ1234F'), 'total': 1180, 'taxable': 1000,
             'tax_lines': [{'head': 'IGST', 'rate': 18, 'amount': 150}], 'lines': []}
        f = anomaly.analyse(v, {'company_state': 'Telangana', 'partner_history': [1000] * 10,
                                'lock_date': date(2026, 10, 31), 'weekend_check': True})
        codes = {x['code'] for x in f}
        self.assertTrue({'gst_pos', 'gst_math', 'locked', 'sunday'} <= codes, codes)
        self.assertEqual(anomaly.risk_score(f), 100)
        hist = [1000, 1100, 950, 1050, 1000, 980, 1020]
        out = anomaly.analyse(dict(v, total=90000, tax_lines=[]),
                              {'partner_history': hist, 'z_threshold': 4})
        self.assertIn('outlier', {x['code'] for x in out})

    def test_error_doctor(self):
        code, hint, arg = error_kb.diagnose("Ledger 'Output CGST 9%' does not exist!")
        self.assertEqual((code, arg), ('missing_ledger', 'Output CGST 9%'))
        self.assertIn('Auto-heal', hint)
        self.assertEqual(error_kb.diagnose('Voucher totals do not match! Dr: 1 Cr: 2')[0],
                         'unbalanced')
        self.assertEqual(error_kb.diagnose('something new')[0], 'unknown')

    def test_llm_providers_and_json(self):
        calls = []

        def fake_post(url, headers, body, timeout):
            calls.append((url, headers, body))
            if 'anthropic' in url:
                return {'content': [{'type': 'text', 'text': 'ok-claude'}]}
            if 'generativelanguage' in url:
                return {'candidates': [{'content': {'parts': [{'text': 'ok-gemini'}]}}]}
            return {'choices': [{'message': {'content': 'ok-openai'}}]}
        with patch.object(llm, '_post', fake_post):
            self.assertEqual(llm.complete('anthropic', 'k', None, 'hi', system='s'), 'ok-claude')
            self.assertEqual(calls[-1][2]['model'], 'claude-sonnet-5-5')
            self.assertEqual(calls[-1][1]['x-api-key'], 'k')
            self.assertEqual(llm.complete('openai', 'k', 'gpt-x', 'hi'), 'ok-openai')
            self.assertEqual(llm.complete('openai_compatible', '', 'llama3', 'hi',
                                          base_url='http://localhost:11434/v1'), 'ok-openai')
            self.assertTrue(calls[-1][0].startswith('http://localhost:11434/v1/chat'))
            self.assertEqual(llm.complete('gemini', 'k', 'gemini-x', 'hi'), 'ok-gemini')
        with self.assertRaises(llm.LLMError):
            llm.complete('openai', 'k', '', 'hi')
        self.assertEqual(llm.extract_json('Sure! ```json\n[{"id": 1, "x": "a}b"}]\n``` done'),
                         [{'id': 1, 'x': 'a}b'}])
        self.assertEqual(llm.extract_json('answer: {"a": {"b": 2}} trailing'), {'a': {'b': 2}})
        json.dumps(llm.DEFAULT_MODELS)
