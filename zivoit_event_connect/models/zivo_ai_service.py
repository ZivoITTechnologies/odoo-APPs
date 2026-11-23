# -*- coding: utf-8 -*-
"""Provider-agnostic AI service for ZivoIT Event Connect.

Supported providers: OpenAI, Anthropic Claude, Google Gemini and any OpenAI-compatible endpoint
(Azure OpenAI, Ollama, LM Studio, vLLM, OpenRouter...). The customer brings their own API key.
Every call is logged in ``zivo.ai.log``; AI output is always a *suggestion* that a user applies.
"""
import json
import logging
import re
import time

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.modules import module as odoo_module

_logger = logging.getLogger(__name__)

PARAM = 'zivoit_event_connect.%s'
DEFAULT_MODELS = {
    'openai': 'gpt-4o-mini',
    'anthropic': 'claude-sonnet-4-5',
    'gemini': 'gemini-2.0-flash',
    'openai_compatible': 'llama3.1',
}
FEATURES = [
    ('writer', 'Event content writer'),
    ('search', 'Natural-language event search'),
    ('booking', 'Booking approval assistant'),
    ('survey', 'Survey insights'),
    ('document', 'Document assistant'),
    ('anomaly', 'Audit anomaly alerts'),
    ('test', 'Connection test'),
]


class ZivoAiLog(models.Model):
    _name = 'zivo.ai.log'
    _description = 'Event Connect AI Usage Log'
    _order = 'id desc'

    feature = fields.Selection(FEATURES, required=True, readonly=True, index=True)
    provider = fields.Char(readonly=True)
    model_name = fields.Char(string='Model', readonly=True)
    user_id = fields.Many2one('res.users', readonly=True, default=lambda self: self.env.user)
    res_model = fields.Char(readonly=True)
    res_id = fields.Integer(readonly=True)
    state = fields.Selection([('ok', 'Success'), ('error', 'Error')], readonly=True)
    duration_ms = fields.Integer(string='Duration (ms)', readonly=True)
    input_tokens = fields.Integer(readonly=True)
    output_tokens = fields.Integer(readonly=True)
    error = fields.Text(readonly=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, readonly=True)


class ZivoAiService(models.AbstractModel):
    _name = 'zivo.ai.service'
    _description = 'Event Connect AI Service'

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @api.model
    def _get_config(self):
        icp = self.env['ir.config_parameter'].sudo()
        provider = icp.get_param(PARAM % 'ai_provider', 'openai')
        return {
            'enabled': str(icp.get_param(PARAM % 'ai_enabled', 'False')).lower() in ('1', 'true'),
            'provider': provider,
            'api_key': icp.get_param(PARAM % 'ai_api_key', ''),
            'model': icp.get_param(PARAM % 'ai_model', '') or DEFAULT_MODELS.get(provider, ''),
            'base_url': (icp.get_param(PARAM % 'ai_base_url', '') or '').rstrip('/'),
            'temperature': float(icp.get_param(PARAM % 'ai_temperature', '0.3') or 0.3),
            'max_tokens': int(icp.get_param(PARAM % 'ai_max_tokens', '1500') or 1500),
            'timeout': int(icp.get_param(PARAM % 'ai_timeout', '60') or 60),
        }

    @api.model
    def _feature_enabled(self, feature):
        cfg = self._get_config()
        if not cfg['enabled']:
            return False
        flag = self.env['ir.config_parameter'].sudo().get_param(PARAM % ('ai_feature_%s' % feature), 'False')
        return str(flag).lower() in ('1', 'true')

    @api.model
    def _ensure_feature(self, feature):
        if not self._feature_enabled(feature):
            raise UserError(self.env._(
                'This AI feature is disabled. Enable AI and the feature in Events › Configuration › Settings '
                '› Event Connect AI.'))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @api.model
    def _zivo_ai_complete(self, feature, system, prompt, as_json=False, record=None):
        """Return the model answer (str, or dict when ``as_json``). Raises UserError on failure."""
        self._ensure_feature(feature) if feature != 'test' else None
        cfg = self._get_config()
        if cfg['provider'] != 'openai_compatible' and not cfg['api_key']:
            raise UserError(self.env._('Please set the AI API key in Event Connect AI settings.'))
        if as_json:
            system = (system or '') + '\nAnswer ONLY with a valid JSON object, no markdown, no commentary.'
        start = time.time()
        log_vals = {
            'feature': feature, 'provider': cfg['provider'], 'model_name': cfg['model'],
            'res_model': record._name if record else False, 'res_id': record.id if record else 0,
        }
        try:
            text, usage = self._zivo_call_provider(cfg, system, prompt, as_json)
            result = self._zivo_parse_json(text) if as_json else (text or '').strip()
        except UserError as exc:
            log_vals.update(state='error', error=str(exc.args[0])[:2000],
                            duration_ms=int((time.time() - start) * 1000))
            self._zivo_log(log_vals, separate=True)
            raise
        except Exception as exc:  # noqa: BLE001 - network / provider errors are reported to the user
            _logger.warning('Event Connect AI call failed: %s', exc)
            log_vals.update(state='error', error=str(exc)[:2000], duration_ms=int((time.time() - start) * 1000))
            self._zivo_log(log_vals, separate=True)
            raise UserError(self.env._('The AI provider could not be reached: %s', exc)) from exc
        log_vals.update(state='ok', duration_ms=int((time.time() - start) * 1000),
                        input_tokens=usage.get('input', 0), output_tokens=usage.get('output', 0))
        self._zivo_log(log_vals)
        if record:
            self.env['zivo.audit.log']._zivo_log(record, 'ai', note='AI %s generated a suggestion (%s)' % (
                dict(FEATURES).get(feature, feature), cfg['model']))
        return result

    @api.model
    def _zivo_log(self, vals, separate=False):
        """Log usage. Errors are written in a separate transaction because the request will be rolled back."""
        if separate and not odoo_module.current_test:
            try:
                with self.env.registry.cursor() as cr:
                    self.with_env(self.env(cr=cr))['zivo.ai.log'].sudo().create(vals)
                return
            except Exception:  # noqa: BLE001
                _logger.exception('Could not log AI usage')
                return
        try:
            with self.env.cr.savepoint():
                self.env['zivo.ai.log'].sudo().create(vals)
        except Exception:  # noqa: BLE001
            _logger.exception('Could not log AI usage')

    @api.model
    def _zivo_parse_json(self, text):
        text = (text or '').strip()
        fenced = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
        if fenced:
            text = fenced.group(1).strip()
        try:
            return json.loads(text)
        except ValueError:
            pass
        start, end = text.find('{'), text.rfind('}')
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                pass
        raise UserError(self.env._('The AI answer was not valid JSON. Please try again.'))

    # ------------------------------------------------------------------
    # Providers
    # ------------------------------------------------------------------
    @api.model
    def _zivo_call_provider(self, cfg, system, prompt, as_json):
        provider = cfg['provider']
        timeout = cfg['timeout']
        if provider in ('openai', 'openai_compatible'):
            base = 'https://api.openai.com/v1' if provider == 'openai' else (cfg['base_url'] or 'http://localhost:11434/v1')
            headers = {'Content-Type': 'application/json'}
            if cfg['api_key']:
                headers['Authorization'] = 'Bearer %s' % cfg['api_key']
            payload = {
                'model': cfg['model'],
                'temperature': cfg['temperature'],
                'max_tokens': cfg['max_tokens'],
                'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}],
            }
            if as_json and provider == 'openai':
                payload['response_format'] = {'type': 'json_object'}
            resp = requests.post(base + '/chat/completions', json=payload, headers=headers, timeout=timeout)
            data = self._zivo_check(resp)
            usage = data.get('usage') or {}
            return (data['choices'][0]['message']['content'],
                    {'input': usage.get('prompt_tokens', 0), 'output': usage.get('completion_tokens', 0)})
        if provider == 'anthropic':
            headers = {'x-api-key': cfg['api_key'], 'anthropic-version': '2023-06-01',
                       'content-type': 'application/json'}
            payload = {'model': cfg['model'], 'max_tokens': cfg['max_tokens'], 'temperature': cfg['temperature'],
                       'system': system, 'messages': [{'role': 'user', 'content': prompt}]}
            resp = requests.post((cfg['base_url'] or 'https://api.anthropic.com') + '/v1/messages',
                                 json=payload, headers=headers, timeout=timeout)
            data = self._zivo_check(resp)
            text = ''.join(block.get('text', '') for block in data.get('content', []) if block.get('type') == 'text')
            usage = data.get('usage') or {}
            return text, {'input': usage.get('input_tokens', 0), 'output': usage.get('output_tokens', 0)}
        if provider == 'gemini':
            url = '%s/v1beta/models/%s:generateContent' % (
                cfg['base_url'] or 'https://generativelanguage.googleapis.com', cfg['model'])
            payload = {
                'systemInstruction': {'parts': [{'text': system}]},
                'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
                'generationConfig': {'temperature': cfg['temperature'], 'maxOutputTokens': cfg['max_tokens']},
            }
            if as_json:
                payload['generationConfig']['responseMimeType'] = 'application/json'
            resp = requests.post(url, json=payload, headers={'x-goog-api-key': cfg['api_key'],
                                                             'Content-Type': 'application/json'}, timeout=timeout)
            data = self._zivo_check(resp)
            parts = data['candidates'][0]['content']['parts']
            usage = data.get('usageMetadata') or {}
            return ''.join(p.get('text', '') for p in parts), {
                'input': usage.get('promptTokenCount', 0), 'output': usage.get('candidatesTokenCount', 0)}
        raise UserError(self.env._('Unknown AI provider: %s', provider))

    @api.model
    def _zivo_check(self, resp):
        if resp.status_code >= 400:
            try:
                detail = resp.json()
                detail = detail.get('error', detail)
                if isinstance(detail, dict):
                    detail = detail.get('message') or json.dumps(detail)
            except ValueError:
                detail = resp.text[:300]
            raise UserError(self.env._('AI provider error (HTTP %(code)s): %(detail)s',
                                       code=resp.status_code, detail=detail))
        return resp.json()

    @api.model
    def action_test_connection(self):
        answer = self._zivo_ai_complete('test', 'You are a connectivity probe.',
                                        'Reply with the single word: pong', as_json=False)
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'type': 'success', 'title': self.env._('AI connection works'),
                       'message': (answer or '')[:200], 'sticky': False},
        }
