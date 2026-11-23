# -*- coding: utf-8 -*-
import json

from odoo.tests import HttpCase, tagged

from .common import ZivoCommon

KEY = 'zec_test_key_123456789'


@tagged('post_install', '-at_install', 'zivo_event_connect')
class TestHeadlessApi(ZivoCommon, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._zivo_setup()
        cls.client = cls.env['zivo.api.client'].create({
            'name': 'Test WP', 'api_key': KEY, 'allowed_origins': 'https://wp.example.com'})

    def _get(self, path, key=KEY, headers=None):
        hdrs = dict(headers or {})
        if key:
            hdrs['X-API-Key'] = key
        return self.url_open('/api/zivo/v1' + path, headers=hdrs)

    def _names(self, response):
        return {e['name'] for e in response.json()['data']}

    def test_auth(self):
        self.assertEqual(self._get('/ping', key=None).status_code, 401)
        self.assertEqual(self._get('/ping', key='wrong').status_code, 401)
        res = self._get('/ping')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['client'], 'Test WP')
        res = self.url_open('/api/zivo/v1/ping', headers={'Authorization': 'Bearer %s' % KEY})
        self.assertEqual(res.status_code, 200)
        self.client.invalidate_recordset()
        self.assertEqual(self.client.call_count, 2)

    def test_disabled_api(self):
        self.env['ir.config_parameter'].sudo().set_param('zivoit_event_connect.api_enabled', 'False')
        self.assertEqual(self._get('/ping').status_code, 503)

    def test_list_respects_scope(self):
        res = self._get('/events?limit=100&q=ZEC')
        self.assertEqual(res.status_code, 200)
        names = self._names(res)
        self.assertIn('ZEC Open Day', names)
        self.assertIn('ZEC Payroll Clinic', names)
        self.assertNotIn('ZEC Intranet Workshop', names, 'IP restricted events are hidden by default')
        self.assertNotIn('ZEC Board Dinner', names, 'Invitation-only events are hidden by default')
        self.assertNotIn('ZEC Draft Event', names, 'Unpublished events are hidden')
        self.assertNotIn('ZEC Past Event', names, 'Past events are hidden from upcoming')
        self.assertIn('Cache-Control', res.headers)
        # past
        self.assertIn('ZEC Past Event', self._names(self._get('/events?when=past&q=ZEC')))
        # restricted included when allowed
        self.client.include_restricted = True
        names = self._names(self._get('/events?limit=100&q=ZEC'))
        self.assertIn('ZEC Intranet Workshop', names)
        self.assertIn('ZEC Board Dinner', names)
        names = self._names(self._get('/events?q=ZEC&access_mode=ip'))
        self.assertEqual(names, {'ZEC Intranet Workshop'})

    def test_hidden_from_api(self):
        self.ev_open.zivo_api_published = False
        self.assertNotIn('ZEC Open Day', self._names(self._get('/events?q=ZEC')))

    def test_filters_and_pagination(self):
        self.assertEqual(self._names(self._get('/events?q=ZEC&department=hrx')), {'ZEC Payroll Clinic'})
        self.assertEqual(self._names(self._get('/events?q=ZEC&department=itx')), {'ZEC Open Day'})
        res = self._get('/events?q=ZEC&limit=1&page=2&sort=name').json()
        self.assertEqual(res['meta']['limit'], 1)
        self.assertEqual(res['meta']['page'], 2)
        self.assertEqual(res['meta']['total'], 2)
        self.assertEqual(res['data'][0]['name'], 'ZEC Payroll Clinic')
        self.assertEqual(self._get('/events?from=not-a-date').status_code, 400)
        self.assertEqual(self._get('/events?limit=abc').status_code, 400)
        res = self._get('/events?q=ZEC&from=2000-01-01&to=2100-01-01').json()
        self.assertEqual(res['meta']['total'], 2)

    def test_client_department_scope(self):
        self.client.department_ids = self.dept_it
        self.assertEqual(self._names(self._get('/events?q=ZEC')), {'ZEC Open Day'})
        depts = self._get('/departments').json()['data']
        self.assertEqual({d['code'] for d in depts}, {'itx'})

    def test_detail(self):
        ev = self.ev_open
        res = self._get('/events/%s' % ev.id)
        self.assertEqual(res.status_code, 200)
        data = res.json()['data']
        self.assertEqual(data['id'], ev.id)
        self.assertEqual(data['departments'][0]['code'], 'itx')
        self.assertIn('description_html', data)
        self.assertIn('tickets', data)
        self.assertTrue(data['date_begin'].endswith('+00:00'))
        res = self._get('/events/%s' % ev.zivo_slug)
        self.assertEqual(res.json()['data']['id'], ev.id)
        self.assertEqual(self._get('/events/%s' % self.ev_invite.id).status_code, 404)
        self.assertEqual(self._get('/events/wrong-slug-%s' % ev.id).status_code, 404)

    def test_registration(self):
        url = '/api/zivo/v1/events/%s/registrations' % self.ev_open.id
        headers = {'X-API-Key': KEY, 'Content-Type': 'application/json'}
        res = self.url_open(url, data=json.dumps({'name': 'Api Person', 'email': 'API.Person@Example.com'}),
                            headers=headers)
        self.assertEqual(res.status_code, 201, res.text)
        reg = self.env['event.registration'].browse(res.json()['data']['id'])
        self.assertEqual(reg.email, 'api.person@example.com')
        self.assertEqual(reg.event_id, self.ev_open)
        self.assertTrue(self.env['zivo.audit.log'].search([('model', '=', 'event.registration'),
                                                           ('res_id', '=', reg.id), ('action', '=', 'api')]))
        res = self.url_open(url, data=json.dumps({'name': 'No mail'}), headers=headers)
        self.assertEqual(res.status_code, 422)
        res = self.url_open(url, data='not json', headers=headers)
        self.assertEqual(res.status_code, 400)
        self.client.allow_registration = False
        res = self.url_open(url, data=json.dumps({'name': 'X', 'email': 'x@example.com'}), headers=headers)
        self.assertEqual(res.status_code, 403)

    def test_registration_full(self):
        self.ev_open.write({'seats_limited': True, 'seats_max': 1})
        url = '/api/zivo/v1/events/%s/registrations' % self.ev_open.id
        headers = {'X-API-Key': KEY, 'Content-Type': 'application/json'}
        ok = self.url_open(url, data=json.dumps({'name': 'A', 'email': 'a@example.com'}), headers=headers)
        self.assertEqual(ok.status_code, 201)
        full = self.url_open(url, data=json.dumps({'name': 'B', 'email': 'b@example.com'}), headers=headers)
        self.assertIn(full.status_code, (409,))

    def test_cors(self):
        res = self._get('/events', headers={'Origin': 'https://wp.example.com'})
        self.assertEqual(res.headers.get('Access-Control-Allow-Origin'), 'https://wp.example.com')
        res = self._get('/events', headers={'Origin': 'https://evil.example.com'})
        self.assertIsNone(res.headers.get('Access-Control-Allow-Origin'))
        pre = self.opener.options(self.base_url() + '/api/zivo/v1/events',
                                  headers={'Origin': 'https://wp.example.com',
                                           'Access-Control-Request-Method': 'GET'}, timeout=10)
        self.assertEqual(pre.status_code, 204)
        self.assertEqual(pre.headers.get('Access-Control-Allow-Origin'), 'https://wp.example.com')

    def test_tags_and_calendar(self):
        self.assertEqual(self._get('/tags').status_code, 200)
        res = self.url_open('/api/zivo/v1/calendar.ics?q=ZEC&api_key=%s' % KEY)
        self.assertEqual(res.status_code, 200)
        self.assertIn('text/calendar', res.headers['Content-Type'])
        self.assertIn('BEGIN:VCALENDAR', res.text)
        self.assertIn('SUMMARY:ZEC Open Day', res.text)
        self.assertNotIn('ZEC Board Dinner', res.text)
