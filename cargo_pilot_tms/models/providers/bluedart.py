# -*- coding: utf-8 -*-
import calendar
from datetime import datetime

from lxml import etree

from odoo import _, fields

from .base import CourierError, CourierProvider, guess_status, parse_dt


def _bd_date(dt):
    """Blue Dart wants .NET style dates: /Date(<epoch ms>)/"""
    return '/Date(%d)/' % (calendar.timegm(dt.timetuple()) * 1000)


class BlueDartProvider(CourierProvider):
    """Blue Dart through the DHL / Blue Dart API gateway (JWT + LoginID / LicenceKey)."""
    code = 'bluedart'
    label = 'Blue Dart'
    capabilities = {'rate': False, 'serviceability': True, 'book': True, 'label': True, 'track': True,
                    'cancel': True, 'pickup': True, 'ndr': False, 'webhook': False}
    base_urls = {'test': 'https://apigateway-sandbox.bluedart.com/in/transportation',
                 'production': 'https://apigateway.bluedart.com/in/transportation'}
    tracking_url = 'https://www.bluedart.com/web/guest/trackdartresult?trackFor=0&trackNo={awb}'
    token_hours = 20

    def _profile(self):
        acc = self.account.sudo()
        self._require('login_id', 'licence_key')
        return {'Api_type': 'S', 'LicenceKey': acc.licence_key, 'LoginID': acc.login_id}

    def _fetch_token(self):
        acc = self.account.sudo()
        self._require('client_id', 'client_secret')
        data = self._request('token', 'GET', self.base_url + '/token/v1/login', auth=False,
                             headers={'ClientID': acc.client_id, 'clientSecret': acc.client_secret})
        return data.get('JWTToken') or data.get('token')

    def _auth_headers(self):
        return {'JWTToken': self._cached_token(self._fetch_token), 'Content-Type': 'application/json'}

    @staticmethod
    def _result(data, key):
        if not isinstance(data, dict):
            return {}
        return data.get(key) or data.get(key.replace('Result', '')) or data

    @staticmethod
    def _errors(result):
        status = result.get('Status') or []
        if isinstance(status, dict):
            status = [status]
        return '; '.join(s.get('StatusInformation') or '' for s in status if isinstance(s, dict)) or str(result)[:300]

    # ------------------------------------------------------------------
    def test_connection(self):
        self.account.sudo().write({'token': False, 'token_expiry': False})
        self._auth_headers()
        return _('Connected to Blue Dart. Access token received.')

    def serviceable(self, parcel):
        data = self._request('serviceability', 'POST', self.base_url + '/finder/v1/GetServicesforPincode',
                             json_body={'pinCode': parcel.dest_pin, 'profile': self._profile()})
        result = self._result(data, 'GetServicesforPincodeResult')
        if result.get('IsError'):
            return {'ok': False, 'cod': False, 'note': result.get('ErrorMessage') or self._errors(result)}
        cod = any(str(result.get(k, '')).lower() == 'yes'
                  for k in ('eTailCODAirInbound', 'eTailCODGroundInbound', 'ApexCODInbound'))
        ok = (not parcel.payment_mode == 'cod') or cod
        return {'ok': ok, 'cod': cod, 'note': '' if ok else _('Blue Dart has no COD service to %s.', parcel.dest_pin),
                'area': result.get('AreaCode') or ''}

    def _transit_date(self, parcel):
        acc = self.account.sudo()
        now = fields.Datetime.now()
        data = self._request('transit', 'POST',
                             self.base_url + '/transit/v1/GetDomesticTransitTimeForPinCodeandProduct', json_body={
                                 'pPinCodeFrom': parcel.origin_pin, 'pPinCodeTo': parcel.dest_pin,
                                 'pProductCode': acc.service_code or 'A',
                                 'pSubProductCode': 'C' if parcel.payment_mode == 'cod' else 'P',
                                 'pPudate': _bd_date(now), 'pPickupTime': '16:00', 'profile': self._profile()})
        result = self._result(data, 'GetDomesticTransitTimeForPinCodeandProductResult')
        if result.get('IsError'):
            return None
        return parse_dt(result.get('ExpectedDateDelivery'), formats=('%d-%b-%Y', '%d-%m-%Y'))

    def rates(self, parcel):
        """Blue Dart has no public price API, so the price comes from the account's rate card."""
        service = self.serviceable(parcel)
        if not service['ok']:
            return [{'courier_name': 'Blue Dart', 'service': '', 'price': 0.0, 'days': 0, 'cod': False,
                     'error': service['note']}]
        edd = None
        try:
            edd = self._transit_date(parcel)
        except CourierError:
            edd = None
        days = max((edd.date() - fields.Date.context_today(self.account)).days, 1) if edd else 0
        price = self.account._cp_rate_card_price(parcel)
        return [{'courier_name': 'Blue Dart', 'service': self.account.service_code or 'A',
                 'price': price, 'days': days, 'cod': service['cod'], 'price_from_card': True,
                 'note': _('Price from your rate card')}]

    # ------------------------------------------------------------------
    def create_shipment(self, parcel, option=None):
        acc = self.account.sudo()
        self._require('customer_code')
        to = self.address(parcel.recipient_partner_id)
        frm = self.address(parcel.sender_partner_id)
        pickup_at = fields.Datetime.now()
        cod = parcel.payment_mode == 'cod'
        dims = [{'Length': p.length_cm, 'Breadth': p.width_cm, 'Height': p.height_cm, 'Count': p.quantity}
                for p in parcel.package_ids]
        street = to['full_street']
        request = {
            'Consignee': {
                'ConsigneeName': to['name'][:30], 'ConsigneeAttention': to['name'][:30],
                'ConsigneeAddress1': street[:30], 'ConsigneeAddress2': street[30:60],
                'ConsigneeAddress3': (to['city'] + ' ' + street[60:90]).strip()[:30],
                'ConsigneePincode': to['zip'], 'ConsigneeMobile': to['phone'], 'ConsigneeTelephone': to['phone'],
                'ConsigneeEmailID': to['email'], 'ConsigneeGSTNumber': to['vat'],
            },
            'Shipper': {
                'CustomerCode': acc.customer_code, 'CustomerName': (frm['company'] or frm['name'])[:30],
                'CustomerAddress1': frm['full_street'][:30], 'CustomerAddress2': frm['full_street'][30:60],
                'CustomerAddress3': frm['city'][:30], 'CustomerPincode': frm['zip'],
                'CustomerMobile': frm['phone'], 'CustomerTelephone': frm['phone'], 'CustomerEmailID': frm['email'],
                'CustomerGSTNumber': frm['vat'], 'OriginArea': acc.origin_area or '', 'Sender': frm['name'][:30],
                'IsToPayCustomer': False, 'VendorCode': '',
            },
            'Returnadds': {
                'ReturnAddress1': frm['full_street'][:30], 'ReturnAddress2': frm['full_street'][30:60],
                'ReturnAddress3': frm['city'][:30], 'ReturnPincode': frm['zip'], 'ReturnMobile': frm['phone'],
                'ReturnTelephone': frm['phone'], 'ReturnEmailID': frm['email'], 'ReturnContact': frm['name'][:30],
            },
            'Services': {
                'ProductCode': acc.service_code or 'A', 'SubProductCode': 'C' if cod else 'P',
                'ProductType': 1, 'PieceCount': parcel.piece_count,
                'ActualWeight': round(parcel.total_weight_kg, 2), 'DeclaredValue': parcel.declared_value,
                'CollectableAmount': parcel.cod_amount if cod else 0,
                'CreditReferenceNo': parcel.name, 'InvoiceNo': parcel.invoice_number or '',
                'Commodity': {'CommodityDetail1': (parcel.contents or 'Goods')[:30]},
                'Dimensions': dims, 'PickupDate': _bd_date(pickup_at), 'PickupTime': '1600',
                'RegisterPickup': bool(acc.auto_pickup), 'PDFOutputNotRequired': False, 'PackType': '',
                'SpecialInstruction': (parcel.note or '')[:50],
            },
        }
        if parcel.eway_bill_no:
            request['Services']['itemdtl'] = [{'ItemValue': parcel.declared_value, 'Itemquantity': parcel.piece_count,
                                               'ItemName': parcel.contents or 'Goods',
                                               'InvoiceNumber': parcel.invoice_number or parcel.name,
                                               'EWBNumber': parcel.eway_bill_no}]
        data = self._request('book', 'POST', self.base_url + '/waybill/v1/GenerateWayBill',
                             json_body={'Request': request, 'Profile': self._profile()})
        result = self._result(data, 'GenerateWayBillResult')
        if result.get('IsError') or not result.get('AWBNo'):
            raise CourierError(_('Blue Dart did not create the waybill: %s', self._errors(result)))
        return {'awb': str(result['AWBNo']), 'courier_name': 'Blue Dart', 'service': acc.service_code or 'A',
                'label': self._pdf_from(result.get('AWBPrintContent')),
                'provider_ref': result.get('TokenNumber') or '',
                'data': {'destination_area': result.get('DestinationArea'),
                         'destination_location': result.get('DestinationLocation')}}

    def cancel(self, parcel):
        data = self._request('cancel', 'POST', self.base_url + '/waybill/v1/CancelWaybill',
                             json_body={'Request': {'AWBNo': parcel.awb}, 'Profile': self._profile()})
        result = self._result(data, 'CancelWaybillResult')
        if result.get('IsError'):
            raise CourierError(_('Blue Dart did not cancel %(awb)s: %(msg)s', awb=parcel.awb,
                                 msg=self._errors(result)))
        return True

    def request_pickup(self, parcels, when):
        acc = self.account.sudo()
        self._require('customer_code')
        frm = self.address(parcels[0].sender_partner_id)
        data = self._request('pickup', 'POST', self.base_url + '/pickup/v1/RegisterPickup', json_body={
            'request': {
                'AreaCode': acc.origin_area or '', 'CustomerCode': acc.customer_code,
                'CustomerName': (frm['company'] or frm['name'])[:30], 'ContactPersonName': frm['name'][:30],
                'CustomerAddress1': frm['full_street'][:30], 'CustomerAddress2': frm['full_street'][30:60],
                'CustomerAddress3': frm['city'][:30], 'CustomerPincode': frm['zip'],
                'CustomerTelephoneNumber': frm['phone'], 'MobileTelNo': frm['phone'], 'EmailID': frm['email'],
                'NumberofPieces': sum(parcels.mapped('piece_count')),
                'WeightofShipment': round(sum(parcels.mapped('total_weight_kg')), 2),
                'ProductCode': acc.service_code or 'A', 'ShipmentPickupDate': _bd_date(when),
                'ShipmentPickupTime': when.strftime('%H%M'), 'AWBNo': [p.awb for p in parcels],
                'isToPayShipper': False, 'Remarks': '',
            },
            'profile': self._profile(),
        })
        result = self._result(data, 'RegisterPickupResult')
        if result.get('IsError') or not result.get('TokenNumber'):
            raise CourierError(_('Blue Dart did not register the pickup: %s', self._errors(result)))
        return str(result['TokenNumber'])

    # ------------------------------------------------------------------
    @staticmethod
    def _status_of(status_type, text):
        st = (status_type or '').upper()
        if st == 'DL':
            return 'delivered'
        if st == 'RT':
            return 'returned' if 'deliver' in (text or '').lower() else 'rto'
        if st == 'UD':
            return 'ndr'
        if st == 'PU':
            return 'picked_up'
        guessed = guess_status(text)
        if st == 'IT':
            return guessed if guessed in ('out_for_delivery', 'ndr', 'rto') else 'in_transit'
        return guessed

    def _parse_xml(self, content):
        result = {}
        try:
            root = etree.fromstring(content.encode() if isinstance(content, str) else content)
        except etree.XMLSyntaxError:
            raise CourierError(_('Blue Dart sent tracking data that could not be read.'))

        def txt(node, tag):
            found = node.find(tag)
            return (found.text or '').strip() if found is not None and found.text else ''

        for shp in root.iter('Shipment'):
            awb = shp.get('WaybillNo') or txt(shp, 'WaybillNo')
            if not awb:
                continue
            events = []
            for scan in shp.iter('ScanDetail'):
                when = parse_dt('%s %s' % (txt(scan, 'ScanDate'), txt(scan, 'ScanTime')),
                                formats=('%d-%b-%Y %H:%M', '%d-%b-%Y %H%M', '%d-%m-%Y %H:%M'))
                desc = txt(scan, 'Scan')
                events.append({'time': when, 'code': txt(scan, 'ScanCode') or txt(scan, 'ScanType'),
                               'description': desc, 'location': txt(scan, 'ScannedLocation'),
                               'status': self._status_of(txt(scan, 'ScanType'), desc)})
            status_text = txt(shp, 'Status')
            result[awb] = {
                'status': self._status_of(txt(shp, 'StatusType'), status_text),
                'status_text': status_text,
                'events': events,
                'expected_delivery': parse_dt(txt(shp, 'ExpectedDelivery'), formats=('%d %B %Y', '%d-%b-%Y')),
            }
        return result

    def track(self, parcels):
        acc = self.account.sudo()
        self._profile()
        awbs = [p.awb for p in parcels if p.awb]
        result = {}
        for start in range(0, len(awbs), 25):
            content = self._request('track', 'GET', self.base_url + '/tracking/v1', expect='text', params={
                'handler': 'tnt', 'action': 'custawbquery', 'loginid': acc.login_id, 'awb': 'awb',
                'numbers': ','.join(awbs[start:start + 25]), 'format': 'xml', 'lickey': acc.licence_key,
                'verno': 1, 'scan': 1})
            result.update(self._parse_xml(content))
        return result
