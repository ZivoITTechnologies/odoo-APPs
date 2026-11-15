# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class PumpDashboard(models.AbstractModel):
    _name = 'pump.dashboard'
    _description = 'Petrol Pump Dashboard Service'

    @api.model
    def get_dashboard_data(self, date=None):
        if not self.env.user.has_group('zivoit_fuel_management.group_pump_user'):
            raise AccessError(_('Only fuel station users can open the dashboard.'))
        day = fields.Date.to_date(date) if date else fields.Date.context_today(self)
        Line = self.env['pump.shift.sheet.line']
        VLine = self.env['pump.voucher.line']
        company = self.env.company
        cur = company.currency_id

        def acc_balance(xmlid, upto=day):
            acc = self.env.ref('zivoit_fuel_management.%s' % xmlid)
            return sum(VLine.search([('account_id', '=', acc.id), ('state', '=', 'posted'),
                                     ('date', '<=', upto)]).mapped('balance'))

        # Today's sales by product
        products = self.env['pump.product'].search([])
        today_lines = Line.search([('date', '=', day), ('state', '=', 'posted')])
        prod_rows = []
        for p in products:
            pl = today_lines.filtered(lambda l: l.product_id == p)
            prod_rows.append({
                'id': p.id, 'name': p.name, 'color': p.color or '#0b5fa5', 'type': p.product_type,
                'rate': p.get_rate(day), 'qty': sum(pl.mapped('sale_qty')), 'amount': sum(pl.mapped('amount')),
                'stock': p.stock_qty, 'uom': p.uom,
            })
        sheets = self.env['pump.shift.sheet'].search([('date', '=', day), ('state', '=', 'posted')])
        slips_today = self.env['pump.credit.sale'].search([('date', '=', day), ('state', 'in', ('confirmed', 'billed'))])

        # Receivables
        customers = self.env['res.partner'].search([('is_pump_customer', '=', True)])
        outstanding = sum(customers.mapped('pump_balance'))
        unbilled = sum(customers.mapped('pump_unbilled'))
        over_limit = customers.filtered('pump_over_limit')
        top = sorted(customers, key=lambda c: c.pump_exposure, reverse=True)[:7]
        receipts_today = sum(VLine.search([('date', '=', day), ('state', '=', 'posted'),
                                           ('voucher_type', '=', 'receipt'),
                                           ('account_type', '=', 'receivable')]).mapped('credit'))

        # 14-day trend
        start = day - timedelta(days=13)
        trend_lines = Line.search([('date', '>=', start), ('date', '<=', day), ('state', '=', 'posted')])
        labels, series = [], {p.id: [] for p in products if p.product_type == 'fuel'}
        totals = []
        for i in range(14):
            d = start + timedelta(days=i)
            labels.append(d.strftime('%d %b'))
            dl = trend_lines.filtered(lambda l: l.date == d)
            totals.append(round(sum(dl.mapped('amount')), 2))
            for pid in series:
                series[pid].append(round(sum(dl.filtered(lambda l: l.product_id.id == pid).mapped('sale_qty')), 2))

        tanks = [{
            'id': t.id, 'name': t.name, 'product': t.product_id.name, 'color': t.product_id.color or '#0b5fa5',
            'capacity': t.capacity, 'stock': round(t.stock_qty, 2), 'percent': round(t.fill_percent, 1),
            'ullage': round(t.ullage, 0), 'low': bool(t.reorder_level and t.stock_qty <= t.reorder_level),
        } for t in self.env['pump.tank'].search([])]

        recent_slips = [{
            'id': s.id, 'name': s.name, 'date': s.date.strftime('%d-%m-%Y'), 'customer': s.partner_id.display_name,
            'vehicle': s.vehicle_no or '', 'product': s.product_id.name, 'qty': s.qty, 'amount': s.amount,
            'state': s.state,
        } for s in self.env['pump.credit.sale'].search([('state', '!=', 'cancel')], limit=8)]

        Ai = self.env['pump.ai']
        fleet = self.env['fleet.vehicle'].search([('is_pump_fleet', '=', True)])
        status = fleet.mapped('pump_status')
        trips = self.env['pump.tanker.trip'].search([('state', 'in', ('planned', 'in_transit', 'delivered'))], limit=6)
        month_start = day.replace(day=1)
        fleet_cost = sum(self.env['pump.fleet.fuel.log'].search([('state', '=', 'posted'), ('date', '>=', month_start), ('date', '<=', day)]).mapped('amount')) \
            + sum(self.env['pump.maintenance.order'].search([('state', '=', 'done'), ('date', '>=', month_start), ('date', '<=', day)]).mapped('amount_total')) \
            + sum(self.env['pump.tanker.trip'].search([('state', '=', 'closed'), ('date', '>=', month_start), ('date', '<=', day)]).mapped('expense_total'))
        fleet_data = {
            'vehicles': len(fleet), 'on_trip': status.count('on_trip'), 'maintenance': status.count('maintenance'),
            'available': status.count('available'),
            'docs_alert': self.env['pump.vehicle.document'].search_count([('state', 'in', ('expiring', 'expired'))]),
            'maint_due': len(self.env['pump.maintenance.schedule'].search([]).filtered(lambda s: s.due_state != 'ok')),
            'month_cost': fleet_cost,
            'trips': [{'id': t.id, 'name': t.name, 'type': dict(t._fields['trip_type'].selection)[t.trip_type].split(' ')[0],
                       'vehicle': t.vehicle_id.license_plate or t.vehicle_id.name, 'dest': t.destination or '',
                       'qty': t.qty, 'state': t.state} for t in trips],
        }
        return {
            'ai_insights': Ai.station_insights(),
            'forecast': [{k: (v if k != 'tank' else v.id) for k, v in f.items() if k != 'order_by'} | {
                'order_by': f['order_by'].strftime('%d %b') if f['order_by'] else ''} for f in Ai.tank_forecast()],
            'fleet': fleet_data,
            'date': fields.Date.to_string(day),
            'weekday': day.strftime('%A'),
            'company': company.pump_outlet_name or company.name,
            'dealer_code': company.pump_dealer_code or '',
            'currency': cur.symbol or '₹',
            'kpi': {
                'sale_amount': sum(sheets.mapped('total_amount')),
                'sale_qty': sum(sheets.mapped('total_qty')),
                'credit_sale': sum(slips_today.mapped('amount')),
                'card_sale': sum(sheets.mapped('card_amount')) + sum(sheets.mapped('upi_amount'))
                             + sum(sheets.mapped('fleet_card_amount')),
                'cash_in_hand': acc_balance('acc_cash'),
                'bank_balance': acc_balance('acc_bank'),
                'outstanding': outstanding,
                'unbilled': unbilled,
                'receipts_today': receipts_today,
                'over_limit_count': len(over_limit),
                'customer_count': len(customers),
                'sheets_posted': len(sheets),
                'sheets_draft': self.env['pump.shift.sheet'].search_count([('state', '=', 'draft')]),
                'pending_indents': self.env['pump.indent'].search_count([('state', 'in', ('requested', 'approved'))]),
                'short_excess': sum(sheets.mapped('short_excess')),
            },
            'products': prod_rows,
            'tanks': tanks,
            'trend': {'labels': labels, 'totals': totals,
                      'series': [{'name': p.name, 'color': p.color or '#0b5fa5', 'data': series[p.id]}
                                 for p in products if p.id in series]},
            'top_customers': [{
                'id': c.id, 'name': c.display_name, 'code': c.pump_code or '', 'balance': c.pump_balance,
                'unbilled': c.pump_unbilled, 'limit': c.pump_credit_limit,
                'used': round(c.pump_exposure / c.pump_credit_limit * 100, 1) if c.pump_credit_limit else 0,
                'over': c.pump_over_limit,
            } for c in top],
            'recent_slips': recent_slips,
        }
