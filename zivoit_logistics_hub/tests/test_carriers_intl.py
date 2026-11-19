# -*- coding: utf-8 -*-
"""Contract tests for UPS, USPS, DHL Express, Aramex and Ekart against carrier shaped payloads."""
import base64
import io
import json

from odoo.tests import tagged

from ..providers import PROVIDERS
from ..providers.base import (Address, Item, NotSupported, Parcel, ProviderError, ShipmentRequest, image_to_pdf,
                              parse_datetime)
from .common import LogisticsCommon, carrier_server, fake_response
from .test_providers import PDF, make_request, provider


def gif_label():
    from PIL import Image
    out = io.BytesIO()
    Image.new('RGB', (1200, 800), 'white').save(out, format='GIF')
    return out.getvalue()


def us_request(service='', insure=False, to_country='US'):
    req = ShipmentRequest(
        reference='SO200-OUT1',
        pickup=Address(name='ZivoIT US', company='ZivoIT Inc', street='100 Main St', city='Austin', state_code='TX',
                       zip='78701', country_code='US', phone='5125550100', email='ops@zivoit.com'),
        delivery=Address(name='Jane Doe', street='200 Pine Ave', street2='Apt 4', city='Seattle', state_code='WA',
                         zip='98101', country_code=to_country, phone='2065550199', email='jane@example.com'),
        parcels=[Parcel(weight_kg=2.0, length_cm=30, width_cm=20, height_cm=15, declared_value=250.0)],
        items=[Item(sku='TS-1', name='T-Shirt', qty=2, unit_price=125.0, hsn='610910', weight_kg=0.4, origin_country='IN')],
        declared_value=250.0, currency='USD', service_code=service)
    if insure:
        req.extra['insure'] = True
    return req


def intl_request():
    req = make_request()
    req.delivery = Address(name='Ahmed Ali', street='Sheikh Zayed Rd 1', city='Dubai', zip='00000', country_code='AE',
                           phone='+971501234567', email='a@example.ae')
    req.items = [Item(sku='KS-01', name='Kurta Set', qty=1, unit_price=1499.0, hsn='620442', weight_kg=0.8, origin_country='IN')]
    req.currency = 'INR'
    return req


@tagged('post_install', '-at_install', 'zivo_logistics')
class TestNewCarriers(LogisticsCommon):

    def test_registry_and_ui(self):
        for code in ('ups', 'usps', 'dhl_express', 'aramex', 'ekart'):
            self.assertIn(code, PROVIDERS)
            self.assertTrue(PROVIDERS[code].setup_help)
        selection = dict(self.env['zivo.logistics.account']._selection_provider())
        self.assertEqual(selection['ups'], 'UPS')
        self.assertEqual(selection['dhl_express'], 'DHL Express')
        acc = self.env['zivo.logistics.account'].create({'name': 'UPS US', 'provider': 'ups'})
        self.assertIn('client_id', acc.credential_fields)
        self.assertIn('developer.ups.com', acc.provider_help)
        self.assertEqual(acc._tracking_link('1Z999'), 'https://www.ups.com/track?tracknum=1Z999')

    def test_helpers(self):
        self.assertEqual(parse_datetime('/Date(1762329600000+0400)/').year, 2025)
        self.assertEqual(parse_datetime('20261105143000').hour, 14)
        self.assertEqual(parse_datetime('20261105').day, 5)
        self.assertEqual(parse_datetime('05112026').month, 11)
        pdf = image_to_pdf([gif_label(), gif_label()])
        self.assertEqual(pdf[:4], b'%PDF')
        self.assertEqual(image_to_pdf(PDF), PDF)
        self.assertIsNone(image_to_pdf([]))

    # ------------------------------------------------------------------ UPS
    def test_ups_full_cycle(self):
        label = base64.b64encode(gif_label()).decode()

        def ship(method, url, kw):
            body = kw['json']['ShipmentRequest']['Shipment']
            assert body['Service']['Code'] == '03'
            assert body['Package'][0]['PackageWeight']['UnitOfMeasurement']['Code'] == 'LBS'  # US -> imperial
            assert body['Package'][0]['PackageServiceOptions']['DeclaredValue']['MonetaryValue'] == '250.00'
            return fake_response(200, {'ShipmentResponse': {'ShipmentResults': {
                'ShipmentIdentificationNumber': '1ZSHIP0001',
                'NegotiatedRateCharges': {'TotalCharge': {'CurrencyCode': 'USD', 'MonetaryValue': '11.20'}},
                'ShipmentCharges': {'TotalCharges': {'CurrencyCode': 'USD', 'MonetaryValue': '14.00'}},
                'PackageResults': {'TrackingNumber': '1Z999AA10123456784', 'ShippingLabel': {'GraphicImage': label}}}}})
        routes = [
            ('POST', r'/security/v1/oauth/token', fake_response(200, {'access_token': 'UPS-T', 'expires_in': '14399'})),
            ('POST', r'/api/rating/v2409/Shoptimeintransit', fake_response(200, {'RateResponse': {'RatedShipment': [
                {'Service': {'Code': '03'}, 'TotalCharges': {'CurrencyCode': 'USD', 'MonetaryValue': '14.00'},
                 'NegotiatedRateCharges': {'TotalCharge': {'CurrencyCode': 'USD', 'MonetaryValue': '11.20'}},
                 'TimeInTransit': {'ServiceSummary': {'EstimatedArrival': {'BusinessDaysInTransit': '4'}}}},
                {'Service': {'Code': '01'}, 'TotalCharges': {'CurrencyCode': 'USD', 'MonetaryValue': '62.10'},
                 'GuaranteedDelivery': {'BusinessDaysInTransit': '1'}},
            ]}})),
            ('POST', r'/api/shipments/v2409/ship', ship),
            ('GET', r'/api/track/v1/details/1Z999AA10123456784', fake_response(200, {'trackResponse': {'shipment': [{'package': [{
                'trackingNumber': '1Z999AA10123456784', 'currentStatus': {'description': 'Out For Delivery Today', 'code': '021'},
                'deliveryDate': [{'type': 'SDD', 'date': '20261108'}],
                'activity': [
                    {'status': {'type': 'M', 'description': 'Shipper created a label'}, 'date': '20261105', 'time': '101500',
                     'location': {'address': {'city': 'Austin', 'stateProvince': 'TX', 'countryCode': 'US'}}},
                    {'status': {'type': 'P', 'description': 'Picked up'}, 'date': '20261105', 'time': '180000'},
                    {'status': {'type': 'I', 'description': 'Out For Delivery Today'}, 'date': '20261107', 'time': '073000',
                     'location': {'address': {'city': 'Seattle', 'stateProvince': 'WA'}}},
                ]}]}]}})),
            ('DELETE', r'/void/cancel/1ZSHIP0001', fake_response(200, {'VoidShipmentResponse': {'Response': {
                'ResponseStatus': {'Code': '1', 'Description': 'Success'}}}})),
        ]
        with carrier_server(routes) as server:
            ups = provider('ups', client_id='cid', client_secret='sec', account_number='A1B2C3')
            rates = ups.get_rates(us_request())
            ground = next(r for r in rates if r.service_code == '03')
            self.assertEqual((ground.price, ground.tat_days, ground.service_name), (11.20, 4.0, 'UPS Ground'))
            self.assertTrue(ground.extra['negotiated'])
            self.assertEqual(next(r for r in rates if r.service_code == '01').price, 62.10)
            res = ups.create_shipment(us_request(service='03', insure=True))
            self.assertEqual(res.tracking_number, '1Z999AA10123456784')
            self.assertEqual(res.carrier_reference, '1ZSHIP0001')
            self.assertEqual(res.price, 11.20)
            self.assertEqual(res.label_pdf[:4], b'%PDF')
            track = ups.track(['1Z999AA10123456784'])['1Z999AA10123456784']
            self.assertEqual(track.normalized, 'out_for_delivery')
            self.assertEqual([e.normalized for e in track.events], ['booked', 'picked_up', 'out_for_delivery'])
            self.assertEqual(track.edd.day, 8)
            self.assertTrue(ups.cancel('1Z999AA10123456784', '1ZSHIP0001'))
            auth = server.called(r'oauth/token')[0]
            self.assertTrue(auth['headers']['Authorization'].startswith('Basic '))
            self.assertEqual(len(server.called(r'oauth/token')), 1)  # token cached across calls

    def test_ups_errors(self):
        with carrier_server([
            ('POST', r'oauth/token', fake_response(200, {'access_token': 'T', 'expires_in': '3600'})),
            ('POST', r'/api/rating/', fake_response(400, {'response': {'errors': [{'code': '111285', 'message': 'Invalid postal code'}]}})),
            ('GET', r'/api/track/', fake_response(404, {'response': {'errors': [{'message': 'not found'}]}})),
        ]):
            ups = provider('ups', client_id='c', client_secret='s', account_number='A1')
            with self.assertRaises(ProviderError):
                ups.get_rates(us_request())
            self.assertFalse(ups.track(['1ZX'])['1ZX'].found)
        with self.assertRaises(ProviderError):
            provider('ups').authenticate()  # missing credentials

    def test_ups_custom_version_path(self):
        with carrier_server([
            ('POST', r'oauth/token', fake_response(200, {'access_token': 'T', 'expires_in': '3600'})),
            ('POST', r'/api/rating/v9999/Shoptimeintransit', fake_response(200, {'RateResponse': {'RatedShipment': {
                'Service': {'Code': '11'}, 'TotalCharges': {'CurrencyCode': 'INR', 'MonetaryValue': '4200'}}}})),
        ]):
            ups = provider('ups', client_id='c', client_secret='s', account_number='A1', extra={'api_version': 'v9999'})
            rates = ups.get_rates(intl_request())
            self.assertEqual((rates[0].service_name, rates[0].tat_days), ('UPS Standard', 5.0))

    # ------------------------------------------------------------------ USPS
    def test_usps_full_cycle(self):
        def rate(method, url, kw):
            prices = {'USPS_GROUND_ADVANTAGE': 8.15, 'PRIORITY_MAIL': 12.40, 'PRIORITY_MAIL_EXPRESS': 41.20}
            assert kw['json']['weight'] == 4.41  # 2 kg in lb
            return fake_response(200, {'totalBasePrice': prices[kw['json']['mailClass']], 'rates': []})

        def label(method, url, kw):
            assert kw['headers']['X-Payment-Authorization-Token'] == 'PAY-TOKEN'
            assert kw['headers']['Accept'] == 'application/vnd.usps.labels+json'
            assert kw['json']['toAddress']['ZIPCode'] == '98101'
            return fake_response(200, {'labelMetadata': {'trackingNumber': '9400100000000000000000', 'postage': 8.15, 'SKU': 'DUXP0'},
                                       'labelImage': base64.b64encode(PDF).decode()})
        routes = [
            ('POST', r'/oauth2/v3/token', fake_response(200, {'access_token': 'USPS-T', 'expires_in': 28799})),
            ('POST', r'/prices/v3/base-rates/search', rate),
            ('POST', r'/payments/v3/payment-authorization', fake_response(200, {'paymentAuthorizationToken': 'PAY-TOKEN'})),
            ('POST', r'/labels/v3/label$', label),
            ('GET', r'/tracking/v3/tracking/9400100000000000000000', fake_response(200, {
                'trackingNumber': '9400100000000000000000', 'statusCategory': 'Delivered', 'status': 'Delivered, In/At Mailbox',
                'trackingEvents': [
                    {'eventType': 'Accepted at USPS Origin Facility', 'eventTimestamp': '2026-11-05T09:10:00Z', 'eventCity': 'AUSTIN', 'eventState': 'TX'},
                    {'eventType': 'Out for Delivery', 'eventTimestamp': '2026-11-08T07:00:00Z', 'eventCity': 'SEATTLE'},
                    {'eventType': 'Delivered, In/At Mailbox', 'eventTimestamp': '2026-11-08T13:20:00Z', 'eventCity': 'SEATTLE'},
                ]})),
            ('DELETE', r'/labels/v3/label/9400100000000000000000', fake_response(204, '')),
        ]
        with carrier_server(routes):
            usps = provider('usps', client_id='c', client_secret='s', customer_code='CRID1', account_number='EPS1',
                            extra={'mid': '903000001'})
            rates = usps.get_rates(us_request())
            self.assertEqual(sorted(r.price for r in rates), [8.15, 12.40, 41.20])
            self.assertTrue(all(r.currency == 'USD' for r in rates))
            res = usps.create_shipment(us_request(service='USPS_GROUND_ADVANTAGE'))
            self.assertEqual(res.tracking_number, '9400100000000000000000')
            self.assertEqual(res.label_pdf, PDF)
            tr = usps.track([res.tracking_number])[res.tracking_number]
            self.assertEqual(tr.normalized, 'delivered')
            self.assertEqual([e.normalized for e in tr.events], ['picked_up', 'out_for_delivery', 'delivered'])
            self.assertTrue(tr.delivered_at)
            self.assertTrue(usps.cancel(res.tracking_number))

    def test_usps_needs_mid_and_domestic(self):
        with carrier_server([('POST', r'/oauth2/v3/token', fake_response(200, {'access_token': 'T', 'expires_in': 3600}))]):
            usps = provider('usps', client_id='c', client_secret='s', customer_code='CRID', account_number='EPS')
            with self.assertRaises(ProviderError) as ctx:
                usps.create_shipment(us_request())
            self.assertIn('MID', ctx.exception.message)
            with self.assertRaises(ProviderError):
                usps.create_shipment(us_request(to_country='CA'))

    # ------------------------------------------------------------------ DHL Express
    def test_dhl_express_full_cycle(self):
        def ship(method, url, kw):
            body = kw['json']
            assert body['content']['isCustomsDeclarable'] is True
            assert body['content']['exportDeclaration']['lineItems'][0]['commodityCodes'][0]['value'] == '620442'
            assert body['valueAddedServices'][0]['serviceCode'] == 'II'
            assert kw['headers']['Authorization'].startswith('Basic ')
            return fake_response(201, {'shipmentTrackingNumber': '1234567890',
                                       'documents': [{'typeCode': 'label', 'imageFormat': 'PDF', 'content': base64.b64encode(PDF).decode()}],
                                       'shipmentCharges': [{'currencyType': 'BILLC', 'priceCurrency': 'INR', 'price': 3560.5}],
                                       'estimatedDeliveryDate': {'estimatedDeliveryDate': '2026-11-09'}})
        routes = [
            ('GET', r'/mydhlapi/test/rates', fake_response(200, {'products': [
                {'productName': 'EXPRESS WORLDWIDE', 'productCode': 'P',
                 'totalPrice': [{'currencyType': 'BILLC', 'priceCurrency': 'INR', 'price': 3560.5},
                                {'currencyType': 'PULCL', 'priceCurrency': 'INR', 'price': 3999}],
                 'deliveryCapabilities': {'totalTransitDays': 3, 'estimatedDeliveryDateAndTime': '2026-11-09T23:59:00'}},
                {'productName': 'EXPRESS 12:00', 'productCode': 'Y', 'totalPrice': [{'currencyType': 'BILLC', 'priceCurrency': 'INR', 'price': 5200}],
                 'deliveryCapabilities': {'totalTransitDays': 2}}]})),
            ('POST', r'/mydhlapi/test/shipments$', ship),
            ('GET', r'/shipments/1234567890/tracking', fake_response(200, {'shipments': [{'shipmentTrackingNumber': '1234567890',
                'status': 'transit', 'events': [
                    {'date': '2026-11-05', 'time': '18:00:00', 'typeCode': 'PU', 'description': 'Shipment picked up',
                     'serviceArea': [{'code': 'HYD', 'description': 'Hyderabad - India'}]},
                    {'date': '2026-11-07', 'time': '09:00:00', 'typeCode': 'NH', 'description': 'Consignee not home'},
                ]}]})),
        ]
        req = intl_request()
        req.extra['insure'] = True
        with carrier_server(routes):
            dhl = provider('dhl_express', username='key', password='secret', account_number='123456789')
            rates = dhl.get_rates(req)
            self.assertEqual([(r.service_code, r.price, r.tat_days) for r in rates], [('P', 3560.5, 3.0), ('Y', 5200.0, 2.0)])
            self.assertEqual(rates[0].service_name, 'DHL Express Worldwide')
            res = dhl.create_shipment(req)
            self.assertEqual((res.tracking_number, res.price, res.label_pdf), ('1234567890', 3560.5, PDF))
            self.assertEqual(res.edd.day, 9)
            tr = dhl.track(['1234567890'])['1234567890']
            self.assertEqual(tr.normalized, 'ndr')
            self.assertEqual(tr.ndr_reason, 'Consignee not home')
            self.assertEqual(tr.events[0].location, 'Hyderabad - India')
            self.assertTrue(dhl.cancel('1234567890'))  # no void API: local cancel

    # ------------------------------------------------------------------ Aramex
    def test_aramex_full_cycle(self):
        def rate(method, url, kw):
            body = kw['json']
            assert body['ClientInfo']['AccountPin'] == '554433'
            assert body['ShipmentDetails']['ProductGroup'] == 'EXP'
            value = {'PPX': 4100.0, 'EPX': 2650.0, 'DPX': 0}[body['ShipmentDetails']['ProductType']]
            if not value:
                return fake_response(200, {'HasErrors': True, 'Notifications': [{'Code': 'ERR01', 'Message': 'Product not available'}]})
            return fake_response(200, {'HasErrors': False, 'TotalAmount': {'CurrencyCode': 'INR', 'Value': value}})
        routes = [
            ('POST', r'CalculateRate', rate),
            ('POST', r'CreateShipments', fake_response(200, {'HasErrors': False, 'Shipments': [{
                'ID': '44712345678', 'HasErrors': False, 'ShipmentLabel': {'LabelURL': 'https://aramex.example/label.pdf',
                                                                         'LabelFileContents': list(PDF)}}]})),
            ('POST', r'TrackShipments', fake_response(200, {'HasErrors': False, 'TrackingResults': [{'Key': '44712345678', 'Value': [
                {'UpdateCode': 'SH014', 'UpdateDescription': 'Shipment picked up', 'UpdateDateTime': '/Date(1762336800000+0400)/',
                 'UpdateLocation': 'Hyderabad, India'},
                {'UpdateCode': 'SH003', 'UpdateDescription': 'Out for Delivery', 'UpdateDateTime': '/Date(1762510000000+0400)/',
                 'UpdateLocation': 'Dubai'},
                {'UpdateCode': 'SH005', 'UpdateDescription': 'Delivered', 'UpdateDateTime': '/Date(1762520000000+0400)/',
                 'UpdateLocation': 'Dubai'}]}]})),
        ]
        with carrier_server(routes):
            ax = provider('aramex', username='u', password='p', account_number='60500', license_key='554433',
                          customer_code='HYD', extra={'account_country': 'IN'})
            rates = ax.get_rates(intl_request())
            self.assertEqual(sorted((r.service_code, r.price) for r in rates), [('EPX', 2650.0), ('PPX', 4100.0)])
            res = ax.create_shipment(intl_request())
            self.assertEqual(res.tracking_number, '44712345678')
            self.assertEqual(res.label_pdf, PDF)
            tr = ax.track(['44712345678'])['44712345678']
            self.assertEqual(tr.normalized, 'delivered')
            self.assertEqual([e.normalized for e in tr.events], ['picked_up', 'out_for_delivery', 'delivered'])
            with self.assertRaises(NotSupported):
                ax.cancel('44712345678')
        with carrier_server([('POST', r'CreateShipments', fake_response(200, {'HasErrors': True, 'Shipments': [
                {'HasErrors': True, 'Notifications': [{'Code': 'ERR52', 'Message': 'Invalid consignee phone'}]}]}))]):
            with self.assertRaises(ProviderError) as ctx:
                ax.create_shipment(intl_request())
            self.assertIn('Invalid consignee phone', ctx.exception.message)

    # ------------------------------------------------------------------ Ekart
    def test_ekart_full_cycle(self):
        def create(method, url, kw):
            body = kw['json']
            assert body['payment_mode'] == 'COD' and body['cod_amount'] == 1499.0
            assert body['drop_location']['pin'] == '560038'
            assert body['weight'] == 1200
            return fake_response(200, {'status': True, 'tracking_id': 'EKL1234567890', 'vendor_order_id': 'V1', 'total_charge': 78.5})
        routes = [
            ('POST', r'/integrations/v2/auth/token/CL1', fake_response(200, {'access_token': 'EK-T', 'expires_in': 86400})),
            ('GET', r'/api/v2/serviceability/560038', fake_response(200, {'status': True, 'details': {
                'cod': True, 'forward_pickup': True, 'forward_drop': True, 'tat': 4}})),
            ('POST', r'/data/pricing/estimate', fake_response(200, {'total': 82.6, 'shippingCharge': 60, 'codCharge': 22.6})),
            ('PUT', r'/api/v1/package/create', create),
            ('POST', r'/api/v1/package/label', fake_response(200, content=PDF, content_type='application/pdf')),
            ('GET', r'/api/v1/track/EKL1234567890', fake_response(200, {'track': {'status': 'Undelivered', 'ndrStatus': 'Customer not available',
                'details': [{'status': 'Shipment Picked Up', 'ctime': '2026-11-05 18:00:00', 'location': 'Hyderabad'},
                            {'status': 'Undelivered', 'ctime': '2026-11-07 14:00:00', 'location': 'Bengaluru',
                             'desc': 'Customer not available'}]}, 'edd': '2026-11-08'})),
            ('POST', r'/api/v2/package/ndr', fake_response(200, {'status': True})),
            ('DELETE', r'/api/v1/package/cancel', fake_response(200, {'status': True})),
        ]
        with carrier_server(routes) as server:
            ek = provider('ekart', client_id='CL1', username='u', password='p')
            svc = ek.check_serviceability('500051', '560038', cod=True)
            self.assertTrue(svc.serviceable and svc.cod)
            self.assertEqual(ek.get_rates(make_request(cod=True))[0].price, 82.6)
            res = ek.create_shipment(make_request(cod=True))
            self.assertEqual(res.tracking_number, 'EKL1234567890')
            self.assertEqual(ek.get_label('EKL1234567890').pdf, PDF)
            tr = ek.track(['EKL1234567890'])['EKL1234567890']
            self.assertEqual(tr.normalized, 'ndr')
            self.assertEqual(tr.ndr_reason, 'Customer not available')
            self.assertTrue(ek.ndr_action('EKL1234567890', 'reattempt', {'date': '2026-11-09'}))
            self.assertTrue(ek.cancel('EKL1234567890'))
            self.assertEqual(server.called(r'package/cancel')[0]['params'], {'tracking_id': 'EKL1234567890'})
            self.assertNotIn('Authorization', server.called(r'/api/v1/track/')[0]['headers'])  # public tracking

    def test_path_override(self):
        with carrier_server([
            ('POST', r'/auth/token/CL1', fake_response(200, {'access_token': 'T'})),
            ('GET', r'/v9/track/EK1', fake_response(200, {'track': {'status': 'Delivered', 'details': []}})),
        ]):
            ek = provider('ekart', client_id='CL1', username='u', password='p', extra={'paths': {'track': '/v9/track/EK1'}})
            self.assertEqual(ek.track(['EK1'])['EK1'].normalized, 'delivered')

    # ------------------------------------------------------------------ Odoo integration with a USD carrier
    def test_foreign_currency_carrier_is_converted(self):
        company_currency = self.company.currency_id
        foreign = self.env.ref('base.EUR') if company_currency.name != 'EUR' else self.env.ref('base.USD')
        foreign.active = True
        self.env['res.currency.rate'].search([('currency_id', 'in', (foreign | company_currency).ids)]).unlink()
        self.env['res.currency.rate'].create({'currency_id': foreign.id, 'name': '2020-01-01', 'rate': 0.0125,
                                              'company_id': self.company.id})  # 1 foreign = 80 company currency
        ups = self.env['zivo.logistics.account'].create({
            'name': 'UPS Export', 'provider': 'ups', 'rate_source': 'api', 'client_id': 'c', 'client_secret': 's',
            'account_number': 'A1'})
        carrier = self.carrier.copy({'name': 'Export', 'zivo_account_ids': [(6, 0, ups.ids)], 'zivo_strategy': 'cheapest'})
        routes = [
            ('POST', r'oauth/token', fake_response(200, {'access_token': 'T', 'expires_in': '3600'})),
            ('POST', r'/api/rating/', fake_response(200, {'RateResponse': {'RatedShipment': [
                {'Service': {'Code': '65'}, 'TotalCharges': {'CurrencyCode': foreign.name, 'MonetaryValue': '1.00'}}]}})),
        ]
        with carrier_server(routes):
            ranked, errors = carrier._zivo_ranked_options(intl_request(), carrier._zivo_ctx(self.customer, self.company))
        self.assertFalse(errors)
        self.assertAlmostEqual(ranked[0]['price'], 80.0, places=1)  # converted to company currency
        self.assertEqual(ranked[0]['quote'].currency, foreign.name)
