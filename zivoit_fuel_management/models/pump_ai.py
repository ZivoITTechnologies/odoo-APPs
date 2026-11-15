# -*- coding: utf-8 -*-
"""AI layer for the fuel station.

* Built-in analytics engine (always on, no internet needed): stock-out
  forecasting, slip anomaly detection, customer credit-risk scoring,
  fleet alerts, customer insights.
* Built-in natural-language assistant (English)
  that answers account questions from live data.
* Optional LLM (Anthropic Claude or OpenAI) for free-form questions — the model
  only receives the customer's own account summary; if the call fails the
  built-in assistant answers instead.
"""
import json
import logging
import re
from datetime import timedelta

import requests

from odoo import api, fields, models
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)

PLATE_RE = re.compile(r'\b([A-Z]{2}\s?\d{1,2}\s?[A-Z]{0,3}\s?\d{3,4})\b', re.I)
NUM_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(?:l|ltr|ltrs|litre|litres|liter|liters)\b', re.I)

KW = {
    'greet': ['hi', 'hello', 'hey', 'good morning', 'good evening', 'help', 'what can you do'],
    'balance': ['balance', 'outstanding', 'due', 'owe', 'pending amount', 'dues', 'how much do i owe', 'amount payable'],
    'limit': ['limit', 'available credit', 'credit available', 'how much credit', 'headroom'],
    'rate': ['rate', 'price', 'cost of', 'tariff', 'today petrol', 'today diesel'],
    'bill': ['bill', 'invoice', 'billing'],
    'payment': ['paid', 'payment', 'last payment', 'receipt', 'how to pay', 'pay now', 'upi'],
    'statement': ['statement', 'ledger', 'download', 'pdf', 'excel', 'report'],
    'mileage': ['mileage', 'km/l', 'kmpl', 'efficiency', 'average', 'kilometre per', 'km per'],
    'consumption': ['consumption', 'how much', 'litres', 'liters', 'spent', 'spend', 'used', 'filled', 'fuel taken', 'quantity'],
    'indent': ['indent', 'book', 'order fuel', 'request fuel', 'reserve', 'need fuel', 'send fuel', 'doorstep', 'delivery'],
    'slip': ['slip', 'today fill', 'filling'],
    'vehicles': ['vehicle', 'vehicles', 'truck', 'fleet', 'bus', 'car'],
    # staff
    'sale': ['sale', 'sales', 'collection', 'revenue', 'turnover'],
    'stock': ['stock', 'tank', 'dry', 'run out', 'tanker', 'reorder'],
    'risk': ['risk', 'over limit', 'defaulter', 'overdue', 'top outstanding', 'who owes'],
    'fleet': ['maintenance', 'service due', 'insurance', 'permit', 'document', 'expiry', 'peso'],
    'anomaly': ['anomaly', 'anomalies', 'fraud', 'theft', 'suspicious', 'unusual', 'pilferage'],
}


def _money(v):
    return '₹ {:,.2f}'.format(v or 0.0)


class PumpAiLog(models.Model):
    _name = 'pump.ai.log'
    _description = 'AI Assistant Conversation'
    _order = 'id desc'

    partner_id = fields.Many2one('res.partner', index=True)
    user_id = fields.Many2one('res.users', default=lambda s: s.env.user)
    mode = fields.Selection([('customer', 'Customer Portal'), ('staff', 'Back Office')], default='customer')
    question = fields.Text()
    answer = fields.Text()
    intent = fields.Char()
    source = fields.Selection([('builtin', 'Built-in AI'), ('llm', 'LLM')], default='builtin')
    create_date = fields.Datetime(readonly=True)


class PumpAi(models.AbstractModel):
    _name = 'pump.ai'
    _description = 'Fuel Station AI Engine'

    def _check_access_ai(self, partner=None):
        """Back-office users, or a portal user asking about their own account (via the portal controller, sudo)."""
        if self.env.su or self.env.user.has_group('zivoit_fuel_management.group_pump_user'):
            return True
        raise AccessError(self.env._('You are not allowed to use the station AI services.'))

    # ------------------------------------------------------------------ anomalies
    @api.model
    def _slip_flags(self, slips):
        res = {}
        slips = slips.filtered(lambda s: s.id and s.state in ('confirmed', 'billed'))
        for s in slips:
            flags = []
            v = s.vehicle_id
            if v and v.tank_capacity and s.product_id.product_type == 'fuel' and s.qty > v.tank_capacity * 1.02:
                flags.append(self.env._('Qty above vehicle tank (%.0f L)', v.tank_capacity))
            if v:
                same_day = self.env['pump.credit.sale'].sudo().search_count([
                    ('vehicle_id', '=', v.id), ('date', '=', s.date), ('state', 'in', ('confirmed', 'billed')),
                    ('product_id.product_type', '=', 'fuel'), ('id', '!=', s.id)])
                if same_day and s.product_id.product_type == 'fuel':
                    flags.append(self.env._('Filled %s times the same day', same_day + 1))
                if s.kmpl and v.avg_kmpl and s.kmpl < v.avg_kmpl * 0.6:
                    flags.append(self.env._('Mileage %(k).1f km/L vs usual %(a).1f', k=s.kmpl, a=v.avg_kmpl))
            if flags:
                res[s.id] = '; '.join(flags)
        return res

    @api.model
    def recent_anomalies(self, days=10, partner=None, limit=20):
        self._check_access_ai()
        dom = [('date', '>=', fields.Date.context_today(self) - timedelta(days=days)),
               ('state', 'in', ('confirmed', 'billed')), ('vehicle_id', '!=', False)]
        if partner:
            dom.append(('partner_id', '=', partner.commercial_partner_id.id))
        slips = self.env['pump.credit.sale'].sudo().search(dom, limit=600)
        flags = self._slip_flags(slips)
        out = []
        for s in slips.filtered(lambda x: x.id in flags)[:limit]:
            out.append({'id': s.id, 'name': s.name, 'date': s.date.strftime('%d-%m-%Y'), 'customer': s.partner_id.name,
                        'vehicle': s.vehicle_no or '', 'qty': s.qty, 'flag': flags[s.id]})
        return out

    # ------------------------------------------------------------------ forecasting
    @api.model
    def tank_forecast(self, days=14):
        self._check_access_ai()
        today = fields.Date.context_today(self)
        res = []
        Move = self.env['pump.stock.move'].sudo()
        for t in self.env['pump.tank'].sudo().search([]):
            out = -sum(Move.search([('tank_id', '=', t.id), ('date', '>', today - timedelta(days=days)),
                                    ('date', '<=', today), ('move_type', 'in', ('sale', 'internal', 'delivery'))]).mapped('qty'))
            daily = out / days if out > 0 else 0.0
            usable = max(t.stock_qty - (t.dead_stock or 0.0), 0.0)
            days_left = usable / daily if daily else 999.0
            order_qty = 0.0
            if daily:
                lot = 4000.0 if t.capacity < 12000 else 12000.0
                order_qty = max(min(lot, t.ullage - lot * 0.02), 0.0) if t.ullage >= lot else round(t.ullage / 1000.0) * 1000
            res.append({'tank': t, 'name': t.name, 'product': t.product_id.name, 'daily': round(daily, 1),
                        'stock': round(t.stock_qty, 0), 'days_left': round(days_left, 1),
                        'order_by': today + timedelta(days=max(int(days_left) - 1, 0)) if daily else False,
                        'order_qty': order_qty, 'level': 'danger' if days_left < 2 else ('warn' if days_left < 4 else 'ok')})
        return res

    # ------------------------------------------------------------------ credit risk
    @api.model
    def customer_risk(self, partner):
        self._check_access_ai()
        partner = partner.commercial_partner_id.sudo()
        today = fields.Date.context_today(self)
        util = (partner.pump_exposure / partner.pump_credit_limit) if partner.pump_credit_limit else 0.0
        bills = self.env['pump.bill'].sudo().search([('partner_id', '=', partner.id), ('state', '=', 'posted')])
        overdue_days = max(bills.mapped('days_overdue') or [0])
        overdue_amt = sum(b.amount_due for b in bills if b.payment_state == 'overdue')
        since = today - timedelta(days=90)
        Line = self.env['pump.voucher.line'].sudo()
        base = [('partner_id', '=', partner.id), ('state', '=', 'posted'), ('account_type', '=', 'receivable'),
                ('date', '>=', since)]
        billed = sum(Line.search(base + [('voucher_type', '=', 'sale')]).mapped('debit'))
        paid = sum(Line.search(base).mapped('credit'))
        pay_ratio = (paid / billed) if billed else 1.0
        score = min(util, 1.5) / 1.5 * 40 + min(overdue_days, 45) / 45 * 40 + (1 - min(pay_ratio, 1.0)) * 20
        score = round(min(score, 100), 0)
        monthly_bill = billed / 3.0
        suggested = round(monthly_bill * ((partner.pump_credit_days or 15) / 30.0 + 0.5) * (1.2 if score < 30 else (1.0 if score < 60 else 0.7)), -4)
        grade = 'A' if score < 25 else ('B' if score < 50 else ('C' if score < 75 else 'D'))
        return {'partner': partner, 'score': score, 'grade': grade, 'utilisation': round(util * 100, 1),
                'overdue_days': overdue_days, 'overdue_amount': overdue_amt, 'pay_ratio': round(pay_ratio * 100, 0),
                'monthly_billing': monthly_bill, 'suggested_limit': suggested}

    # ------------------------------------------------------------------ station insights
    @api.model
    def station_insights(self):
        self._check_access_ai()
        today = fields.Date.context_today(self)
        out = []
        for f in self.tank_forecast():
            if f['level'] != 'ok':
                out.append({'level': 'danger' if f['level'] == 'danger' else 'warn', 'icon': 'fa-tint',
                            'title': self.env._('%(t)s runs dry in %(d)s days', t=f['name'], d=f['days_left']),
                            'text': self.env._('Selling %(r)s L/day. Order %(q).0f L of %(p)s by %(o)s.', r=f['daily'],
                                      q=f['order_qty'], p=f['product'], o=f['order_by'].strftime('%d %b') if f['order_by'] else '-')})
        customers = self.env['res.partner'].sudo().search([('is_pump_customer', '=', True)])
        risky = sorted((self.customer_risk(c) for c in customers), key=lambda r: -r['score'])
        for r in risky[:3]:
            if r['score'] >= 50:
                out.append({'level': 'danger' if r['score'] >= 75 else 'warn', 'icon': 'fa-user-times',
                            'title': self.env._('Credit risk %(g)s: %(n)s (score %(s).0f)', g=r['grade'], n=r['partner'].name, s=r['score']),
                            'text': self.env._('%(u)s%% of limit used, oldest bill %(d)s days overdue, paid %(p)s%% of 90-day billing. '
                                      'Suggested limit %(l)s.', u=r['utilisation'], d=r['overdue_days'], p=r['pay_ratio'],
                                      l=_money(r['suggested_limit']))})
        anomalies = self.recent_anomalies(days=7)
        seen, uniq = set(), []
        for a in anomalies:
            if (a['vehicle'], a['date'], a['flag']) not in seen:
                seen.add((a['vehicle'], a['date'], a['flag']))
                uniq.append(a)
        anomalies = uniq
        if anomalies:
            out.append({'level': 'warn', 'icon': 'fa-search',
                        'title': self.env._('%s suspicious filling slip(s) this week', len(anomalies)),
                        'text': '; '.join('%s %s: %s' % (a['vehicle'], a['date'], a['flag']) for a in anomalies[:3])})
        dips = self.env['pump.dip.reading'].sudo().search([('state', '=', 'posted'), ('date', '>=', today - timedelta(days=14))])
        bad = dips.filtered(lambda d: d.book_qty and d.variance / d.book_qty < -0.003)
        if bad:
            out.append({'level': 'warn', 'icon': 'fa-flask',
                        'title': self.env._('DIP loss above 0.3%% on %s reading(s)', len(bad)),
                        'text': self.env._('Check for leakage / meter calibration: %s', ', '.join(set(bad.mapped('tank_id.name'))))})
        sheets = self.env['pump.shift.sheet'].sudo()
        this_w = sum(sheets.search([('state', '=', 'posted'), ('date', '>', today - timedelta(days=7))]).mapped('total_amount'))
        last_w = sum(sheets.search([('state', '=', 'posted'), ('date', '>', today - timedelta(days=14)),
                                    ('date', '<=', today - timedelta(days=7))]).mapped('total_amount'))
        if last_w:
            ch = (this_w - last_w) / last_w * 100
            out.append({'level': 'success' if ch >= 0 else 'info', 'icon': 'fa-line-chart',
                        'title': self.env._('Sales %(s)s %(p).1f%% week on week', s='up' if ch >= 0 else 'down', p=abs(ch)),
                        'text': self.env._('Last 7 days %(a)s vs previous 7 days %(b)s.', a=_money(this_w), b=_money(last_w))})
        if 'pump.vehicle.document' in self.env:
            docs = self.env['pump.vehicle.document'].sudo().search([('state', 'in', ('expiring', 'expired'))])
            if docs:
                out.append({'level': 'danger' if 'expired' in docs.mapped('state') else 'warn', 'icon': 'fa-id-card',
                            'title': self.env._('%s fleet document(s) need renewal', len(docs)),
                            'text': ', '.join('%s (%s d)' % (d.display_name, d.days_left) for d in docs[:3])})
            due = self.env['pump.maintenance.schedule'].sudo().search([]).filtered(lambda s: s.due_state != 'ok')
            if due:
                out.append({'level': 'warn', 'icon': 'fa-wrench', 'title': self.env._('%s maintenance job(s) due', len(due)),
                            'text': ', '.join(due[:3].mapped('display_name'))})
        return out

    # ------------------------------------------------------------------ customer insights
    @api.model
    def customer_insights(self, partner):
        self._check_access_ai()
        partner = partner.commercial_partner_id.sudo()
        today = fields.Date.context_today(self)
        Sale = self.env['pump.credit.sale'].sudo()
        base = [('partner_id', '=', partner.id), ('state', 'in', ('confirmed', 'billed'))]
        m_start = today.replace(day=1)
        lm_end = m_start - timedelta(days=1)
        lm_start = lm_end.replace(day=1)
        this_m = Sale.search(base + [('date', '>=', m_start)])
        same_point = Sale.search(base + [('date', '>=', lm_start), ('date', '<=', lm_start + timedelta(days=today.day - 1))])
        cards = []
        a, b = sum(this_m.mapped('amount')), sum(same_point.mapped('amount'))
        if b:
            ch = (a - b) / b * 100
            cards.append({'icon': 'fa-line-chart', 'level': 'info',
                          'title': self.env._('Spend %(d)s %(p).0f%% vs last month', d='up' if ch >= 0 else 'down', p=abs(ch)),
                          'text': self.env._('%(a)s so far this month vs %(b)s by the same day last month.', a=_money(a), b=_money(b))})
        # forecast of next bill
        last30 = Sale.search(base + [('date', '>', today - timedelta(days=30))])
        daily = sum(last30.mapped('amount')) / 30.0
        cycle_days = {'weekly': 7, 'fortnightly': 15, 'monthly': 30}.get(partner.pump_billing_cycle, 15)
        if partner.pump_billing_cycle == 'fortnightly':
            cycle_end = today.replace(day=15) if today.day <= 15 else (m_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        elif partner.pump_billing_cycle == 'monthly':
            cycle_end = (m_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        else:
            cycle_end = today + timedelta(days=(6 - today.weekday()))
        forecast = partner.pump_unbilled + daily * max((cycle_end - today).days, 0)
        if daily:
            cards.append({'icon': 'fa-magic', 'level': 'success',
                          'title': self.env._('Next bill forecast: %s', _money(forecast)),
                          'text': self.env._('Based on your average of %(d)s/day; cycle closes %(c)s (%(n)s-day billing).',
                                    d=_money(daily), c=cycle_end.strftime('%d %b'), n=cycle_days)})
        if partner.pump_credit_limit and daily:
            headroom_days = partner.pump_available_credit / daily if daily else 0
            lvl = 'danger' if headroom_days < 3 else ('warn' if headroom_days < 7 else 'success')
            cards.append({'icon': 'fa-shield', 'level': lvl,
                          'title': self.env._('Credit lasts ~%.0f more days', max(headroom_days, 0)),
                          'text': self.env._('Available %(a)s at your current usage. Pay early to avoid stoppage.',
                                    a=_money(partner.pump_available_credit))})
        due = self.env['pump.bill'].sudo().search([('partner_id', '=', partner.id), ('state', '=', 'posted')]).filtered(
            lambda x: x.amount_due > 0.5)
        if due:
            nxt = due.sorted('due_date')[:1]
            over = due.filtered(lambda x: x.payment_state == 'overdue')
            cards.append({'icon': 'fa-calendar-check-o', 'level': 'danger' if over else 'warn',
                          'title': self.env._('%(n)s bill(s) due — %(a)s', n=len(due), a=_money(sum(due.mapped('amount_due')))),
                          'text': (self.env._('%(o)s overdue. ', o=len(over)) if over else '') +
                                  self.env._('Next: %(b)s due %(d)s.', b=nxt.name, d=nxt.due_date.strftime('%d %b') if nxt.due_date else '-')})
        vehicles = partner.pump_vehicle_ids.filtered(lambda v: v.avg_kmpl > 0)
        if len(vehicles) >= 2:
            best = max(vehicles, key=lambda v: v.avg_kmpl)
            worst = min(vehicles, key=lambda v: v.avg_kmpl)
            if worst.avg_kmpl < best.avg_kmpl * 0.85:
                cards.append({'icon': 'fa-truck', 'level': 'warn',
                              'title': self.env._('%s is the least efficient vehicle', worst.name),
                              'text': self.env._('%(w).1f km/L vs %(b).1f km/L for %(bn)s — a service or tyre check may save fuel.',
                                        w=worst.avg_kmpl, b=best.avg_kmpl, bn=best.name)})
        anomalies = self.recent_anomalies(days=30, partner=partner, limit=5)
        if anomalies:
            cards.append({'icon': 'fa-exclamation-triangle', 'level': 'danger',
                          'title': self.env._('%s unusual filling(s) on your vehicles', len(anomalies)),
                          'text': '; '.join('%s %s: %s' % (x['vehicle'], x['date'], x['flag']) for x in anomalies[:2])})
        return cards

    # ------------------------------------------------------------------ assistant
    @api.model
    def _detect(self, q):
        ql = q.lower()
        scores = {}
        for intent, words in KW.items():
            for w in words:
                if re.search(r'(?<![a-z])%s(?:s|es)?(?![a-z])' % re.escape(w), ql):
                    scores[intent] = scores.get(intent, 0) + (2 if ' ' in w else 1)
        return scores

    @api.model
    def _period(self, q):
        today = fields.Date.context_today(self)
        ql = q.lower()
        m_start = today.replace(day=1)
        if 'last month' in ql or 'previous month' in ql:
            end = m_start - timedelta(days=1)
            return end.replace(day=1), end, self.env._('last month')
        if 'today' in ql:
            return today, today, self.env._('today')
        if 'yesterday' in ql:
            y = today - timedelta(days=1)
            return y, y, self.env._('yesterday')
        if 'week' in ql:
            return today - timedelta(days=6), today, self.env._('the last 7 days')
        if 'year' in ql or ' fy' in ql:
            fy = self.env['res.partner'].pump_current_fy(today)
            return self.env['res.partner'].pump_fy_bounds(fy)[0], today, self.env._('this financial year')
        return m_start, today, self.env._('this month')

    @api.model
    def _product_in(self, q):
        ql = q.lower()
        for p in self.env['pump.product'].sudo().search([]):
            names = [p.name.lower(), (p.code or '').lower()] + p.name.lower().replace('(', ' ').replace(')', ' ').split()
            if any(n and len(n) > 2 and re.search(r'(?<![a-z])%s(?![a-z])' % re.escape(n), ql) for n in names):
                return p
        if 'petrol' in ql:
            return self.env.ref('zivoit_fuel_management.product_ms', raise_if_not_found=False)
        if 'diesel' in ql:
            return self.env.ref('zivoit_fuel_management.product_hsd', raise_if_not_found=False)
        return self.env['pump.product']

    @api.model
    def ask(self, question, partner=None, mode='customer'):
        self._check_access_ai()
        question = (question or '').strip()[:500]
        if not question:
            return {'answer': self.env._('Please type a question.'), 'links': [], 'source': 'builtin'}
        builtin = self._builtin_answer(question, partner, mode)
        result = dict(builtin)
        company = self.env.company.sudo()
        if company.pump_ai_provider in ('anthropic', 'openai') and company.pump_ai_api_key:
            try:
                llm = self._llm_answer(question, partner, mode, builtin)
                if llm:
                    result.update({'answer': llm, 'source': 'llm'})
            except Exception as e:  # noqa: BLE001 - never break the portal on LLM issues
                _logger.warning('AI provider call failed, using built-in answer: %s', e)
        self.env['pump.ai.log'].sudo().create({
            'partner_id': partner.commercial_partner_id.id if partner else False, 'mode': mode,
            'question': question, 'answer': result['answer'], 'intent': builtin.get('intent'),
            'source': result.get('source', 'builtin')})
        return result

    def _builtin_answer(self, q, partner, mode):
        scores = self._detect(q)
        if mode == 'staff':
            return self._staff_answer(q, scores)
        p = partner.commercial_partner_id.sudo()
        links = []
        plate = PLATE_RE.search(q.upper())
        vehicle = False
        if plate:
            norm = plate.group(1).replace(' ', '').upper()
            vehicle = p.pump_vehicle_ids.filtered(lambda v: v.name.replace(' ', '').upper() == norm)[:1]
        intent = max(scores, key=scores.get) if scores else 'unknown'
        if vehicle and intent not in ('indent', 'mileage'):
            intent = 'consumption'
        if 'indent' in scores and (NUM_RE.search(q) or 'full' in q.lower()):
            intent = 'indent'
        Sale = self.env['pump.credit.sale'].sudo()
        bills = self.env['pump.bill'].sudo().search([('partner_id', '=', p.id), ('state', '=', 'posted')])

        if intent == 'greet':
            ans = self.env._("Hello %(n)s! I'm your fuel account assistant. Ask me things like:\n"
                    "• What is my outstanding balance?\n• How much diesel did %(v)s take last month?\n"
                    "• Today's petrol rate\n• Download my statement for last month\n• Book 200 litres diesel for %(v)s",
                    n=p.name, v=(p.pump_vehicle_ids[:1].name or 'TS09AB1234'))
        elif intent == 'balance' or (intent == 'payment' and not any(
                w in q.lower() for w in ('last', 'how', 'upi', 'when'))):
            due = bills.filtered(lambda b: b.amount_due > 0.5)
            over = due.filtered(lambda b: b.payment_state == 'overdue')
            ans = self.env._("Your outstanding balance is **%(b)s %(dc)s**. Unbilled slips: %(u)s. ",
                    b=_money(abs(p.pump_balance)), dc='Dr' if p.pump_balance >= 0 else 'Cr (advance)', u=_money(p.pump_unbilled))
            if due:
                ans += self.env._("%(n)s bill(s) are open for %(a)s", n=len(due), a=_money(sum(due.mapped('amount_due'))))
                ans += self.env._(", of which %s overdue.", len(over)) if over else '.'
            else:
                ans += self.env._("All bills are paid. Thank you!")
            links.append({'label': self.env._('Open ledger'), 'url': '/my/fuel/ledger'})
            links.append({'label': self.env._('View bills'), 'url': '/my/fuel/bills'})
        elif intent == 'payment':
            last = self.env['pump.voucher.line'].sudo().search([
                ('partner_id', '=', p.id), ('state', '=', 'posted'), ('voucher_type', '=', 'receipt'),
                ('account_type', '=', 'receivable')], order='date desc, id desc', limit=1)
            c = self.env.company.sudo()
            if 'how' in q.lower() or 'upi' in q.lower():
                ans = self.env._("You can pay by UPI to **%(u)s** or bank transfer to %(b)s A/c %(a)s (IFSC %(i)s). "
                        "Every bill PDF also carries a scan-to-pay UPI QR code.",
                        u=c.pump_upi_id or '-', b=c.pump_bank_name or '-', a=c.pump_bank_account or '-', i=c.pump_bank_ifsc or '-')
            elif last:
                ans = self.env._("Your last payment was **%(a)s** on %(d)s (%(r)s).", a=_money(last.credit),
                        d=last.date.strftime('%d-%m-%Y'), r=last.voucher_id.cheque_no or last.voucher_id.name)
            else:
                ans = self.env._("I couldn't find any payment on your account yet.")
            links.append({'label': self.env._('Statement'), 'url': '/my/fuel/statement'})
        elif intent == 'limit':
            ans = self.env._("Credit limit **%(l)s**, used %(e)s (outstanding + unbilled). Available credit: **%(a)s**.",
                    l=_money(p.pump_credit_limit), e=_money(p.pump_exposure), a=_money(p.pump_available_credit))
        elif intent == 'rate':
            prod = self._product_in(q)
            prods = prod or self.env['pump.product'].sudo().search([('product_type', '=', 'fuel')])
            ans = self.env._("Today's rates: ") + ', '.join('%s **%s**/%s' % (x.name, _money(x.current_rate), x.uom) for x in prods) + '.'
            links.append({'label': self.env._('Rate history'), 'url': '/my/fuel/rates'})
        elif intent == 'bill':
            last = bills.sorted(lambda b: (b.date, b.id), reverse=True)[:1]
            if last:
                st = dict(last._fields['payment_state'].selection)[last.payment_state]
                ans = self.env._("Your latest bill **%(n)s** (%(f)s to %(t)s) is %(a)s for %(s)s fillings — status: **%(st)s**",
                        n=last.name, f=last.date_from.strftime('%d-%m'), t=last.date_to.strftime('%d-%m-%Y'),
                        a=_money(last.amount_total), s=last.slip_count, st=st)
                ans += self.env._(", %(d)s due by %(dd)s.", d=_money(last.amount_due), dd=last.due_date.strftime('%d-%m-%Y')) \
                    if last.amount_due > 0.5 and last.due_date else '.'
                links.append({'label': self.env._('Open bill'), 'url': '/my/fuel/bill/%s' % last.id})
                links.append({'label': self.env._('Download PDF'), 'url': '/my/fuel/bill/%s/pdf' % last.id})
            else:
                ans = self.env._("No bills have been raised yet. Unbilled slips: %s.", _money(p.pump_unbilled))
        elif intent == 'statement':
            d_from, d_to, label = self._period(q)
            ans = self.env._("Here is your statement for %(l)s (%(f)s to %(t)s).", l=label, f=d_from.strftime('%d-%m-%Y'),
                    t=d_to.strftime('%d-%m-%Y'))
            qs = 'date_from=%s&date_to=%s' % (d_from, d_to)
            links += [{'label': self.env._('Download PDF'), 'url': '/my/fuel/ledger/pdf?' + qs},
                      {'label': self.env._('Download Excel'), 'url': '/my/fuel/ledger/xlsx?' + qs},
                      {'label': self.env._('View online'), 'url': '/my/fuel/statement?' + qs}]
        elif intent == 'mileage':
            vs = vehicle or p.pump_vehicle_ids
            rows = ['%s: %s' % (v.name, ('%.1f km/L' % v.avg_kmpl) if v.avg_kmpl else self.env._('no odometer data')) for v in vs]
            ans = self.env._("Average mileage (from odometer readings on your slips):\n• ") + '\n• '.join(rows or [self.env._('No vehicles')])
            links.append({'label': self.env._('My vehicles'), 'url': '/my/fuel/vehicles'})
        elif intent == 'indent':
            prod = self._product_in(q) or (vehicle.product_id if vehicle else self.env['pump.product'])
            m = NUM_RE.search(q)
            qty = float(m.group(1)) if m else 0.0
            params = {'vehicle': vehicle.id if vehicle else '', 'product': prod.id if prod else '', 'qty': qty or '',
                      'type': 'full' if 'full' in q.lower() else 'qty'}
            url = '/my/fuel/indent?' + '&'.join('%s=%s' % (k, v) for k, v in params.items() if v != '')
            ans = self.env._("I've prepared an indent%(v)s%(q)s%(p)s. Please review and press **Submit** to send it to the station.",
                    v=(self.env._(' for %s', vehicle.name) if vehicle else ''), q=(self.env._(', %.0f L', qty) if qty else ''),
                    p=(self.env._(' of %s', prod.name) if prod else ''))
            if prod and qty:
                est = qty * prod.current_rate
                ans += self.env._(" Estimated value %(e)s; available credit %(a)s.", e=_money(est), a=_money(p.pump_available_credit))
            links.append({'label': self.env._('Review & submit indent'), 'url': url})
        elif intent in ('consumption', 'slip', 'vehicles'):
            d_from, d_to, label = self._period(q)
            dom = [('partner_id', '=', p.id), ('state', 'in', ('confirmed', 'billed')), ('date', '>=', d_from), ('date', '<=', d_to)]
            prod = self._product_in(q)
            if prod:
                dom.append(('product_id', '=', prod.id))
            if vehicle:
                dom.append(('vehicle_id', '=', vehicle.id))
            sales = Sale.search(dom)
            subject = vehicle.name if vehicle else self.env._('your vehicles')
            ans = self.env._("%(s)s took **%(q).2f L** %(p)sfor **%(a)s** in %(l)s across %(n)s filling(s).",
                    s=subject, q=sum(sales.mapped('qty')), p=(prod.name + ' ' if prod else ''),
                    a=_money(sum(sales.mapped('amount'))), l=label, n=len(sales))
            if not vehicle and sales:
                by_v = {}
                for s in sales:
                    by_v[s.vehicle_no or '-'] = by_v.get(s.vehicle_no or '-', 0.0) + s.qty
                top = sorted(by_v.items(), key=lambda x: -x[1])[:4]
                ans += '\n' + '\n'.join('• %s: %.2f L' % t for t in top)
            links.append({'label': self.env._('Open slips'), 'url': '/my/fuel/slips?date_from=%s&date_to=%s' % (d_from, d_to)})
        else:
            ans = self.env._("I can help with your balance, bills, payments, rates, statements, vehicle consumption, "
                    "mileage and fuel indents. Try: \"What is my balance?\" or \"Diesel taken last month\".")
            intent = 'unknown'
        return {'answer': ans, 'links': links, 'source': 'builtin', 'intent': intent}

    def _staff_answer(self, q, scores):
        intent = max(scores, key=scores.get) if scores else 'unknown'
        links = []
        if intent in ('sale', 'consumption'):
            d_from, d_to, label = self._period(q)
            sheets = self.env['pump.shift.sheet'].search([('state', '=', 'posted'), ('date', '>=', d_from), ('date', '<=', d_to)])
            ans = self.env._("Fuel sale %(l)s: **%(a)s** (%(q).0f L) across %(n)s shift(s). Credit slips %(c)s, card/UPI %(k)s.",
                    l=label, a=_money(sum(sheets.mapped('total_amount'))), q=sum(sheets.mapped('total_qty')), n=len(sheets),
                    c=_money(sum(sheets.mapped('credit_amount'))),
                    k=_money(sum(sheets.mapped('card_amount')) + sum(sheets.mapped('upi_amount'))))
        elif intent == 'stock':
            rows = self.tank_forecast()
            ans = self.env._("Tank forecast:\n") + '\n'.join(
                '• %s: %s L, ~%s days left%s' % (r['name'], '{:,.0f}'.format(r['stock']), r['days_left'],
                                                 (self.env._(' → order %(q).0f L by %(d)s', q=r['order_qty'], d=r['order_by'].strftime('%d %b'))
                                                  if r['level'] != 'ok' and r['order_by'] else '')) for r in rows)
        elif intent in ('risk', 'balance', 'limit'):
            cust = self.env['res.partner'].search([('is_pump_customer', '=', True)])
            risks = sorted((self.customer_risk(c) for c in cust), key=lambda r: -r['score'])[:5]
            ans = self.env._("Highest credit risk:\n") + '\n'.join(
                '• %s — score %.0f (%s), outstanding %s, %s%% of limit' % (
                    r['partner'].name, r['score'], r['grade'], _money(r['partner'].pump_balance), r['utilisation']) for r in risks)
        elif intent == 'anomaly':
            an = self.recent_anomalies(days=14)
            ans = (self.env._("Flagged slips (14 days):\n") + '\n'.join('• %s %s %s: %s' % (a['date'], a['customer'], a['vehicle'], a['flag'])
                                                            for a in an[:8])) if an else self.env._("No anomalies in the last 14 days.")
        elif intent == 'fleet':
            docs = self.env['pump.vehicle.document'].search([('state', 'in', ('expiring', 'expired'))])
            due = self.env['pump.maintenance.schedule'].search([]).filtered(lambda s: s.due_state != 'ok')
            ans = self.env._("Fleet: %(d)s document(s) to renew, %(m)s maintenance job(s) due.", d=len(docs), m=len(due))
            for d in docs[:4]:
                ans += '\n• %s — %s' % (d.display_name, self.env._('expired') if d.days_left < 0 else self.env._('%s days left', d.days_left))
            for m in due[:4]:
                ans += '\n• %s — %s' % (m.display_name, dict(m._fields['due_state'].selection)[m.due_state])
        elif intent == 'rate':
            ans = ', '.join('%s %s' % (p.name, _money(p.current_rate)) for p in self.env['pump.product'].search([]))
        else:
            ans = self.env._("Ask me about: today's sale, stock / days left, credit risk, anomalies, fleet documents & maintenance, rates.")
        return {'answer': ans, 'links': links, 'source': 'builtin', 'intent': intent}

    # ------------------------------------------------------------------ LLM
    def _context_for(self, partner, mode):
        if mode == 'staff' or not partner:
            return {'station': self.env.company.pump_outlet_name or self.env.company.name,
                    'insights': [i['title'] + ': ' + i['text'] for i in self.station_insights()]}
        p = partner.commercial_partner_id.sudo()
        bills = self.env['pump.bill'].sudo().search([('partner_id', '=', p.id), ('state', '=', 'posted')], limit=6)
        return {
            'customer': p.name, 'code': p.pump_code, 'outstanding': round(p.pump_balance, 2),
            'unbilled': round(p.pump_unbilled, 2), 'credit_limit': p.pump_credit_limit,
            'available_credit': round(p.pump_available_credit, 2), 'billing_cycle': p.pump_billing_cycle,
            'rates_today': {x.name: x.current_rate for x in self.env['pump.product'].sudo().search([])},
            'recent_bills': [{'no': b.name, 'date': str(b.date), 'amount': b.amount_total, 'due': round(b.amount_due, 2),
                              'status': b.payment_state, 'due_date': str(b.due_date)} for b in bills],
            'vehicles': [{'no': v.name, 'litres_this_month': v.month_qty, 'spend_this_month': v.month_amount,
                          'avg_kmpl': round(v.avg_kmpl, 2), 'last_fill': str(v.last_fill_date or '')} for v in p.pump_vehicle_ids],
            'insights': [c['title'] for c in self.customer_insights(p)],
            'portal_pages': {'ledger': '/my/fuel/ledger', 'bills': '/my/fuel/bills', 'slips': '/my/fuel/slips',
                             'indent': '/my/fuel/indent', 'statement': '/my/fuel/statement'},
        }

    def _llm_answer(self, question, partner, mode, builtin):
        company = self.env.company.sudo()
        ctx = json.dumps(self._context_for(partner, mode), default=str)
        system = ("You are the friendly assistant of an Indian fuel station (oil company retail outlet). Answer ONLY from the JSON "
                  "account data provided; never invent numbers. Use ₹ and Indian formatting, be brief (max 4 sentences), "
                  "always reply in English. If data is missing, say so and point to "
                  "the relevant portal page. A rule-based draft answer is supplied; you may refine it.")
        user = 'ACCOUNT DATA:\n%s\n\nDRAFT ANSWER:\n%s\n\nQUESTION:\n%s' % (ctx, builtin.get('answer'), question)
        if company.pump_ai_provider == 'anthropic':
            r = requests.post('https://api.anthropic.com/v1/messages', timeout=20, headers={
                'x-api-key': company.pump_ai_api_key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
                json={'model': company.pump_ai_model or 'claude-haiku-5-5', 'max_tokens': 400, 'system': system,
                      'messages': [{'role': 'user', 'content': user}]})
            r.raise_for_status()
            return ''.join(b.get('text', '') for b in r.json().get('content', []) if b.get('type') == 'text').strip()
        r = requests.post('https://api.openai.com/v1/chat/completions', timeout=20, headers={
            'Authorization': 'Bearer %s' % company.pump_ai_api_key, 'Content-Type': 'application/json'},
            json={'model': company.pump_ai_model or 'gpt-4o-mini', 'max_tokens': 400,
                  'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]})
        r.raise_for_status()
        return r.json()['choices'][0]['message']['content'].strip()
