# -*- coding: utf-8 -*-
"""Provider-agnostic AI gateway for ZivoIT Retail LMS.

Every AI feature calls ``zlms.ai.service``. When no provider is configured (or a call
fails), callers fall back to the deterministic offline engine implemented next to each
feature, so the LMS keeps working without an API key.
"""
import json
import logging
import re
import time

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PROVIDERS = [
    ('none', 'Offline engine (no external AI)'),
    ('openai', 'OpenAI'),
    ('anthropic', 'Anthropic Claude'),
    ('gemini', 'Google Gemini'),
    ('openai_compatible', 'OpenAI-compatible endpoint (Azure, Groq, Ollama, Sarvam...)'),
]

DEFAULT_MODELS = {
    'openai': 'gpt-4.1-mini',
    'anthropic': 'claude-sonnet-5-5',
    'gemini': 'gemini-2.5-flash',
    'openai_compatible': 'llama-3.3-70b-versatile',
}

RETAIL_SECTORS = [
    ('general', 'General / Multi-category Retail'),
    ('fashion', 'Fashion & Apparel'),
    ('sports', 'Sports & Activewear'),
    ('footwear', 'Footwear'),
    ('electronics', 'Consumer Electronics'),
    ('beauty', 'Beauty & Personal Care'),
    ('grocery', 'Grocery & Supermarket'),
    ('home', 'Home, Furniture & Decor'),
    ('jewellery', 'Jewellery & Watches'),
    ('pharmacy', 'Pharmacy & Wellness'),
    ('eyewear', 'Eyewear'),
    ('toys', 'Toys & Kids'),
    ('auto', 'Automotive & Accessories'),
]

LANGUAGES = [
    ('English', 'English'),
    ('Spanish', 'Spanish'),
    ('French', 'French'),
    ('German', 'German'),
    ('Portuguese', 'Portuguese'),
    ('Arabic', 'Arabic'),
    ('Indonesian', 'Indonesian'),
    ('Chinese', 'Chinese'),
    ('Hindi', 'Hindi'),
    ('Tamil', 'Tamil'),
    ('Telugu', 'Telugu'),
    ('Kannada', 'Kannada'),
    ('Malayalam', 'Malayalam'),
    ('Marathi', 'Marathi'),
    ('Bengali', 'Bengali'),
    ('Gujarati', 'Gujarati'),
    ('Odia', 'Odia'),
]


class ZlmsAiUnavailable(Exception):
    """Raised when no provider is configured or the provider call failed."""


class ZlmsAiService(models.AbstractModel):
    _name = 'zlms.ai.service'
    _description = 'Retail LMS AI Gateway'

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @api.model
    def _get_config(self):
        icp = self.env['ir.config_parameter'].sudo()
        provider = icp.get_param('zivoit_retail_lms.ai_provider', 'none') or 'none'
        return {
            'provider': provider,
            'api_key': icp.get_param('zivoit_retail_lms.ai_api_key', '') or '',
            'model': icp.get_param('zivoit_retail_lms.ai_model', '') or DEFAULT_MODELS.get(provider, ''),
            'base_url': (icp.get_param('zivoit_retail_lms.ai_base_url', '') or '').rstrip('/'),
            'temperature': float(icp.get_param('zivoit_retail_lms.ai_temperature', '0.4') or 0.4),
            'timeout': int(icp.get_param('zivoit_retail_lms.ai_timeout', '60') or 60),
        }

    @api.model
    def brand_context(self):
        """One sentence describing the retailer, injected into every AI prompt."""
        icp = self.env['ir.config_parameter'].sudo()
        brand = icp.get_param('zivoit_retail_lms.brand_name') or self.env.company.name or 'the brand'
        sector = icp.get_param('zivoit_retail_lms.retail_sector', 'general') or 'general'
        market = icp.get_param('zivoit_retail_lms.market') or ''
        sector_label = dict(RETAIL_SECTORS).get(sector, 'retail')
        text = '%s is a %s retailer operating a network of physical stores' % (brand, sector_label.lower())
        if market:
            text += ' in %s' % market
        return text + '.'

    @api.model
    def is_enabled(self):
        cfg = self._get_config()
        if cfg['provider'] == 'none':
            return False
        if cfg['provider'] == 'openai_compatible':
            return bool(cfg['base_url'])
        return bool(cfg['api_key'])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @api.model
    def chat(self, messages, system=None, json_mode=False, feature='generic', max_tokens=2000):
        """Send a chat request.

        :param messages: list of {'role': 'user'|'assistant', 'content': str}
        :return: assistant text
        :raises ZlmsAiUnavailable: when AI is disabled or the call fails
        """
        if not self.is_enabled():
            raise ZlmsAiUnavailable('AI provider not configured')
        cfg = self._get_config()
        if json_mode:
            system = (system or '') + '\nRespond with one valid JSON object only, no markdown fences.'
        started = time.time()
        try:
            text = self._call_provider(cfg, messages, system, json_mode, max_tokens)
            self._log(feature, cfg, True, time.time() - started, prompt=messages[-1]['content'] if messages else '',
                      response=text)
            return text
        except ZlmsAiUnavailable:
            raise
        except Exception as exc:  # network, auth, quota, parsing...
            _logger.warning('ZivoIT Retail LMS AI call failed (%s): %s', cfg['provider'], exc)
            self._log(feature, cfg, False, time.time() - started,
                      prompt=messages[-1]['content'] if messages else '', response=str(exc))
            raise ZlmsAiUnavailable(str(exc)) from exc

    @api.model
    def chat_json(self, prompt, system=None, feature='generic', max_tokens=3000):
        text = self.chat([{'role': 'user', 'content': prompt}], system=system, json_mode=True,
                         feature=feature, max_tokens=max_tokens)
        return self.parse_json(text)

    @api.model
    def parse_json(self, text):
        if isinstance(text, dict):
            return text
        text = (text or '').strip()
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.MULTILINE).strip()
        try:
            return json.loads(text)
        except ValueError:
            match = re.search(r'\{.*\}', text, flags=re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except ValueError:
                    pass
        raise ZlmsAiUnavailable('AI response was not valid JSON')

    @api.model
    def test_connection(self):
        if not self.is_enabled():
            raise UserError(_('Select an AI provider and enter its API key (or base URL) first.'))
        try:
            answer = self.chat([{'role': 'user', 'content': 'Reply with the single word: READY'}],
                               feature='test', max_tokens=20)
        except ZlmsAiUnavailable as exc:
            raise UserError(_('AI connection failed: %s', exc)) from exc
        return answer

    # ------------------------------------------------------------------
    # Providers
    # ------------------------------------------------------------------
    @api.model
    def _call_provider(self, cfg, messages, system, json_mode, max_tokens):
        provider = cfg['provider']
        timeout = cfg['timeout']
        if provider in ('openai', 'openai_compatible'):
            base = cfg['base_url'] or 'https://api.openai.com/v1'
            payload = {
                'model': cfg['model'],
                'temperature': cfg['temperature'],
                'max_tokens': max_tokens,
                'messages': ([{'role': 'system', 'content': system}] if system else []) + messages,
            }
            if json_mode and provider == 'openai':
                payload['response_format'] = {'type': 'json_object'}
            headers = {'Content-Type': 'application/json'}
            if cfg['api_key']:
                headers['Authorization'] = 'Bearer %s' % cfg['api_key']
                headers['api-key'] = cfg['api_key']  # Azure OpenAI style
            resp = requests.post('%s/chat/completions' % base, json=payload, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()['choices'][0]['message']['content']

        if provider == 'anthropic':
            base = cfg['base_url'] or 'https://api.anthropic.com/v1'
            payload = {
                'model': cfg['model'],
                'max_tokens': max_tokens,
                'temperature': cfg['temperature'],
                'messages': messages,
            }
            if system:
                payload['system'] = system
            headers = {
                'x-api-key': cfg['api_key'],
                'anthropic-version': '2023-06-01',
                'Content-Type': 'application/json',
            }
            resp = requests.post('%s/messages' % base, json=payload, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            return ''.join(block.get('text', '') for block in data.get('content', []) if block.get('type') == 'text')

        if provider == 'gemini':
            base = cfg['base_url'] or 'https://generativelanguage.googleapis.com/v1beta'
            contents = [{
                'role': 'model' if m['role'] == 'assistant' else 'user',
                'parts': [{'text': m['content']}],
            } for m in messages]
            payload = {
                'contents': contents,
                'generationConfig': {'temperature': cfg['temperature'], 'maxOutputTokens': max_tokens},
            }
            if json_mode:
                payload['generationConfig']['responseMimeType'] = 'application/json'
            if system:
                payload['systemInstruction'] = {'parts': [{'text': system}]}
            resp = requests.post('%s/models/%s:generateContent' % (base, cfg['model']),
                                 params={'key': cfg['api_key']}, json=payload, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            return ''.join(p.get('text', '') for p in data['candidates'][0]['content']['parts'])

        raise ZlmsAiUnavailable('Unknown provider %s' % provider)

    @api.model
    def _log(self, feature, cfg, success, duration, prompt='', response=''):
        try:
            self.env['zlms.ai.log'].sudo().create({
                'feature': feature,
                'provider': cfg['provider'],
                'model_name': cfg['model'],
                'success': success,
                'duration': round(duration, 2),
                'prompt_excerpt': (prompt or '')[:1000],
                'response_excerpt': (response or '')[:2000],
            })
        except Exception:  # logging must never break the feature
            _logger.debug('Could not write AI log', exc_info=True)


class ZlmsAiLog(models.Model):
    _name = 'zlms.ai.log'
    _description = 'Retail LMS AI Usage Log'
    _order = 'id desc'

    create_date = fields.Datetime(readonly=True)
    user_id = fields.Many2one('res.users', default=lambda self: self.env.user, readonly=True)
    feature = fields.Char(readonly=True)
    provider = fields.Selection(PROVIDERS, readonly=True)
    model_name = fields.Char(readonly=True)
    success = fields.Boolean(readonly=True)
    duration = fields.Float('Duration (s)', readonly=True)
    prompt_excerpt = fields.Text(readonly=True)
    response_excerpt = fields.Text(readonly=True)
