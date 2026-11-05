# -*- coding: utf-8 -*-
"""Provider-agnostic LLM client (Anthropic Claude, OpenAI-compatible, Google Gemini).

Stdlib only.  Every call is optional: the connector degrades to deterministic
logic when no provider is configured or a call fails.
"""
import json
import re
import urllib.error
import urllib.request


class LLMError(Exception):
    pass


DEFAULT_MODELS = {
    'anthropic': 'claude-sonnet-5-5',
    'openai': '',
    'gemini': '',
    'openai_compatible': '',
}


def _post(url, headers, body, timeout):
    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST', headers=dict(
        headers, **{'Content-Type': 'application/json'}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'ignore')[:400]
        raise LLMError('AI provider HTTP %s: %s' % (e.code, detail))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LLMError('AI provider unreachable: %s' % e)


def complete(provider, api_key, model, prompt, system=None, base_url=None, timeout=45,
             max_tokens=1200):
    """Return the text completion for `prompt`."""
    model = model or DEFAULT_MODELS.get(provider) or ''
    if not provider or provider == 'none':
        raise LLMError('No AI provider configured')
    if not model:
        raise LLMError('Please set an AI model name on the connection')
    if provider == 'anthropic':
        body = {'model': model, 'max_tokens': max_tokens,
                'messages': [{'role': 'user', 'content': prompt}]}
        if system:
            body['system'] = system
        res = _post((base_url or 'https://api.anthropic.com').rstrip('/') + '/v1/messages',
                    {'x-api-key': api_key or '', 'anthropic-version': '2023-06-01'}, body, timeout)
        return ''.join(b.get('text', '') for b in res.get('content', []) if b.get('type') == 'text')
    if provider in ('openai', 'openai_compatible'):
        msgs = ([{'role': 'system', 'content': system}] if system else []) + [
            {'role': 'user', 'content': prompt}]
        base = (base_url or 'https://api.openai.com/v1').rstrip('/')
        headers = {'Authorization': 'Bearer %s' % api_key} if api_key else {}
        res = _post(base + '/chat/completions', headers,
                    {'model': model, 'messages': msgs, 'temperature': 0}, timeout)
        return res['choices'][0]['message']['content']
    if provider == 'gemini':
        base = (base_url or 'https://generativelanguage.googleapis.com').rstrip('/')
        body = {'contents': [{'role': 'user', 'parts': [{'text': prompt}]}]}
        if system:
            body['systemInstruction'] = {'parts': [{'text': system}]}
        res = _post('%s/v1beta/models/%s:generateContent' % (base, model),
                    {'x-goog-api-key': api_key or ''}, body, timeout)
        return ''.join(p.get('text', '') for p in
                       res['candidates'][0]['content'].get('parts', []))
    raise LLMError('Unknown AI provider %s' % provider)


def extract_json(text):
    """Pull the first JSON object/array out of a model reply."""
    if not text:
        raise LLMError('Empty AI response')
    fence = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
    if fence:
        text = fence.group(1)
    start = min([i for i in (text.find('{'), text.find('[')) if i >= 0] or [-1])
    if start < 0:
        raise LLMError('AI response contained no JSON')
    opener = text[start]
    closer = '}' if opener == '{' else ']'
    depth = 0
    in_str = esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise LLMError('Unterminated JSON in AI response')
