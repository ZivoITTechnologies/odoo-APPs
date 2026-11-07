# -*- coding: utf-8 -*-
"""Each adapter against canned answers shaped like the courier's documented responses."""
import base64
import json
from urllib.parse import unquote

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common_courier import PDF, CourierCase, FakeResponse, fake_courier


@tagged('post_install', '-at_install')
class TestProviders(CourierCase):

    # ------------------------------------------------------------------
    def test_delhivery(self):
        acc = self.make_account('delhivery', api_key='dlv-secret-token', pickup_location_name='RAIPUR-WH',
                                service_code='Surface')
        parcel = self.make_parcel(acc, payment_mode='cod', cod_amount=1500)
        routes = {
            ('GET', '/c/api/pin-codes/json/'): {'delivery_codes': [{'postal_code': {
                'pin': 411005, 'cod': 'Y', 'pre_paid': 'Y', 'pickup': 'Y'}}]},
            ('GET', '/api/kinko/v1/invoice/charges/.json'): [{'total_amount': 112.5, 'gross_amount': 95.3}],
            ('POST', '/api/cmu/create.json'): {'success': True, 'upload_wbn': 'UPL123', 'packages': [
                {'waybill': '1490811234567', 'status': 'Success', 'refnum': 'SO-TEST', 'remarks': []}]},
            ('GET', '/api/p/packing_slip'): {'packages': [{'pdf_download_link': 'https://s3.example/label.pdf'}]},
            ('GET', 's3.example/label.pdf'): FakeResponse(content=PDF),
            ('GET', '/api/v1/packages/json/'): {'ShipmentData': [{'Shipment': {
                'AWB': '1490811234567', 'ExpectedDeliveryDate': '2026-10-27T23:59:59',
                'Status': {'Status': 'Dispatched', 'StatusType': 'UD', 'StatusLocation': 'Pune_Shivajinagar_D',
                           'StatusDateTime': '2026-10-26T09:12:00', 'Instructions': 'Out for delivery'},
                'Scans': [
                    {'ScanDetail': {'ScanDateTime': '2026-10-24T18:00:00', 'ScanType': 'UD', 'Scan': 'Manifested',
                                    'ScannedLocation': 'Raipur', 'Instructions': 'Shipment picked up'}},
                    {'ScanDetail': {'ScanDateTime': '2026-10-25T08:00:00', 'ScanType': 'UD', 'Scan': 'In Transit',
                                    'ScannedLocation': 'Nagpur_Hub', 'Instructions': 'Shipment received at facility'}},
                    {'ScanDetail': {'ScanDateTime': '2026-10-26T09:12:00', 'ScanType': 'UD', 'Scan': 'Dispatched',
                                    'ScannedLocation': 'Pune_Shivajinagar_D', 'Instructions': 'Out for delivery'}},
                ]}}]},
            ('POST', '/api/p/edit'): {'status': True, 'remark': 'Shipment has been cancelled'},
            ('POST', '/fm/request/new/'): {'pickup_id': 7781, 'pickup_date': '2026-10-24'},
        }
        with fake_courier(routes) as fake:
            options = acc._cp_provider().rates(parcel)
            self.assertEqual(options[0]['price'], 112.5)
            self.assertEqual(fake.last('charges')['params']['pt'], 'COD')
            self.assertEqual(fake.last('charges')['params']['cgm'], 1200)
            self.assertEqual(fake.last('charges')['headers']['Authorization'], 'Token dlv-secret-token')
            parcel.action_book(options[0])
            self.assertEqual(parcel.awb, '1490811234567')
            body = fake.last('cmu/create')['data']
            self.assertTrue(body.startswith('format=json&data='))
            payload = json.loads(unquote(body[len('format=json&data='):]))
            shipment = payload['shipments'][0]
            self.assertEqual(shipment['payment_mode'], 'COD')
            self.assertEqual(shipment['cod_amount'], 1500)
            self.assertEqual(shipment['pin'], '411005')
            self.assertEqual(shipment['weight'], 1200)
            self.assertEqual(payload['pickup_location']['name'], 'RAIPUR-WH')
            self.assertTrue(parcel.label_attachment_id, 'label downloaded at booking')
            self.assertEqual(base64.b64decode(parcel.label_attachment_id.datas), PDF)
            parcel.action_track_now()
            self.assertEqual(parcel.state, 'out_for_delivery')
            self.assertEqual(len(parcel.event_ids.filtered(lambda e: e.source == 'courier')), 3)
            self.assertEqual(str(parcel.expected_delivery), '2026-10-27')
            self.assertEqual(parcel.tracking_url, 'https://www.delhivery.com/track-v2/package/1490811234567')
            ref = acc._cp_provider().request_pickup(parcel, parcel.booked_at)
            self.assertEqual(ref, '7781')

        other = self.make_parcel(acc)
        failing = dict(routes)
        failing[('POST', '/api/cmu/create.json')] = {
            'success': False, 'packages': [{'status': 'Fail', 'remarks': ['Pincode not serviceable']}]}
        with fake_courier(failing):
            with self.assertRaisesRegex(UserError, 'Pincode not serviceable'):
                other.action_book()
        with fake_courier(routes):
            other.action_book()
            other.action_cancel()
            self.assertEqual(other.state, 'cancelled')

    def test_delhivery_status_mapping_and_webhook(self):
        acc = self.make_account('delhivery', api_key='t', pickup_location_name='WH')
        provider = acc._cp_provider()
        self.assertEqual(provider._status_of('DL', 'Delivered'), 'delivered')
        self.assertEqual(provider._status_of('DL', 'RTO'), 'returned')
        self.assertEqual(provider._status_of('RT', 'In Transit'), 'rto')
        self.assertEqual(provider._status_of('UD', 'Pending', 'Consignee refused to accept'), 'ndr')
        self.assertEqual(provider._status_of('UD', 'Manifested'), 'booked')
        parsed = provider.parse_webhook({'Shipment': {'AWB': '99', 'Status': {
            'Status': 'Delivered', 'StatusType': 'DL', 'StatusDateTime': '2026-10-26T12:00:00',
            'StatusLocation': 'Pune'}}})
        self.assertEqual(parsed['99']['status'], 'delivered')
        self.assertEqual(len(parsed['99']['events']), 1)

    # ------------------------------------------------------------------
    def test_bluedart(self):
        acc = self.make_account('bluedart', client_id='cid', client_secret='csecret', login_id='RAI001',
                                licence_key='lic-123456', customer_code='099960', service_code='A')
        parcel = self.make_parcel(acc)
        tracking_xml = """<?xml version="1.0" encoding="utf-8"?>
<ShipmentData><Shipment WaybillNo="69521234567">
  <Status>SHIPMENT DELIVERED</Status><StatusType>DL</StatusType><ExpectedDelivery>27 October 2026</ExpectedDelivery>
  <Scans>
    <ScanDetail><Scan>SHIPMENT DELIVERED</Scan><ScanCode>000</ScanCode><ScanType>DL</ScanType>
      <ScanDate>26-Oct-2026</ScanDate><ScanTime>14:05</ScanTime><ScannedLocation>PUNE</ScannedLocation></ScanDetail>
    <ScanDetail><Scan>SHIPMENT OUT FOR DELIVERY</Scan><ScanCode>002</ScanCode><ScanType>UD</ScanType>
      <ScanDate>26-Oct-2026</ScanDate><ScanTime>09:30</ScanTime><ScannedLocation>PUNE</ScannedLocation></ScanDetail>
    <ScanDetail><Scan>SHIPMENT PICKED UP</Scan><ScanCode>015</ScanCode><ScanType>PU</ScanType>
      <ScanDate>24-Oct-2026</ScanDate><ScanTime>17:10</ScanTime><ScannedLocation>RAIPUR</ScannedLocation></ScanDetail>
  </Scans></Shipment></ShipmentData>"""
        routes = {
            ('GET', '/token/v1/login'): {'JWTToken': 'jwt-abc'},
            ('POST', '/finder/v1/GetServicesforPincode'): {'GetServicesforPincodeResult': {
                'IsError': False, 'ApexInbound': 'Yes', 'eTailCODAirInbound': 'Yes', 'AreaCode': 'PNQ'}},
            ('POST', '/transit/v1/GetDomesticTransitTimeForPinCodeandProduct'): {
                'GetDomesticTransitTimeForPinCodeandProductResult': {'IsError': False,
                                                                     'ExpectedDateDelivery': '26-Oct-2026'}},
            ('POST', '/waybill/v1/GenerateWayBill'): {'GenerateWayBillResult': {
                'IsError': False, 'AWBNo': '69521234567', 'AWBPrintContent': list(PDF), 'TokenNumber': 'TK1',
                'DestinationArea': 'PNQ', 'Status': [{'StatusCode': 'Valid', 'StatusInformation': 'Waybill Generation Sucessful'}]}},
            ('GET', '/tracking/v1'): FakeResponse(content=tracking_xml.encode()),
            ('POST', '/waybill/v1/CancelWaybill'): {'CancelWaybillResult': {'IsError': False}},
        }
        with fake_courier(routes) as fake:
            options = acc._cp_provider().rates(parcel)
            self.assertTrue(options[0]['price_from_card'])
            self.assertGreater(options[0]['price'], 0)
            parcel.action_book(options[0])
            self.assertEqual(parcel.awb, '69521234567')
            self.assertEqual(base64.b64decode(parcel.label_attachment_id.datas), PDF)
            req = fake.last('GenerateWayBill')
            self.assertEqual(req['headers']['JWTToken'], 'jwt-abc')
            self.assertEqual(req['json']['Profile']['LoginID'], 'RAI001')
            self.assertEqual(req['json']['Request']['Services']['SubProductCode'], 'P')
            self.assertTrue(req['json']['Request']['Services']['PickupDate'].startswith('/Date('))
            # token is cached: only one login so far
            self.assertEqual(len([c for c in fake.calls if 'token/v1/login' in c['url']]), 1)
            parcel.action_track_now()
            self.assertEqual(parcel.state, 'delivered')
            self.assertEqual(parcel.delivered_at.hour, 14)
            self.assertEqual(fake.last('/tracking/v1')['params']['numbers'], '69521234567')

    def test_bluedart_token_refresh_on_401(self):
        acc = self.make_account('bluedart', client_id='cid', client_secret='cs', login_id='L', licence_key='K12345',
                                customer_code='C')
        acc.sudo().write({'token': 'stale', 'token_expiry': '2099-01-01 00:00:00'})
        attempts = []

        def finder(method, url, headers, params, body, data):
            attempts.append(headers.get('JWTToken'))
            if headers.get('JWTToken') == 'stale':
                return FakeResponse({'message': 'expired'}, status=401)
            return {'GetServicesforPincodeResult': {'IsError': False, 'ApexInbound': 'Yes'}}
        with fake_courier({('GET', '/token/v1/login'): {'JWTToken': 'fresh'},
                           ('POST', 'GetServicesforPincode'): finder}):
            result = acc._cp_provider().serviceable(self.make_parcel(acc))
        self.assertTrue(result['ok'])
        self.assertEqual(attempts, ['stale', 'fresh'])
        self.assertEqual(acc.sudo().token, 'fresh')

    # ------------------------------------------------------------------
    def test_dtdc(self):
        acc = self.make_account('dtdc', api_key='dtdc-key-xyz', customer_code='GL017', username='trkuser',
                                password='trkpass', service_code='B2C PRIORITY')
        parcel = self.make_parcel(acc, payment_mode='cod', cod_amount=900)
        routes = {
            ('POST', '/consignment/softdata'): {'status': 'OK', 'data': [{
                'success': True, 'reference_number': 'D80012345', 'customer_reference_number': 'SO-TEST'}]},
            ('GET', '/shippinglabel/stream'): FakeResponse(content=PDF),
            ('GET', '/api/dtdc/authenticate'): FakeResponse(content=b'"trk-token-1"'),
            ('POST', '/JSONCnTrk/getTrackDetails'): {
                'statusCode': 200, 'statusFlag': True,
                'trackHeader': {'strShipmentNo': 'D80012345', 'strStatus': 'In Transit',
                                'strExpectedDeliveryDate': '28102026'},
                'trackDetails': [
                    {'strCode': 'BKD', 'strAction': 'Booked', 'strActionDate': '24102026', 'strActionTime': '1015',
                     'strOrigin': 'RAIPUR'},
                    {'strCode': 'IT', 'strAction': 'In Transit', 'strActionDate': '25102026', 'strActionTime': '0640',
                     'strOrigin': 'NAGPUR HUB', 'sTrRemarks': 'Connected to Pune'}]},
            ('POST', '/consignment/cancel'): {'status': 'OK', 'success': True},
        }
        with fake_courier(routes) as fake:
            parcel.action_book()
            self.assertEqual(parcel.awb, 'D80012345')
            req = fake.last('softdata')
            self.assertEqual(req['headers']['api-key'], 'dtdc-key-xyz')
            c = req['json']['consignments'][0]
            self.assertEqual(c['cod_amount'], 900)
            self.assertEqual(c['destination_details']['pincode'], '411005')
            self.assertEqual(c['service_type_id'], 'B2C PRIORITY')
            self.assertTrue(parcel.label_attachment_id)
            parcel.action_track_now()
            self.assertEqual(parcel.state, 'in_transit')
            self.assertEqual(str(parcel.expected_delivery), '2026-10-28')
            self.assertEqual(fake.last('getTrackDetails')['headers']['X-Access-Token'], 'trk-token-1')

    # ------------------------------------------------------------------
    def test_shiprocket(self):
        acc = self.make_account('shiprocket', username='api@example.com', password='sr-pass-1',
                                pickup_location_name='Primary', auto_pickup=True)
        parcel = self.make_parcel(acc)
        routes = {
            ('POST', '/auth/login'): {'token': 'sr-token'},
            ('GET', '/courier/serviceability/'): {'status': 200, 'data': {
                'recommended_courier_company_id': 24, 'available_courier_companies': [
                    {'courier_company_id': 24, 'courier_name': 'Xpressbees Surface', 'rate': 74.0,
                     'estimated_delivery_days': '4', 'cod': 1, 'rating': 4.2, 'is_surface': True},
                    {'courier_company_id': 10, 'courier_name': 'Delhivery Air', 'rate': 118.0,
                     'estimated_delivery_days': '2', 'cod': 1, 'rating': 4.5},
                    {'courier_company_id': 33, 'courier_name': 'Blue Dart Air', 'rate': 162.0,
                     'estimated_delivery_days': '1', 'cod': 0, 'rating': 4.7}]}},
            ('POST', '/orders/create/adhoc'): {'order_id': 9911, 'shipment_id': 8822, 'status': 'NEW'},
            ('POST', '/courier/assign/awb'): {'awb_assign_status': 1, 'response': {'data': {
                'awb_code': '14326012345', 'courier_name': 'Delhivery Air', 'courier_company_id': 10}}},
            ('POST', '/courier/generate/pickup'): {'pickup_status': 1, 'response': {
                'pickup_scheduled_date': '2026-10-24 12:00:00', 'pickup_token_number': 'Ref-556'}},
            ('POST', '/courier/generate/label'): {'label_created': 1, 'label_url': 'https://cdn.example/l.pdf'},
            ('GET', 'cdn.example/l.pdf'): FakeResponse(content=PDF),
            ('GET', '/courier/track/awb/14326012345'): {'tracking_data': {
                'track_status': 1, 'shipment_status': 21, 'etd': '2026-10-27 18:00:00',
                'shipment_track': [{'current_status': 'UNDELIVERED', 'courier_name': 'Delhivery Air'}],
                'shipment_track_activities': [
                    {'date': '2026-10-26 18:20:00', 'activity': 'Consignee unavailable', 'location': 'Pune',
                     'sr-status': '21', 'sr-status-label': 'UNDELIVERED'},
                    {'date': '2026-10-24 15:00:00', 'activity': 'Shipment picked up', 'location': 'Raipur',
                     'sr-status': '42', 'sr-status-label': 'PICKED UP'}]}},
            ('POST', '/ndr/14326012345/action'): {'status': True},
        }
        with fake_courier(routes) as fake:
            options = acc._cp_provider().rates(parcel)
            self.assertEqual(len(options), 3)
            self.assertEqual(fake.last('serviceability')['headers']['Authorization'], 'Bearer sr-token')
            wizard = self.env['cp.courier.rate.wizard'].create({'parcel_id': parcel.id})
            wizard.action_fetch()
            chosen = wizard.line_ids.filtered(lambda l: l.courier_name == 'Delhivery Air')
            self.assertTrue(wizard.line_ids.filtered(lambda l: l.courier_name == 'Xpressbees Surface').is_cheapest)
            self.assertTrue(wizard.line_ids.filtered(lambda l: l.courier_name == 'Blue Dart Air').is_fastest)
            chosen.action_choose()
            self.assertEqual(fake.last('assign/awb')['json'], {'shipment_id': 8822, 'courier_id': '10'})
            self.assertEqual(parcel.awb, '14326012345')
            self.assertEqual(parcel.courier_name, 'Delhivery Air')
            self.assertEqual(parcel.provider_ref, '8822')
            self.assertEqual(parcel.pickup_ref, 'Ref-556')
            self.assertEqual(parcel.shipping_cost, 118.0)
            self.assertTrue(parcel.label_attachment_id)
            parcel.action_track_now()
            self.assertEqual(parcel.state, 'ndr')
            self.assertTrue(parcel.picked_up_at is False or parcel.state == 'ndr')
            self.env['cp.parcel.ndr.wizard'].create({'parcel_id': parcel.id, 'action': 'reattempt',
                                                    'note': 'Call before'}).action_confirm()
            self.assertEqual(fake.last('/ndr/')['json']['action'], 're-attempt')
            # one login for the whole flow: the token is cached
            self.assertEqual(len([c for c in fake.calls if '/auth/login' in c['url']]), 1)

    def test_shiprocket_rates_skip_non_cod_couriers(self):
        acc = self.make_account('shiprocket', username='a@b.c', password='pass1', pickup_location_name='Primary')
        parcel = self.make_parcel(acc, payment_mode='cod', cod_amount=500)
        with fake_courier({('POST', '/auth/login'): {'token': 't'},
                           ('GET', '/courier/serviceability/'): {'data': {'available_courier_companies': [
                               {'courier_company_id': 1, 'courier_name': 'A', 'rate': 50, 'cod': 1},
                               {'courier_company_id': 2, 'courier_name': 'B', 'rate': 40, 'cod': 0}]}}}):
            wizard = self.env['cp.courier.rate.wizard'].create({'parcel_id': parcel.id})
            wizard.action_fetch()
        names = wizard.line_ids.filtered(lambda l: l.account_id == acc).mapped('courier_name')
        self.assertEqual(names, ['A'])

    # ------------------------------------------------------------------
    def test_xpressbees(self):
        acc = self.make_account('xpressbees', username='xb@example.com', password='xb-pass-1',
                                pickup_location_name='Raipur WH')
        parcel = self.make_parcel(acc)
        routes = {
            ('POST', '/users/login'): {'status': True, 'data': 'xb-token'},
            ('POST', '/courier/serviceability'): {'status': True, 'data': [
                {'id': '1', 'name': 'Surface Xpressbees 0.5 K.G', 'total_charges': 61.36},
                {'id': '6', 'name': 'Air Xpressbees 0.5 K.G', 'total_charges': 98.2}]},
            ('POST', '/shipments2'): {'status': True, 'data': {
                'awb_number': '1335412345678', 'courier_name': 'Surface Xpressbees 0.5 K.G', 'shipment_id': 55,
                'label': 'https://xb.example/label.pdf'}},
            ('GET', 'xb.example/label.pdf'): FakeResponse(content=PDF),
            ('GET', '/shipments2/track/1335412345678'): {'status': True, 'data': {
                'status': 'delivered', 'history': [
                    {'status_code': 'DL', 'location': 'Pune', 'event_time': '2026-10-27 12:30', 'message': 'Delivered'},
                    {'status_code': 'OFD', 'location': 'Pune', 'event_time': '2026-10-27 08:10',
                     'message': 'Out for delivery'}]}},
        }
        with fake_courier(routes) as fake:
            options = acc._cp_provider().rates(parcel)
            self.assertEqual([o['ref'] for o in options], ['1', '6'])
            parcel.action_book(options[0])
            self.assertEqual(fake.last('/shipments2')['json']['courier_id'], '1')
            self.assertEqual(fake.last('/shipments2')['json']['package_weight'], 1200)
            self.assertEqual(parcel.awb, '1335412345678')
            self.assertTrue(parcel.label_attachment_id)
            parcel.action_track_now()
            self.assertEqual(parcel.state, 'delivered')

    # ------------------------------------------------------------------
    def test_clickpost_shree_maruti(self):
        acc = self.make_account('clickpost', username='acme', api_key='cp-key-123', courier_partner_id='69',
                                account_code='Maruti Raipur', courier_display_name='Shree Maruti Courier')
        parcel = self.make_parcel(acc)
        routes = {
            ('POST', '/v3/create-order/'): {'meta': {'status': 200, 'success': True, 'message': 'SUCCESS'},
                                            'result': {'waybill': 'SMC100200300', 'label': 'https://cp.example/x.pdf',
                                                       'reference_number': 'PCL1', 'courier_partner_id': 69,
                                                       'courier_name': 'Shree Maruti Courier'}},
            ('GET', 'cp.example/x.pdf'): FakeResponse(content=PDF),
            ('GET', '/v2/track-order/'): {'meta': {'success': True}, 'result': {'SMC100200300': {
                'latest_status': {'clickpost_status_code': 6, 'clickpost_status_description': 'OutForDelivery',
                                  'location': 'Pune', 'timestamp': '2026-10-27T09:00:00Z'},
                'scans': [{'clickpost_status_code': 4, 'clickpost_status_description': 'PickedUp',
                           'status': 'Picked up', 'location': 'Raipur', 'timestamp': '2026-10-25T10:00:00Z'},
                          {'clickpost_status_code': 6, 'clickpost_status_description': 'OutForDelivery',
                           'status': 'Out for delivery', 'location': 'Pune', 'timestamp': '2026-10-27T09:00:00Z'}]}}},
            ('GET', '/v1/cancel-order/'): {'meta': {'success': True}},
        }
        with fake_courier(routes) as fake:
            parcel.action_book()
            req = fake.last('create-order')
            self.assertEqual(req['params'], {'username': 'acme', 'key': 'cp-key-123'})
            self.assertEqual(req['json']['shipment_details']['courier_partner'], 69)
            self.assertEqual(req['json']['additional']['account_code'], 'Maruti Raipur')
            self.assertEqual(parcel.awb, 'SMC100200300')
            self.assertEqual(parcel.courier_name, 'Shree Maruti Courier')
            self.assertTrue(parcel.label_attachment_id)
            parcel.action_track_now()
            self.assertEqual(parcel.state, 'out_for_delivery')
            self.assertEqual(fake.last('track-order')['params']['cp_id'], '69')

    # ------------------------------------------------------------------
    def test_errors_are_logged_without_secrets(self):
        acc = self.make_account('delhivery', api_key='very-secret-token-42', pickup_location_name='WH')
        parcel = self.make_parcel(acc)
        with fake_courier({('POST', '/api/cmu/create.json'): FakeResponse(
                {'error': 'Invalid token very-secret-token-42'}, status=401)}):
            try:
                parcel.action_book()
                raised = ''
            except UserError as err:
                raised = str(err)
        self.assertIn('HTTP 401', raised)
        log = self.env['cp.courier.log'].search([('account_id', '=', acc.id)], limit=1)
        self.assertTrue(log)
        self.assertFalse(log.success)
        self.assertNotIn('very-secret-token-42', log.response_body)
        self.assertIn('***', log.response_body)
        self.assertEqual(parcel.state, 'draft')

    def test_unreachable_courier(self):
        import requests
        acc = self.make_account('delhivery', api_key='tok123', pickup_location_name='WH')

        def boom(*args, **kwargs):
            raise requests.exceptions.ConnectionError('down')
        with fake_courier({('GET', '/c/api/pin-codes/json/'): boom}):
            with self.assertRaisesRegex(UserError, 'could not be reached'):
                acc.action_test_connection()
        self.assertEqual(acc.state, 'error')

    def test_missing_credentials_message(self):
        acc = self.make_account('shiprocket')
        with self.assertRaisesRegex(UserError, 'API User'):
            acc._cp_provider().rates(self.make_parcel(acc))
