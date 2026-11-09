# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
"""Finance and analytics tools.

Every statement is computed from posted journal items (account.move.line) with
the integration user's access rights, per company, in the company currency.
They work on Odoo Community (no account_reports needed) and Enterprise.
"""
import datetime
from collections import defaultdict

from dateutil.relativedelta import relativedelta

from odoo import fields as odoo_fields

from . import utils
from .context import ToolError

PL_SECTIONS = [
    # key, label, account types, sign (1 = debit balance is positive)
    ('revenue', 'Revenue', ('income',), -1),
    ('cost_of_revenue', 'Cost of Revenue', ('expense_direct_cost',), 1),
    ('operating_expenses', 'Operating Expenses', ('expense',), 1),
    ('depreciation', 'Depreciation', ('expense_depreciation',), 1),
    ('other_income', 'Other Income', ('income_other',), -1),
    ('other_expenses', 'Other Expenses', ('expense_other',), 1),
]
PL_TYPES = tuple(t for s in PL_SECTIONS for t in s[2])

BS_LAYOUT = [
    ('assets', 'Assets', 1, [
        ('current_assets', 'Current Assets', [
            ('asset_cash', 'Bank and Cash'), ('asset_receivable', 'Receivables'),
            ('asset_current', 'Current Assets'), ('asset_prepayments', 'Prepayments')]),
        ('non_current_assets', 'Non-current Assets', [
            ('asset_fixed', 'Fixed Assets'), ('asset_non_current', 'Non-current Assets')]),
    ]),
    ('liabilities', 'Liabilities', -1, [
        ('current_liabilities', 'Current Liabilities', [
            ('liability_payable', 'Payables'), ('liability_credit_card', 'Credit Cards'),
            ('liability_current', 'Current Liabilities')]),
        ('non_current_liabilities', 'Non-current Liabilities', [
            ('liability_non_current', 'Non-current Liabilities')]),
    ]),
    ('equity', 'Equity', -1, [
        ('share_capital_and_reserves', 'Capital and Reserves', [
            ('equity', 'Equity'), ('equity_unaffected', 'Unaffected Earnings Account')]),
    ]),
]
BS_TYPES = tuple(t for _k, _l, _s, groups in BS_LAYOUT for _g, _gl, types in groups for t, _tl in types)

AGE_BUCKETS = [('not_due', 'Not due', None, 0), ('1_30', '1-30 days', 1, 30), ('31_60', '31-60 days', 31, 60),
               ('61_90', '61-90 days', 61, 90), ('91_120', '91-120 days', 91, 120), ('over_120', 'Over 120 days', 121, None)]


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------
def _today(ctx):
    return odoo_fields.Date.context_today(ctx.env.user)


def _fiscal_year(company, date):
    if hasattr(company, 'compute_fiscalyear_dates'):
        dates = company.compute_fiscalyear_dates(date)
        return dates['date_from'], dates['date_to']
    return datetime.date(date.year, 1, 1), datetime.date(date.year, 12, 31)


def _period(ctx, args, company):
    today = _today(ctx)
    date_to = utils.as_date(args.get('date_to'), name='date_to')
    date_from = utils.as_date(args.get('date_from'), name='date_from')
    if not date_to:
        date_to = today
    if not date_from:
        date_from = _fiscal_year(company, date_to)[0]
    if date_from > date_to:
        raise ToolError("date_from (%s) is after date_to (%s)." % (date_from, date_to))
    return date_from, date_to


def _comparison(args, date_from, date_to):
    cmp_from = utils.as_date(args.get('compare_date_from'), name='compare_date_from')
    cmp_to = utils.as_date(args.get('compare_date_to'), name='compare_date_to')
    if cmp_from and cmp_to:
        return cmp_from, cmp_to
    mode = (args.get('compare') or '').strip().lower()
    if mode in ('previous_year', 'prior_year', 'last_year', 'yoy'):
        return date_from - relativedelta(years=1), date_to - relativedelta(years=1)
    if mode in ('previous_period', 'prior_period', 'previous', 'pop'):
        length = (date_to - date_from).days + 1
        # whole months compare against the previous whole months
        if date_from.day == 1 and (date_to + datetime.timedelta(days=1)).day == 1:
            months = (date_to.year - date_from.year) * 12 + date_to.month - date_from.month + 1
            start = date_from - relativedelta(months=months)
            return start, date_from - datetime.timedelta(days=1)
        return date_from - datetime.timedelta(days=length), date_from - datetime.timedelta(days=1)
    if mode in ('', 'none', 'no', 'false'):
        return None
    raise ToolError("compare must be 'previous_period', 'previous_year' or 'none'.")


def _state_domain(args):
    if utils.as_bool(args.get('include_draft'), False):
        return [('parent_state', 'in', ('posted', 'draft'))]
    return [('parent_state', '=', 'posted')]


def _aml(ctx, company):
    ctx.check_model('account.move.line', 'read')
    return ctx.env['account.move.line'].with_company(company)


def _account_balances(ctx, company, account_types, date_from=None, date_to=None, args=None, extra=None):
    """{account: {'balance', 'debit', 'credit'}} for journal items of ``company``."""
    aml = _aml(ctx, company)
    domain = [('company_id', '=', company.id), ('account_id.account_type', 'in', list(account_types))]
    domain += _state_domain(args or {})
    if date_from:
        domain.append(('date', '>=', date_from))
    if date_to:
        domain.append(('date', '<=', date_to))
    domain += extra or []
    result = {}
    for account, balance, debit, credit in aml._read_group(
            domain, ['account_id'], ['balance:sum', 'debit:sum', 'credit:sum']):
        result[account] = {'balance': balance or 0.0, 'debit': debit or 0.0, 'credit': credit or 0.0}
    return result


def _account_label(account):
    return {'account_id': account.id, 'code': account.code or '', 'name': account.name}


def _company_header(company, **extra):
    data = {'company_id': company.id, 'company': company.name, 'currency': company.currency_id.name}
    data.update(extra)
    return data


def _group_total(per_company, key):
    currencies = {c['currency'] for c in per_company}
    if len(per_company) > 1 and len(currencies) == 1:
        return {'currency': currencies.pop(), key: utils.money(sum(c[key] for c in per_company if c.get(key) is not None))}
    return None


# ----------------------------------------------------------------------
# Profit & Loss
# ----------------------------------------------------------------------
def _pl_compute(ctx, company, date_from, date_to, args, detail=True, top_accounts=25):
    balances = _account_balances(ctx, company, PL_TYPES, date_from, date_to, args)
    sections = {}
    for key, label, types, sign in PL_SECTIONS:
        accounts = [(acc, vals['balance'] * sign) for acc, vals in balances.items() if acc.account_type in types]
        total = sum(amount for _acc, amount in accounts)
        section = {'label': label, 'total': utils.money(total)}
        if detail:
            accounts.sort(key=lambda item: -abs(item[1]))
            lines = [dict(_account_label(acc), amount=utils.money(amount)) for acc, amount in accounts[:top_accounts]
                     if abs(amount) >= 0.005]
            rest = accounts[top_accounts:]
            if rest:
                lines.append({'code': '', 'name': 'Other accounts (%s)' % len(rest),
                              'amount': utils.money(sum(a for _x, a in rest))})
            section['accounts'] = lines
        sections[key] = section
    revenue = sections['revenue']['total']
    gross = revenue - sections['cost_of_revenue']['total']
    operating = gross - sections['operating_expenses']['total'] - sections['depreciation']['total']
    net = operating + sections['other_income']['total'] - sections['other_expenses']['total']
    return {
        'date_from': date_from.isoformat(), 'date_to': date_to.isoformat(),
        'sections': sections,
        'gross_profit': utils.money(gross),
        'gross_margin_pct': utils.pct(gross, revenue),
        'operating_profit': utils.money(operating),
        'net_profit': utils.money(net),
        'net_margin_pct': utils.pct(net, revenue),
    }


def _pl_trend(ctx, company, date_from, date_to, args, granularity):
    aml = _aml(ctx, company)
    domain = [('company_id', '=', company.id), ('account_id.account_type', 'in', list(PL_TYPES)),
              ('date', '>=', date_from), ('date', '<=', date_to)] + _state_domain(args)
    buckets = defaultdict(lambda: defaultdict(float))
    for period, account, balance in aml._read_group(domain, ['date:%s' % granularity, 'account_id'], ['balance:sum']):
        for key, _label, types, sign in PL_SECTIONS:
            if account.account_type in types:
                buckets[period][key] += (balance or 0.0) * sign
    trend = []
    for period in sorted(buckets):
        s = buckets[period]
        gross = s['revenue'] - s['cost_of_revenue']
        net = gross - s['operating_expenses'] - s['depreciation'] + s['other_income'] - s['other_expenses']
        label = period.strftime('%Y-%m') if granularity == 'month' else (
            '%s-Q%s' % (period.year, (period.month - 1) // 3 + 1) if granularity == 'quarter' else period.isoformat())
        trend.append({'period': label, 'revenue': utils.money(s['revenue']),
                      'cost_of_revenue': utils.money(s['cost_of_revenue']),
                      'gross_profit': utils.money(gross), 'gross_margin_pct': utils.pct(gross, s['revenue']),
                      'operating_expenses': utils.money(s['operating_expenses'] + s['depreciation']),
                      'net_profit': utils.money(net), 'net_margin_pct': utils.pct(net, s['revenue'])})
    return trend


def profit_and_loss(ctx, args):
    companies = ctx.resolve_companies(args.get('company_ids'), args.get('company'))
    detail = (args.get('detail') or 'accounts') != 'summary'
    granularity = (args.get('granularity') or '').lower() or None
    if granularity and granularity not in ('month', 'quarter', 'year'):
        raise ToolError("granularity must be 'month', 'quarter' or 'year'.")
    reports = []
    for company in companies:
        date_from, date_to = _period(ctx, args, company)
        report = _company_header(company, basis='posted entries' if not utils.as_bool(args.get('include_draft')) else 'posted and draft entries')
        report.update(_pl_compute(ctx, company, date_from, date_to, args, detail=detail))
        cmp = _comparison(args, date_from, date_to)
        if cmp:
            previous = _pl_compute(ctx, company, cmp[0], cmp[1], args, detail=False)
            report['comparison'] = {
                'date_from': previous['date_from'], 'date_to': previous['date_to'],
                'revenue': previous['sections']['revenue']['total'],
                'gross_profit': previous['gross_profit'], 'operating_profit': previous['operating_profit'],
                'net_profit': previous['net_profit'],
                'sections': {k: v['total'] for k, v in previous['sections'].items()},
            }
            report['variance'] = {
                k: {'amount': utils.money(report['sections'][k]['total'] - previous['sections'][k]['total']),
                    'pct': utils.pct(report['sections'][k]['total'] - previous['sections'][k]['total'],
                                     abs(previous['sections'][k]['total']))}
                for k in report['sections']}
            report['variance']['net_profit'] = {
                'amount': utils.money(report['net_profit'] - previous['net_profit']),
                'pct': utils.pct(report['net_profit'] - previous['net_profit'], abs(previous['net_profit']))}
        if granularity:
            report['trend'] = _pl_trend(ctx, company, date_from, date_to, args, granularity)
        reports.append(report)
    result = {'report': 'Profit and Loss', 'entities': reports}
    total = _group_total(reports, 'net_profit')
    if total:
        total['revenue'] = utils.money(sum(r['sections']['revenue']['total'] for r in reports))
        result['group_total'] = total
    ctx.result_count = len(reports)
    return result


# ----------------------------------------------------------------------
# Balance sheet
# ----------------------------------------------------------------------
def balance_sheet(ctx, args):
    companies = ctx.resolve_companies(args.get('company_ids'), args.get('company'))
    detail = (args.get('detail') or 'accounts') != 'summary'
    reports = []
    for company in companies:
        as_of = utils.as_date(args.get('date') or args.get('date_to'), _today(ctx), 'date')
        fy_start = _fiscal_year(company, as_of)[0]
        balances = _account_balances(ctx, company, BS_TYPES, None, as_of, args)
        pl_current = _account_balances(ctx, company, PL_TYPES, fy_start, as_of, args)
        pl_prior = _account_balances(ctx, company, PL_TYPES, None, fy_start - datetime.timedelta(days=1), args)
        current_earnings = -sum(v['balance'] for v in pl_current.values())
        prior_earnings = -sum(v['balance'] for v in pl_prior.values())
        report = _company_header(company, date=as_of.isoformat(), fiscal_year_start=fy_start.isoformat())
        totals = {}
        for key, label, sign, groups in BS_LAYOUT:
            block = {'label': label, 'groups': []}
            block_total = 0.0
            for gkey, glabel, types in groups:
                group = {'key': gkey, 'label': glabel, 'lines': []}
                gtotal = 0.0
                for type_key, type_label in types:
                    accounts = [(acc, v['balance'] * sign) for acc, v in balances.items() if acc.account_type == type_key]
                    amount = sum(a for _x, a in accounts)
                    if not accounts:
                        continue
                    line = {'type': type_key, 'label': type_label, 'amount': utils.money(amount)}
                    if detail:
                        accounts.sort(key=lambda item: item[0].code or '')
                        line['accounts'] = [dict(_account_label(acc), amount=utils.money(a))
                                            for acc, a in accounts if abs(a) >= 0.005][:60]
                    group['lines'].append(line)
                    gtotal += amount
                if key == 'equity':
                    group['lines'].append({'type': 'retained_earnings', 'label': 'Previous Years Unallocated Earnings',
                                           'amount': utils.money(prior_earnings)})
                    group['lines'].append({'type': 'current_year_earnings', 'label': 'Current Year Earnings',
                                           'amount': utils.money(current_earnings)})
                    gtotal += prior_earnings + current_earnings
                group['total'] = utils.money(gtotal)
                block['groups'].append(group)
                block_total += gtotal
            block['total'] = utils.money(block_total)
            report[key] = block
            totals[key] = block_total
        difference = totals['assets'] - totals['liabilities'] - totals['equity']
        report['total_liabilities_and_equity'] = utils.money(totals['liabilities'] + totals['equity'])
        report['balanced'] = abs(difference) < 0.01
        if not report['balanced']:
            report['difference'] = utils.money(difference)
        report['working_capital'] = utils.money(
            sum(g['total'] for g in report['assets']['groups'] if g['key'] == 'current_assets')
            - sum(g['total'] for g in report['liabilities']['groups'] if g['key'] == 'current_liabilities'))
        reports.append(report)
    ctx.result_count = len(reports)
    return {'report': 'Balance Sheet', 'entities': reports}


# ----------------------------------------------------------------------
# Trial balance
# ----------------------------------------------------------------------
def trial_balance(ctx, args):
    companies = ctx.resolve_companies(args.get('company_ids'), args.get('company'))
    include_zero = utils.as_bool(args.get('include_zero'), False)
    reports = []
    for company in companies:
        date_from, date_to = _period(ctx, args, company)
        fy_start = _fiscal_year(company, date_from)[0]
        all_types = BS_TYPES + PL_TYPES + ('off_balance',)
        opening_bs = _account_balances(ctx, company, BS_TYPES + ('off_balance',), None, date_from - datetime.timedelta(days=1), args)
        opening_pl = _account_balances(ctx, company, PL_TYPES, fy_start, date_from - datetime.timedelta(days=1), args) \
            if fy_start < date_from else {}
        period = _account_balances(ctx, company, all_types, date_from, date_to, args)
        accounts = set(opening_bs) | set(opening_pl) | set(period)
        lines = []
        totals = defaultdict(float)
        for account in sorted(accounts, key=lambda a: a.code or ''):
            opening = opening_bs.get(account, opening_pl.get(account, {})).get('balance', 0.0)
            debit = period.get(account, {}).get('debit', 0.0)
            credit = period.get(account, {}).get('credit', 0.0)
            closing = opening + debit - credit
            if not include_zero and all(abs(v) < 0.005 for v in (opening, debit, credit)):
                continue
            lines.append(dict(_account_label(account), type=account.account_type,
                              opening=utils.money(opening), debit=utils.money(debit),
                              credit=utils.money(credit), closing=utils.money(closing)))
            for k, v in (('opening', opening), ('debit', debit), ('credit', credit), ('closing', closing)):
                totals[k] += v
        report = _company_header(company, date_from=date_from.isoformat(), date_to=date_to.isoformat(),
                                 accounts=lines[:utils.as_int(args.get('limit'), 500, 1, 2000)],
                                 totals={k: utils.money(v) for k, v in totals.items()},
                                 account_count=len(lines))
        reports.append(report)
    ctx.result_count = sum(r['account_count'] for r in reports)
    return {'report': 'Trial Balance', 'entities': reports}


# ----------------------------------------------------------------------
# Aged receivables / payables
# ----------------------------------------------------------------------
def _aged(ctx, args, kind):
    account_type = 'asset_receivable' if kind == 'receivable' else 'liability_payable'
    sign = 1 if kind == 'receivable' else -1
    companies = ctx.resolve_companies(args.get('company_ids'), args.get('company'))
    min_days = utils.as_int(args.get('min_days_overdue'), None, 0)
    top = utils.as_int(args.get('limit'), 25, 1, 500)
    include_documents = utils.as_bool(args.get('include_documents'), min_days is not None)
    partner_filter = (args.get('partner') or '').strip()
    reports = []
    for company in companies:
        as_of = utils.as_date(args.get('date'), _today(ctx), 'date')
        aml = _aml(ctx, company)
        domain = [('company_id', '=', company.id), ('account_id.account_type', '=', account_type),
                  ('parent_state', '=', 'posted'), ('reconciled', '=', False), ('date', '<=', as_of),
                  ('amount_residual', '!=', 0)]
        if partner_filter:
            domain.append(('partner_id', 'ilike', partner_filter))
        partners = defaultdict(lambda: defaultdict(float))
        totals = defaultdict(float)
        for partner, due, residual in aml._read_group(domain, ['partner_id', 'date_maturity:day'], ['amount_residual:sum']):
            amount = (residual or 0.0) * sign
            days = (as_of - due).days if due else 0
            if min_days is not None and days < min_days:
                continue
            bucket = 'not_due'
            for key, _label, low, high in AGE_BUCKETS[1:]:
                if days >= low and (high is None or days <= high):
                    bucket = key
            pkey = (partner.id, partner.display_name if partner else 'No partner')
            partners[pkey][bucket] += amount
            partners[pkey]['total'] += amount
            if bucket != 'not_due':
                partners[pkey]['overdue'] += amount
            totals[bucket] += amount
            totals['total'] += amount
            if bucket != 'not_due':
                totals['overdue'] += amount
        rows = sorted(partners.items(), key=lambda item: -abs(item[1]['total']))
        report = _company_header(company, date=as_of.isoformat(),
                                 buckets=[{'key': k, 'label': l} for k, l, _a, _b in AGE_BUCKETS],
                                 totals={k: utils.money(totals[k]) for k in [b[0] for b in AGE_BUCKETS] + ['overdue', 'total']},
                                 partner_count=len(rows),
                                 partners=[dict({'partner_id': pid or None, 'partner': pname},
                                                **{k: utils.money(v[k]) for k in [b[0] for b in AGE_BUCKETS] + ['overdue', 'total']})
                                           for (pid, pname), v in rows[:top]])
        if include_documents:
            doc_domain = list(domain)
            if min_days is not None:
                doc_domain.append(('date_maturity', '<=', as_of - datetime.timedelta(days=min_days)))
            docs = aml.search_read(doc_domain, ['move_name', 'partner_id', 'date', 'date_maturity', 'amount_residual', 'ref'],
                                   order='date_maturity asc, id asc', limit=200)
            report['documents'] = [{
                'document': d['move_name'], 'partner': d['partner_id'][1] if d['partner_id'] else None,
                'date': d['date'], 'due_date': d['date_maturity'],
                'days_overdue': max(0, (as_of - d['date_maturity']).days) if d['date_maturity'] else None,
                'amount_due': utils.money(d['amount_residual'] * sign), 'reference': d['ref'] or None,
            } for d in docs]
        reports.append(report)
    ctx.result_count = sum(r['partner_count'] for r in reports)
    title = 'Aged Receivables' if kind == 'receivable' else 'Aged Payables'
    return {'report': title, 'entities': reports,
            'note': 'Amounts due are open balances of posted documents dated on or before the report date.'}


def aged_receivables(ctx, args):
    return _aged(ctx, args, 'receivable')


def aged_payables(ctx, args):
    return _aged(ctx, args, 'payable')


# ----------------------------------------------------------------------
# Sales and purchase analysis
# ----------------------------------------------------------------------
SALES_GROUPS = {
    'orders': {'customer': 'commercial_partner_id', 'product': 'product_id', 'category': 'categ_id',
               'salesperson': 'user_id', 'team': 'team_id', 'country': 'country_id', 'company': 'company_id',
               'month': 'date:month', 'quarter': 'date:quarter', 'year': 'date:year', 'week': 'date:week'},
    'invoices': {'customer': 'commercial_partner_id', 'product': 'product_id', 'category': 'product_categ_id',
                 'salesperson': 'invoice_user_id', 'country': 'country_id', 'company': 'company_id',
                 'month': 'invoice_date:month', 'quarter': 'invoice_date:quarter', 'year': 'invoice_date:year',
                 'week': 'invoice_date:week'},
}
PURCHASE_GROUPS = {
    'orders': {'vendor': 'commercial_partner_id', 'product': 'product_id', 'category': 'category_id',
               'buyer': 'user_id', 'country': 'country_id', 'company': 'company_id',
               'month': 'date_order:month', 'quarter': 'date_order:quarter', 'year': 'date_order:year'},
    'bills': {'vendor': 'commercial_partner_id', 'product': 'product_id', 'category': 'product_categ_id',
              'country': 'country_id', 'company': 'company_id',
              'month': 'invoice_date:month', 'quarter': 'invoice_date:quarter', 'year': 'invoice_date:year'},
}


def _group_label(value, groupby):
    if hasattr(value, '_name'):
        return value.display_name if value else 'Undefined'
    if isinstance(value, (datetime.date, datetime.datetime)):
        gran = groupby.split(':')[1] if ':' in groupby else 'day'
        if gran == 'month':
            return value.strftime('%Y-%m')
        if gran == 'quarter':
            return '%s-Q%s' % (value.year, (value.month - 1) // 3 + 1)
        if gran == 'year':
            return str(value.year)
        return value.isoformat()
    return value if value not in (None, False) else 'Undefined'


def _analysis(ctx, args, spec):
    source = (args.get('source') or spec['default_source']).lower()
    if source not in spec['sources']:
        raise ToolError("source must be one of %s." % ', '.join(spec['sources']))
    src = spec['sources'][source]
    if not ctx.has_model(src['model']):
        raise ToolError("%s are not available: the %s app is not installed. Try source=%r."
                        % (source, src['app'], spec['fallback'].get(source)))
    group_key = (args.get('group_by') or spec['default_group']).lower()
    groups_map = spec['groups'][source]
    if group_key not in groups_map:
        raise ToolError("group_by must be one of %s for source %s." % (', '.join(groups_map), source))
    groupby = groups_map[group_key]
    model = ctx.check_model(src['model'], 'read')
    top = utils.as_int(args.get('limit'), 20, 1, 200)
    companies = ctx.resolve_companies(args.get('company_ids'), args.get('company'))
    search_text = (args.get('product') or '').strip()
    partner_text = (args.get('partner') or '').strip()
    reports = []
    for company in companies:
        date_from, date_to = _period(ctx, args, company)

        def run(d_from, d_to, company=company):
            domain = [('company_id', '=', company.id), (src['date'], '>=', d_from), (src['date'], '<=', d_to)] + src['domain']
            if search_text:
                domain.append(('product_id', 'ilike', search_text))
            if partner_text:
                domain.append(('commercial_partner_id', 'ilike', partner_text))
            measures = [m for m in src['measures'] if m.split(':')[0] in model._fields]
            rows = model.with_company(company)._read_group(domain, [groupby], measures)
            out = {}
            for row in rows:
                label = _group_label(row[0], groupby)
                vals = dict(zip([m.split(':')[0] for m in measures], row[1:]))
                key = row[0].id if hasattr(row[0], '_name') else label
                out[key] = (label, vals)
            return out

        current = run(date_from, date_to)
        cmp = _comparison(args, date_from, date_to)
        previous = run(*cmp) if cmp else {}
        amount_field = src['amount']
        amount_sign = src.get('sign', 1)
        total = sum((v.get(amount_field) or 0.0) * amount_sign for _l, v in current.values())
        rows = []
        for key, (label, vals) in current.items():
            amount = (vals.get(amount_field) or 0.0) * amount_sign
            row = {'name': label, 'amount': utils.money(amount), 'share_pct': utils.pct(amount, total)}
            if src.get('qty') and src['qty'] in vals:
                row['quantity'] = round(float(vals[src['qty']] or 0.0) * amount_sign, 3)
            if src.get('margin') and vals.get(src['margin']) is not None:
                row['margin'] = utils.money(vals[src['margin']])
                row['margin_pct'] = utils.pct(vals[src['margin']], amount)
            if cmp:
                prev = (previous.get(key, (None, {}))[1].get(amount_field) or 0.0) * amount_sign
                row['previous_amount'] = utils.money(prev)
                row['growth'] = utils.money(amount - prev)
                row['growth_pct'] = utils.pct(amount - prev, abs(prev)) if prev else None
            rows.append(row)
        order = (args.get('order') or 'amount').lower()
        if order == 'growth' and cmp:
            rows.sort(key=lambda r: -(r.get('growth') or 0.0))
        elif group_key in ('month', 'quarter', 'year', 'week'):
            rows.sort(key=lambda r: str(r['name']))
        else:
            rows.sort(key=lambda r: -r['amount'])
        report = _company_header(company, source=source, measure=src['label'], group_by=group_key,
                                 date_from=date_from.isoformat(), date_to=date_to.isoformat(),
                                 total=utils.money(total), row_count=len(rows), rows=rows[:top])
        if cmp:
            prev_total = sum((v.get(amount_field) or 0.0) * amount_sign for _l, v in previous.values())
            report.update(compare_date_from=cmp[0].isoformat(), compare_date_to=cmp[1].isoformat(),
                          previous_total=utils.money(prev_total),
                          growth_pct=utils.pct(total - prev_total, abs(prev_total)) if prev_total else None)
        reports.append(report)
    ctx.result_count = sum(r['row_count'] for r in reports)
    result = {'report': spec['title'], 'entities': reports}
    total = _group_total(reports, 'total')
    if total:
        result['group_total'] = total
    return result


def sales_analysis(ctx, args):
    default_source = 'orders' if ctx.has_model('sale.report') else 'invoices'
    spec = {
        'title': 'Sales Analysis', 'default_source': default_source, 'default_group': 'customer',
        'groups': SALES_GROUPS, 'fallback': {'orders': 'invoices', 'invoices': 'orders'},
        'sources': {
            'orders': {'model': 'sale.report', 'app': 'Sales', 'date': 'date', 'label': 'Untaxed amount of confirmed sales orders',
                       'domain': [('state', '=', 'sale')], 'amount': 'price_subtotal', 'qty': 'product_uom_qty',
                       'measures': ['price_subtotal:sum', 'product_uom_qty:sum', 'nbr:sum']},
            'invoices': {'model': 'account.invoice.report', 'app': 'Invoicing', 'date': 'invoice_date',
                         'label': 'Untaxed amount of posted customer invoices net of credit notes',
                         'domain': [('state', '=', 'posted'), ('move_type', 'in', ('out_invoice', 'out_refund'))],
                         'amount': 'price_subtotal', 'qty': 'quantity', 'margin': 'price_margin',
                         'measures': ['price_subtotal:sum', 'quantity:sum', 'price_margin:sum']},
        },
    }
    return _analysis(ctx, args, spec)


def purchase_analysis(ctx, args):
    default_source = 'orders' if ctx.has_model('purchase.report') else 'bills'
    spec = {
        'title': 'Purchase Analysis', 'default_source': default_source, 'default_group': 'vendor',
        'groups': PURCHASE_GROUPS, 'fallback': {'orders': 'bills', 'bills': 'orders'},
        'sources': {
            'orders': {'model': 'purchase.report', 'app': 'Purchase', 'date': 'date_order',
                       'label': 'Untaxed amount of confirmed purchase orders',
                       'domain': [('state', 'in', ('purchase', 'done'))], 'amount': 'untaxed_total', 'qty': 'qty_ordered',
                       'measures': ['untaxed_total:sum', 'qty_ordered:sum', 'price_total:sum']},
            'bills': {'model': 'account.invoice.report', 'app': 'Invoicing', 'date': 'invoice_date',
                      'label': 'Untaxed amount of posted vendor bills net of refunds',
                      'domain': [('state', '=', 'posted'), ('move_type', 'in', ('in_invoice', 'in_refund'))],
                      'amount': 'price_subtotal', 'qty': 'quantity', 'sign': -1,
                      'measures': ['price_subtotal:sum', 'quantity:sum']},
        },
    }
    return _analysis(ctx, args, spec)


# ----------------------------------------------------------------------
# Inventory
# ----------------------------------------------------------------------
def inventory_snapshot(ctx, args):
    if not ctx.has_model('stock.quant'):
        raise ToolError("Inventory is not installed in this database.")
    quant_model = ctx.check_model('stock.quant', 'read')
    group_key = (args.get('group_by') or 'product').lower()
    groups = {'product': 'product_id', 'category': 'product_categ_id', 'warehouse': 'warehouse_id', 'location': 'location_id'}
    if group_key not in groups:
        raise ToolError("group_by must be one of %s." % ', '.join(groups))
    top = utils.as_int(args.get('limit'), 25, 1, 500)
    threshold = args.get('low_stock_threshold')
    product_text = (args.get('product') or '').strip()
    reports = []
    for company in ctx.resolve_companies(args.get('company_ids'), args.get('company')):
        quants = quant_model.with_company(company)
        domain = [('company_id', '=', company.id), ('location_id.usage', '=', 'internal')]
        if product_text:
            domain.append(('product_id', 'ilike', product_text))
        if args.get('warehouse'):
            domain.append(('warehouse_id', 'ilike', args['warehouse']))
        rows = []
        total_value = 0.0
        total_qty = 0.0
        if group_key == 'product':
            data = quants._read_group(domain, ['product_id'], ['quantity:sum', 'reserved_quantity:sum'])
            for product, qty, reserved in data:
                cost = product.with_company(company).standard_price
                value = (qty or 0.0) * cost
                total_value += value
                total_qty += qty or 0.0
                rows.append({'product_id': product.id, 'product': product.display_name,
                             'default_code': product.default_code or None, 'uom': product.uom_id.name,
                             'on_hand': round(qty or 0.0, 3), 'reserved': round(reserved or 0.0, 3),
                             'available': round((qty or 0.0) - (reserved or 0.0), 3),
                             'unit_cost': utils.money(cost), 'value': utils.money(value)})
        else:
            data = quants._read_group(domain, [groups[group_key], 'product_id'], ['quantity:sum'])
            agg = defaultdict(lambda: {'on_hand': 0.0, 'value': 0.0, 'products': 0})
            for grp, product, qty in data:
                key = grp.display_name if grp else 'Undefined'
                value = (qty or 0.0) * product.with_company(company).standard_price
                agg[key]['on_hand'] += qty or 0.0
                agg[key]['value'] += value
                agg[key]['products'] += 1
                total_value += value
                total_qty += qty or 0.0
            rows = [{'name': k, 'on_hand': round(v['on_hand'], 3), 'value': utils.money(v['value']),
                     'products': v['products']} for k, v in agg.items()]
        rows.sort(key=lambda r: -r['value'])
        report = _company_header(company, group_by=group_key, total_quantity=round(total_qty, 3),
                                 total_value=utils.money(total_value), valuation_basis='quantity on hand x product cost',
                                 row_count=len(rows), rows=rows[:top])
        if threshold not in (None, ''):
            limit_qty = float(threshold)
            ctx.check_model('product.product', 'read')
            product_domain = [('type', '=', 'consu'), ('company_id', 'in', [False, company.id])]
            if 'is_storable' in ctx.env['product.product']._fields:
                product_domain.append(('is_storable', '=', True))
            products = ctx.env['product.product'].with_company(company).search(product_domain, limit=2000)
            low = [p for p in products if p.qty_available <= limit_qty]
            report['low_stock'] = [{'product_id': p.id, 'product': p.display_name, 'on_hand': p.qty_available,
                                    'forecast': p.virtual_available, 'uom': p.uom_id.name} for p in low[:200]]
        reports.append(report)
    ctx.result_count = sum(r['row_count'] for r in reports)
    return {'report': 'Inventory Snapshot', 'entities': reports}


# ----------------------------------------------------------------------
# KPI snapshot
# ----------------------------------------------------------------------
def kpi_snapshot(ctx, args):
    reports = []
    for company in ctx.resolve_companies(args.get('company_ids'), args.get('company')):
        as_of = utils.as_date(args.get('date'), _today(ctx), 'date')
        fy_start = _fiscal_year(company, as_of)[0]
        month_start = as_of.replace(day=1)
        ytd = _pl_compute(ctx, company, fy_start, as_of, {}, detail=False)
        mtd = _pl_compute(ctx, company, month_start, as_of, {}, detail=False)
        cash = _account_balances(ctx, company, ('asset_cash',), None, as_of)
        aml = _aml(ctx, company)

        def open_items(account_type, sign):
            base = [('company_id', '=', company.id), ('account_id.account_type', '=', account_type),
                    ('parent_state', '=', 'posted'), ('reconciled', '=', False), ('amount_residual', '!=', 0)]
            total = aml._read_group(base, [], ['amount_residual:sum'])[0][0] or 0.0
            overdue = aml._read_group(base + [('date_maturity', '<', as_of)], [], ['amount_residual:sum'])[0][0] or 0.0
            return utils.money(total * sign), utils.money(overdue * sign)

        ar_total, ar_overdue = open_items('asset_receivable', 1)
        ap_total, ap_overdue = open_items('liability_payable', -1)
        report = _company_header(company, date=as_of.isoformat(), fiscal_year_start=fy_start.isoformat(), kpis={
            'revenue_mtd': mtd['sections']['revenue']['total'],
            'revenue_ytd': ytd['sections']['revenue']['total'],
            'gross_profit_ytd': ytd['gross_profit'], 'gross_margin_ytd_pct': ytd['gross_margin_pct'],
            'net_profit_ytd': ytd['net_profit'], 'net_margin_ytd_pct': ytd['net_margin_pct'],
            'cash_and_bank': utils.money(sum(v['balance'] for v in cash.values())),
            'receivables_open': ar_total, 'receivables_overdue': ar_overdue,
            'payables_open': ap_total, 'payables_overdue': ap_overdue,
        })
        Move = ctx.env['account.move'].with_company(company)
        if Move.has_access('read'):
            report['kpis']['draft_customer_invoices'] = Move.search_count(
                [('company_id', '=', company.id), ('state', '=', 'draft'), ('move_type', '=', 'out_invoice')])
            report['kpis']['draft_vendor_bills'] = Move.search_count(
                [('company_id', '=', company.id), ('state', '=', 'draft'), ('move_type', '=', 'in_invoice')])
        if ctx.has_model('sale.order') and ctx.env['sale.order'].has_access('read'):
            SO = ctx.env['sale.order'].with_company(company)
            to_invoice = SO._read_group([('company_id', '=', company.id), ('state', '=', 'sale'),
                                         ('invoice_status', '=', 'to invoice')], [], ['__count', 'amount_untaxed:sum'])[0]
            report['kpis']['sales_orders_to_invoice'] = to_invoice[0]
            report['kpis']['sales_to_invoice_amount'] = utils.money(to_invoice[1])
            report['kpis']['open_quotations'] = SO.search_count([('company_id', '=', company.id), ('state', 'in', ('draft', 'sent'))])
        if ctx.has_model('purchase.order') and ctx.env['purchase.order'].has_access('read'):
            PO = ctx.env['purchase.order'].with_company(company)
            to_bill = PO._read_group([('company_id', '=', company.id), ('state', '=', 'purchase'),
                                      ('invoice_status', '=', 'to invoice')], [], ['__count', 'amount_untaxed:sum'])[0]
            report['kpis']['purchase_orders_to_bill'] = to_bill[0]
            report['kpis']['purchases_to_bill_amount'] = utils.money(to_bill[1])
        reports.append(report)
    ctx.result_count = len(reports)
    return {'report': 'KPI Snapshot', 'entities': reports}


# ----------------------------------------------------------------------
# CRM pipeline
# ----------------------------------------------------------------------
def crm_pipeline(ctx, args):
    if not ctx.has_model('crm.lead'):
        raise ToolError("CRM is not installed in this database.")
    Lead = ctx.check_model('crm.lead', 'read')
    groups = {'stage': 'stage_id', 'salesperson': 'user_id', 'team': 'team_id', 'month': 'date_deadline:month',
              'customer': 'partner_id', 'source': 'source_id'}
    group_key = (args.get('group_by') or 'stage').lower()
    if group_key not in groups:
        raise ToolError("group_by must be one of %s." % ', '.join(groups))
    groupby = groups[group_key]
    top = utils.as_int(args.get('limit'), 20, 1, 200)
    reports = []
    for company in ctx.resolve_companies(args.get('company_ids'), args.get('company')):
        date_from, date_to = _period(ctx, args, company)
        leads = Lead.with_company(company).with_context(active_test=False)
        base = [('company_id', 'in', [company.id, False]), ('type', '=', 'opportunity')]
        if args.get('salesperson'):
            base.append(('user_id', 'ilike', args['salesperson']))
        open_domain = base + [('active', '=', True), ('won_status', '=', 'pending')]
        rows = []
        total_expected = total_weighted = 0.0
        total_count = 0
        for value, count, expected, weighted in leads._read_group(
                open_domain, [groupby], ['__count', 'expected_revenue:sum', 'prorated_revenue:sum']):
            rows.append({'name': _group_label(value, groupby), 'opportunities': count,
                         'expected_revenue': utils.money(expected), 'weighted_revenue': utils.money(weighted)})
            total_expected += expected or 0.0
            total_weighted += weighted or 0.0
            total_count += count
        if group_key == 'stage':
            seq = {s.display_name: s.sequence for s in ctx.env['crm.stage'].search([])}
            rows.sort(key=lambda r: seq.get(r['name'], 999))
        elif group_key == 'month':
            rows.sort(key=lambda r: str(r['name']))
        else:
            rows.sort(key=lambda r: -r['expected_revenue'])
        closed = base + [('date_closed', '>=', date_from), ('date_closed', '<', date_to + datetime.timedelta(days=1))]
        won = leads._read_group(closed + [('won_status', '=', 'won')], [], ['__count', 'expected_revenue:sum'])[0]
        lost = leads._read_group(closed + [('won_status', '=', 'lost')], [], ['__count', 'expected_revenue:sum'])[0]
        lost_reasons = [{'reason': _group_label(r, 'lost_reason_id'), 'count': c} for r, c in leads._read_group(
            closed + [('won_status', '=', 'lost')], ['lost_reason_id'], ['__count'], order='__count desc', limit=10)]
        top_deals = leads.search_read(open_domain, ['name', 'partner_id', 'user_id', 'stage_id', 'expected_revenue',
                                                    'probability', 'date_deadline'],
                                      order='expected_revenue desc', limit=min(top, 20))
        report = _company_header(
            company, group_by=group_key, open_opportunities=total_count,
            pipeline_expected=utils.money(total_expected), pipeline_weighted=utils.money(total_weighted),
            rows=rows[:top], row_count=len(rows),
            closed_period={'date_from': date_from.isoformat(), 'date_to': date_to.isoformat(),
                           'won_count': won[0], 'won_revenue': utils.money(won[1]),
                           'lost_count': lost[0], 'lost_revenue': utils.money(lost[1]),
                           'win_rate_pct': utils.pct(won[0], won[0] + lost[0]),
                           'lost_reasons': lost_reasons},
            top_open_deals=[{
                'opportunity': d['name'], 'customer': d['partner_id'][1] if d['partner_id'] else None,
                'salesperson': d['user_id'][1] if d['user_id'] else None,
                'stage': d['stage_id'][1] if d['stage_id'] else None,
                'expected_revenue': utils.money(d['expected_revenue']), 'probability': d['probability'],
                'expected_closing': d['date_deadline'],
            } for d in top_deals])
        reports.append(report)
    ctx.result_count = sum(r['row_count'] for r in reports)
    return {'report': 'CRM Pipeline', 'entities': reports}
