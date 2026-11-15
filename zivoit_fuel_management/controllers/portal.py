# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

from odoo import fields, http
from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.addons.portal.controllers.web import Home
from odoo.exceptions import ValidationError
from odoo.http import content_disposition, request

from .excel import ledger_xlsx, slips_xlsx, xlsx_response

_logger = logging.getLogger(__name__)


def _parse_date(value, default):
    try:
        return fields.Date.to_date(value) if value else default
    except (ValueError, TypeError):
        return default


class PumpHome(Home):

    def _login_redirect(self, uid, redirect=None):
        if not redirect:
            user = request.env['res.users'].sudo().browse(uid)
            if user._is_portal() and user.partner_id.commercial_partner_id.is_pump_customer:
                redirect = '/my/fuel'
        return super()._login_redirect(uid, redirect=redirect)


class PumpCustomerPortal(CustomerPortal):

    # ------------------------------------------------------------------ helpers
    def _pump_partner(self):
        partner = request.env.user.partner_id.commercial_partner_id.sudo()
        return partner if partner.is_pump_customer else None

    def _pump_render(self, template, active, values):
        partner = values.get('partner') or self._pump_partner()
        company = request.env.company.sudo()
        base = {
            'partner': partner, 'company': company, 'active_menu': active,
            'currency': company.currency_id, 'today': fields.Date.context_today(request.env.user),
            'fmt': lambda v: '{:,.2f}'.format(v or 0.0),
            'drcr': lambda v: '%s %s' % ('{:,.2f}'.format(abs(v or 0.0)), 'Dr' if (v or 0.0) >= 0 else 'Cr'),
            'dfmt': lambda d: d.strftime('%d-%m-%Y') if d else '',
        }
        today = base['today']
        fy = request.env['res.partner'].sudo().pump_current_fy(today)
        fy_start, _fy_end = request.env['res.partner'].sudo().pump_fy_bounds(fy)
        last_month_end = today.replace(day=1) - timedelta(days=1)
        base['presets'] = [
            ('This Month', today.replace(day=1), today),
            ('Last Month', last_month_end.replace(day=1), last_month_end),
            ('Last 90 Days', today - timedelta(days=89), today),
            ('This FY', fy_start, today),
        ]
        base.update(values)
        return request.render(template, base)

    def _no_access(self):
        return request.render('zivoit_fuel_management.portal_fuel_not_linked', {'company': request.env.company.sudo()})

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if 'pump_bill_count' in counters:
            partner = self._pump_partner()
            values['pump_bill_count'] = request.env['pump.bill'].sudo().search_count(
                [('partner_id', '=', partner.id), ('state', '=', 'posted')]) if partner else 0
        return values

    def _period(self, kw, default_days=None):
        today = fields.Date.context_today(request.env.user)
        default_from = today - timedelta(days=default_days) if default_days else today.replace(day=1)
        d_from = _parse_date(kw.get('date_from'), default_from)
        d_to = _parse_date(kw.get('date_to'), today)
        if d_from > d_to:
            d_from, d_to = d_to, d_from
        return d_from, d_to

    # ------------------------------------------------------------------ dashboard
    @http.route(['/my/fuel'], type='http', auth='user', website=False)
    def fuel_dashboard(self, **kw):
        partner = self._pump_partner()
        if not partner:
            return self._no_access()
        today = fields.Date.context_today(request.env.user)
        products = request.env['pump.product'].sudo().search([])
        rates = []
        for p in products:
            cur = p._get_rate_record(today)
            prev = request.env['pump.rate'].sudo().search(
                [('product_id', '=', p.id), ('date', '<', cur.date)], order='date desc', limit=1) if cur else False
            rates.append({'product': p, 'rate': cur.rate if cur else 0.0, 'date': cur.date if cur else False,
                          'change': (cur.rate - prev.rate) if cur and prev else 0.0})
        Sale = request.env['pump.credit.sale'].sudo()
        month_start = today.replace(day=1)
        month_slips = Sale.search([('partner_id', '=', partner.id), ('date', '>=', month_start),
                                   ('state', 'in', ('confirmed', 'billed'))])
        month_by_product = {}
        for s in month_slips:
            r = month_by_product.setdefault(s.product_id, {'product': s.product_id, 'qty': 0.0, 'amount': 0.0})
            r['qty'] += s.qty
            r['amount'] += s.amount
        last_receipt = request.env['pump.voucher.line'].sudo().search(
            [('partner_id', '=', partner.id), ('state', '=', 'posted'), ('voucher_type', '=', 'receipt'),
             ('account_type', '=', 'receivable')], order='date desc, id desc', limit=1)
        last_bill = request.env['pump.bill'].sudo().search(
            [('partner_id', '=', partner.id), ('state', '=', 'posted')], order='date desc, id desc', limit=1)
        used_pct = (partner.pump_exposure / partner.pump_credit_limit * 100.0) if partner.pump_credit_limit else 0.0
        open_bills = request.env['pump.bill'].sudo().search(
            [('partner_id', '=', partner.id), ('state', '=', 'posted')]).filtered(lambda b: b.amount_due > 0.5)
        return self._pump_render('zivoit_fuel_management.portal_fuel_dashboard', 'dashboard', {
            'partner': partner, 'rates': rates,
            'recent_slips': Sale.search([('partner_id', '=', partner.id), ('state', '!=', 'cancel'),
                                         ('state', '!=', 'draft')], limit=6),
            'month_summary': list(month_by_product.values()),
            'month_total': sum(month_slips.mapped('amount')), 'month_qty': sum(month_slips.mapped('qty')),
            'last_receipt': last_receipt, 'last_bill': last_bill,
            'used_pct': min(used_pct, 100.0), 'used_pct_raw': used_pct,
            'insights': request.env['pump.ai'].sudo().customer_insights(partner),
            'open_bills': open_bills.sorted('due_date'),
            'vehicle_count': len(partner.pump_vehicle_ids),
            'open_indents': request.env['pump.indent'].sudo().search_count(
                [('partner_id', '=', partner.id), ('state', 'in', ('requested', 'approved'))]),
        })

    # ------------------------------------------------------------------ ledger
    @http.route(['/my/fuel/ledger'], type='http', auth='user', website=False)
    def fuel_ledger(self, **kw):
        partner = self._pump_partner()
        if not partner:
            return self._no_access()
        d_from, d_to = self._period(kw)
        data = partner.pump_ledger(d_from, d_to)
        return self._pump_render('zivoit_fuel_management.portal_fuel_ledger', 'ledger', {
            'partner': partner, 'data': data, 'date_from': d_from, 'date_to': d_to})

    @http.route(['/my/fuel/ledger/pdf'], type='http', auth='user', website=False)
    def fuel_ledger_pdf(self, **kw):
        partner = self._pump_partner()
        if not partner:
            return self._no_access()
        d_from, d_to = self._period(kw)
        pdf, _t = request.env['ir.actions.report'].sudo()._render_qweb_pdf(
            'zivoit_fuel_management.action_report_statement', [partner.id],
            data={'date_from': str(d_from), 'date_to': str(d_to)})
        return request.make_response(pdf, headers=[
            ('Content-Type', 'application/pdf'),
            ('Content-Disposition', content_disposition('Statement_%s_%s_%s.pdf' % (partner.pump_code, d_from, d_to)))])

    @http.route(['/my/fuel/ledger/xlsx'], type='http', auth='user', website=False)
    def fuel_ledger_xlsx(self, **kw):
        partner = self._pump_partner()
        if not partner:
            return self._no_access()
        d_from, d_to = self._period(kw)
        content = ledger_xlsx(request.env(su=True), partner, d_from, d_to)
        return xlsx_response(content, 'Ledger_%s_%s_%s.xlsx' % (partner.pump_code, d_from, d_to))

    # ------------------------------------------------------------------ monthly
    @http.route(['/my/fuel/monthly'], type='http', auth='user', website=False)
    def fuel_monthly(self, fy=None, **kw):
        partner = self._pump_partner()
        if not partner:
            return self._no_access()
        Partner = request.env['res.partner'].sudo()
        current = Partner.pump_current_fy()
        try:
            fy_year = int(fy) if fy else current
        except ValueError:
            fy_year = current
        data = partner.pump_monthly(fy_year)
        years = [(y, '%s-%s' % (y, str(y + 1)[-2:])) for y in range(current, current - 4, -1)]
        return self._pump_render('zivoit_fuel_management.portal_fuel_monthly', 'monthly', {
            'partner': partner, 'data': data, 'fy_year': fy_year, 'years': years})

    # ------------------------------------------------------------------ statement
    @http.route(['/my/fuel/statement'], type='http', auth='user', website=False)
    def fuel_statement(self, **kw):
        partner = self._pump_partner()
        if not partner:
            return self._no_access()
        d_from, d_to = self._period(kw)
        data = partner.pump_ledger(d_from, d_to)
        bills = request.env['pump.bill'].sudo().search([
            ('partner_id', '=', partner.id), ('state', '=', 'posted'),
            ('date', '>=', d_from), ('date', '<=', d_to)], order='date')
        receipts = [l for l in data['lines'] if l['vtype'] == 'Receipt']
        return self._pump_render('zivoit_fuel_management.portal_fuel_statement', 'statement', {
            'partner': partner, 'data': data, 'bills': bills, 'receipts': receipts,
            'date_from': d_from, 'date_to': d_to, 'show': bool(kw.get('date_from'))})

    # ------------------------------------------------------------------ bills
    @http.route(['/my/fuel/bills'], type='http', auth='user', website=False)
    def fuel_bills(self, **kw):
        partner = self._pump_partner()
        if not partner:
            return self._no_access()
        d_from, d_to = self._period(kw, default_days=180)
        bills = request.env['pump.bill'].sudo().search([
            ('partner_id', '=', partner.id), ('state', '=', 'posted'),
            ('date', '>=', d_from), ('date', '<=', d_to)])
        return self._pump_render('zivoit_fuel_management.portal_fuel_bills', 'bills', {
            'partner': partner, 'bills': bills, 'date_from': d_from, 'date_to': d_to})

    @http.route(['/my/fuel/bill/<int:bill_id>'], type='http', auth='user', website=False)
    def fuel_bill_view(self, bill_id, **kw):
        partner = self._pump_partner()
        bill = request.env['pump.bill'].sudo().browse(bill_id).exists()
        if not partner or not bill or bill.partner_id != partner or bill.state != 'posted':
            raise request.not_found()
        return self._pump_render('zivoit_fuel_management.portal_fuel_bill_detail', 'bills', {
            'partner': partner, 'bill': bill, 'acc': bill.account_summary(), 'qr': bill.upi_qr_data_uri(bill.amount_due)})

    @http.route(['/my/fuel/vehicles'], type='http', auth='user', website=False)
    def fuel_vehicles(self, **kw):
        partner = self._pump_partner()
        if not partner:
            return self._no_access()
        Sale = request.env['pump.credit.sale'].sudo()
        today = fields.Date.context_today(request.env.user)
        rows = []
        for v in partner.pump_vehicle_ids:
            last = Sale.search([('vehicle_id', '=', v.id), ('state', 'in', ('confirmed', 'billed'))], limit=6)
            rows.append({'v': v, 'last': last})
        months = []
        cur = today.replace(day=1)
        for _i in range(6):
            months.insert(0, cur)
            cur = (cur - timedelta(days=1)).replace(day=1)
        chart = []
        for v in partner.pump_vehicle_ids:
            vals = []
            for mstart in months:
                mend = (mstart + timedelta(days=32)).replace(day=1) - timedelta(days=1)
                vals.append(sum(Sale.search([('vehicle_id', '=', v.id), ('state', 'in', ('confirmed', 'billed')),
                                             ('date', '>=', mstart), ('date', '<=', mend)]).mapped('qty')))
            chart.append({'v': v, 'vals': vals, 'max': max(vals) or 1})
        anomalies = request.env['pump.ai'].sudo().recent_anomalies(days=30, partner=partner)
        return self._pump_render('zivoit_fuel_management.portal_fuel_vehicles', 'vehicles', {
            'partner': partner, 'rows': rows, 'months': months, 'chart': chart, 'anomalies': anomalies})

    @http.route(['/my/fuel/ai/ask'], type='http', auth='user', methods=['POST'], website=False)
    def fuel_ai_ask(self, question='', **kw):
        partner = self._pump_partner()
        if not partner:
            return request.make_json_response({'answer': request.env._('Your login is not linked to a fuel account.'), 'links': []})
        if not request.env.company.sudo().pump_ai_enabled:
            return request.make_json_response({'answer': request.env._('The assistant is turned off by the station.'), 'links': []})
        res = request.env['pump.ai'].sudo().ask(question, partner=partner, mode='customer')
        return request.make_json_response({'answer': res['answer'], 'links': res.get('links', []),
                                           'source': res.get('source')})

    @http.route(['/my/fuel/bill/<int:bill_id>/pdf'], type='http', auth='user', website=False)
    def fuel_bill_pdf(self, bill_id, **kw):
        partner = self._pump_partner()
        bill = request.env['pump.bill'].sudo().browse(bill_id).exists()
        if not partner or not bill or bill.partner_id != partner or bill.state != 'posted':
            raise request.not_found()
        pdf, _t = request.env['ir.actions.report'].sudo()._render_qweb_pdf(
            'zivoit_fuel_management.action_report_bill', [bill.id])
        disp = 'inline' if kw.get('view') else 'attachment'
        return request.make_response(pdf, headers=[
            ('Content-Type', 'application/pdf'),
            ('Content-Disposition', content_disposition('%s.pdf' % bill.name.replace('/', '_'), disp))])

    # ------------------------------------------------------------------ slips
    def _slip_domain(self, partner, d_from, d_to, kw):
        domain = [('partner_id', '=', partner.id), ('state', 'in', ('confirmed', 'billed')),
                  ('date', '>=', d_from), ('date', '<=', d_to)]
        if kw.get('vehicle'):
            domain.append(('vehicle_no', 'ilike', kw['vehicle']))
        if kw.get('product'):
            try:
                domain.append(('product_id', '=', int(kw['product'])))
            except ValueError:
                _logger.debug('Ignoring invalid product filter %r', kw.get('product'))
        if kw.get('status') in ('confirmed', 'billed'):
            domain.append(('state', '=', kw['status']))
        return domain

    @http.route(['/my/fuel/slips'], type='http', auth='user', website=False)
    def fuel_slips(self, **kw):
        partner = self._pump_partner()
        if not partner:
            return self._no_access()
        d_from, d_to = self._period(kw)
        slips = request.env['pump.credit.sale'].sudo().search(self._slip_domain(partner, d_from, d_to, kw))
        return self._pump_render('zivoit_fuel_management.portal_fuel_slips', 'slips', {
            'partner': partner, 'slips': slips, 'date_from': d_from, 'date_to': d_to,
            'products': request.env['pump.product'].sudo().search([]),
            'f_vehicle': kw.get('vehicle', ''), 'f_product': kw.get('product', ''), 'f_status': kw.get('status', ''),
            'qs': request.httprequest.query_string.decode()})

    @http.route(['/my/fuel/slips/xlsx'], type='http', auth='user', website=False)
    def fuel_slips_xlsx(self, **kw):
        partner = self._pump_partner()
        if not partner:
            return self._no_access()
        d_from, d_to = self._period(kw)
        slips = request.env['pump.credit.sale'].sudo().search(self._slip_domain(partner, d_from, d_to, kw), order='date, id')
        return xlsx_response(slips_xlsx(request.env(su=True), partner, slips, d_from, d_to),
                             'FillingSlips_%s_%s_%s.xlsx' % (partner.pump_code, d_from, d_to))

    @http.route(['/my/fuel/slip/<int:slip_id>/pdf'], type='http', auth='user', website=False)
    def fuel_slip_pdf(self, slip_id, **kw):
        partner = self._pump_partner()
        slip = request.env['pump.credit.sale'].sudo().browse(slip_id).exists()
        if not partner or not slip or slip.partner_id != partner or slip.state in ('draft', 'cancel'):
            raise request.not_found()
        pdf, _t = request.env['ir.actions.report'].sudo()._render_qweb_pdf('zivoit_fuel_management.action_report_slip', [slip.id])
        return request.make_response(pdf, headers=[
            ('Content-Type', 'application/pdf'),
            ('Content-Disposition', content_disposition('%s.pdf' % slip.name.replace('/', '_'), 'inline'))])

    # ------------------------------------------------------------------ indent
    @http.route(['/my/fuel/indent'], type='http', auth='user', website=False, methods=['GET', 'POST'])
    def fuel_indent(self, **kw):
        partner = self._pump_partner()
        if not partner:
            return self._no_access()
        error, success = None, None
        Indent = request.env['pump.indent'].sudo()
        if request.httprequest.method == 'POST':
            try:
                vehicle = request.env['pump.vehicle'].sudo().browse(int(kw.get('vehicle_id') or 0)).exists()
                product = request.env['pump.product'].sudo().browse(int(kw.get('product_id') or 0)).exists()
                if vehicle and vehicle.partner_id != partner:
                    vehicle = request.env['pump.vehicle']
                if not product:
                    raise ValidationError(request.env._('Please select a product.'))
                itype = kw.get('indent_type') if kw.get('indent_type') in ('qty', 'amount', 'full') else 'qty'
                qty = float(kw.get('qty') or 0)
                amount = qty if itype == 'amount' else 0.0
                est = amount or qty * product.get_rate()
                if partner.pump_credit_limit and partner.pump_exposure + est > partner.pump_credit_limit:
                    raise ValidationError(request.env._('This indent exceeds your available credit of %.2f.',
                                            partner.pump_available_credit))
                ind = Indent.create({
                    'partner_id': partner.id, 'vehicle_id': vehicle.id if vehicle else False,
                    'product_id': product.id, 'indent_type': itype,
                    'qty': qty if itype == 'qty' else 0.0, 'amount': amount,
                    'comment': (kw.get('comment') or '')[:200], 'source': 'portal',
                    'date': _parse_date(kw.get('date'), fields.Date.context_today(request.env.user)),
                })
                ind.message_post(body=request.env._('Indent raised from customer portal by %s', request.env.user.name))
                success = request.env._('Indent %s submitted. The station will confirm it shortly.', ind.name)
            except (ValueError, ValidationError) as e:
                error = str(e.args[0]) if getattr(e, 'args', None) else str(e)
        prefill = {k: kw.get(k, '') for k in ('vehicle', 'product', 'qty', 'type')} \
            if request.httprequest.method == 'GET' else {}
        return self._pump_render('zivoit_fuel_management.portal_fuel_indent', 'indent', {
            'partner': partner, 'error': error, 'success': success, 'prefill': prefill,
            'vehicles': partner.pump_vehicle_ids,
            'products': request.env['pump.product'].sudo().search([]),
            'indents': Indent.search([('partner_id', '=', partner.id)], limit=20)})

    # ------------------------------------------------------------------ rates
    @http.route(['/my/fuel/rates'], type='http', auth='user', website=False)
    def fuel_rates(self, **kw):
        partner = self._pump_partner()
        if not partner:
            return self._no_access()
        today = fields.Date.context_today(request.env.user)
        rates = request.env['pump.rate'].sudo().search([('date', '>=', today - timedelta(days=60))],
                                                       order='date desc, product_id')
        return self._pump_render('zivoit_fuel_management.portal_fuel_rates', 'rates', {
            'partner': partner, 'rates': rates, 'products': request.env['pump.product'].sudo().search([])})
