# -*- coding: utf-8 -*-
"""Connector contract tests: every adapter is exercised against recorded-style carrier payloads."""
import base64
import json
from datetime import timedelta

from odoo.tests import tagged

from ..providers import PROVIDERS
from ..providers.base import (Address, HttpClient, Parcel, ProviderConfig, ProviderError, ShipmentRequest,
                              json_path, mask_secrets, parse_datetime, utcnow)
from ..providers.generic import render_template
from .common import LogisticsCommon, carrier_server, fake_response

PDF = b'%PDF-1.4 fake label'


def make_request(cod=False, service=''):
    return ShipmentRequest(
        reference='SO100-OUT1',
        pickup=Address(name='ZivoIT WH', company='ZivoIT', street='Plot 12, IDA Cherlapally', city='Hyderabad',
                       state='Telangana', state_code='TS', zip='500051', phone='9849012345'),
        delivery=Address(name='Ravi Kumar', street='Flat 402, Sai Residency', street2='Near Metro', city='Bengaluru',
                         state='Karnataka', state_code='KA', zip='560038', phone='+91 98480 22338', email='r@x.com'),
        parcels=[Parcel(weight_kg=1.2, length_cm=30, width_cm=20, height_cm=10, declared_value=1499)],
        payment_mode='cod' if cod else 'prepaid', cod_amount=1499.0 if cod else 0.0, declared_value=1499.0,
        service_code=service, pickup_location_code='Primary')


def provider(code, logs=None, **cfg):
    http = HttpClient(code, log_callback=(logs.append if logs is not None else None), max_retries=0, backoff=0)
    return PROVIDERS[code](ProviderConfig(**cfg), http=http)


@tagged('post_install', '-at_install', 'zivo_logistics')
class TestFramework(LogisticsCommon):

    def test_registry_contains_all_connectors(self):
        for code in ('shiprocket', 'delhivery', 'dtdc', 'bluedart', 'fedex', 'xpressbees', 'shree_maruti', 'generic', 'demo'):
            self.assertIn(code, PROVIDERS)
        selection = dict(self.env['zivo.logistics.account']._selection_provider())
        self.assertEqual(selection['shiprocket'], 'Shiprocket')

    def test_request_weights(self):
        req = make_request()
        self.assertAlmostEqual(req.volumetric_weight, 1.2)  # 30*20*10/5000
        self.assertAlmostEqual(req.chargeable_weight, 1.2)
        req.parcels[0].length_cm = 60
        self.assertAlmostEqual(req.chargeable_weight, 2.4)
        self.assertEqual(req.delivery.clean_phone, '9848022338')

    def test_helpers(self):
        self.assertEqual(json_path({'a': [{'b': 5}]}, 'a.0.b'), 5)
        self.assertIsNone(json_path({'a': []}, 'a.0.b'))
        masked = mask_secrets({'Authorization': 'Bearer x', 'nested': {'api-key': 'k', 'name': 'ok'}})
        self.assertEqual(masked['Authorization'], '***')
        self.assertEqual(masked['nested']['api-key'], '***')
        self.assertEqual(masked['nested']['name'], 'ok')
        self.assertEqual(parse_datetime('2026-11-05T10:30:00+05:30').hour, 5)
        self.assertEqual(parse_datetime('05112026' + '1430').day, 5)
        self.assertEqual(parse_datetime('2026-11-05 10:30:00').minute, 30)
        rendered = render_template('{"w": "{{weight_kg}}", "to": "{{delivery.name}} ({{delivery.zip}})"}',
                                   {'weight_kg': 1.5, 'delivery': {'name': 'Ravi', 'zip': '560038'}})
        self.assertEqual(rendered, {'w': 1.5, 'to': 'Ravi (560038)'})

    def test_http_client_logs_and_masks(self):
        logs = []
        with carrier_server([('POST', r'/auth/login', fake_response(200, {'token': 'SECRET-TOKEN'}))]):
            sr = provider('shiprocket', logs, username='api@x.com', password='pw')
            self.assertEqual(sr.get_token(), 'SECRET-TOKEN')
        self.assertEqual(len(logs), 1)
        self.assertNotIn('pw', logs[0]['request_body'])
        self.assertNotIn('SECRET-TOKEN', logs[0]['response_body'])
        self.assertTrue(logs[0]['success'])

    def test_http_error_is_provider_error(self):
        with carrier_server([('GET', r'pin-codes', fake_response(401, {'message': 'Invalid token'}))]):
            with self.assertRaises(ProviderError) as ctx:
                provider('delhivery', api_key='bad').check_serviceability('500051', '560038')
        self.assertIn('Invalid token', ctx.exception.message)
        self.assertFalse(ctx.exception.retryable)

    def test_account_provider_writes_api_log(self):
        account = self.env['zivo.logistics.account'].create({'name': 'DLV', 'provider': 'delhivery', 'api_key': 'tok-123'})
        with carrier_server([('GET', r'pin-codes', fake_response(200, {'delivery_codes': [
                {'postal_code': {'pin': 560038, 'cod': 'Y', 'pre_paid': 'Y', 'pickup': 'Y', 'remarks': ''}}]}))]):
            res = account._check_serviceability('500051', '560038')
            self.assertTrue(res.serviceable and res.cod)
            # second call served from cache
            account._check_serviceability('500051', '560038')
        logs = self.env['zivo.logistics.api.log'].search([('account_id', '=', account.id)])
        self.assertEqual(len(logs), 1)
        self.assertIn('***', logs.request_headers)
        self.assertNotIn('tok-123', logs.request_headers)


@tagged('post_install', '-at_install', 'zivo_logistics')
class TestShiprocket(LogisticsCommon):

    def test_full_cycle(self):
        routes = [
            ('POST', r'/auth/login', fake_response(200, {'token': 'T1'})),
            ('GET', r'/courier/serviceability/', fake_response(200, {'data': {'available_courier_companies': [
                {'courier_company_id': 10, 'courier_name': 'Delhivery Surface', 'rate': 92.5, 'estimated_delivery_days': '4', 'cod': 1},
                {'courier_company_id': 24, 'courier_name': 'Xpressbees', 'rate': 85.0, 'estimated_delivery_days': '5', 'cod': 0},
            ]}})),
            ('POST', r'/orders/create/adhoc', fake_response(200, {'order_id': 9001, 'shipment_id': 7001, 'status': 'NEW'})),
            ('POST', r'/courier/assign/awb', fake_response(200, {'awb_assign_status': 1, 'response': {'data': {
                'awb_code': 'SR123456', 'courier_company_id': 10, 'courier_name': 'Delhivery Surface', 'freight_charges': 92.5}}})),
            ('POST', r'/courier/generate/label', fake_response(200, {'label_created': 1, 'label_url': 'https://x/label.pdf'})),
            ('GET', r'/courier/track/awb/SR123456', fake_response(200, {'tracking_data': {
                'track_status': 1, 'shipment_status': 7,
                'shipment_track': [{'current_status': 'Delivered', 'edd': '2026-11-08 18:00:00', 'delivered_date': '2026-11-07 14:00:00'}],
                'shipment_track_activities': [
                    {'date': '2026-11-07 14:00:00', 'status': 'DLVD', 'activity': 'Delivered', 'location': 'Bengaluru', 'sr-status-label': 'DELIVERED'},
                    {'date': '2026-11-05 10:00:00', 'status': 'PKD', 'activity': 'Picked up', 'location': 'Hyderabad', 'sr-status-label': 'PICKED UP'},
                ]}})),
            ('POST', r'/orders/cancel/shipment/awbs', fake_response(200, {'message': 'ok'})),
        ]
        with carrier_server(routes) as server:
            sr = provider('shiprocket', username='api@x.com', password='pw')
            cod_rates = sr.get_rates(make_request(cod=True))
            self.assertEqual([r.service_code for r in cod_rates], ['10'])  # Xpressbees filtered: no COD
            self.assertEqual(cod_rates[0].tat_days, 4.0)
            res = sr.create_shipment(make_request(cod=True, service='10'))
            self.assertEqual(res.tracking_number, 'SR123456')
            self.assertEqual(res.carrier_reference, '9001/7001')
            self.assertEqual(res.label_url, 'https://x/label.pdf')
            body = server.called(r'/orders/create/adhoc')[0]['json']
            self.assertEqual(body['payment_method'], 'COD')
            self.assertEqual(body['billing_phone'], '9848022338')
            tr = sr.track(['SR123456'])['SR123456']
            self.assertEqual(tr.normalized, 'delivered')
            self.assertEqual([e.normalized for e in tr.events], ['picked_up', 'delivered'])
            self.assertTrue(sr.cancel('SR123456'))
            # token cached: only one login for the whole cycle
            self.assertEqual(len(server.called(r'/auth/login')), 1)

    def test_webhook_parsing(self):
        sr = provider('shiprocket')
        results = sr.parse_webhook({'awb': '123', 'current_status': 'OUT FOR DELIVERY', 'etd': '2026-11-08 10:00:00',
                                    'scans': [{'date': '2026-11-07 08:00:00', 'activity': 'Out for delivery', 'location': 'BLR'}]})
        self.assertEqual(results[0].normalized, 'out_for_delivery')

    def test_awb_failure_raises(self):
        routes = [('POST', r'/auth/login', fake_response(200, {'token': 'T1'})),
                  ('POST', r'/orders/create/adhoc', fake_response(200, {'order_id': 1, 'shipment_id': 2})),
                  ('POST', r'/courier/assign/awb', fake_response(200, {'awb_assign_status': 0, 'message': 'Wallet balance low'}))]
        with carrier_server(routes):
            with self.assertRaises(ProviderError) as ctx:
                provider('shiprocket', username='u', password='p').create_shipment(make_request())
        self.assertIn('Wallet balance low', ctx.exception.message)


@tagged('post_install', '-at_install', 'zivo_logistics')
class TestDelhivery(LogisticsCommon):

    def test_booking_rates_tracking(self):
        routes = [
            ('GET', r'invoice/charges', fake_response(200, [{'total_amount': 74.34}])),
            ('POST', r'/api/cmu/create.json', fake_response(200, {'success': True, 'packages': [
                {'waybill': '1490801234567', 'status': 'Success', 'refnum': 'SO100-OUT1', 'remarks': []}]})),
            ('GET', r'packing_slip', fake_response(200, {'packages': [{'pdf_download_link': 'https://dl/label.pdf'}]})),
            ('GET', r'/api/v1/packages/json/', fake_response(200, {'ShipmentData': [{'Shipment': {
                'AWB': '1490801234567', 'ExpectedDeliveryDate': '2026-11-09T23:59:59',
                'Status': {'Status': 'RTO', 'StatusType': 'DL', 'StatusDateTime': '2026-11-12T11:00:00', 'Instructions': 'Returned to seller'},
                'Scans': [
                    {'ScanDetail': {'Scan': 'Manifested', 'ScanType': 'UD', 'ScanDateTime': '2026-11-05T10:00:00', 'ScannedLocation': 'HYD'}},
                    {'ScanDetail': {'Scan': 'Pending', 'ScanType': 'UD', 'ScanDateTime': '2026-11-08T19:00:00',
                                    'Instructions': 'Consignee not available', 'ScannedLocation': 'BLR'}},
                    {'ScanDetail': {'Scan': 'In Transit', 'ScanType': 'RT', 'ScanDateTime': '2026-11-10T09:00:00', 'ScannedLocation': 'BLR'}},
                ]}}]})),
            ('POST', r'/api/p/edit', fake_response(200, {'status': True, 'remark': 'Shipment has been cancelled'})),
        ]
        with carrier_server(routes) as server:
            dl = provider('delhivery', api_key='tok', pickup_location_code='ZIVO WH')
            rates = dl.get_rates(make_request(cod=True))
            self.assertEqual(len(rates), 2)
            self.assertEqual(server.called(r'invoice/charges')[0]['params']['pt'], 'COD')
            req = make_request(cod=True)
            req.pickup_location_code = ''  # falls back to the account's registered warehouse name
            res = dl.create_shipment(req)
            self.assertEqual(res.tracking_number, '1490801234567')
            sent = server.called(r'/api/cmu/create.json')[0]['data']
            self.assertTrue(sent.startswith('format=json&data='))
            payload = json.loads(sent[len('format=json&data='):])
            self.assertEqual(payload['pickup_location']['name'], 'ZIVO WH')
            self.assertEqual(payload['shipments'][0]['payment_mode'], 'COD')
            tr = dl.track(['1490801234567'])['1490801234567']
            self.assertEqual(tr.normalized, 'rto_delivered')
            self.assertEqual([e.normalized for e in tr.events][-1], 'rto_in_transit')
            self.assertTrue(dl.cancel('1490801234567'))

    def test_booking_error_message(self):
        with carrier_server([('POST', r'/api/cmu/create.json', fake_response(200, {'success': False, 'packages': [
                {'waybill': '', 'status': 'Fail', 'remarks': ['Non serviceable pincode']}]}))]):
            with self.assertRaises(ProviderError) as ctx:
                provider('delhivery', api_key='tok').create_shipment(make_request())
        self.assertIn('Non serviceable', ctx.exception.message)


@tagged('post_install', '-at_install', 'zivo_logistics')
class TestDTDCBlueDartFedexXpressbees(LogisticsCommon):

    def test_dtdc(self):
        routes = [
            ('POST', r'consignment/softdata', fake_response(200, {'status': 'OK', 'data': [
                {'success': True, 'reference_number': 'D1001234567', 'customer_reference_number': 'SO100-OUT1'}]})),
            ('GET', r'shippinglabel/stream', fake_response(200, content=PDF, content_type='application/pdf')),
            ('GET', r'dtdc/authenticate', fake_response(200, 'TRACKTOKEN123', content_type='text/plain')),
            ('POST', r'getTrackDetails', fake_response(200, {'statusCode': 200, 'trackHeader': {
                'strShipmentNo': 'D1001234567', 'strStatus': 'Delivered', 'strExpectedDeliveryDate': '09112026'},
                'trackDetails': [{'strCode': 'BKD', 'strAction': 'Booked', 'strActionDate': '05112026', 'strActionTime': '1015', 'strOrigin': 'HYDERABAD'},
                                 {'strCode': 'DLV', 'strAction': 'Delivered', 'strActionDate': '08112026', 'strActionTime': '1530', 'strOrigin': 'BANGALORE'}]})),
            ('POST', r'consignment/cancel', fake_response(200, {'success': True})),
        ]
        with carrier_server(routes) as server:
            dt = provider('dtdc', api_key='k', customer_code='GL123', username='u', password='p')
            res = dt.create_shipment(make_request(cod=True))
            self.assertEqual(res.tracking_number, 'D1001234567')
            self.assertEqual(res.label_pdf, PDF)
            cons = server.called(r'softdata')[0]['json']['consignments'][0]
            self.assertEqual(cons['customer_code'], 'GL123')
            self.assertEqual(cons['cod_amount'], 1499.0)
            self.assertEqual(server.called(r'softdata')[0]['headers']['api-key'], 'k')
            tr = dt.track(['D1001234567'])['D1001234567']
            self.assertEqual(tr.normalized, 'delivered')
            self.assertEqual(tr.events[-1].time.hour, 15)
            self.assertTrue(dt.cancel('D1001234567'))
            self.assertFalse(dt.supports('rate'))

    def test_bluedart(self):
        label = base64.b64encode(PDF).decode()
        routes = [
            ('GET', r'/token/v1/login', fake_response(200, {'JWTToken': 'JWT1'})),
            ('POST', r'GenerateWayBill', fake_response(200, {'GenerateWayBillResult': {
                'AWBNo': '50012345678', 'IsError': False, 'Status': [{'StatusCode': 'Valid', 'StatusInformation': 'Waybill Generation Sucessful'}],
                'AWBPrintContent': list(PDF), 'TokenNumber': 'TK1'}})),
            ('GET', r'/tracking/v1/shipment', fake_response(200, {'ShipmentData': {'Shipment': [{
                'WaybillNo': '50012345678', 'Status': 'SHIPMENT DELIVERED', 'StatusType': 'DL',
                'Scans': [{'ScanDetail': {'Scan': 'SHIPMENT DELIVERED', 'ScanCode': '000', 'ScanDate': '08-11-2026', 'ScanTime': '12:20', 'ScannedLocation': 'BANGALORE'}}]}]}})),
            ('POST', r'CancelWaybill', fake_response(200, {'CancelWaybillResult': {'IsError': True, 'Status': [{'StatusInformation': 'Shipment already inscanned'}]}})),
        ]
        with carrier_server(routes) as server:
            bd = provider('bluedart', client_id='ck', client_secret='cs', username='HYD123', license_key='lic', customer_code='123456')
            res = bd.create_shipment(make_request(cod=True))
            self.assertEqual(res.tracking_number, '50012345678')
            self.assertEqual(res.label_pdf, PDF)
            req = server.called('GenerateWayBill')[0]
            self.assertEqual(req['headers']['JWTToken'], 'JWT1')
            self.assertEqual(req['json']['Request']['Services']['SubProductCode'], 'C')
            self.assertEqual(req['json']['Profile']['LicenceKey'], 'lic')
            tr = bd.track(['50012345678'])['50012345678']
            self.assertEqual(tr.normalized, 'delivered')
            with self.assertRaises(ProviderError) as ctx:
                bd.cancel('50012345678')
            self.assertIn('inscanned', ctx.exception.message)
        self.assertTrue(label)

    def test_fedex(self):
        routes = [
            ('POST', r'/oauth/token', fake_response(200, {'access_token': 'FX', 'expires_in': 3599})),
            ('POST', r'/rate/v1/rates/quotes', fake_response(200, {'output': {'rateReplyDetails': [
                {'serviceType': 'PRIORITY_OVERNIGHT', 'serviceName': 'FedEx Priority Overnight',
                 'ratedShipmentDetails': [{'rateType': 'ACCOUNT', 'totalNetCharge': 640.5, 'currency': 'INR'}],
                 'operationalDetail': {'transitTime': 'ONE_DAY'}},
                {'serviceType': 'STANDARD_OVERNIGHT', 'serviceName': 'FedEx Standard Overnight',
                 'ratedShipmentDetails': [{'rateType': 'LIST', 'totalNetCharge': 520.0, 'currency': 'INR'}],
                 'operationalDetail': {'transitTime': 'TWO_DAYS'}}]}})),
            ('POST', r'/ship/v1/shipments$', fake_response(200, {'output': {'transactionShipments': [{
                'masterTrackingNumber': '794600000001', 'serviceType': 'STANDARD_OVERNIGHT',
                'pieceResponses': [{'packageDocuments': [{'encodedLabel': base64.b64encode(PDF).decode()}]}],
                'completedShipmentDetail': {'shipmentRating': {'shipmentRateDetails': [{'totalNetCharge': 520.0, 'currency': 'INR'}]}}}]}})),
            ('POST', r'/track/v1/trackingnumbers', fake_response(200, {'output': {'completeTrackResults': [{
                'trackingNumber': '794600000001', 'trackResults': [{
                    'latestStatusDetail': {'code': 'DE', 'description': 'Delivery exception',
                                           'ancillaryDetails': [{'reasonDescription': 'Customer not available'}]},
                    'scanEvents': [{'date': '2026-11-06T09:00:00+05:30', 'eventType': 'PU', 'eventDescription': 'Picked up',
                                    'scanLocation': {'city': 'HYDERABAD', 'countryCode': 'IN'}},
                                   {'date': '2026-11-07T18:00:00+05:30', 'eventType': 'DE', 'eventDescription': 'Delivery exception',
                                    'scanLocation': {'city': 'BENGALURU', 'countryCode': 'IN'}}]}]}]}})),
            ('PUT', r'/ship/v1/shipments/cancel', fake_response(200, {'output': {'cancelledShipment': True}})),
        ]
        with carrier_server(routes) as server:
            fx = provider('fedex', client_id='id', client_secret='sec', account_number='740561073')
            rates = fx.get_rates(make_request())
            self.assertEqual({r.service_code: r.tat_days for r in rates}, {'PRIORITY_OVERNIGHT': 1, 'STANDARD_OVERNIGHT': 2})
            res = fx.create_shipment(make_request(service='STANDARD_OVERNIGHT'))
            self.assertEqual(res.tracking_number, '794600000001')
            self.assertEqual(res.label_pdf, PDF)
            self.assertEqual(res.price, 520.0)
            tr = fx.track(['794600000001'])['794600000001']
            self.assertEqual(tr.normalized, 'ndr')
            self.assertEqual(tr.ndr_reason, 'Customer not available')
            self.assertTrue(fx.cancel('794600000001'))
            self.assertEqual(len(server.called('/oauth/token')), 1)
            self.assertEqual(server.called('/oauth/token')[0]['data']['grant_type'], 'client_credentials')

    def test_xpressbees(self):
        routes = [
            ('POST', r'/api/users/login', fake_response(200, {'status': True, 'data': 'XBTOKEN'})),
            ('POST', r'/api/courier/serviceability', fake_response(200, {'status': True, 'data': [
                {'id': '1', 'name': 'Surface 0.5kg', 'total_charges': 66.0}, {'id': '6', 'name': 'Air', 'total_charges': 120.0}]})),
            ('POST', r'/api/shipments2$', fake_response(200, {'status': True, 'data': {
                'awb_number': '14326911111111', 'shipment_id': 55, 'courier_name': 'Xpressbees Surface', 'label': 'https://xb/l.pdf'}})),
            ('GET', r'/api/shipments2/track/', fake_response(200, {'status': True, 'data': {'status': 'out for delivery', 'history': [
                {'status_code': 'OFD', 'message': 'Out for delivery', 'location': 'BLR', 'event_time': '2026-11-08 08:00'}]}})),
        ]
        with carrier_server(routes):
            xb = provider('xpressbees', username='e@x.com', password='p')
            self.assertEqual(len(xb.get_rates(make_request())), 2)
            res = xb.create_shipment(make_request())
            self.assertEqual(res.tracking_number, '14326911111111')
            self.assertEqual(xb.track(['14326911111111'])['14326911111111'].normalized, 'out_for_delivery')


@tagged('post_install', '-at_install', 'zivo_logistics')
class TestTemplateConnector(LogisticsCommon):

    def test_shree_maruti_preset_and_flow(self):
        account = self.env['zivo.logistics.account'].create({
            'name': 'Shree Maruti HYD', 'provider': 'shree_maruti', 'username': 'zivo', 'password': 'pw',
            'customer_code': 'SM001', 'base_url': 'https://api.example-smil.test'})
        self.assertEqual(set(account.endpoint_ids.mapped('operation')), {'auth', 'create', 'track', 'cancel'})
        self.assertEqual(account.auth_style, 'token_login')
        # paths come from the onboarding document
        paths = {'auth': '/auth/login', 'create': '/booking/create', 'track': '/tracking', 'cancel': '/booking/cancel'}
        for ep in account.endpoint_ids:
            ep.path = paths[ep.operation]
        prov = account._get_provider()
        with self.assertRaises(ProviderError):
            prov.get_rates(make_request())  # rate endpoint not configured -> clear message
        routes = [
            ('POST', r'/auth/login', fake_response(200, {'data': {'token': 'SMTOKEN'}})),
            ('POST', r'/booking/create', fake_response(200, {'status': True, 'data': {'awbNo': 'SM900001', 'labelUrl': 'https://sm/l.pdf'}})),
            ('GET', r'/tracking', fake_response(200, {'data': {'currentStatus': 'Out For Delivery', 'scans': [
                {'dateTime': '2026-11-07 09:10', 'status': 'Out For Delivery', 'remarks': 'With FE', 'location': 'BLR'}]}})),
        ]
        with carrier_server(routes) as server:
            res = prov.create_shipment(make_request(cod=True))
            self.assertEqual(res.tracking_number, 'SM900001')
            body = server.called('/booking/create')[0]['json']
            self.assertEqual(body['clientCode'], 'SM001')
            self.assertEqual(body['codAmount'], 1499.0)
            self.assertEqual(body['consignee']['pincode'], '560038')
            self.assertEqual(server.called('/booking/create')[0]['headers']['Authorization'], 'Bearer SMTOKEN')
            tr = prov.track(['SM900001'])['SM900001']
            self.assertEqual(tr.normalized, 'out_for_delivery')
            self.assertEqual(server.called('/tracking')[0]['params'], {'awbNo': 'SM900001'})
        account.invalidate_recordset()
        self.assertEqual(account.token, 'SMTOKEN')
        self.assertTrue(account.token_expiry > utcnow() + timedelta(hours=11))

    def test_generic_api_key_and_success_flag(self):
        account = self.env['zivo.logistics.account'].create({
            'name': 'Regional Courier', 'provider': 'generic', 'api_key': 'K1', 'auth_style': 'api_key_header',
            'auth_header_name': 'x-api-key', 'base_url': 'https://courier.test/v1',
            'endpoint_ids': [(0, 0, {'operation': 'create', 'method': 'POST', 'path': '/shipments',
                                     'body_template': '{"ref": "{{reference}}", "grams": "{{weight_grams}}", "to": "{{delivery.zip}}"}',
                                     'success_path': 'ok', 'error_path': 'error.text', 'awb_path': 'result.awb'})],
        })
        prov = account._get_provider()
        with carrier_server([('POST', r'/shipments', fake_response(200, {'ok': False, 'error': {'text': 'Pincode blocked'}}))]):
            with self.assertRaises(ProviderError) as ctx:
                prov.create_shipment(make_request())
        self.assertIn('Pincode blocked', ctx.exception.message)
        with carrier_server([('POST', r'/shipments', fake_response(200, {'ok': True, 'result': {'awb': 'RC1'}}))]) as server:
            self.assertEqual(prov.create_shipment(make_request()).tracking_number, 'RC1')
            call = server.called('/shipments')[0]
            self.assertEqual(call['headers']['x-api-key'], 'K1')
            self.assertEqual(call['json'], {'ref': 'SO100-OUT1', 'grams': 1200, 'to': '560038'})
