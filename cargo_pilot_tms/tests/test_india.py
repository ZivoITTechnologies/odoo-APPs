# -*- coding: utf-8 -*-
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.cargo_pilot_tms.tests.common import CpCommon

REQ = 'odoo.addons.cargo_pilot_tms.models.cp_integration_base.requests.request'


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError('not json')
        return self._payload


class Recorder:
    """Answers HTTP calls from a list of (url part, payload) rules and keeps every call."""

    def __init__(self, rules):
        self.rules = rules
        self.calls = []

    def __call__(self, method, url, headers=None, params=None, json=None, data=None, timeout=None):
        self.calls.append({'method': method, 'url': url, 'headers': headers or {}, 'json': json, 'params': params})
        for part, payload in self.rules:
            if part in url:
                if isinstance(payload, tuple):
                    return FakeResponse(payload[0], payload[1])
                return FakeResponse(payload(json) if callable(payload) else payload)
        return FakeResponse({'error': 'no rule'}, 404)


@tagged('post_install', '-at_install', 'cargo_pilot')
class TestIndia(CpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        india = env.ref('base.in')
        state = env['res.country.state'].search([('country_id', '=', india.id), ('code', '=', 'CT')], limit=1) \
            or env['res.country.state'].create({'name': 'Chhattisgarh', 'code': 'CT', 'country_id': india.id})
        cls.origin.write({'state_id': state.id, 'city': 'Raipur', 'pincode': '492001'})
        cls.tr_a.phone = '+91 98765 43210'
        # start without the demo simulators
        for model in ('cp.gsp.account', 'cp.ulip.account', 'cp.sim.provider', 'cp.whatsapp.account'):
            env[model].search([]).active = False
        cls.gsp = env['cp.gsp.account'].create({
            'name': 'Test GSP', 'gstin': '22AAAAA0000A1Z5', 'base_url': 'https://gsp.test/ewb',
            'client_code': 'CLIENT', 'user_code': 'USER', 'password': 'gsp-secret-pass'})

    def _trip(self, **vals):
        values = {'lane_id': self.lane.id, 'vehicle_type_id': self.vtype.id, 'transporter_id': self.tr_a.id,
                  'agreed_freight': 50000, 'eway_bill_no': '3910 1234 5678'}
        values.update(vals)
        return self.env['cp.trip'].with_context(cp_no_offer=True).create(values)

    # ------------------------------------------------------------------
    # E-way bill
    # ------------------------------------------------------------------
    def test_part_b_on_assign(self):
        trip = self._trip(vehicle_number='CG-04 AB 1234')
        rec = Recorder([('/updatevehicle', {'status': '1', 'data': {'vehUpdDate': '24/10/2026 10:00:00 AM',
                                                                     'validUpto': '27/10/2026 11:59:00 PM'}})])
        with patch(REQ, rec):
            trip.action_assign()
        self.assertEqual(len(rec.calls), 1)
        call = rec.calls[0]
        self.assertEqual(call['json']['ewbNo'], 391012345678)
        self.assertEqual(call['json']['vehicleNo'], 'CG04AB1234')
        self.assertEqual(call['json']['fromState'], 22)
        self.assertEqual(call['json']['fromPlace'], 'Raipur')
        self.assertEqual(call['json']['reasonCode'], '4')
        self.assertEqual(call['headers']['ClientCode'], 'CLIENT')
        self.assertTrue(trip.partb_updated_at)
        self.assertEqual(trip.eway_bill_expiry.date().isoformat(), '2026-10-27')
        self.assertEqual(trip.ewb_action_ids.action, 'part_b')
        log = self.env['cp.integration.log'].search([('service', '=', 'gsp')], limit=1)
        self.assertNotIn('gsp-secret-pass', log.request_body + log.url)

    def test_part_b_refused_raises_issue(self):
        trip = self._trip(vehicle_number='CG04AB9999')
        rec = Recorder([('/updatevehicle', {'status': '0', 'error': {'errorCodes': '312', 'message': 'Invalid EWB'}})])
        with patch(REQ, rec):
            trip.action_assign()
        self.assertEqual(trip.state, 'assigned')
        issue = self.env['cp.exception'].search([('trip_id', '=', trip.id), ('issue_type', '=', 'integration')])
        self.assertTrue(issue)
        self.assertIn('Invalid EWB', issue.detail)
        with patch(REQ, rec), self.assertRaises(UserError):
            trip.action_ewb_update_part_b()

    def test_extend_and_consolidate_with_simulator(self):
        self.gsp.provider = 'simulator'
        trip = self._trip(vehicle_number='CG04AB1111', loaded_weight_ton=10)
        trip.order_ids = [(0, 0, {'order_ref': 'O-1', 'eway_bill_no': '391000000001', 'weight_kg': 5000}),
                          (0, 0, {'order_ref': 'O-2', 'eway_bill_no': '391000000002', 'weight_kg': 5000})]
        trip.action_assign()
        self.assertEqual(len(trip.ewb_action_ids.filtered(lambda a: a.action == 'part_b')), 3)
        trip.action_dispatch()
        trip.eway_bill_expiry = fields.Datetime.now() + timedelta(hours=2)
        self.gsp.auto_extend = True
        self.env['cp.gsp.account']._cron_extend()
        self.assertGreater(trip.eway_bill_expiry, fields.Datetime.now() + timedelta(hours=12))
        number = trip._cp_ewb_consolidate()
        self.assertEqual(len(number), 12)
        self.assertEqual(trip.consolidated_ewb_no, number)

    # ------------------------------------------------------------------
    # ULIP
    # ------------------------------------------------------------------
    def test_fastag_tolls_become_positions(self):
        ulip = self.env['cp.ulip.account'].create({'name': 'ULIP', 'username': 'u', 'password': 'ulip-pass-123',
                                                   'check_vahan': False})
        trip = self._trip(vehicle_number='CG04AB2222', eway_bill_no=False, tracking_method='fastag')
        trip.action_assign()
        trip.action_dispatch()
        now_ist = fields.Datetime.now() + timedelta(hours=5, minutes=30)
        txns = [{'readerReadTime': (now_ist - timedelta(hours=h)).strftime('%Y-%m-%d %H:%M:%S.0'),
                 'tollPlazaGeocode': '%s,%s' % (20.0 - h / 10.0, 80.5), 'tollPlazaName': 'Plaza %s' % h,
                 'laneDirection': 'S', 'vehicleRegNo': 'CG04AB2222'} for h in (0.5, 0.2)]
        txns.append({'readerReadTime': 'bad', 'tollPlazaGeocode': '1,2'})
        rec = Recorder([('/user/login', {'response': {'id': 'tok-1'}, 'error': 'false'}),
                        ('/FASTAG/01', {'error': 'false', 'response': [
                            {'response': {'vehicle': {'vehltxnList': {'txn': txns}}}}]})])
        with patch(REQ, rec):
            self.assertEqual(trip._cp_fastag_sync(ulip), 2)
            self.assertEqual(trip._cp_fastag_sync(ulip), 0)
        self.assertEqual(rec.calls[1]['headers']['Authorization'], 'Bearer tok-1')
        self.assertEqual(rec.calls[1]['json'], {'vehiclenumber': 'CG04AB2222'})
        self.assertEqual(len([c for c in rec.calls if '/user/login' in c['url']]), 1, 'token reused')
        pings = trip.ping_ids.filtered(lambda p: p.source == 'fastag')
        self.assertEqual(len(pings), 2)
        self.assertEqual(set(pings.mapped('place')), {'Plaza 0.5', 'Plaza 0.2'})

    def test_vahan_expired_puc_raises_compliance(self):
        self.env['cp.ulip.account'].create({'name': 'ULIP', 'username': 'u', 'password': 'p-123456'})
        xml = ('<VehicleDetails><rc_regn_no>CG04AB3333</rc_regn_no><rc_owner_name>Ramesh</rc_owner_name>'
               '<rc_status>ACTIVE</rc_status><rc_fit_upto>31-Dec-2030</rc_fit_upto>'
               '<rc_insurance_upto>31-Dec-2030</rc_insurance_upto><rc_pucc_upto>01-Jan-2020</rc_pucc_upto>'
               '<rc_np_upto>31-Dec-2030</rc_np_upto><rc_tax_upto>31-Dec-2030</rc_tax_upto></VehicleDetails>')
        rec = Recorder([('/user/login', {'response': {'id': 'tok'}}),
                        ('/VAHAN/01', {'error': 'false', 'response': [{'response': xml}]})])
        trip = self._trip(vehicle_number='CG04AB3333', eway_bill_no=False)
        with patch(REQ, rec):
            trip.action_assign()
        check = trip.vahan_check_id
        self.assertTrue(check)
        self.assertFalse(check.ok)
        self.assertEqual(check.owner_name, 'Ramesh')
        self.assertEqual(check.puc_upto.isoformat(), '2020-01-01')
        self.assertIn('PUC', check.problems)
        self.assertTrue(self.env['cp.exception'].search([('trip_id', '=', trip.id), ('issue_type', '=', 'compliance')]))

    # ------------------------------------------------------------------
    # SIM tracking
    # ------------------------------------------------------------------
    def test_sim_tracking_consent_then_location(self):
        self.env['cp.sim.provider'].create({'name': 'Telco', 'base_url': 'https://sim.test/v1', 'api_key': 'sim-key-1234',
                                            'key_lat': 'data.lat', 'key_lon': 'data.lng', 'key_time': 'data.ts',
                                            'key_consent': 'data.status'})
        trip = self._trip(vehicle_number='CG04AB4444', eway_bill_no=False, tracking_method='sim',
                          driver_phone='98765 00000')
        state = {'consent': 'PENDING'}
        rec = Recorder([('/consent/919876500000', lambda body: {'data': {'status': state['consent']}}),
                        ('/consent', {'data': {'status': 'PENDING'}}),
                        ('/location/919876500000', {'data': {'lat': 19.5, 'lng': 79.9, 'ts': 1790000000000}})])
        with patch(REQ, rec):
            trip.action_assign()
            self.assertEqual(trip.sim_consent, 'requested')
            self.assertEqual(rec.calls[0]['json']['phone'], '919876500000')
            self.assertEqual(rec.calls[0]['headers']['Authorization'], 'Bearer sim-key-1234')
            self.assertFalse(trip._cp_sim_sync())
            state['consent'] = 'APPROVED'
            self.assertTrue(trip._cp_sim_sync())
        ping = trip.ping_ids.filtered(lambda p: p.source == 'sim')
        self.assertEqual(len(ping), 1)
        self.assertAlmostEqual(ping.latitude, 19.5)
        self.assertEqual(trip.sim_consent, 'granted')

    # ------------------------------------------------------------------
    # WhatsApp
    # ------------------------------------------------------------------
    def _whatsapp(self, provider='meta'):
        acc = self.env['cp.whatsapp.account'].create({'name': 'WA', 'provider': provider, 'phone_number_id': '1055',
                                                      'access_token': 'wa-token-abcdef'})
        acc.action_add_default_templates()
        return acc

    def test_whatsapp_offer_and_driver_link(self):
        acc = self._whatsapp()
        sent = []
        rec = Recorder([('graph.facebook.com', lambda body: sent.append(body) or
                         {'messages': [{'id': 'wamid.%s' % len(sent)}]})])
        with patch(REQ, rec):
            trip = self.env['cp.trip'].create({'lane_id': self.lane.id, 'vehicle_type_id': self.vtype.id,
                                               'transporter_id': self.tr_a.id, 'agreed_freight': 50000})
            self.assertTrue(trip.offer_ids)
            trip.write({'vehicle_number': 'CG04AB5555', 'driver_phone': '9000011111'})
            trip.action_assign()
        self.assertEqual(rec.calls[0]['url'], 'https://graph.facebook.com/v21.0/1055/messages')
        self.assertEqual(rec.calls[0]['headers']['Authorization'], 'Bearer wa-token-abcdef')
        offer = sent[0]
        self.assertEqual(offer['to'], '919876543210')
        self.assertEqual(offer['template']['name'], 'cp_booking_offer')
        values = [p['text'] for p in offer['template']['components'][0]['parameters']]
        self.assertEqual(values[0], trip.name)
        self.assertEqual(values[1], 'Test Plant to Test Depot')
        self.assertIn('/cp/offer/', values[3])
        driver = sent[1]
        self.assertEqual(driver['to'], '919000011111')
        self.assertIn('/cp/driver/', driver['template']['components'][0]['parameters'][2]['text'])
        self.assertEqual(trip.whatsapp_count, 2)
        self.assertEqual(set(trip.whatsapp_ids.mapped('state')), {'sent'})
        self.assertEqual(acc.message_count, 2)

    def test_whatsapp_failure_is_logged_not_blocking(self):
        self._whatsapp()
        rec = Recorder([('graph.facebook.com', ({'error': {'message': 'Template not approved'}}, 400))])
        with patch(REQ, rec):
            trip = self.env['cp.trip'].create({'lane_id': self.lane.id, 'vehicle_type_id': self.vtype.id,
                                               'transporter_id': self.tr_a.id, 'agreed_freight': 50000})
        message = trip.whatsapp_ids
        self.assertEqual(message.state, 'failed')
        self.assertIn('Template not approved', message.error)
        self.assertEqual(trip.acceptance_state, 'pending')

    def test_whatsapp_status_and_pod_reminder(self):
        self._whatsapp(provider='simulator')
        trip = self._delivered_trip(with_epod=False)
        Message = self.env['cp.whatsapp.message']
        trip.actual_delivery = fields.Datetime.now() - timedelta(hours=30)
        trip.actual_arrival = trip.actual_delivery
        trip._cp_refresh_pod_bucket()
        self.assertEqual(trip.pod_age_bucket, 'd1')
        self.env['cp.trip']._cron_pod_reminders()
        self.env['cp.trip']._cron_pod_reminders()
        reminder = Message.search([('trip_id', '=', trip.id), ('event', '=', 'pod_missing')])
        self.assertEqual(len(reminder), 1, 'one reminder a day')
        self.assertEqual(reminder.phone, '919876543210')
        Message.cp_apply_status(reminder.wamid, 'read')
        self.assertEqual(reminder.state, 'read')
        self.assertTrue(reminder.delivered_at)
        Message.cp_apply_status(reminder.wamid, 'delivered')
        self.assertEqual(reminder.state, 'read', 'status never goes back')

    def test_template_values_are_checked(self):
        acc = self._whatsapp()
        with self.assertRaises(UserError):
            acc.template_ids[:1].params = 'name,price'

    def test_sim_provider_test_connection(self):
        """Live test bug 13: the SIM tracking provider has a Test Connection button like the other integrations."""
        Sim = self.env['cp.sim.provider']
        sim = Sim.create({'name': 'Simulated SIM', 'provider': 'simulator'})
        result = sim.action_test()
        self.assertEqual(result['params']['type'], 'success')
        self.assertEqual(sim.state, 'ok')
        live = Sim.create({'name': 'Telco', 'base_url': 'https://sim.test/v1'})
        with self.assertRaises(UserError):
            live.action_test()  # no API key
        live.api_key = 'sim-key-1234'
        rec = Recorder([('sim.test', 'OK')])
        with patch(REQ, rec):
            live.action_test()
        self.assertEqual(live.state, 'ok')
        self.assertEqual(rec.calls[0]['method'], 'GET')
        self.assertEqual(rec.calls[0]['headers']['Authorization'], 'Bearer sim-key-1234')
        with patch(REQ, Recorder([('sim.test', ('denied', 401))])):
            with self.assertRaises(UserError):
                live.action_test()
