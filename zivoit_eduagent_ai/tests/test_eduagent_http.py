# -*- coding: utf-8 -*-
import json
import re

from odoo.tests import HttpCase, new_test_user, tagged


@tagged("post_install", "-at_install", "eduagent")
class TestEduagentHttp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.eduagent_default_provider_id = cls.env.ref("zivoit_eduagent_ai.provider_demo")
        cls.course = cls.env["slide.channel"].create({"name": "Excel to Python", "is_published": True})
        cls.env["slide.slide"].create({
            "name": "Loops", "channel_id": cls.course.id, "slide_category": "article", "is_published": True,
            "html_content": "<p>A for loop repeats code for each item.</p>"})
        cls.env["eduagent.knowledge.chunk"].index_channel(cls.course)
        cls.portal = new_test_user(cls.env, login="edu_portal", groups="base.group_portal",
                                   password="edu_portal_pw", email="edu_portal@example.com")
        cls.course._action_add_members(cls.portal.partner_id)

    def _csrf(self, path="/eduagent"):
        html = self.url_open(path).text
        return re.search(r'csrf_token:\s*"([^"]+)"', html).group(1)

    def _chat(self, payload, token):
        res = self.url_open("/eduagent/chat?csrf_token=%s" % token, data=json.dumps(payload),
                            headers={"Content-Type": "application/json"})
        self.assertEqual(res.status_code, 200, res.text[:300])
        return res.json()

    def test_admissions_page_and_chat(self):
        page = self.url_open("/eduagent")
        self.assertEqual(page.status_code, 200)
        self.assertIn("o_eduagent_chat", page.text)
        token = self._csrf()
        start = self._chat({"agent": "admissions", "message": ""}, token)
        self.assertTrue(start["token"])
        reply = self._chat({"agent": "admissions", "message": "I want to learn Python. me@example.com",
                            "token": start["token"]}, token)
        self.assertIn("capture_lead", reply["actions"])
        self.assertTrue(self.env["crm.lead"].search([("email_from", "=", "me@example.com")]))

    def test_chat_requires_csrf(self):
        self.url_open("/eduagent")
        res = self.url_open("/eduagent/chat", data=json.dumps({"agent": "admissions", "message": "x"}),
                            headers={"Content-Type": "application/json"})
        self.assertEqual(res.status_code, 400)

    def test_tutor_requires_login_then_works(self):
        token = self._csrf()
        self.assertEqual(self._chat({"agent": "tutor", "message": "hi"}, token)["error"], "login")
        self.authenticate("edu_portal", "edu_portal_pw")
        token = self._csrf("/my/eduagent")
        start = self._chat({"agent": "tutor", "message": "", "channel_id": str(self.course.id)}, token)
        res = self._chat({"agent": "tutor", "message": "what is a for loop?", "channel_id": str(self.course.id),
                          "token": start["token"]}, token)
        self.assertTrue(res["sources"])

    def test_other_learner_cannot_reuse_conversation(self):
        self.authenticate("edu_portal", "edu_portal_pw")
        token = self._csrf("/my/eduagent")
        start = self._chat({"agent": "support", "message": ""}, token)
        other = new_test_user(self.env, login="edu_other", groups="base.group_portal", password="edu_other_pw")
        self.authenticate("edu_other", "edu_other_pw")
        token2 = self._csrf("/my/eduagent")
        res = self._chat({"agent": "support", "message": "", "token": start["token"]}, token2)
        self.assertNotEqual(res["token"], start["token"])
        conv = self.env["eduagent.conversation"].search([("token", "=", res["token"])])
        self.assertEqual(conv.partner_id, other.partner_id)

    def test_learner_hub_and_portal_card(self):
        self.authenticate("edu_portal", "edu_portal_pw")
        hub = self.url_open("/my/eduagent")
        self.assertEqual(hub.status_code, 200)
        self.assertIn("Excel to Python", hub.text)
        self.assertIn("AI Learning Hub", self.url_open("/my").text)

    def test_verify_page(self):
        partner = self.portal.partner_id
        cred = self.env["eduagent.credential"].create({"partner_id": partner.id, "channel_id": self.course.id})
        self.assertIn("Authentic credential", self.url_open("/eduagent/verify/%s" % cred.code).text)
        self.assertIn("No valid credential", self.url_open("/eduagent/verify/NOPE").text)

    def test_rest_api(self):
        key = self.env["eduagent.api.key"].create({"name": "t", "scope": "chat"})
        key.action_generate()
        raw = key.new_key
        self.assertEqual(self.url_open("/eduagent/api/v1/kpis").status_code, 401)
        res = self.url_open("/eduagent/api/v1/kpis", headers={"Authorization": "Bearer " + raw})
        self.assertIn("active_learners", res.json())
        res = self.url_open("/eduagent/api/v1/learners/at-risk", headers={"Authorization": "Bearer " + raw})
        self.assertIsInstance(res.json(), list)
        res = self.url_open("/eduagent/api/v1/chat", data=json.dumps({"agent": "support", "message": "hi"}),
                            headers={"Authorization": "Bearer " + raw, "Content-Type": "application/json"})
        self.assertTrue(res.json()["conversation_token"])
        read_key = self.env["eduagent.api.key"].create({"name": "r", "scope": "read"})
        read_key.action_generate()
        res = self.url_open("/eduagent/api/v1/chat", data=json.dumps({"agent": "support", "message": "hi"}),
                            headers={"Authorization": "Bearer " + read_key.new_key,
                                     "Content-Type": "application/json"})
        self.assertEqual(res.status_code, 401)
