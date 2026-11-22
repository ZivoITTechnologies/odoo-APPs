# -*- coding: utf-8 -*-
import json
import logging
from urllib.parse import quote_plus, urljoin

import requests
from lxml import etree, html as lxml_html

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from . import tender_utils as tu

_logger = logging.getLogger(__name__)

USER_AGENT = 'Mozilla/5.0 (compatible; ZivoIT-TenderAI/19.0; +https://www.zivoit.com)'


class TenderSource(models.Model):
    _name = 'zivoit.tender.source'
    _description = 'Tender Source / Portal Connector'
    _inherit = ['mail.thread']
    _order = 'sequence, name'

    name = fields.Char(required=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    source_type = fields.Selection([
        ('ai_web', 'AI Web Search Agent'),
        ('rss', 'RSS / Atom Feed'),
        ('html', 'HTML Listing Page'),
        ('json_api', 'JSON API / Aggregator'),
        ('email', 'Tender Alert E-mails'),
        ('manual', 'Manual / Excel Import'),
    ], required=True, default='ai_web', tracking=True)
    portal = fields.Selection([
        ('cppp', 'CPPP - eprocure.gov.in'),
        ('ireps', 'IREPS - Indian Railways'),
        ('gem', 'GeM'),
        ('telangana', 'Telangana e-Procurement'),
        ('andhra', 'AP e-Procurement'),
        ('nhai', 'NHAI / MoRTH'),
        ('cpwd', 'CPWD'),
        ('state_pwd', 'State PWD / R&B'),
        ('aggregator', 'Tender Aggregator'),
        ('other', 'Other'),
    ], default='other', required=True)
    url = fields.Char(
        'Endpoint URL',
        help='Feed / page / API URL. Placeholders {keyword}, {state} and {query} are replaced at run time.')
    domains = fields.Char(
        'Website Domains',
        help='Comma separated domains the AI web-search agent is restricted to, e.g. ireps.gov.in')
    search_hint = fields.Char(
        help='Extra words appended to the AI search for this portal, e.g. "IREPS open tenders works"')
    default_state_id = fields.Many2one('res.country.state', domain="[('country_id.code', '=', 'IN')]")
    default_zone_id = fields.Many2one('zivoit.tender.zone')
    default_organisation = fields.Char()
    default_sector_ids = fields.Many2many('zivoit.tender.sector', string='Default Sectors')

    # HTTP options
    http_method = fields.Selection([('GET', 'GET'), ('POST', 'POST')], default='GET')
    request_headers = fields.Text(help='JSON object of extra HTTP headers (e.g. API keys)')
    request_body = fields.Text(help='JSON body for POST requests; placeholders allowed')
    timeout = fields.Integer(default=60)
    verify_ssl = fields.Boolean(default=True)

    # HTML & JSON mappings
    row_xpath = fields.Char('Row XPath', default='//table//tr[td]',
                            help='XPath selecting one element per tender on the listing page')
    field_mapping = fields.Text(
        help='JSON mapping of tender field -> XPath (HTML, relative to the row) or key path (JSON, dot notation). '
             'Keys: title, reference, organisation, department, location, state, estimated_value, emd, '
             'tender_fee, published_date, closing_date, opening_date, completion_period, url, description')
    results_path = fields.Char('JSON Results Path', help='Dot path to the list of tenders, e.g. data.items')

    # monitoring
    last_run = fields.Datetime(readonly=True)
    last_status = fields.Selection([('ok', 'Success'), ('error', 'Error')], readonly=True)
    last_message = fields.Text(readonly=True)
    last_count = fields.Integer('Last Found', readonly=True)
    log_ids = fields.One2many('zivoit.tender.fetch.log', 'source_id', readonly=True)
    tender_ids = fields.One2many('zivoit.tender', 'source_id')
    tender_count = fields.Integer(compute='_compute_tender_count')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    def _compute_tender_count(self):
        data = self.env['zivoit.tender']._read_group([('source_id', 'in', self.ids)], ['source_id'], ['__count'])
        counts = {source.id: count for source, count in data}
        for source in self:
            source.tender_count = counts.get(source.id, 0)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _json_field(self, name):
        value = self[name]
        if not value:
            return {}
        try:
            data = json.loads(value)
        except ValueError as error:
            raise UserError(_('Invalid JSON in "%(field)s" of source %(source)s: %(err)s',
                              field=self._fields[name].string, source=self.name, err=error))
        return data if isinstance(data, dict) else {}

    def _expand(self, template, query, filters):
        if not template:
            return template
        keyword = ' '.join((filters.get('keywords') or [])[:3]) or query or ''
        state = (filters.get('states') or [''])[0]
        return (template.replace('{keyword}', quote_plus(keyword))
                .replace('{query}', quote_plus(query or ''))
                .replace('{state}', quote_plus(state)))

    def _http(self, query, filters):
        self.ensure_one()
        if not self.url:
            raise UserError(_('Source %s has no endpoint URL.', self.name))
        headers = {'User-Agent': USER_AGENT, 'Accept': '*/*'}
        headers.update({str(k): str(v) for k, v in self._json_field('request_headers').items()})
        url = self._expand(self.url, query, filters)
        if self.http_method == 'POST':
            body = self._expand(self.request_body or '{}', query, filters)
            response = requests.post(url, data=body.encode('utf-8'), headers=dict(headers, **{'Content-Type': 'application/json'}),
                                     timeout=self.timeout or 60, verify=self.verify_ssl)
        else:
            response = requests.get(url, headers=headers, timeout=self.timeout or 60, verify=self.verify_ssl)
        response.raise_for_status()
        return response

    @staticmethod
    def _dig(data, path):
        if not path:
            return data
        for part in path.split('.'):
            if isinstance(data, dict):
                data = data.get(part)
            elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
                data = data[int(part)]
            else:
                return None
        return data

    # ------------------------------------------------------------------
    # connectors
    # ------------------------------------------------------------------
    def _fetch(self, query='', filters=None):
        """Return a list of raw tender dicts from this source."""
        self.ensure_one()
        filters = filters or {}
        method = getattr(self, '_fetch_%s' % self.source_type, None)
        if not method:
            return []
        return method(query, filters) or []

    def _fetch_email(self, query, filters):
        return []  # e-mails arrive through the mail gateway (see zivoit.tender.message_new)

    def _fetch_manual(self, query, filters):
        return []

    def _fetch_ai_web(self, query, filters):
        ai = self.env['zivoit.tender.ai']
        if not ai._is_enabled():
            raise UserError(_('Configure an AI provider (Claude or OpenAI) in Tender AI settings to use the AI web-search agent.'))
        search = ' '.join(filter(None, [query, self.search_hint]))
        domains = [d.strip() for d in (self.domains or '').split(',') if d.strip()] or None
        return ai.search_tenders_web(search, allowed_domains=domains, filters=filters)

    def _fetch_rss(self, query, filters):
        response = self._http(query, filters)
        root = etree.fromstring(response.content, parser=etree.XMLParser(recover=True, resolve_entities=False, no_network=True))
        if root is None:
            return []
        rows = []
        items = root.xpath('//*[local-name()="item"] | //*[local-name()="entry"]')
        ai = self.env['zivoit.tender.ai']
        for item in items:
            def text_of(tag):
                nodes = item.xpath('./*[local-name()="%s"]' % tag)
                if not nodes:
                    return ''
                node = nodes[0]
                if tag == 'link' and node.get('href'):
                    return node.get('href')
                return ''.join(node.itertext()).strip()
            title = tu.clean_text(text_of('title'))
            if not title:
                continue
            description = text_of('description') or text_of('summary') or text_of('content')
            extracted = ai._extract_rules(description, title)
            row = {k: v for k, v in extracted.items() if v}
            row.update({
                'title': title,
                'url': text_of('link') or extracted.get('url'),
                'published_date': text_of('pubDate') or text_of('published') or text_of('updated')
                or extracted.get('published_date'),
                'description': tu.clean_text(description)[:2000],
                'reference': text_of('guid') if not extracted.get('reference') and len(text_of('guid')) < 60
                else extracted.get('reference'),
            })
            rows.append(row)
        return rows

    def _fetch_html(self, query, filters):
        response = self._http(query, filters)
        doc = lxml_html.fromstring(response.content)
        mapping = self._json_field('field_mapping') or {
            'title': './td[2]//text()', 'reference': './td[3]//text()',
            'closing_date': './td[4]//text()', 'url': './/a/@href',
        }
        rows = []
        for node in doc.xpath(self.row_xpath or '//table//tr[td]'):
            row = {}
            for key, xpath in mapping.items():
                try:
                    found = node.xpath(xpath)
                except etree.XPathError:
                    raise UserError(_('Invalid XPath for "%(key)s" in source %(source)s', key=key, source=self.name))
                if isinstance(found, list):
                    value = ' '.join(str(v).strip() for v in found if str(v).strip())
                else:
                    value = str(found)
                row[key] = tu.clean_text(value)
            if row.get('url'):
                row['url'] = urljoin(response.url or self.url, row['url'].split(' ')[0])
            if row.get('title'):
                rows.append(row)
        return rows

    def _fetch_json_api(self, query, filters):
        response = self._http(query, filters)
        data = response.json()
        items = self._dig(data, self.results_path) if self.results_path else data
        if isinstance(items, dict):
            items = items.get('results') or items.get('items') or items.get('data') or [items]
        mapping = self._json_field('field_mapping')
        rows = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if mapping:
                row = {key: self._dig(item, path) for key, path in mapping.items()}
            else:
                row = dict(item)
            if row.get('title'):
                rows.append(row)
        return rows

    # ------------------------------------------------------------------
    # orchestration
    # ------------------------------------------------------------------
    def _run(self, query='', filters=None, profile=False):
        """Fetch + ingest for each source. Returns tenders created or updated."""
        Tender = self.env['zivoit.tender']
        all_tenders = Tender.browse()
        for source in self:
            if source.source_type in ('email', 'manual'):
                continue
            log_vals = {'source_id': source.id, 'profile_id': profile.id if profile else False,
                        'query': (query or '')[:500]}
            try:
                with self.env.cr.savepoint():
                    rows = source._fetch(query, filters or {})
                    tenders, created, updated = Tender._ingest_rows(rows, source=source)
                all_tenders |= tenders
                log_vals.update(status='ok', found=len(rows), created=created, updated=updated,
                                message=_('%(found)s found, %(new)s new, %(upd)s updated',
                                          found=len(rows), new=created, upd=updated))
                source.write({'last_run': fields.Datetime.now(), 'last_status': 'ok',
                              'last_count': len(rows), 'last_message': log_vals['message']})
            except Exception as error:  # noqa: BLE001 - one broken portal must not stop the others
                _logger.warning('Tender source %s failed: %s', source.name, error)
                message = str(error)[:2000]
                log_vals.update(status='error', message=message)
                source.write({'last_run': fields.Datetime.now(), 'last_status': 'error',
                              'last_count': 0, 'last_message': message})
            self.env['zivoit.tender.fetch.log'].sudo().create(log_vals)
        return all_tenders

    def action_fetch_now(self):
        tenders = self._run()
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': _('Tender fetch finished'),
                'message': _('%(count)s tenders found. %(status)s', count=len(tenders),
                             status=self[:1].last_message or ''),
                'type': 'success' if all(s.last_status != 'error' for s in self) else 'warning',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

    def action_view_tenders(self):
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_tender_ai.action_zivoit_tender')
        action['domain'] = [('source_id', 'in', self.ids)]
        action['context'] = {}
        return action

    @api.model
    def _cron_fetch_all(self):
        self.search([('source_type', 'in', ('rss', 'html', 'json_api'))])._run()


class TenderFetchLog(models.Model):
    _name = 'zivoit.tender.fetch.log'
    _description = 'Tender Fetch Log'
    _order = 'id desc'

    source_id = fields.Many2one('zivoit.tender.source', ondelete='cascade', index=True)
    profile_id = fields.Many2one('zivoit.tender.profile', ondelete='set null')
    date = fields.Datetime(default=fields.Datetime.now, required=True)
    status = fields.Selection([('ok', 'Success'), ('error', 'Error')], required=True, default='ok')
    query = fields.Char()
    found = fields.Integer()
    created = fields.Integer('New')
    updated = fields.Integer()
    message = fields.Text()

    @api.autovacuum
    def _gc_old_logs(self):
        limit = fields.Datetime.subtract(fields.Datetime.now(), days=60)
        self.sudo().search([('date', '<', limit)]).unlink()
