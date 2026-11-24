# -*- coding: utf-8 -*-
import json
import logging
import time

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

DEFAULT_MODELS = {
    "openai": ("gpt-4.1", "gpt-4.1-mini", "text-embedding-3-small", "https://api.openai.com/v1"),
    "anthropic": ("claude-sonnet-4-5", "claude-haiku-4-5", "", "https://api.anthropic.com/v1"),
    "gemini": ("gemini-2.5-pro", "gemini-2.5-flash", "text-embedding-004",
               "https://generativelanguage.googleapis.com/v1beta"),
    "openai_compatible": ("llama3.1", "llama3.1", "nomic-embed-text", "http://localhost:11434/v1"),
    "mock": ("mock", "mock", "", ""),
}


class EduagentProvider(models.Model):
    _name = "eduagent.provider"
    _description = "EduAgent AI Provider (Bring Your Own Key)"
    _order = "sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)
    provider_type = fields.Selection([
        ("openai", "OpenAI"),
        ("anthropic", "Anthropic Claude"),
        ("gemini", "Google Gemini"),
        ("openai_compatible", "OpenAI-compatible (Azure, Ollama, vLLM, Groq...)"),
        ("mock", "Demo / Offline (no API calls)"),
    ], required=True, default="openai")
    api_key = fields.Char(string="API Key", groups="zivoit_eduagent_ai.group_eduagent_manager", copy=False)
    base_url = fields.Char(string="Base URL")
    chat_model = fields.Char(string="Main Model", help="Used for tutoring, grading, course building.")
    fast_model = fields.Char(string="Fast Model", help="Cheaper model for routing, nudges, admissions.")
    embedding_model = fields.Char(string="Embedding Model",
                                  help="Leave empty to use keyword retrieval for the RAG store.")
    temperature = fields.Float(default=0.3)
    timeout = fields.Integer(string="Timeout (s)", default=60)
    cost_input_per_1k = fields.Float(string="Cost / 1K input tokens (USD)", digits=(12, 6))
    cost_output_per_1k = fields.Float(string="Cost / 1K output tokens (USD)", digits=(12, 6))
    note = fields.Text()

    @api.onchange("provider_type")
    def _onchange_provider_type(self):
        for rec in self:
            chat, fast, emb, url = DEFAULT_MODELS.get(rec.provider_type, ("", "", "", ""))
            rec.chat_model = chat
            rec.fast_model = fast
            rec.embedding_model = emb
            rec.base_url = url

    # ------------------------------------------------------------------
    # Low level transport
    # ------------------------------------------------------------------
    def _get_key(self):
        self.ensure_one()
        return self.sudo().api_key or ""

    def _post(self, url, payload, headers):
        self.ensure_one()
        try:
            resp = requests.post(url, data=json.dumps(payload), headers=headers, timeout=self.timeout or 60)
        except requests.RequestException as exc:
            raise UserError(_("AI provider %(name)s is unreachable: %(err)s", name=self.name, err=exc))
        if resp.status_code >= 400:
            raise UserError(_("AI provider %(name)s returned %(code)s: %(body)s",
                              name=self.name, code=resp.status_code, body=resp.text[:500]))
        return resp.json()

    def _chat(self, system, messages, tier="chat", json_mode=False, max_tokens=1200,
              temperature=None, mock_response=None):
        """Send a chat completion. ``messages`` = [{'role': 'user'|'assistant', 'content': str}].

        Returns dict(text, input_tokens, output_tokens, model).
        """
        self.ensure_one()
        model = (self.fast_model if tier == "fast" else self.chat_model) or self.chat_model
        temperature = self.temperature if temperature is None else temperature
        ptype = self.provider_type
        if ptype == "mock":
            text = mock_response if mock_response is not None else json.dumps(
                {"reply": _("(Demo mode) AI provider not configured yet. Configure a provider "
                            "under EduAgent AI > Configuration > AI Providers."), "actions": []})
            if not isinstance(text, str):
                text = json.dumps(text)
            return {"text": text, "input_tokens": _approx_tokens(system, messages),
                    "output_tokens": len(text) // 4, "model": "mock"}

        key = self._get_key()
        if not key and ptype != "openai_compatible":
            raise UserError(_("No API key configured for AI provider %s.", self.name))

        if ptype in ("openai", "openai_compatible"):
            base = (self.base_url or DEFAULT_MODELS[ptype][3]).rstrip("/")
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = "Bearer %s" % key
                headers["api-key"] = key  # Azure OpenAI
            payload = {
                "model": model,
                "messages": [{"role": "system", "content": system}] + messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            data = self._post(base + "/chat/completions", payload, headers)
            usage = data.get("usage") or {}
            return {
                "text": data["choices"][0]["message"]["content"] or "",
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "model": model,
            }

        if ptype == "anthropic":
            base = (self.base_url or DEFAULT_MODELS[ptype][3]).rstrip("/")
            headers = {
                "Content-Type": "application/json",
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            }
            sys_prompt = system
            if json_mode:
                sys_prompt += "\n\nRespond with a single valid JSON object only, no prose around it."
            payload = {
                "model": model,
                "system": sys_prompt,
                "messages": _merge_roles(messages),
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            data = self._post(base + "/messages", payload, headers)
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            usage = data.get("usage") or {}
            return {"text": text, "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0), "model": model}

        if ptype == "gemini":
            base = (self.base_url or DEFAULT_MODELS[ptype][3]).rstrip("/")
            contents = [{"role": "model" if m["role"] == "assistant" else "user",
                         "parts": [{"text": m["content"]}]} for m in _merge_roles(messages)]
            gen_cfg = {"temperature": temperature, "maxOutputTokens": max_tokens}
            if json_mode:
                gen_cfg["responseMimeType"] = "application/json"
            payload = {"systemInstruction": {"parts": [{"text": system}]},
                       "contents": contents, "generationConfig": gen_cfg}
            url = "%s/models/%s:generateContent?key=%s" % (base, model, key)
            data = self._post(url, payload, {"Content-Type": "application/json"})
            cands = data.get("candidates") or [{}]
            parts = (cands[0].get("content") or {}).get("parts") or []
            usage = data.get("usageMetadata") or {}
            return {"text": "".join(p.get("text", "") for p in parts),
                    "input_tokens": usage.get("promptTokenCount", 0),
                    "output_tokens": usage.get("candidatesTokenCount", 0), "model": model}

        raise UserError(_("Unsupported provider type %s", ptype))

    def _embed(self, texts):
        """Return a list of embedding vectors, or None when embeddings are not available."""
        self.ensure_one()
        if not self.embedding_model or self.provider_type in ("mock", "anthropic") or not texts:
            return None
        key = self._get_key()
        try:
            if self.provider_type in ("openai", "openai_compatible"):
                base = (self.base_url or DEFAULT_MODELS[self.provider_type][3]).rstrip("/")
                headers = {"Content-Type": "application/json"}
                if key:
                    headers["Authorization"] = "Bearer %s" % key
                    headers["api-key"] = key
                data = self._post(base + "/embeddings", {"model": self.embedding_model, "input": texts}, headers)
                return [row["embedding"] for row in sorted(data["data"], key=lambda r: r["index"])]
            if self.provider_type == "gemini":
                base = (self.base_url or DEFAULT_MODELS["gemini"][3]).rstrip("/")
                url = "%s/models/%s:batchEmbedContents?key=%s" % (base, self.embedding_model, key)
                payload = {"requests": [{"model": "models/%s" % self.embedding_model,
                                         "content": {"parts": [{"text": t}]}} for t in texts]}
                data = self._post(url, payload, {"Content-Type": "application/json"})
                return [e["values"] for e in data.get("embeddings", [])]
        except UserError as exc:
            _logger.warning("EduAgent embedding failed, falling back to keyword retrieval: %s", exc)
        return None

    def action_test_connection(self):
        self.ensure_one()
        start = time.time()
        res = self._chat("You are a connection test.", [{"role": "user", "content": "Reply with the word OK."}],
                         tier="fast", max_tokens=10, mock_response="OK")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Connection successful"),
                "message": _("Model %(model)s answered %(text)r in %(sec).1f s",
                             model=res["model"], text=res["text"][:40], sec=time.time() - start),
                "type": "success",
                "sticky": False,
            },
        }

    def action_set_default(self):
        self.ensure_one()
        self.env.company.eduagent_default_provider_id = self


def _approx_tokens(system, messages):
    total = len(system or "")
    for m in messages or []:
        total += len(m.get("content") or "")
    return total // 4


def _merge_roles(messages):
    """Anthropic / Gemini need alternating roles starting with 'user'."""
    merged = []
    for m in messages:
        role = "assistant" if m.get("role") == "assistant" else "user"
        content = m.get("content") or ""
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n\n" + content
        else:
            merged.append({"role": role, "content": content})
    if merged and merged[0]["role"] == "assistant":
        merged.insert(0, {"role": "user", "content": "(conversation start)"})
    if not merged:
        merged = [{"role": "user", "content": "Hello"}]
    return merged
