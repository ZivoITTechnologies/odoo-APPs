# -*- coding: utf-8 -*-
"""Odoo facade of the logistics AI: combines the local explainable engine with the company's
history and (optionally) an LLM. Every LLM call is optional and falls back silently."""
import json
import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.modules import module as odoo_module

from ..ai import engine
from ..ai.india import zone_for
from ..ai.llm import LLMClient, LLMError
from ..providers.base import NORMALIZED_STATUSES, STATUS_CODES

_logger = logging.getLogger(__name__)
PARAM = 'zivoit_logistics_hub.%s'


class LogisticsAI(models.AbstractModel):
    _name = 'zivo.logistics.ai'
    _description = 'Logistics AI Services'

    # ------------------------------------------------------------------ configuration
    def _param(self, key, default=None):
        return self.env['ir.config_parameter'].sudo().get_param(PARAM % key, default)

    def _llm(self):
        provider = self._param('ai_provider', 'local')
        api_key = self._param('ai_api_key')
        if provider in (None, '', 'local') or not api_key:
            return None
        if odoo_module.current_test and not self.env.context.get('zivo_allow_llm_in_test'):
            return None
        return LLMClient(provider, api_key, model=self._param('ai_model') or None,
                         base_url=self._param('ai_base_url') or None, timeout=int(self._param('ai_timeout', 20)))

    def _llm_json(self, system, user):
        client = self._llm()
        if not client:
            return None
        try:
            return client.complete(system, user, want_json=True)
        except (LLMError, Exception) as exc:  # noqa: BLE001 - AI must never break logistics
            _logger.info('ZivoIT Logistics AI: LLM unavailable, using local engine (%s)', exc)
            return None

    @api.model
    def _zone(self, origin_zip, dest_zip):
        return zone_for(origin_zip, dest_zip)

    # ------------------------------------------------------------------ status classification
    @api.model
    def _classify_status(self, provider, raw_status, description=''):
        text = ' '.join(x for x in (raw_status, description) if x)
        local_status, local_conf = engine.classify_status(text)
        if local_conf >= 0.7:
            return local_status, local_conf, 'ai'
        data = self._llm_json(
            'You map courier tracking statuses from Indian and global carriers to a fixed vocabulary. '
            'Answer with JSON {"status": <one of %s>, "confidence": 0..1}.' % ', '.join(sorted(STATUS_CODES)),
            'Carrier: %s\nRaw status: %s\nDescription: %s' % (provider, raw_status, description))
        if isinstance(data, dict) and data.get('status') in STATUS_CODES:
            return data['status'], float(data.get('confidence') or 0.7), 'ai'
        return local_status, local_conf, 'ai'

    # ------------------------------------------------------------------ address intelligence
    @api.model
    def _partner_address_dict(self, partner):
        return {
            'name': partner.name or (partner.parent_id.name if partner.parent_id else ''),
            'street': partner.street or '', 'street2': partner.street2 or '', 'city': partner.city or '',
            'state_code': partner.state_id.code or '', 'zip': partner.zip or '',
            'phone': partner.phone or getattr(partner, 'mobile', '') or '', 'email': partner.email or '',
            'country_code': (partner.country_id.code or 'IN').upper(),
        }

    @api.model
    def _assess_partner(self, partner):
        result = engine.assess_address(self._partner_address_dict(partner))
        result['text'] = '\n'.join('[%s] %s' % (sev.upper(), msg) for sev, msg in result['issues'] if sev != 'info') or ''
        return result

    @api.model
    def _ai_fix_address(self, partner):
        """LLM powered address repair (structured). Returns dict of suggested field values or {}."""
        addr = self._partner_address_dict(partner)
        data = self._llm_json(
            'You clean Indian delivery addresses for couriers. Return JSON with keys street, street2, city, '
            'zip, state_code (Indian state code like TS, KA, MH), landmark, issues (list of strings). '
            'Never invent a house number or phone number; keep unknown values empty.',
            json.dumps(addr))
        if not isinstance(data, dict):
            return {}
        out = {k: (data.get(k) or '').strip() for k in ('street', 'street2', 'city', 'zip', 'state_code')}
        if data.get('landmark') and data['landmark'].lower() not in (out['street2'] or '').lower():
            out['street2'] = ', '.join(x for x in (out['street2'], 'Near %s' % data['landmark']) if x)
        out['issues'] = data.get('issues') or []
        return out

    # ------------------------------------------------------------------ RTO risk
    @api.model
    def _customer_history(self, partner):
        if not partner:
            return 0, 0
        commercial = partner.commercial_partner_id
        domain = ['|', ('partner_id', 'child_of', commercial.id), ('dest_phone', '=', engine.clean_phone(partner.phone or ''))] \
            if partner.phone else [('partner_id', 'child_of', commercial.id)]
        counts = dict(self.env['zivo.logistics.shipment'].sudo()._read_group(domain, ['state'], ['__count']))
        delivered = counts.get('delivered', 0)
        rto = sum(counts.get(s, 0) for s in ('rto_initiated', 'rto_in_transit', 'rto_delivered'))
        return delivered, rto

    @api.model
    def _pincode_stats(self, dest_zip):
        if not dest_zip:
            return 0.0, 0
        counts = dict(self.env['zivo.logistics.shipment'].sudo()._read_group(
            [('dest_zip', '=', dest_zip), ('is_final', '=', True), ('state', '!=', 'cancelled')], ['state'], ['__count']))
        rto = sum(counts.get(s, 0) for s in ('rto_delivered',))
        total = sum(counts.values())
        return (rto / total if total else 0.0), total

    @api.model
    def _risk_values(self, partner, amount, is_cod, order_dt=None, dest_zip=None):
        """Return shipment/order field values for RTO risk + address quality."""
        if not partner:
            return {}
        address = self._assess_partner(partner)
        delivered, rto = self._customer_history(partner)
        pin_rate, pin_samples = self._pincode_stats(dest_zip or partner.zip)
        local_dt = fields.Datetime.context_timestamp(self, order_dt) if order_dt else None
        risk = engine.rto_risk({
            'is_cod': is_cod, 'order_value': amount or 0.0, 'address_score': address['score'],
            'phone_valid': engine.phone_is_valid(partner.phone or '', (partner.country_id.code or 'IN').upper()),
            'customer_delivered': delivered, 'customer_rto': rto, 'pincode_rto_rate': pin_rate,
            'pincode_samples': pin_samples, 'dest_state': partner.state_id.code or '',
            'order_hour': local_dt.hour if local_dt else None, 'has_email': bool(partner.email),
        })
        return {
            'rto_risk': risk['score'], 'rto_risk_level': risk['level'],
            'rto_risk_reasons': '\n'.join('- %s' % r for r in risk['reasons']),
            'address_score': address['score'], 'address_issues': address['text'],
        }

    # ------------------------------------------------------------------ carrier recommendation
    @api.model
    def _lane_performance(self, accounts, dest_zip, dest_state=None):
        """Historical performance per account for the destination (pincode -> state fallback)."""
        Shipment = self.env['zivo.logistics.shipment'].sudo()
        since = fields.Datetime.now() - timedelta(days=180)
        perf = {}
        for level, domain in (('pincode', [('dest_zip', '=', dest_zip)]),
                              ('state', [('dest_state_id', '=', dest_state.id)] if dest_state else None),
                              ('all', [])):
            if domain is None:
                continue
            rows = Shipment._read_group(
                [('account_id', 'in', accounts.ids), ('create_date', '>=', since), ('is_final', '=', True),
                 ('state', '!=', 'cancelled')] + domain, ['account_id', 'state'], ['__count'])
            agg = {}
            for account, state, count in rows:
                agg.setdefault(account.id, {}).setdefault(state, 0)
                agg[account.id][state] += count
            for acc_id, counts in agg.items():
                total = sum(counts.values())
                if acc_id in perf or total < (5 if level != 'all' else 1):
                    continue
                perf[acc_id] = {
                    'samples': total, 'level': level,
                    'delivered_rate': counts.get('delivered', 0) / total,
                    'rto_rate': counts.get('rto_delivered', 0) / total,
                }
        # delay vs promise (delivered shipments)
        delays = Shipment._read_group(
            [('account_id', 'in', accounts.ids), ('state', '=', 'delivered'), ('create_date', '>=', since),
             ('edd', '!=', False), ('delivered_at', '!=', False)], ['account_id'], ['id:recordset'])
        for account, recs in delays:
            late = [max((s.delivered_at - s.edd).total_seconds() / 86400.0, 0) for s in recs]
            if account.id in perf and late:
                perf[account.id]['avg_delay_days'] = sum(late) / len(late)
        return perf

    @api.model
    def _recommend(self, quotes, request, strategy='ai_best', dest_state=None):
        """quotes: list of (account, RateOption). Returns ranked list of dicts."""
        if not quotes:
            return []
        accounts = self.env['zivo.logistics.account'].browse({a.id for a, _q in quotes})
        perf = self._lane_performance(accounts, request.delivery.zip, dest_state)
        company_currency = self.env.company.currency_id
        options = []
        for account, quote in quotes:
            currency = self.env['res.currency'].search([('name', '=', quote.currency)], limit=1) or account.rate_currency_id
            price = currency._convert(quote.price, company_currency, account.company_id, fields.Date.today()) \
                if currency and currency != company_currency else quote.price
            options.append({'key': (account.id, quote.service_code), 'account': account, 'quote': quote,
                            'price': price, 'tat_days': quote.tat_days, 'perf': perf.get(account.id)})
        return engine.score_rates(options, strategy=strategy, is_cod=request.is_cod)

    # ------------------------------------------------------------------ EDD
    @api.model
    def _predict_tat(self, account, dest_zip, dest_state, zone, carrier_tat=0.0):
        Shipment = self.env['zivo.logistics.shipment'].sudo()
        base = [('account_id', '=', account.id), ('state', '=', 'delivered'), ('transit_days', '>', 0)]
        history = Shipment.search(base + [('dest_zip', '=', dest_zip)], limit=200, order='id desc').mapped('transit_days')
        if len(history) < 5 and dest_state:
            history = Shipment.search(base + [('dest_state_id', '=', dest_state.id)], limit=200, order='id desc').mapped('transit_days')
        return engine.predict_tat(history, carrier_tat, zone or 'national')

    # ------------------------------------------------------------------ NDR assistant
    @api.model
    def _ndr_assist(self, shipment, reason):
        local = engine.classify_ndr(reason)
        amount = ''
        if shipment.payment_mode == 'cod':
            amount = '%s %.2f' % (shipment.currency_id.symbol or '', shipment.cod_amount)
        ref = (shipment.sale_id.name if shipment.sale_id else shipment.name)
        message = engine.customer_message(local['category'], shipment.partner_id.name, ref, amount)
        data = self._llm_json(
            'You are an Indian e-commerce logistics NDR (non-delivery) specialist. Classify the failed delivery and '
            'suggest the next action. Return JSON {"category": one of %s, "action": one of %s, "advice": short '
            'advice for the operations team, "message": a polite WhatsApp/SMS message to the customer (max 300 chars, '
            'no emojis, mention order reference)}.' % (list(engine.NDR_LABELS), [a for a, _l in
                                                       self.env['zivo.logistics.shipment']._fields['ndr_suggested_action'].selection]),
            json.dumps({'reason': reason, 'attempts': shipment.ndr_count + 1, 'payment_mode': shipment.payment_mode,
                        'cod_amount': amount, 'customer': shipment.partner_id.name, 'order_ref': ref,
                        'rto_risk': shipment.rto_risk, 'address_issues': shipment.address_issues}))
        if isinstance(data, dict) and data.get('category') in engine.NDR_LABELS:
            local.update({k: data[k] for k in ('category', 'action', 'advice') if data.get(k)})
            if local['action'] not in dict(self.env['zivo.logistics.shipment']._fields['ndr_suggested_action'].selection):
                local['action'] = 'contact_customer'
            message = (data.get('message') or message)[:600]
        # third failed attempt on a high-risk COD shipment: recommend RTO to cut losses
        if shipment.ndr_count + 1 >= 3 and local['action'] == 'reattempt' and shipment.rto_risk >= 60:
            local['action'] = 'rto'
            local['advice'] += _(' Third failed attempt on a high-risk COD order: RTO recommended.')
        local['message'] = message
        return local

    # ------------------------------------------------------------------ natural language search
    @api.model
    def _nl_domain(self, question):
        accounts = self.env['zivo.logistics.account'].search([])
        names = accounts.mapped('name') + [n for n in set(accounts.mapped('provider_label')) if n]
        filters = None
        data = self._llm_json(
            'Translate a logistics manager question into JSON filters for shipments. Keys: states (list of %s), '
            'carriers (list of names from %s), cod (true/false/null), delayed (bool), high_risk (bool), '
            'weight_dispute (bool), days_back (int or null), city (string or null). Only use these keys.'
            % (sorted(STATUS_CODES), names), question)
        if isinstance(data, dict):
            filters = {
                'states': [s for s in data.get('states') or [] if s in STATUS_CODES],
                'carriers': [c for c in data.get('carriers') or [] if c in names],
                'cod': data.get('cod') if data.get('cod') in (True, False) else None,
                'delayed': bool(data.get('delayed')), 'high_risk': bool(data.get('high_risk')),
                'weight_dispute': bool(data.get('weight_dispute')),
                'date_from': fields.Datetime.now() - timedelta(days=int(data['days_back'])) if isinstance(data.get('days_back'), int) else None,
                'city': data.get('city') if isinstance(data.get('city'), str) else None,
            }
        if filters is None:
            filters = engine.parse_nl_query(question, names, now=fields.Datetime.now())
        domain = []
        if filters['states']:
            domain.append(('state', 'in', filters['states']))
        if filters['carriers']:
            matched = accounts.filtered(lambda a: a.name in filters['carriers'] or a.provider_label in filters['carriers'])
            domain.append(('account_id', 'in', matched.ids))
        if filters['cod'] is not None:
            domain.append(('payment_mode', '=', 'cod' if filters['cod'] else 'prepaid'))
        if filters['delayed']:
            domain.append(('is_delayed', '=', True))
        if filters['high_risk']:
            domain.append(('rto_risk_level', '=', 'high'))
        if filters['weight_dispute']:
            domain.append(('weight_dispute', '=', True))
        if filters['date_from']:
            domain.append(('create_date', '>=', filters['date_from']))
        if filters['city']:
            domain.append('|')
            domain.append(('dest_city', 'ilike', filters['city']))
            domain.append(('dest_state_id.name', 'ilike', filters['city']))
        return domain, filters

    @api.model
    def _status_label(self, code):
        return dict(NORMALIZED_STATUSES).get(code, code)
