# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1
import base64
import hashlib
import hmac
import json

from odoo.tests import HttpCase, tagged


@tagged("post_install", "-at_install", "qbo")
class TestWebhookEndpoint(HttpCase):

    def test_webhook_endpoint(self):
        conn = self.env["qb.connection"].create({
            "name": "Hook", "client_id": "c", "client_secret": "s", "webhook_verifier_token": "vt",
            "realm_id": "4620816365", "state": "connected"})
        body = json.dumps({"eventNotifications": [{"realmId": "4620816365", "dataChangeEvent": {"entities": [
            {"name": "Invoice", "id": "130", "operation": "Create"}]}}]}).encode()
        sig = base64.b64encode(hmac.new(b"vt", body, hashlib.sha256).digest()).decode()
        resp = self.url_open("/zivoit_qbo/webhook", data=body,
                             headers={"intuit-signature": sig, "Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 200)
        job = self.env["qb.sync.job"].search([("connection_id", "=", conn.id), ("qb_id", "=", "130")])
        self.assertTrue(job)
        # bad signature still acknowledged (Intuit retries otherwise) but nothing queued
        resp = self.url_open("/zivoit_qbo/webhook", data=body.replace(b"130", b"131"),
                             headers={"intuit-signature": sig, "Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.env["qb.sync.job"].search([("qb_id", "=", "131")]))

    def test_oauth_callback_rejects_bad_state(self):
        self.authenticate("admin", "admin")
        resp = self.url_open("/zivoit_qbo/oauth/callback?code=x&state=1:2:3&realmId=9")
        self.assertEqual(resp.status_code, 400)
