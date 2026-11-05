import json
from datetime import date
from unittest.mock import patch

import requests

from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import EpcCommon

POST = "odoo.addons.zivoit_epc_ai.models.epc_ai_service.requests.post"


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


def openai_answer(text):
    return FakeResponse({"choices": [{"message": {"content": text}}]})


def anthropic_answer(text):
    return FakeResponse({"content": [{"type": "text", "text": text}]})


@tagged("post_install", "-at_install", "zivoit_epc")
class TestAiService(EpcCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # the fixture forces the fallback; these tests exercise the provider path
        cls.env = cls.env(context=dict(cls.env.context, epc_ai_force_fallback=False))
        cls.service = cls.env["epc.ai.service"]
        cls.icp = cls.env["ir.config_parameter"].sudo()

    def _configure(self, provider, key="sk-test", model="", base_url=""):
        self.icp.set_param("zivoit_epc_ai.provider", provider)
        self.icp.set_param("zivoit_epc_ai.api_key", key)
        self.icp.set_param("zivoit_epc_ai.model", model)
        self.icp.set_param("zivoit_epc_ai.base_url", base_url)

    def test_disabled_uses_fallback_and_logs(self):
        self._configure("none")
        self.assertFalse(self.service.is_enabled())
        with patch(POST) as post:
            result = self.service.ask("unit", "sys", "prompt", lambda: "fallback text")
            post.assert_not_called()
        self.assertEqual(result, {"result": "fallback text", "source": "fallback", "error": ""})
        log = self.env["epc.ai.log"].search([("feature", "=", "unit")], limit=1)
        self.assertTrue(log.used_fallback and log.success)

    def test_key_required(self):
        self._configure("openai", key="")
        self.assertFalse(self.service.is_enabled())
        self._configure("openai_compatible", key="", base_url="http://localhost:11434/v1", model="llama3")
        self.assertTrue(self.service.is_enabled())

    def test_openai_call(self):
        self._configure("openai")
        with patch(POST, return_value=openai_answer("Hello")) as post:
            result = self.service.ask("unit", "sys", "prompt", lambda: "fb")
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["result"], "Hello")
        url = post.call_args[0][0]
        body = post.call_args[1]["json"]
        headers = post.call_args[1]["headers"]
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(body["model"], "gpt-4o-mini")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(headers["Authorization"], "Bearer sk-test")

    def test_anthropic_call(self):
        self._configure("anthropic", model="claude-test")
        with patch(POST, return_value=anthropic_answer('```json\n{"a": 1}\n```')) as post:
            result = self.service.ask("unit", "sys", "prompt", lambda: {}, expect_json=True)
        self.assertEqual(result["result"], {"a": 1})
        self.assertEqual(post.call_args[0][0], "https://api.anthropic.com/v1/messages")
        self.assertEqual(post.call_args[1]["headers"]["x-api-key"], "sk-test")
        self.assertEqual(post.call_args[1]["json"]["system"], "sys")

    @mute_logger("odoo.addons.zivoit_epc_ai.models.epc_ai_service")
    def test_errors_fall_back(self):
        self._configure("openai")
        with patch(POST, return_value=FakeResponse({"error": "bad key"}, status=401)):
            result = self.service.ask("unit_err", "sys", "p", lambda: "fb")
        self.assertEqual((result["source"], result["result"]), ("fallback", "fb"))
        self.assertIn("401", result["error"])
        with patch(POST, side_effect=requests.ConnectionError("down")):
            result = self.service.ask("unit_err", "sys", "p", lambda: "fb")
        self.assertEqual(result["source"], "fallback")
        with patch(POST, return_value=openai_answer("not json at all")):
            result = self.service.ask("unit_err", "sys", "p", lambda: {"x": 1}, expect_json=True)
        self.assertEqual(result["result"], {"x": 1})
        logs = self.env["epc.ai.log"].search([("feature", "=", "unit_err")])
        self.assertEqual(len(logs), 3)
        self.assertFalse(any(logs.mapped("success")))

    def test_parse_json(self):
        self.assertEqual(self.service.parse_json('Here you go: {"k": [1, 2]} thanks'), {"k": [1, 2]})
        self.assertEqual(self.service.parse_json("[1, 2]"), [1, 2])
        with self.assertRaises(ValueError):
            self.service.parse_json("")

    def test_dpr_draft_with_llm(self):
        self._configure("openai")
        llm_json = json.dumps({
            "lines": [{"activity_code": "A", "quantity": 7, "remarks": "7 foundations"},
                      {"activity_code": "NOPE", "quantity": 3}],
            "manpower": [{"trade": "Riggers", "count": 9}],
            "hindrances": [{"type": "right_of_way", "hours_lost": 4, "activity_code": "B",
                            "description": "Farmer objection"}, {"type": "alien", "description": "x"}],
            "weather": "rain",
            "summary": "Good day <script>",
        })
        dpr = self.env["epc.dpr"].create({"project_id": self.project.id, "date": date(2026, 2, 1),
                                          "notes_raw": "aaj 7 foundation hue, kisan ne kaam roka 4 ghante"})
        with patch(POST, return_value=openai_answer(llm_json)):
            dpr.action_ai_draft()
        self.assertEqual(dpr.ai_source, "llm")
        self.assertEqual([(l.activity_id.code, l.quantity) for l in dpr.line_ids], [("A", 7.0)])
        self.assertEqual(dpr.manpower_ids.trade, "Riggers")
        self.assertEqual(sorted(dpr.hindrance_ids.mapped("hindrance_type")), ["other", "right_of_way"])
        self.assertEqual(dpr.weather, "rain")
        self.assertNotIn("<script>", str(dpr.ai_summary))

    def test_assistant_and_report(self):
        self._configure("none")
        self.project.epc_status_date = date(2026, 1, 8)
        self.env["epc.prediction.engine"].run_for_projects(self.project)
        wizard = self.env["epc.ai.assistant"].create({
            "project_id": self.project.id, "question": "When will we finish and what is the LD exposure?"})
        wizard.action_ask()
        self.assertEqual(wizard.source, "fallback")
        self.assertIn("Predicted finish", str(wizard.answer))
        self.assertIn("LD terms", str(wizard.answer))
        wizard.action_post_to_project()
        self.assertIn("LD terms", str(self.project.message_ids[0].body))
        self.project.action_epc_generate_report()
        self.assertIn("Progress report", str(self.project.epc_ai_report))
        self._configure("openai")
        project = self.project.with_env(self.env)
        with patch(POST, return_value=openai_answer("```html\n<p>All good</p>\n```")):
            project.action_epc_generate_report()
        self.assertEqual(str(project.epc_ai_report), "<p>All good</p>")

    def test_settings_button(self):
        self._configure("none")
        settings = self.env["res.config.settings"].create({"epc_ai_provider": "none"})
        action = settings.action_epc_test_ai_connection()
        self.assertEqual(action["params"]["type"], "warning")
        settings = self.env["res.config.settings"].create({"epc_ai_provider": "openai", "epc_ai_api_key": "k"})
        with patch(POST, return_value=openai_answer("OK")):
            action = settings.action_epc_test_ai_connection()
        self.assertEqual(action["params"]["type"], "success")
        self.assertEqual(self.icp.get_param("zivoit_epc_ai.provider"), "openai")
