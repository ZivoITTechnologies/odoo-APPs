# -*- coding: utf-8 -*-
import json
from datetime import timedelta
from urllib.parse import urlencode

from odoo import fields
from odoo.tests import HttpCase, new_test_user, tagged


@tagged('post_install', '-at_install', 'zivoit_field_tracking')
class TestHttpEndpoints(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sales = new_test_user(cls.env, login='zv_http_sales', password='zv_http_sales_pw',
                                  name='HTTP Sales', tz='Asia/Kolkata',
                                  groups='zivoit_field_tracking.group_field_user')
        cls.plain = new_test_user(cls.env, login='zv_http_plain', password='zv_http_plain_pw',
                                  name='Plain User', groups='base.group_user')
        cls.device = cls.env['zivo.track.device'].create({'user_id': cls.sales.id, 'source': 'traccar'})
        cls.customer = cls.env['res.partner'].create({
            'name': 'HTTP Customer', 'is_company': True, 'user_id': cls.sales.id,
            'partner_latitude': 17.43, 'partner_longitude': 78.44})

    def _sync(self, payload, token=None):
        return self.url_open('/zivoit_tracking/api/sync', data=json.dumps(payload), headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer %s' % (token or self.device.token)})

    def _points(self):
        return self.env['zivo.track.point'].search([('device_id', '=', self.device.id)])

    def test_sync_api_offline_batch(self):
        ts = (fields.Datetime.now() - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        payload = {
            'points': [{'uid': 'h1', 'lat': 17.44, 'lon': 78.44, 'ts': ts, 'accuracy': 7, 'offline': True},
                       {'uid': 'h2', 'lat': 17.441, 'lon': 78.44, 'ts': ts, 'accuracy': 7}],
            'events': [{'uid': 'e1', 'type': 'day_start', 'ts': ts}],
            'visits': [{'uid': 'v1', 'partner_id': self.customer.id, 'check_in_ts': ts, 'lat': 17.4301, 'lon': 78.4401}],
        }
        resp = self._sync(payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(sorted(data['points']['accepted']), ['h1', 'h2'])
        self.assertEqual(data['events']['accepted'], ['e1'])
        self.assertEqual(data['visits']['accepted'], ['v1'])
        self.assertIn('interval_sec', data['config'])
        self.assertEqual(len(self._points()), 2)
        session = self._points()[0].session_id
        self.assertEqual(session.state, 'active')
        self.assertTrue(session.start_time)
        # retry of the same request (reply lost on the phone) -> acknowledged as duplicates
        data2 = self._sync(payload).json()
        self.assertEqual(sorted(data2['points']['duplicates']), ['h1', 'h2'])
        self.assertEqual(len(self._points()), 2)
        # end of day event
        self._sync({'events': [{'uid': 'e2', 'type': 'day_end', 'ts': ts}]})
        self.assertEqual(session.state, 'closed')

    def test_sync_api_rejects_bad_token_and_bad_json(self):
        self.assertEqual(self._sync({'points': []}, token='zv-wrong').status_code, 401)
        resp = self.url_open('/zivoit_tracking/api/sync', data='{not json', headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer %s' % self.device.token})
        self.assertEqual(resp.status_code, 400)
        resp = self.url_open('/zivoit_tracking/api/sync', data='[1,2]', headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer %s' % self.device.token})
        self.assertEqual(resp.status_code, 400)

    def test_osmand_query_get(self):
        """Classic OsmAnd / older Traccar Client: GET with query parameters, speed in knots."""
        params = {'id': self.device.token, 'lat': '17.4483', 'lon': '78.3915', 'timestamp': '1794551400',
                  'speed': '10', 'bearing': '45', 'altitude': '540', 'accuracy': '6', 'batt': '81'}
        resp = self.url_open('/zivoit_tracking/osmand?' + urlencode(params))
        self.assertEqual(resp.status_code, 200)
        point = self._points()
        self.assertEqual(len(point), 1)
        self.assertAlmostEqual(point.latitude, 17.4483, places=4)
        self.assertAlmostEqual(point.speed_kmh, 18.5, places=1)
        self.assertEqual(point.battery, 81)
        self.assertEqual(fields.Datetime.to_string(point.fix_time), '2026-11-13 06:30:00')
        self.assertTrue(point.captured_offline)  # the GPS time is far in the past -> buffered upload
        # Traccar re-sends the same buffered position -> stored once
        self.url_open('/zivoit_tracking/osmand?' + urlencode(params))
        self.assertEqual(len(self._points()), 1)

    def test_osmand_form_post_and_root_path(self):
        data = {'deviceid': self.device.token, 'location': '17.40,78.50', 'timestamp': '2026-11-13 06:31:00'}
        resp = self.url_open('/zivoit_tracking/osmand/', data=data)
        self.assertEqual(resp.status_code, 200)
        self.assertAlmostEqual(self._points().longitude, 78.5, places=4)

    def test_traccar_client_json(self):
        """Traccar Client v9+: JSON body, speed m/s, battery 0..1, mock flag."""
        body = {'device_id': self.device.token, 'location': {
            'timestamp': (fields.Datetime.now() - timedelta(minutes=45)).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
            'coords': {'latitude': 17.4474, 'longitude': 78.3762, 'accuracy': 9.5, 'speed': 8.0,
                       'heading': 120, 'altitude': 530},
            'is_moving': True, 'odometer': 12000, 'event': 'motionchange', 'mock': True,
            'battery': {'level': 0.47, 'is_charging': False}, 'uuid': 'trc-1'}}
        resp = self.url_open('/zivoit_tracking/osmand', data=json.dumps(body),
                             headers={'Content-Type': 'application/json'})
        self.assertEqual(resp.status_code, 200)
        point = self._points()
        self.assertEqual(point.client_uid, 'trc-1')
        self.assertAlmostEqual(point.speed_kmh, 28.8, places=1)
        self.assertAlmostEqual(point.battery, 47.0)
        self.assertTrue(point.is_mock)
        self.assertTrue(point.captured_offline)
        self.assertTrue(self.device.mock_detected)
        # batch form {"location": [...]}
        body2 = {'device_id': self.device.token, 'location': [
            dict(body['location'], uuid='trc-2', coords=dict(body['location']['coords'], latitude=17.45)),
            dict(body['location'], uuid='trc-3', coords=dict(body['location']['coords'], latitude=17.46))]}
        self.url_open('/zivoit_tracking/osmand', data=json.dumps(body2), headers={'Content-Type': 'application/json'})
        self.assertEqual(len(self._points()), 3)

    def test_osmand_unknown_device(self):
        self.assertEqual(self.url_open('/zivoit_tracking/osmand?id=zv-nope&lat=1&lon=1').status_code, 400)
        resp = self.url_open('/zivoit_tracking/osmand', data=json.dumps({'device_id': 'zv-nope', 'location': {}}),
                             headers={'Content-Type': 'application/json'})
        self.assertEqual(resp.status_code, 404)

    def test_customers_api(self):
        resp = self.url_open('/zivoit_tracking/api/customers',
                             headers={'Authorization': 'Bearer %s' % self.device.token})
        self.assertEqual(resp.status_code, 200)
        customers = resp.json()['customers']
        mine = [c for c in customers if c['id'] == self.customer.id]
        self.assertTrue(mine and mine[0]['mine'])
        self.assertEqual(self.url_open('/zivoit_tracking/api/customers').status_code, 401)

    def test_pwa_pages(self):
        # not logged in -> redirected to login
        resp = self.url_open('/zivoit_tracking/app', allow_redirects=False)
        self.assertIn(resp.status_code, (302, 303))
        # salesperson -> app with an auto-provisioned device token
        self.authenticate('zv_http_sales', 'zv_http_sales_pw')
        resp = self.url_open('/zivoit_tracking/app')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.text.startswith('<!DOCTYPE html>'))
        self.assertIn('data-boot=', resp.text)
        pwa_device = self.env['zivo.track.device'].search([('user_id', '=', self.sales.id), ('source', '=', 'pwa')])
        self.assertEqual(len(pwa_device), 1)
        self.assertIn(pwa_device.token, resp.text)
        # opening the app again re-uses the same device
        self.url_open('/zivoit_tracking/app')
        self.assertEqual(self.env['zivo.track.device'].search_count(
            [('user_id', '=', self.sales.id), ('source', '=', 'pwa')]), 1)
        # service worker & manifest
        sw = self.url_open('/zivoit_tracking/sw.js')
        self.assertEqual(sw.headers.get('Service-Worker-Allowed'), '/zivoit_tracking/')
        self.assertIn('addEventListener("sync"', sw.text)
        manifest = self.url_open('/zivoit_tracking/manifest.webmanifest').json()
        self.assertEqual(manifest['start_url'], '/zivoit_tracking/app')
        self.assertEqual(self.url_open('/zivoit_tracking/api/ping').json()['ok'], True)

    def test_pwa_refused_without_group(self):
        self.authenticate('zv_http_plain', 'zv_http_plain_pw')
        resp = self.url_open('/zivoit_tracking/app')
        self.assertIn('Tracking not enabled', resp.text)
        self.assertFalse(self.env['zivo.track.device'].search([('user_id', '=', self.plain.id)]))
