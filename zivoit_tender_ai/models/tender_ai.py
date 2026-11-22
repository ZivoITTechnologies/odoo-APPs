# -*- coding: utf-8 -*-
import json
import logging
import re

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from . import tender_utils as tu

_logger = logging.getLogger(__name__)

ANTHROPIC_URL = 'https://api.anthropic.com/v1/messages'
OPENAI_URL = 'https://api.openai.com/v1/responses'

TENDER_JSON_SPEC = """Return ONLY a JSON array (no prose, no markdown). Each element is an object with keys:
"title" (name of work / item / service), "reference" (tender / NIT / tender ID number), "organisation" (buyer, e.g. South Central Railway),
"department" (division / circle / unit), "location" (city or district), "state" (Indian state name),
"estimated_value" (number in INR, convert Lakh / Crore), "emd" (number in INR), "tender_fee" (number in INR),
"published_date" (ISO 8601), "closing_date" (bid submission end, ISO 8601), "opening_date" (ISO 8601),
"completion_period" (e.g. "18 Months"), "url" (direct link to the tender page), "portal" (website name),
"description" (one or two sentences). Use null when a value is unknown. Never invent tenders or numbers:
only include tenders you actually found in the search results."""

QUERY_JSON_SPEC = """Convert the user's tender search request into JSON with keys:
"keywords" (list of important lowercase words or phrases that must appear in the tender, excluding generic words
like tender, construction, works, services, supply), "exclude_keywords" (list), "states" (list of Indian state names),
"zones" (list of Indian Railway zone codes like SCR, SR, CR, WR, NR, ECOR, SWR or PSU codes RVNL, IRCON),
"sectors" (list from: roads, buildings, bridges, railways, water, irrigation, electrical, metro, real_estate,
maintenance, it_software, telecom, manpower, security_services, facility_mgmt, healthcare, consultancy, goods_supply,
transport, education_training, media_events), "organisations" (list of buyer abbreviations, e.g. NHAI, CPWD, GHMC), "min_value" (INR number or 0),
"max_value" (INR number or 0), "open_only" (boolean), "deadline_days" (int, 0 if none),
"published_days" (int, 0 if none). Return ONLY the JSON object."""

ANALYSIS_JSON_SPEC = """Analyse this Indian public tender for a bidder in that industry (works, goods, IT, manpower or other
services) and return ONLY a JSON object
with keys: "summary" (3-5 short bullet points as one string separated by newlines), "scope" (one sentence),
"eligibility" (key pre-qualification requirements you can infer), "risks" (main risks separated by newlines),
"recommendation" ("go", "review" or "no_go"), "score" (0-100 integer for attractiveness),
"reason" (one sentence)."""


class TenderAI(models.AbstractModel):
    _name = 'zivoit.tender.ai'
    _description = 'Tender AI Service'

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------
    @api.model
    def _get_config(self):
        icp = self.env['ir.config_parameter'].sudo()
        return {
            'provider': icp.get_param('zivoit_tender_ai.ai_provider', 'none'),
            'api_key': icp.get_param('zivoit_tender_ai.ai_api_key', ''),
            'model': icp.get_param('zivoit_tender_ai.ai_model', ''),
            'web_search': icp.get_param('zivoit_tender_ai.ai_web_search', 'True') == 'True',
            'web_tool': icp.get_param('zivoit_tender_ai.ai_web_tool', ''),
            'max_uses': int(icp.get_param('zivoit_tender_ai.ai_max_searches', '5') or 5),
            'timeout': int(icp.get_param('zivoit_tender_ai.ai_timeout', '120') or 120),
        }

    @api.model
    def _is_enabled(self):
        config = self._get_config()
        return config['provider'] in ('anthropic', 'openai') and bool(config['api_key'])

    # ------------------------------------------------------------------
    # low level calls
    # ------------------------------------------------------------------
    @api.model
    def _call_llm(self, system, prompt, web_search=False, allowed_domains=None, max_tokens=4000):
        config = self._get_config()
        if not self._is_enabled():
            raise UserError(_('No AI provider is configured. Go to Tender AI > Configuration > Settings.'))
        if config['provider'] == 'anthropic':
            return self._call_anthropic(config, system, prompt, web_search, allowed_domains, max_tokens)
        return self._call_openai(config, system, prompt, web_search, allowed_domains, max_tokens)

    @api.model
    def _call_anthropic(self, config, system, prompt, web_search, allowed_domains, max_tokens):
        payload = {
            'model': config['model'] or 'claude-sonnet-5-5',
            'max_tokens': max_tokens,
            'system': system,
            'messages': [{'role': 'user', 'content': prompt}],
        }
        if web_search and config['web_search']:
            tool = {'type': config['web_tool'] or 'web_search_20250305', 'name': 'web_search',
                    'max_uses': config['max_uses']}
            if allowed_domains:
                tool['allowed_domains'] = allowed_domains
            payload['tools'] = [tool]
        headers = {
            'x-api-key': config['api_key'],
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        }
        response = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=config['timeout'])
        if response.status_code >= 400:
            raise UserError(_('Claude API error %(code)s: %(body)s',
                              code=response.status_code, body=response.text[:500]))
        data = response.json()
        texts = [block.get('text', '') for block in data.get('content', []) if block.get('type') == 'text']
        return '\n'.join(texts).strip()

    @api.model
    def _call_openai(self, config, system, prompt, web_search, allowed_domains, max_tokens):
        payload = {
            'model': config['model'] or 'gpt-4o-mini',
            'instructions': system,
            'input': prompt,
            'max_output_tokens': max_tokens,
        }
        if web_search and config['web_search']:
            tool = {'type': config['web_tool'] or 'web_search'}
            if allowed_domains:
                tool['filters'] = {'allowed_domains': allowed_domains}
            payload['tools'] = [tool]
        headers = {'Authorization': 'Bearer %s' % config['api_key'], 'Content-Type': 'application/json'}
        response = requests.post(OPENAI_URL, headers=headers, json=payload, timeout=config['timeout'])
        if response.status_code >= 400:
            raise UserError(_('OpenAI API error %(code)s: %(body)s',
                              code=response.status_code, body=response.text[:500]))
        data = response.json()
        if data.get('output_text'):
            return data['output_text']
        texts = []
        for item in data.get('output', []):
            for content in item.get('content', []) or []:
                if content.get('type') in ('output_text', 'text'):
                    texts.append(content.get('text', ''))
        return '\n'.join(texts).strip()

    @api.model
    def _extract_json(self, text):
        """Extract the first JSON object/array from an LLM answer (tolerates ```json fences)."""
        if not text:
            return None
        text = re.sub(r'```(?:json)?', '', text)
        for opener, closer in (('[', ']'), ('{', '}')):
            start = text.find(opener)
            end = text.rfind(closer)
            if start != -1 and end > start:
                candidate = text[start:end + 1]
                try:
                    return json.loads(candidate)
                except ValueError:
                    continue
        return None

    # ------------------------------------------------------------------
    # high level features
    # ------------------------------------------------------------------
    @api.model
    def parse_query(self, query):
        """Natural-language search -> structured filters. Rule-based first, refined by AI if enabled."""
        sector_map = self.env['zivoit.tender.sector']._get_keyword_map() or None
        result = tu.parse_query(query, sector_map)
        result['engine'] = 'rules'
        if not self._is_enabled() or not query:
            return result
        try:
            answer = self._call_llm(QUERY_JSON_SPEC, query, max_tokens=800)
            data = self._extract_json(answer)
            if isinstance(data, dict):
                for key in ('keywords', 'exclude_keywords', 'states', 'zones', 'sectors', 'organisations'):
                    values = data.get(key) or []
                    if isinstance(values, str):
                        values = [values]
                    merged = list(result.get(key) or [])
                    for value in values:
                        value = str(value).strip()
                        if key in ('keywords', 'exclude_keywords', 'sectors'):
                            value = value.lower()
                        if key == 'zones':
                            value = value.upper()
                        if value and value not in merged:
                            merged.append(value)
                    result[key] = merged
                for key in ('min_value', 'max_value'):
                    if data.get(key):
                        result[key] = float(data[key])
                for key in ('deadline_days', 'published_days'):
                    if data.get(key):
                        result[key] = int(data[key])
                if 'open_only' in data:
                    result['open_only'] = bool(data['open_only'])
                result['engine'] = 'ai'
        except Exception as error:  # noqa: BLE001 - AI must never block the search
            _logger.warning('Tender AI query parsing failed, using rules: %s', error)
        return result

    @api.model
    def search_tenders_web(self, query, allowed_domains=None, filters=None):
        """Use the LLM web-search tool to discover live tenders. Returns a list of raw tender dicts."""
        filters = filters or {}
        hints = []
        if filters.get('states'):
            hints.append('State(s): %s' % ', '.join(filters['states']))
        if filters.get('zones'):
            hints.append('Railway zone / PSU: %s' % ', '.join(filters['zones']))
        if filters.get('sectors'):
            hints.append('Sector(s): %s' % ', '.join(filters['sectors']))
        if filters.get('min_value'):
            hints.append('Minimum value: INR %.0f' % filters['min_value'])
        if filters.get('max_value'):
            hints.append('Maximum value: INR %.0f' % filters['max_value'])
        today = fields.Date.context_today(self)
        prompt = (
            'Today is %s. Find currently OPEN (bid submission date in the future) Indian public '
            'tenders of any category (works, goods, IT / software, manpower, telecom, services, consultancy) '
            'for this request: "%s".\n%s\nPrefer official portals such as eprocure.gov.in (CPPP), gem.gov.in, '
            'ireps.gov.in, the state e-procurement portals (*.gov.in / *.nic.in, e.g. etenders.kerala.gov.in, '
            'hptenders.gov.in, arunachaltenders.gov.in, tender.telangana.gov.in) and PSU sites.\n%s'
        ) % (today, query, '\n'.join(hints), TENDER_JSON_SPEC)
        system = ('You are a tender research agent working for an Indian company that bids for public tenders. '
                  'You search the web carefully and report only real, verifiable tenders.')
        answer = self._call_llm(system, prompt, web_search=True, allowed_domains=allowed_domains,
                                max_tokens=6000)
        data = self._extract_json(answer)
        if isinstance(data, dict):
            data = data.get('tenders') or [data]
        return [row for row in (data or []) if isinstance(row, dict) and row.get('title')]

    @api.model
    def analyse_tender(self, tender):
        """Return dict(summary, scope, eligibility, risks, recommendation, score, reason, engine)."""
        if self._is_enabled():
            try:
                answer = self._call_llm(ANALYSIS_JSON_SPEC, tender._get_ai_context(), max_tokens=1500)
                data = self._extract_json(answer)
                if isinstance(data, dict) and data.get('summary'):
                    rec = str(data.get('recommendation') or 'review').lower().replace('-', '_').replace(' ', '_')
                    if rec not in ('go', 'review', 'no_go'):
                        rec = 'review'
                    data['recommendation'] = rec
                    data['score'] = max(0, min(100, int(data.get('score') or 0)))
                    data['engine'] = 'ai'
                    return data
            except Exception as error:  # noqa: BLE001
                _logger.warning('Tender AI analysis failed, using rules: %s', error)
        return self._analyse_rules(tender)

    @api.model
    def _analyse_rules(self, tender):
        currency = tender.currency_id
        lines = []
        lines.append('• Buyer: %s%s' % (tender.organisation or '-', (' / %s' % tender.department) if tender.department else ''))
        if tender.estimated_value:
            lines.append('• Estimated value: %s %s (%s)' % (currency.symbol or '', tender._format_inr(tender.estimated_value),
                                                           dict(tender._fields['value_band'].selection).get(tender.value_band)))
        if tender.emd_amount:
            lines.append('• EMD: %s %s' % (currency.symbol or '', tender._format_inr(tender.emd_amount)))
        if tender.completion_months:
            lines.append('• Completion period: %s months' % tender.completion_months)
        if tender.date_closing:
            lines.append('• Bid closes: %s (%s days left)' % (tender.date_closing_local, round(tender.days_left, 1)))
        risks = []
        if tender.days_left is not False and 0 <= tender.days_left < 5:
            risks.append('Very little time left to prepare the bid.')
        if tender.estimated_value and tender.estimated_value > 100 * tu.CRORE:
            risks.append('Large value: check turnover, bid capacity and similar-work criteria.')
        if tender.completion_months and tender.estimated_value and tender.completion_months < 6 \
                and tender.estimated_value > 10 * tu.CRORE:
            risks.append('Aggressive timeline for the contract value.')
        if not tender.estimated_value:
            risks.append('Estimated value not published - read the NIT.')
        score = tender.relevance_score or 50
        if tender.go_score:
            score = int((score + tender.go_score) / 2)
        if risks and score > 30:
            score -= 5 * len(risks)
        recommendation = 'go' if score >= 70 else ('review' if score >= 45 else 'no_go')
        return {
            'summary': '\n'.join(lines),
            'scope': tender.name,
            'eligibility': '',
            'risks': '\n'.join(risks) or 'No major risk detected by rules.',
            'recommendation': recommendation,
            'score': max(0, min(100, score)),
            'reason': 'Rule-based assessment from relevance, value, deadline and qualification checklist.',
            'engine': 'rules',
        }

    @api.model
    def extract_tender_from_text(self, text, subject=''):
        """Read an e-mail / NIT text and extract tender fields."""
        if self._is_enabled():
            try:
                answer = self._call_llm(
                    'You extract structured tender data from Indian tender notices.',
                    'Extract every tender in this notice.\n%s\n\nSUBJECT: %s\n\n%s' % (TENDER_JSON_SPEC, subject, text[:15000]),
                    max_tokens=3000)
                data = self._extract_json(answer)
                if isinstance(data, dict):
                    data = [data]
                rows = [row for row in (data or []) if isinstance(row, dict) and row.get('title')]
                if rows:
                    return rows
            except Exception as error:  # noqa: BLE001
                _logger.warning('Tender AI extraction failed, using rules: %s', error)
        return [self._extract_rules(text, subject)]

    _LABELS = [
        r'name of (?:the )?work', r'description of (?:the )?work', r'work description', r'tender title', r'title',
        r'tender (?:id|no\.?|number|reference)', r'nit no\.?', r'e-?tender no\.?', r'ref(?:erence)? no\.?',
        r'organi[sz]ation(?: name)?', r'department', r'tender inviting authority', r'buyer', r'location',
        r'place of work', r'district', r'estimated (?:contract )?(?:cost|value)', r'tender value',
        r'value of (?:the )?work', r'ecv', r'approximate cost', r'emd(?: amount)?', r'earnest money(?: deposit)?',
        r'bid security', r'tender fee', r'document fee', r'cost of (?:tender|bid) document',
        r'bid submission (?:start|end) date', r'last date(?: and time)?(?: of| for)?(?: submission| bid)?',
        r'closing date', r'due date', r'bid opening date', r'technical bid opening', r'opening date',
        r'published date', r'publish(?:ing)? date', r'date of issue', r'completion period',
        r'period of completion', r'contract period', r'time allowed', r'pre[- ]?bid meeting', r'category',
        r'product category', r'tender type', r'form of contract', r'bid number', r'bid no\.?', r'item category',
        r'items?', r'estimated bid value', r'bid end date', r'bid opening date/time', r'ministry', r'office name',
        r'quantity', r'contract period',
    ]

    @api.model
    def _extract_rules(self, text, subject=''):
        """Regex extraction for NIT / alert e-mails of Indian portals (used when no AI is configured)."""
        body = tu.clean_text(text)
        stop = r'(?=\s+(?:%s)\s*[:\-\u2013]|\s+https?://|\s*$)' % '|'.join(self._LABELS)

        def grab(patterns, length=300):
            for pattern in patterns:
                m = re.search(r'(?<![a-z])' + pattern + r'\s*[:\-\u2013]\s*(.{1,%d}?)' % length + stop, body, re.I)
                if m and m.group(1).strip(' .;,'):
                    return m.group(1).strip(' .;,')
            return ''

        url = re.search(r'https?://[^\s<>"\']+', text or '')
        title = grab([r'name of (?:the )?work', r'description of (?:the )?work', r'work description',
                      r'tender title', r'title', r'item category', r'items?']) or tu.clean_text(subject)
        return {
            'title': title,
            'reference': grab([r'tender (?:id|no\.?|number|reference)', r'bid number', r'bid no\.?', r'nit no\.?',
                               r'e-?tender no\.?', r'ref(?:erence)? no\.?'], 80),
            'organisation': grab([r'organi[sz]ation(?: name)?', r'tender inviting authority', r'department',
                                  r'buyer'], 160),
            'location': grab([r'location', r'place of work', r'district'], 100),
            'estimated_value': tu.parse_indian_amount(grab([r'estimated bid value', r'estimated (?:contract )?(?:cost|value)',
                                                            r'tender value', r'value of (?:the )?work',
                                                            r'ecv', r'approximate cost'], 60)),
            'emd': tu.parse_indian_amount(grab([r'emd(?: amount)?', r'earnest money(?: deposit)?',
                                                r'bid security'], 60)),
            'tender_fee': tu.parse_indian_amount(grab([r'tender fee', r'document fee',
                                                       r'cost of (?:tender|bid) document'], 60)),
            'closing_date': grab([r'bid submission end date', r'bid end date',
                                  r'last date(?: and time)?(?: of| for)?(?: submission| bid)?',
                                  r'closing date', r'due date'], 40),
            'opening_date': grab([r'bid opening date', r'technical bid opening', r'opening date'], 40),
            'published_date': grab([r'published date', r'publish(?:ing)? date', r'date of issue'], 40),
            'completion_period': grab([r'completion period', r'period of completion', r'contract period',
                                       r'time allowed'], 40),
            'url': url.group(0) if url else None,
            'description': body[:600],
        }
