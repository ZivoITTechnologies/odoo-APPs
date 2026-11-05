# -*- coding: utf-8 -*-
"""AI layer: matching, anomaly guard, error doctor, reviews and health summaries.

The deterministic engine (lib.matching / lib.anomaly / lib.error_kb) always runs.
When an LLM provider is configured it is consulted only for ambiguous cases and
for natural-language explanations, with amounts withheld unless allowed.
"""
import json
import logging
import re
from datetime import timedelta

from markupsafe import Markup, escape

from odoo import _, fields, models

from ..lib import anomaly, llm, matching

_logger = logging.getLogger(__name__)

SYSTEM_PROMPT = ('You are an expert Indian chartered accountant and TallyPrime/Odoo '
                 'integration engineer working inside the ZivoIT Tally Connector. Be precise, '
                 'brief and practical. Never invent data.')


class TallyConnectionAI(models.Model):
    _inherit = 'tally.connection'

    # ------------------------------------------------------------------
    def _ai_enabled(self):
        return self.ai_provider and self.ai_provider != 'none'

    def _ai_complete(self, prompt, max_tokens=900):
        self.ensure_one()
        conf = self.sudo()
        return llm.complete(conf.ai_provider, conf.ai_api_key, conf.ai_model, prompt,
                            system=SYSTEM_PROMPT, base_url=conf.ai_base_url,
                            max_tokens=max_tokens)

    def action_ai_test(self):
        self.ensure_one()
        try:
            out = self._ai_complete('Reply with the single word: ready', max_tokens=10)
        except llm.LLMError as e:
            return self._notify(str(e), 'danger')
        return self._notify(_('AI provider answered: %s') % out.strip()[:60], 'success')

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------
    def _candidates(self, kind):
        company = self.company_id
        if kind == 'party':
            partners = self.env['res.partner'].search(
                [('is_company', '=', True), ('company_id', 'in', (False, company.id))]) | \
                self.env['res.partner'].search([('parent_id', '=', False),
                                                ('company_id', 'in', (False, company.id))])
            return [{'rec': p, 'name': p.name, 'gstin': p.vat or '', 'email': p.email or '',
                     'phone': p.phone or '', 'pan': (p.vat or '')[2:12]} for p in partners]
        if kind == 'item':
            has_hsn = 'l10n_in_hsn_code' in self.env['product.product']._fields
            return [{'rec': p, 'name': p.with_context(display_default_code=False).display_name,
                     'code': p.default_code or '',
                     'hsn': (p.l10n_in_hsn_code if has_hsn else '') or ''}
                    for p in self.env['product.product'].search(
                        [('company_id', 'in', (False, company.id))])]
        if kind == 'ledger':
            return [{'rec': a, 'name': a.name, 'code': a.code, 'account_type': a.account_type}
                    for a in self.env['account.account'].with_company(company).search(
                        [('company_ids', 'in', company.id), ('active', '=', True)])]
        if kind == 'bank':
            return [{'rec': j, 'name': j.name,
                     'acc': (j.bank_account_id.acc_number or '') if j.bank_account_id else ''}
                    for j in self.env['account.journal'].search(
                        [('company_id', '=', company.id), ('type', 'in', ('bank', 'cash'))])]
        return []

    def _match_tax(self, m):
        head, rate = matching.gst_head_and_rate(m.tally_name)
        if not head:
            return None, 0.0, ''
        if rate is None:
            data = json.loads(m.tally_data or '{}')
            rate = data.get('rate') or None
        name = m.tally_name.lower()
        uses = ['purchase'] if 'input' in name else ['sale'] if 'output' in name else \
            ['sale', 'purchase']
        Tax = self.env['account.tax']
        dom = [('company_id', '=', self.company_id.id), ('amount_type', '=', 'percent'),
               ('name', 'ilike', head)]
        if rate is not None:
            dom.append(('amount', '=', rate))
        taxes = Tax.search(dom)
        for use in uses:
            # child taxes of groups carry type_tax_use 'none'; check their parents' usage
            for t in taxes:
                parents = Tax.search([('children_tax_ids', 'in', t.id)])
                if t.type_tax_use == use or any(p.type_tax_use == use for p in parents):
                    return t, (0.97 if rate is not None else 0.75), '%s %s%%' % (head, rate)
        return (taxes[:1], 0.6, '%s rate only' % head) if taxes else (None, 0.0, '')

    def _ai_match(self, mappings, force=False):
        """Fill AI suggestions/confirmations for unmapped Tally masters."""
        if not mappings:
            return
        self.ensure_one()
        hi, lo = self.ai_match_threshold or 0.92, self.ai_suggest_threshold or 0.6
        used = {k: set() for k in ('party', 'item', 'bank')}
        for m in self.env['tally.mapping'].search([('connection_id', '=', self.id),
                                                   ('state', '=', 'confirmed')]):
            t = m._target()
            if t and m.kind in used:
                used[m.kind].add(t.id)
        cache, ambiguous = {}, []
        field_of = {'party': 'partner_id', 'item': 'product_id', 'ledger': 'account_id',
                    'bank': 'journal_id', 'tax': 'tax_id'}
        for m in mappings:
            if m.kind not in field_of or (m._target() and not force):
                continue
            if m.kind == 'tax':
                tax, score, why = self._match_tax(m)
                if tax:
                    m.write({'tax_id': tax.id, 'confidence': score, 'match_reason': why,
                             'state': 'confirmed' if score >= hi else 'suggested',
                             'origin': 'ai'})
                continue
            data = json.loads(m.tally_data or '{}')
            src = {'name': m.tally_name, 'gstin': m.tally_gstin or data.get('gstin', ''),
                   'email': data.get('email', ''), 'phone': data.get('phone', ''),
                   'mobile': data.get('mobile', ''), 'pan': data.get('pan', ''),
                   'part_no': data.get('part_no', ''), 'hsn': data.get('hsn', ''),
                   'role': m.tally_role}
            if m.kind not in cache:
                cache[m.kind] = self._candidates(m.kind)
            taken = used.get(m.kind, set())
            cands = cache[m.kind]
            scorer = {'party': matching.score_party, 'item': matching.score_product,
                      'ledger': matching.score_account,
                      'bank': lambda s, c: (matching.name_similarity(s['name'], c['name']) if not (
                          c['acc'] and c['acc'][-4:] in s['name']) else 0.95, 'name/account no')
                      }[m.kind]
            best = matching.best_matches(src, cands, scorer)
            if not best or best[0][0] < lo:
                m.write({'state': 'unmapped', 'confidence': best[0][0] if best else 0.0})
                continue
            score, why, cand = best[0]
            clear_winner = len(best) == 1 or best[0][0] - best[1][0] >= 0.05
            # a record already linked to another Tally master is only ever suggested
            state = 'confirmed' if score >= hi and clear_winner and \
                cand['rec'].id not in taken else 'suggested'
            m.write({field_of[m.kind]: cand['rec'].id, 'confidence': score, 'match_reason': why,
                     'state': state, 'origin': 'ai',
                     'alternatives': '\n'.join('%.0f%% %s (%s)' % (s * 100, c['name'], w)
                                               for s, w, c in best[1:])})
            if state == 'confirmed' and m.kind in used:
                used[m.kind].add(cand['rec'].id)
            elif state == 'suggested':
                ambiguous.append((m, best))
        if ambiguous and self._ai_enabled():
            self._ai_llm_disambiguate(ambiguous, hi)

    def _ai_llm_disambiguate(self, ambiguous, hi):
        field_of = {'party': 'partner_id', 'item': 'product_id', 'ledger': 'account_id',
                    'bank': 'journal_id'}
        for i in range(0, len(ambiguous), 25):
            chunk = ambiguous[i:i + 25]
            payload = [{'id': m.id, 'tally': m.tally_name, 'kind': m.kind,
                        'tally_group': m.tally_parent or '',
                        'candidates': [c['name'] for _s, _w, c in best]} for m, best in chunk]
            prompt = (
                'Match each Tally master to the best Odoo candidate (index) or null if none is '
                'the same real-world entity. Consider abbreviations, legal suffixes and '
                'Indian business naming. Return JSON list of {"id":int,"choice":int|null,'
                '"confidence":0..1,"reason":str}.\n' + json.dumps(payload, ensure_ascii=False))
            try:
                answer = llm.extract_json(self._ai_complete(prompt))
            except llm.LLMError as e:
                _logger.info('AI disambiguation skipped: %s', e)
                return
            by_id = {m.id: (m, best) for m, best in chunk}
            for row in answer if isinstance(answer, list) else []:
                m, best = by_id.get(row.get('id'), (None, None))
                if not m:
                    continue
                choice = row.get('choice')
                conf = float(row.get('confidence') or 0)
                if choice is None:
                    m.write({field_of[m.kind]: False, 'state': 'unmapped',
                             'match_reason': 'AI: %s' % (row.get('reason') or 'no match')})
                elif 0 <= int(choice) < len(best):
                    cand = best[int(choice)][2]
                    m.write({field_of[m.kind]: cand['rec'].id,
                             'state': 'confirmed' if conf >= hi else 'suggested',
                             'confidence': conf,
                             'match_reason': 'AI: %s' % (row.get('reason') or '')})

    def action_ai_match_all(self):
        for conn in self:
            maps = self.env['tally.mapping'].search([('connection_id', '=', conn.id),
                                                     ('state', 'in', ('unmapped', 'suggested'))])
            conn._ai_match(maps, force=True)
        return self._notify(_('AI matching finished. Review the suggestions.'), 'success')

    # ------------------------------------------------------------------
    # Anomaly guard
    # ------------------------------------------------------------------
    def _lock_date(self):
        c = self.company_id
        dates = [d for d in (getattr(c, 'fiscalyear_lock_date', False),
                             getattr(c, 'hard_lock_date', False)) if d]
        return max(dates) if dates else None

    def _history_ctx(self, partner, move_type, amount, date, exclude_id=None, ref=None):
        Move = self.env['account.move']
        if not partner:
            return [], []
        dom = [('company_id', '=', self.company_id.id), ('state', '=', 'posted'),
               ('commercial_partner_id', '=', partner.commercial_partner_id.id),
               ('move_type', '=', move_type)]
        if exclude_id:
            dom.append(('id', '!=', exclude_id))
        hist = Move.search(dom, limit=60, order='date desc')
        dups = []
        if date:
            for mv in hist.filtered(lambda h: abs((h.date - date).days) <= 3
                                    and abs(abs(h.amount_total_signed) - abs(amount)) < 0.01):
                if not ref or (mv.ref or '') != ref:
                    dups.append({'number': mv.name, 'date': str(mv.date),
                                 'total': abs(mv.amount_total_signed)})
        return [abs(h.amount_total_signed) for h in hist], dups

    def _tax_lines_from_move(self, move):
        out = []
        for line in move.line_ids.filtered('tax_line_id'):
            head, rate = matching.gst_head_and_rate(line.tax_line_id.name)
            out.append({'head': head, 'rate': rate if rate is not None else
                        line.tax_line_id.amount, 'amount': line.balance})
        return out

    def _ai_check_move(self, move):
        """Anomaly findings for an Odoo move about to be pushed to Tally."""
        kind = self._move_kind(move)
        partner = move.commercial_partner_id
        taxable = sum(abs(l.balance) for l in move.invoice_line_ids
                      if l.display_type == 'product')
        tax_lines = self._tax_lines_from_move(move)
        hist, dups = self._history_ctx(partner, move.move_type, move.amount_total_signed,
                                       move.date, move.id, move.ref)
        v = {'date': move.date, 'kind': kind, 'party': partner.name,
             'party_gstin': partner.vat if partner.vat and len(partner.vat) == 15 else '',
             'party_state': partner.state_id.name or '', 'number': move.name,
             'total': move.amount_total if move.is_invoice() else
             sum(l.debit for l in move.line_ids), 'taxable': taxable,
             'tax_lines': self._single_rate_view(tax_lines, taxable),
             'lines': [{'name': l.name or '', 'is_product': bool(l.product_id)
                        and l.product_id.type != 'service',
                        'hsn': getattr(l, 'l10n_in_hsn_code', '') or
                        getattr(l.product_id, 'l10n_in_hsn_code', '') or ''}
                       for l in move.invoice_line_ids if l.display_type == 'product']}
        ctx = self._anomaly_ctx(hist, dups)
        if move.tally_state in ('synced',):
            ctx['lock_date'] = None  # re-push of an existing voucher
        findings = anomaly.analyse(v, ctx)
        move.tally_risk_score = anomaly.risk_score(findings)
        return findings

    def _single_rate_view(self, tax_lines, taxable):
        """Only check GST arithmetic when the voucher has one rate per head."""
        heads = {}
        for t in tax_lines:
            heads.setdefault(t['head'], []).append(t)
        out = []
        for head, items in heads.items():
            rates = {i['rate'] for i in items}
            amt = sum(i['amount'] for i in items)
            out.append({'head': head, 'rate': rates.pop() if len(rates) == 1 else None,
                        'amount': amt})
        return out

    def _anomaly_ctx(self, hist, dups):
        return {'company_state': self.company_id.state_id.name or '',
                'partner_history': hist, 'duplicates': dups, 'lock_date': self._lock_date(),
                'z_threshold': self.z_threshold, 'large_amount': self.large_amount,
                'weekend_check': True}

    def _ai_check_voucher(self, stage, move):
        """Anomaly findings for a voucher imported from Tally."""
        data = stage._data()
        taxable, tax_lines = 0.0, []
        Map = self.env['tally.mapping']
        for le in data.get('ledger_entries', []):
            m = stage._resolve(le['ledger'])
            if le['is_party']:
                continue
            if m and m.kind == 'tax':
                head, rate = matching.gst_head_and_rate(le['ledger'])
                if m.tax_id:
                    head = head or matching.gst_head_and_rate(m.tax_id.name)[0]
                    rate = m.tax_id.amount if rate is None else rate
                tax_lines.append({'head': head, 'rate': rate, 'amount': le['amount']})
            elif not (m and m.kind == 'bank'):
                taxable += abs(le['amount'])
        inv = data.get('inventory_entries', [])
        if inv:
            taxable = sum(abs(i['amount']) for i in inv) + sum(
                abs(le['amount']) for le in data.get('ledger_entries', [])
                if not le['is_party'] and not (stage._resolve(le['ledger']).kind in ('tax', 'bank'))
                and not any(i.get('ledger') == le['ledger'] for i in inv)
                and le['ledger'].lower() != (self.round_off_ledger or '').lower())
        hist, dups = self._history_ctx(stage.partner_id, move.move_type, stage.amount_total,
                                       stage.date, move.id, move.ref)
        partner = stage.partner_id
        v = {'date': stage.date, 'kind': stage.kind, 'party': stage.party_name,
             'party_gstin': stage.party_gstin or (partner.vat if partner else ''),
             'party_state': stage.place_of_supply or (partner.state_id.name if partner else ''),
             'number': stage.number, 'total': stage.amount_total, 'taxable': taxable,
             'tax_lines': self._single_rate_view(tax_lines, taxable),
             'imbalance': round(sum(le['amount'] for le in data.get('ledger_entries', [])) + (
                 sum(i['amount'] for i in inv) if inv and not any(
                     le['ledger'] == i.get('ledger') for i in inv
                     for le in data.get('ledger_entries', [])) else 0.0), 2),
             'lines': [{'name': i['item'], 'is_product': True,
                        'hsn': json.loads(Map._find(self, 'item', tally_name=i['item']).tally_data
                                          or '{}').get('hsn', '')} for i in inv]}
        return anomaly.analyse(v, self._anomaly_ctx(hist, dups))

    # ------------------------------------------------------------------
    # Error doctor & reviews
    # ------------------------------------------------------------------
    def _ai_explain_error(self, message, req, force=False):
        if not self._ai_enabled():
            return False
        snippet = (req.request_xml or '')[:1500]
        if not self.ai_send_amounts:
            snippet = re.sub(r'<AMOUNT>[^<]*</AMOUNT>', '<AMOUNT>…</AMOUNT>', snippet)
        prompt = ('TallyPrime rejected an XML import from Odoo.\nError: %s\nRequest excerpt:\n%s\n'
                  'Explain the root cause in one sentence and give up to 3 concrete fix steps '
                  '(Tally menu paths or connector settings).' % (message, snippet))
        try:
            return self._ai_complete(prompt, max_tokens=400).strip()
        except llm.LLMError as e:
            return _('AI unavailable: %s') % e

    def _ai_review_voucher(self, stage):
        findings = json.loads(stage.findings or '[]')
        if not self._ai_enabled():
            if not findings:
                return _('No anomalies detected by the built-in guard.')
            return '\n'.join('• %s' % f['message'] for f in findings)
        data = stage._data()
        if not self.ai_send_amounts:
            for le in data.get('ledger_entries', []):
                le['amount'] = '…'
            for ie in data.get('inventory_entries', []):
                ie['amount'] = ie['rate'] = '…'
        prompt = ('Review this voucher imported from Tally for GST and accounting issues. '
                  'Built-in findings: %s\nVoucher: %s\nAnswer in at most 5 bullet points.'
                  % (json.dumps(findings), json.dumps(data, default=str)[:4000]))
        try:
            return self._ai_complete(prompt).strip()
        except llm.LLMError as e:
            return _('AI unavailable: %s') % e

    def action_ai_health(self):
        """Produce a dashboard summary (deterministic facts + optional LLM narrative)."""
        for conn in self:
            Req = self.env['tally.request']
            since = fields.Datetime.now() - timedelta(days=7)
            dom = [('connection_id', '=', conn.id), ('create_date', '>=', since)]
            done = Req.search_count(dom + [('state', '=', 'done')])
            failed = Req.search(dom + [('state', '=', 'failed')])
            codes = {}
            for r in failed:
                codes[r.error_code or 'unknown'] = codes.get(r.error_code or 'unknown', 0) + 1
            review = self.env['tally.voucher'].search_count(
                [('connection_id', '=', conn.id), ('state', 'in', ('error', 'anomaly', 'conflict'))])
            maps = self.env['tally.mapping'].search_count(
                [('connection_id', '=', conn.id), ('state', 'in', ('suggested', 'unmapped'))])
            recon = self.env['tally.recon.line'].search(
                [('connection_id', '=', conn.id), ('status', '=', 'mismatch')])
            facts = {'requests_ok_7d': done, 'requests_failed_7d': len(failed),
                     'failure_codes': codes, 'vouchers_to_review': review,
                     'masters_unmapped_or_suggested': maps,
                     'balance_mismatches': len(recon),
                     'agent_online': conn.agent_online if conn.transport == 'agent' else None}
            items = [_('%(d)s successful and %(f)s failed exchanges in the last 7 days.') % {
                'd': done, 'f': len(failed)}]
            if codes:
                items.append(_('Top failure causes: %s.') % ', '.join(
                    '%s ×%s' % (k, v) for k, v in sorted(codes.items(), key=lambda kv: -kv[1])))
            if review:
                items.append(_('%s Tally vouchers need review.') % review)
            if maps:
                items.append(_('%s Tally masters are unmapped or awaiting confirmation.') % maps)
            if recon:
                items.append(_('%s party balances differ between Odoo and Tally.') % len(recon))
            narrative = ''
            if conn._ai_enabled():
                try:
                    narrative = conn._ai_complete(
                        'Write a 3-sentence health summary and the single most important next '
                        'action for this Odoo–Tally integration: %s' % json.dumps(facts, default=str),
                        max_tokens=300).strip()
                except llm.LLMError as e:
                    narrative = _('AI unavailable: %s') % e
            html = Markup('<ul>%s</ul>') % Markup('').join(
                Markup('<li>%s</li>') % i for i in items)
            if narrative:
                html += Markup('<p class="mt-2"><b>AI:</b> %s</p>') % escape(narrative)
            conn.ai_summary = html
        return True
