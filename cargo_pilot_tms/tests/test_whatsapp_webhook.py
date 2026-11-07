# -*- coding: utf-8 -*-
import hashlib
import hmac
import json

from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install', 'cargo_pilot')
class TestWhatsappWebhook(HttpCase):

    def test_verify_and_status(self):
        self.env['cp.whatsapp.account'].search([]).active = False
        acc = self.env['cp.whatsapp.account'].create({'name': 'WA', 'provider': 'simulator',
                                                      'verify_token': 'hello-token', 'app_secret': 'app-secret-1'})
        msg = self.env['cp.whatsapp.message'].create({'account_id': acc.id, 'phone': '919000000000',
                                                      'wamid': 'wamid.ABC', 'state': 'sent'})
        ok = self.url_open('/cp/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=hello-token&hub.challenge=42')
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.text, '42')
        bad = self.url_open('/cp/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=nope&hub.challenge=42')
        self.assertEqual(bad.status_code, 403)

        body = json.dumps({'entry': [{'changes': [{'value': {'statuses': [
            {'id': 'wamid.ABC', 'status': 'delivered'}]}}]}]}).encode()
        unsigned = self.url_open('/cp/whatsapp/webhook', data=body, headers={'Content-Type': 'application/json'})
        self.assertEqual(unsigned.status_code, 401)
        sig = 'sha256=' + hmac.new(b'app-secret-1', body, hashlib.sha256).hexdigest()
        res = self.url_open('/cp/whatsapp/webhook', data=body,
                            headers={'Content-Type': 'application/json', 'X-Hub-Signature-256': sig})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()['updated'], 1)
        msg.invalidate_recordset()
        self.assertEqual(msg.state, 'delivered')
