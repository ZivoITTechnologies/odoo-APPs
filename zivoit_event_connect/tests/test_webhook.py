# -*- coding: utf-8 -*-
import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import patch

import requests

from odoo import fields
from odoo.tests import TransactionCase, tagged

from .common import ZivoCommon

POST = 'odoo.addons.zivoit_event_connect.models.zivo_webhook.requests.post'


class FakeResponse:
    def __init__(self, status=200, text='ok'):
        self.status_code = status
        self.text = text


@tagged('post_install', '-at_install', 'zivo_event_connect')
class TestWebhooks(ZivoCommon, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._zivo_setup()
        cls.env['zivo.webhook'].search([]).write({'active': False})
        cls.webhook = cls.env['zivo.webhook'].create({
            'name': 'WP', 'url': 'https://wp.example.com/wp-json/zivo/v1/webhook', 'secret': 's3cr3t',
            'topic_ids': [(6, 0, cls.env['zivo.webhook.topic'].search([]).ids)],
        })
        cls.Delivery = cls.env['zivo.webhook.delivery']

    def _pending(self, topic=None):
        domain = [('webhook_id', '=', self.webhook.id), ('state', '=', 'pending')]
        if topic:
            domain.append(('topic', '=', topic))
        return self.Delivery.search(domain)

    def test_event_lifecycle_enqueues(self):
        ev = self.env['event.event'].create({
            'name': 'Hook Event', 'date_begin': fields.Datetime.now() + timedelta(days=3),
            'date_end': fields.Datetime.now() + timedelta(days=4)})
        created = self._pending('event.created').filtered(lambda d: d.res_id == ev.id)
        self.assertEqual(len(created), 1)
        payload = json.loads(created.payload)
        self.assertEqual(payload['name'], 'Hook Event')
        ev.name = 'Hook Event Renamed'
        self.assertTrue(self._pending('event.updated').filtered(lambda d: d.res_id == ev.id))
        ev.website_published = True
        self.assertTrue(self._pending('event.published').filtered(lambda d: d.res_id == ev.id))
        ev.website_published = False
        self.assertTrue(self._pending('event.unpublished').filtered(lambda d: d.res_id == ev.id))
        event_id = ev.id
        ev.unlink()
        self.assertTrue(self._pending('event.deleted').filtered(lambda d: d.res_id == event_id))

    def test_irrelevant_write_not_enqueued(self):
        before = len(self._pending())
        self.ev_open.write({'note': 'internal note'})
        self.assertEqual(len(self._pending()), before)

    def test_restricted_events_filtered(self):
        before = len(self._pending())
        self.ev_invite.name = 'Board Dinner (updated)'
        self.assertEqual(len(self._pending()), before, 'restricted events are not pushed by default')
        self.webhook.include_restricted = True
        self.ev_invite.name = 'Board Dinner (updated again)'
        self.assertEqual(len(self._pending()), before + 1)

    def test_api_unpublished_events_not_pushed(self):
        self.ev_open.zivo_api_published = False
        self.assertTrue(self._pending('event.unpublished').filtered(lambda d: d.res_id == self.ev_open.id))
        before = len(self._pending())
        self.ev_open.name = 'Silent change'
        self.assertEqual(len(self._pending()), before)

    def test_delivery_signature_and_success(self):
        self.ev_open.name = 'Signed Event'
        delivery = self._pending('event.updated').filtered(lambda d: d.res_id == self.ev_open.id)
        captured = {}

        def fake_post(url, data=None, headers=None, timeout=None):
            captured.update(url=url, data=data, headers=headers)
            return FakeResponse(200)

        with patch(POST, side_effect=fake_post):
            self.Delivery._cron_process()
        self.assertEqual(delivery.state, 'done')
        self.assertEqual(delivery.attempts, 1)
        headers = captured['headers']
        expected = 'sha256=' + hmac.new(b's3cr3t', ('%s.%s' % (headers['X-Zivo-Timestamp'],
                                                              captured['data'].decode())).encode(),
                                        hashlib.sha256).hexdigest()
        self.assertEqual(headers['X-Zivo-Signature'], expected)
        self.assertEqual(headers['X-Zivo-Topic'], 'event.updated')
        body = json.loads(captured['data'])
        self.assertEqual(body['data']['name'], 'Signed Event')
        self.assertEqual(body['id'], delivery.uuid)
        self.assertEqual(self.webhook.last_status, 'HTTP 200')

    def test_retry_then_fail(self):
        self.webhook.max_attempts = 2
        self.ev_open.name = 'Retry Event'
        delivery = self._pending('event.updated').filtered(lambda d: d.res_id == self.ev_open.id)
        with patch(POST, return_value=FakeResponse(500, 'boom')):
            self.Delivery._cron_process()
        self.assertEqual(delivery.state, 'pending')
        self.assertEqual(delivery.response_code, 500)
        self.assertGreater(delivery.next_attempt, fields.Datetime.now())
        delivery.next_attempt = fields.Datetime.now() - timedelta(seconds=1)
        with patch(POST, side_effect=requests.ConnectionError('down')):
            self.Delivery._cron_process()
        self.assertEqual(delivery.state, 'failed')
        self.assertIn('down', delivery.error)
        self.assertEqual(self.webhook.failure_count, 1)
        with patch(POST, return_value=FakeResponse(204)):
            delivery.action_retry()
        self.assertEqual(delivery.state, 'done')

    def test_ping_and_headers(self):
        self.webhook.custom_headers = '{"X-Site": "main"}'
        with patch(POST, return_value=FakeResponse(200)) as mocked:
            action = self.webhook.action_send_ping()
        self.assertEqual(action['params']['type'], 'success')
        self.assertEqual(mocked.call_args.kwargs['headers']['X-Site'], 'main')
        with patch(POST, return_value=FakeResponse(404)):
            action = self.webhook.action_send_ping()
        self.assertEqual(action['params']['type'], 'danger')

    def test_registration_and_booking_topics(self):
        self.env['event.registration'].create({'event_id': self.ev_open.id, 'name': 'R', 'email': 'r@example.com'})
        self.assertTrue(self._pending('registration.created'))

    def test_topic_subscription(self):
        self.webhook.topic_ids = self.env.ref('zivoit_event_connect.topic_event_deleted')
        before = len(self._pending())
        self.ev_open.name = 'Not subscribed'
        self.assertEqual(len(self._pending()), before)
