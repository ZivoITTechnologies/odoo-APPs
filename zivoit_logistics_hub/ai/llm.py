# -*- coding: utf-8 -*-
"""Provider agnostic LLM client (OpenAI, Anthropic Claude, Google Gemini, any OpenAI-compatible endpoint).

The client is intentionally small: JSON-in / JSON-out, short timeouts, never raises into business
flows (callers get ``None`` and fall back to the local engine).
"""
import json
import logging
import re

import requests

_logger = logging.getLogger(__name__)

DEFAULT_MODELS = {
    'openai': 'gpt-4o-mini',
    'anthropic': 'claude-haiku-4-5',
    'gemini': 'gemini-2.0-flash',
    'openai_compatible': 'llama3.1',
}


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, provider, api_key, model=None, base_url=None, timeout=20, log_callback=None):
        self.provider = provider
        self.api_key = api_key
        self.model = model or DEFAULT_MODELS.get(provider)
        self.base_url = (base_url or '').rstrip('/')
        self.timeout = timeout
        self.log_callback = log_callback

    def _post(self, url, headers, payload):
        resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        if resp.status_code >= 300:
            raise LLMError('LLM HTTP %s: %s' % (resp.status_code, resp.text[:300]))
        return resp.json()

    def complete(self, system, user, want_json=True, max_tokens=700):
        if self.provider == 'anthropic':
            data = self._post('https://api.anthropic.com/v1/messages', {
                'x-api-key': self.api_key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json',
            }, {'model': self.model, 'max_tokens': max_tokens, 'system': system,
                'messages': [{'role': 'user', 'content': user}]})
            text = ''.join(b.get('text', '') for b in data.get('content') or [] if b.get('type') == 'text')
        elif self.provider == 'gemini':
            data = self._post(
                'https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent' % self.model,
                {'x-goog-api-key': self.api_key, 'Content-Type': 'application/json'},
                {'systemInstruction': {'parts': [{'text': system}]},
                 'contents': [{'role': 'user', 'parts': [{'text': user}]}],
                 'generationConfig': {'maxOutputTokens': max_tokens,
                                      **({'responseMimeType': 'application/json'} if want_json else {})}})
            parts = ((data.get('candidates') or [{}])[0].get('content') or {}).get('parts') or []
            text = ''.join(p.get('text', '') for p in parts)
        else:  # openai or openai_compatible (Azure OpenAI, OpenRouter, Ollama, vLLM...)
            base = self.base_url or 'https://api.openai.com/v1'
            payload = {'model': self.model, 'max_tokens': max_tokens,
                       'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}
            if want_json:
                payload['response_format'] = {'type': 'json_object'}
            data = self._post('%s/chat/completions' % base,
                              {'Authorization': 'Bearer %s' % self.api_key, 'Content-Type': 'application/json'}, payload)
            text = ((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        if self.log_callback:
            self.log_callback(user[:4000], text[:4000])
        if not want_json:
            return text.strip()
        return extract_json(text)


def extract_json(text):
    text = (text or '').strip()
    fenced = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
    if fenced:
        text = fenced.group(1)
    start = min([i for i in (text.find('{'), text.find('[')) if i >= 0] or [0])
    try:
        return json.loads(text[start:])
    except ValueError:
        # trim trailing garbage after the last closing brace
        end = max(text.rfind('}'), text.rfind(']'))
        try:
            return json.loads(text[start:end + 1])
        except ValueError as exc:
            raise LLMError('LLM did not return valid JSON: %s' % exc)
