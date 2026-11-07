# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models


class CpDashboard(models.AbstractModel):
    _name = 'cp.dashboard'
    _description = 'Cargo Pilot Control Tower'

    @staticmethod
    def _cp_age(since, now):
        if not since:
            return ''
        minutes = int((now - since).total_seconds() // 60)
        if minutes < 60:
            return '%dm' % max(minutes, 1)
        if minutes < 1440:
            return '%dh' % (minutes // 60)
        return '%dd' % (minutes // 1440)

    @api.model
    def get_dashboard_data(self):
        """KPIs for the control tower client action (respects the user's access rights)."""
        env = self.env
        Trip, Indent, Auction = env['cp.trip'], env['cp.indent'], env['cp.auction']
        Bill, Epod, Doc = env['cp.freight.bill'], env['cp.epod'], env['cp.document']
        now = fields.Datetime.now()
        month_start = fields.Date.context_today(self).replace(day=1)
        currency = env.company.currency_id

        moving = ('in_transit', 'arrived')
        done_month = Trip.search([('state', 'in', ('delivered', 'closed')), ('actual_delivery', '>=', month_start)])
        on_time = done_month.filtered(lambda t: not t.is_late)
        awarded = Auction.search([('state', '=', 'awarded'), ('date_end', '>=', month_start)])

        # Trips by stage, for the pipeline strip
        stage_labels = dict(Trip._fields['state'].selection)
        pipeline = []
        for key in ('draft', 'assigned', 'reported', 'loading', 'in_transit', 'arrived', 'delivered'):
            pipeline.append({'key': key, 'label': stage_labels[key],
                             'count': Trip.search_count([('state', '=', key)])})

        # Daily deliveries, last 14 days
        daily = []
        for offset in range(13, -1, -1):
            day = fields.Date.context_today(self) - timedelta(days=offset)
            start = fields.Datetime.to_datetime(day)
            count = Trip.search_count([('actual_delivery', '>=', start),
                                       ('actual_delivery', '<', start + timedelta(days=1))])
            daily.append({'label': day.strftime('%d %b'), 'value': count})

        # Top transporters by delivered trips this month
        transporters = {}
        for trip in done_month:
            if trip.transporter_id:
                entry = transporters.setdefault(trip.transporter_id.id, {
                    'name': trip.transporter_id.name, 'trips': 0, 'late': 0})
                entry['trips'] += 1
                entry['late'] += 1 if trip.is_late else 0
        top = sorted(transporters.values(), key=lambda e: -e['trips'])[:5]
        for entry in top:
            entry['on_time'] = round((entry['trips'] - entry['late']) / entry['trips'] * 100) if entry['trips'] else 0

        alerts = []
        for trip in Trip.search([('state', 'in', moving), '|', ('is_late', '=', True), ('is_stopped', '=', True)],
                                limit=8, order='eta desc'):
            alerts.append({
                'id': trip.id, 'name': trip.name, 'vehicle': trip.vehicle_number or '',
                'lane': trip.lane_id.name, 'kind': 'Stopped' if trip.is_stopped else 'Late',
                'eta': fields.Datetime.to_string(trip.eta) if trip.eta else '',
            })

        # Issues inbox (exception-first)
        Issue = env['cp.exception']
        open_issues = Issue.search([('state', '!=', 'resolved')], limit=8)
        issues = [{
            'id': i.id, 'name': i.name, 'severity': i.severity, 'state': i.state,
            'type': dict(Issue._fields['issue_type'].selection).get(i.issue_type),
            'suggestion': i.suggested_action or '', 'owner': i.user_id.name or '',
            'age': self._cp_age(i.detected_at, now),
        } for i in open_issues]

        # Live map: vehicles with a known position, plus their lane end-points
        positions = []
        for trip in Trip.search([('state', 'in', ('assigned', 'reported', 'loading', 'in_transit', 'arrived'))],
                                limit=200):
            lat, lon = trip.last_latitude, trip.last_longitude
            if not (lat or lon):
                lat, lon = trip.origin_id.latitude, trip.origin_id.longitude
            if not (lat or lon):
                continue
            positions.append({
                'id': trip.id, 'name': trip.name, 'vehicle': trip.vehicle_number or '',
                'lat': lat, 'lon': lon, 'state': trip.state,
                'status': 'stopped' if trip.is_stopped else ('late' if trip.is_late else 'ok'),
                'lane': trip.lane_id.name, 'eta': fields.Datetime.to_string(trip.eta) if trip.eta else '',
                'dest': [trip.destination_id.latitude, trip.destination_id.longitude]
                if (trip.destination_id.latitude or trip.destination_id.longitude) else None,
                'origin': [trip.origin_id.latitude, trip.origin_id.longitude]
                if (trip.origin_id.latitude or trip.origin_id.longitude) else None,
            })

        # Sustainability
        month_trips = Trip.search([('state', 'in', ('delivered', 'closed')), ('actual_delivery', '>=', month_start)])
        co2 = sum(month_trips.mapped('co2_kg'))
        tkm = sum((t.loaded_weight_ton or 0) * (t.gps_distance_km or t.lane_id.distance_km or 0) for t in month_trips)

        # Setup checklist for new users
        company = env.company
        setup = [
            {'key': 'locations', 'label': 'Add your plants, depots and customer sites',
             'done': env['cp.location'].search_count([]) >= 2, 'action': 'cargo_pilot_tms.action_cp_location'},
            {'key': 'vehicles', 'label': 'Define the truck types you use',
             'done': bool(env['cp.vehicle.type'].search_count([])), 'action': 'cargo_pilot_tms.action_cp_vehicle_type'},
            {'key': 'transporters', 'label': 'Add transporters and invite them to the portal',
             'done': bool(env['res.partner'].search_count([('cp_is_transporter', '=', True)])),
             'action': 'cargo_pilot_tms.action_cp_transporter'},
            {'key': 'contracts', 'label': 'Load your contracted lane rates',
             'done': bool(env['cp.rate.contract'].search_count([('state', '=', 'active')])),
             'action': 'cargo_pilot_tms.action_cp_rate_contract'},
            {'key': 'first_load', 'label': 'Book your first load',
             'done': bool(Trip.search_count([])), 'action': 'cargo_pilot_tms.action_cp_booking'},
            {'key': 'api', 'label': 'Connect GPS devices or your ERP (API key)',
             'done': bool(company.sudo().cp_api_key), 'action': 'cargo_pilot_tms.action_cp_settings'},
        ]

        return {
            'issues': issues,
            'issue_counts': {sev: Issue.search_count([('state', '!=', 'resolved'), ('severity', '=', sev)])
                             for sev in ('critical', 'high', 'medium', 'low')},
            'positions': positions,
            'co2': {'month_kg': round(co2), 'intensity': round(co2 * 1000 / tkm, 1) if tkm else 0,
                    'per_trip': round(co2 / len(month_trips)) if month_trips else 0},
            'setup': setup,
            'setup_done': all(step['done'] for step in setup),
            'currency': {'symbol': currency.symbol, 'position': currency.position},
            'kpis': {
                'open_indents': Indent.search_count([('state', 'in', ('confirmed', 'sourcing'))]),
                'live_auctions': Auction.search_count([('state', 'in', ('scheduled', 'live'))]),
                'in_transit': Trip.search_count([('state', 'in', moving)]),
                'late': Trip.search_count([('state', 'in', moving), ('is_late', '=', True)]),
                'stopped': Trip.search_count([('state', '=', 'in_transit'), ('is_stopped', '=', True)]),
                'epod_pending': Trip.search_count([('state', 'in', ('arrived', 'delivered')),
                                                   ('epod_state', 'in', ('none', 'pending', 'exception'))]),
                'bills_in_audit': Bill.search_count([('state', 'in', ('submitted', 'audited'))]),
                'audit_savings': sum(Bill.search([('state', '=', 'approved'),
                                                  ('bill_date', '>=', month_start)]).mapped('deduction_total')),
                'auction_savings': sum(awarded.mapped('savings_amount')),
                'delivered_month': len(done_month),
                'on_time_pct': round(len(on_time) / len(done_month) * 100) if done_month else 0,
                'epod_exceptions': Epod.search_count([('state', '=', 'exception')]),
                'docs_expiring': Doc.search_count([('expiry_date', '<=', fields.Date.context_today(self)
                                                    + timedelta(days=env.company.cp_document_alert_days or 15))]),
            },
            'pipeline': pipeline,
            'daily': daily,
            'top_transporters': top,
            'alerts': alerts,
            'generated_at': fields.Datetime.to_string(now),
        }
