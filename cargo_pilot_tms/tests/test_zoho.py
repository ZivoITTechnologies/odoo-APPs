# -*- coding: utf-8 -*-
import json
from unittest.mock import patch

from odoo import fields
from odoo.tests import tagged

from odoo.addons.cargo_pilot_tms.tests.common import CpCommon

REQ = 'odoo.addons.cargo_pilot_tms.models.cp_zoho.requests.request'


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeZoho:
    """A tiny Zoho: token endpoint, Books contacts/bills/invoices and Creator form/report."""

    def __init__(self):
        self.calls = []
        self.fail_bills = 0
        self.token_calls = 0

    def __call__(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append({'method': method, 'url': url, 'headers': headers or {}, 'params': params or {}, 'json': json})
        if 'accounts.zoho.in/oauth/v2/token' in url:
            self.token_calls += 1
            return FakeResponse({'access_token': 'acc-%s' % self.token_calls, 'expires_in': 3600})
        if '/books/v3/contacts' in url and method == 'GET':
            return FakeResponse({'code': 0, 'contacts': []})
        if '/books/v3/contacts' in url:
            return FakeResponse({'code': 0, 'contact': {'contact_id': 'V-100'}})
        if '/books/v3/bills' in url:
            if self.fail_bills:
                self.fail_bills -= 1
                return FakeResponse({'code': 1001, 'message': 'Bill number already exists'}, 400)
            return FakeResponse({'code': 0, 'bill': {'bill_id': 'B-900'}})
        if '/books/v3/invoices' in url:
            return FakeResponse({'code': 0, 'invoices': [{'invoice_id': 'I-55', 'invoice_number': params['invoice_number']}]})
        if '/customfields' in url:
            return FakeResponse({'code': 0, 'message': 'Custom Fields Updated Successfully'})
        if '/creator/v2.1/data/acme/tms/form/' in url:
            return FakeResponse({'code': 3000, 'data': {'ID': '4000000123'}})
        if '/creator/v2.1/data/acme/tms/report/' in url:
            return FakeResponse({'code': 3000, 'data': {'ID': '4000000123'}})
        return FakeResponse({'code': 404, 'message': 'no route'}, 404)

    def find(self, part, method=None):
        return [c for c in self.calls if part in c['url'] and (not method or c['method'] == method)]


@tagged('post_install', '-at_install', 'cargo_pilot')
class TestZoho(CpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['cp.zoho.account'].search([]).active = False
        cls.tr_a.write({'vat': '22AAAAA0000A1Z5', 'email': 'alpha@example.com'})
        cls.account = cls.env['cp.zoho.account'].create({
            'name': 'Zoho', 'client_id': '1000.CID', 'client_secret': 'csecret', 'refresh_token': '1000.refresh',
            'organization_id': '60001234', 'expense_account_id': 'ACC-FREIGHT', 'tax_id': 'TAX-5', 'tds_tax_id': 'TDS-194C',
            'push_invoice_freight': True, 'invoice_customfield_id': 'CF-77',
            'use_creator': True, 'creator_owner': 'acme', 'creator_app': 'tms'})

    def _approved_bill(self, fake):
        with patch(REQ, fake):
            trip = self._delivered_trip()
        trip.order_ids = [(0, 0, {'order_ref': 'SO-1', 'invoice_ref': 'INV-001', 'weight_kg': 10000}),
                          (0, 0, {'order_ref': 'SO-2', 'invoice_ref': 'INV-002', 'weight_kg': 4000})]
        bill = self.env['cp.freight.bill'].create({'transporter_id': self.tr_a.id, 'bill_number': 'AT/77'})
        bill.action_load_trips()
        bill.action_submit()
        with patch(REQ, fake):
            bill.action_approve()
        return trip, bill

    def test_bill_goes_to_books_not_odoo(self):
        fake = FakeZoho()
        trip, bill = self._approved_bill(fake)
        self.assertFalse(bill.move_id, 'no Odoo vendor bill when Zoho Books is the ledger')
        self.assertEqual(bill.zoho_state, 'pending')
        with patch(REQ, fake):
            self.env['cp.zoho.sync']._cron_run()
        self.assertEqual(bill.zoho_bill_id, 'B-900')
        self.assertEqual(self.tr_a.zoho_vendor_id, 'V-100')
        vendor = fake.find('/books/v3/contacts', 'POST')[0]['json']
        self.assertEqual(vendor['contact_type'], 'vendor')
        self.assertEqual(vendor['gst_no'], '22AAAAA0000A1Z5')
        posted = fake.find('/books/v3/bills')[0]
        self.assertEqual(posted['params']['organization_id'], '60001234')
        self.assertTrue(posted['headers']['Authorization'].startswith('Zoho-oauthtoken acc-'))
        body = posted['json']
        self.assertEqual(body['vendor_id'], 'V-100')
        self.assertEqual(body['bill_number'], 'AT/77')
        self.assertTrue(body['is_reverse_charge_applied'])
        self.assertEqual(body['tds_tax_id'], 'TDS-194C')
        self.assertEqual(body['line_items'][0]['account_id'], 'ACC-FREIGHT')
        self.assertEqual(body['line_items'][0]['hsn_or_sac'], '996511')
        self.assertAlmostEqual(sum(l['rate'] for l in body['line_items']), bill.approved_total)
        # freight per invoice, split by weight
        cf = fake.find('/invoice/I-55/customfields')
        self.assertEqual(len(cf), 2)
        values = sorted(c['json'][0]['value'] for c in cf)
        self.assertAlmostEqual(values[0], round(50000 * 4 / 14, 2))
        self.assertEqual(cf[0]['json'][0]['customfield_id'], 'CF-77')
        self.assertEqual(fake.token_calls, 1, 'access token reused')

    def test_retry_with_backoff_then_success(self):
        fake = FakeZoho()
        fake.fail_bills = 1
        self.account.push_invoice_freight = False
        trip, bill = self._approved_bill(fake)
        with patch(REQ, fake):
            self.env['cp.zoho.sync']._cron_run()
        job = self.env['cp.zoho.sync'].search([('kind', '=', 'bill'), ('res_id', '=', bill.id)])
        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.attempts, 1)
        self.assertIn('already exists', job.error)
        self.assertGreater(job.next_try, fields.Datetime.now())
        with patch(REQ, fake):
            self.env['cp.zoho.sync']._cron_run()
        self.assertEqual(job.attempts, 1, 'not retried before its time')
        with patch(REQ, fake):
            job.action_retry()
        self.assertEqual(job.state, 'done')
        self.assertEqual(bill.zoho_bill_id, 'B-900')

    def test_failure_after_all_retries_raises_issue(self):
        fake = FakeZoho()
        fake.fail_bills = 99
        self.account.push_invoice_freight = False
        trip, bill = self._approved_bill(fake)
        job = self.env['cp.zoho.sync'].search([('kind', '=', 'bill'), ('res_id', '=', bill.id)])
        with patch(REQ, fake):
            for _i in range(7):
                job.next_try = fields.Datetime.now()
                self.env['cp.zoho.sync']._cron_run()
        self.assertEqual(job.state, 'failed')
        self.assertTrue(self.env['cp.exception'].search([('res_model', '=', 'cp.freight.bill'), ('res_id', '=', bill.id),
                                                         ('issue_type', '=', 'integration')]))

    def test_creator_record_created_then_updated(self):
        fake = FakeZoho()
        with patch(REQ, fake):
            trip = self.env['cp.trip'].with_context(cp_no_offer=True).create({
                'lane_id': self.lane.id, 'vehicle_type_id': self.vtype.id, 'transporter_id': self.tr_a.id,
                'agreed_freight': 50000, 'order_ids': [(0, 0, {'order_ref': 'AMZ-9', 'invoice_ref': 'INV-9'})]})
            trip.write({'vehicle_number': 'CG04ZZ0001'})
            trip.action_assign()
            self.env['cp.zoho.sync']._cron_run()
            self.assertEqual(trip.zoho_creator_id, '4000000123')
            posted = fake.find('/form/Shipment')[0]['json']['data']
            self.assertEqual(posted['Shipment_No'], trip.name)
            self.assertEqual(posted['Order_Numbers'], 'AMZ-9')
            self.assertEqual(posted['Vehicle_Number'], 'CG04ZZ0001')
            trip.action_dispatch()
            self.env['cp.zoho.sync']._cron_run()
        patch_call = fake.find('/report/All_Shipments/4000000123', 'PATCH')
        self.assertEqual(len(patch_call), 1)
        self.assertEqual(patch_call[0]['json']['data']['Status'], 'In Transit')
        self.assertEqual(len(fake.find('/form/Shipment')), 1)

    def test_simulator_and_odoo_bill_option(self):
        self.account.write({'provider': 'simulator', 'skip_odoo_bill': False, 'use_creator': False})
        fake = FakeZoho()
        trip, bill = self._approved_bill(fake)
        self.assertTrue(bill.move_id, 'Odoo vendor bill kept when asked')
        bill.action_send_zoho()
        self.assertEqual(bill.zoho_bill_id, 'SIMB%s' % bill.id)
        self.assertFalse(fake.calls)
