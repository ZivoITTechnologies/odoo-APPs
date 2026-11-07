# -*- coding: utf-8 -*-
"""ULIP (Unified Logistics Interface Platform, Government of India).

* FASTAG/01: toll plazas a vehicle crossed (last 72 hours) -> tracking pings for trucks without GPS.
* VAHAN/01: registration, fitness, insurance, PUC, permit and tax validity -> compliance check on assignment.

Access needs ULIP registration (https://goulip.in). Login: POST {base}/user/login {username, password}
returns a token sent as ``Authorization: Bearer <token>``. Dataset calls: POST {base}/{SYSTEM}/{NN}.
"""
import random
import re
from datetime import datetime, timedelta

from lxml import etree

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .cp_integration_base import clean_plate

ULIP_URLS = {'staging': 'https://www.ulipstaging.dpiit.gov.in/ulip/v1.0.0',
             'production': 'https://www.ulip.dpiit.gov.in/ulip/v1.0.0'}
VAHAN_DATES = {
    'fitness_upto': ('rc_fit_upto', 'fitUpto', 'rc_fitness_upto'),
    'insurance_upto': ('rc_insurance_upto', 'insuranceUpto', 'rc_insurance_valid_upto'),
    'puc_upto': ('rc_pucc_upto', 'pucUpto', 'rc_puc_upto'),
    'permit_upto': ('rc_np_upto', 'rc_permit_valid_upto', 'permitValidUpto'),
    'tax_upto': ('rc_tax_upto', 'taxUpto'),
}


def _date(value):
    if not value:
        return False
    value = str(value).strip()
    for fmt in ('%d-%b-%Y', '%d-%m-%Y', '%Y-%m-%d', '%d/%m/%Y', '%d-%b-%y'):
        try:
            return datetime.strptime(value[:11], fmt).date()
        except ValueError:
            continue
    return False


class CpUlipAccount(models.Model):
    _name = 'cp.ulip.account'
    _description = 'ULIP Account'
    _inherit = ['cp.integration.mixin']
    _cp_service = 'ulip'
    _cp_secret_fields = ('password', 'token')

    name = fields.Char(required=True, default='ULIP')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    provider = fields.Selection([('ulip', 'ULIP'), ('simulator', 'Simulator (no real calls)')], default='ulip',
                                required=True)
    environment = fields.Selection([('staging', 'Staging'), ('production', 'Production')], default='staging',
                                   required=True)
    base_url_override = fields.Char(string='API Address')
    username = fields.Char(groups='cargo_pilot_tms.group_cp_manager')
    password = fields.Char(groups='cargo_pilot_tms.group_cp_manager')
    token = fields.Char(groups='cargo_pilot_tms.group_cp_manager', readonly=True)
    token_expiry = fields.Datetime(groups='cargo_pilot_tms.group_cp_manager', readonly=True)
    use_fastag = fields.Boolean(string='Track by FASTag', default=True,
                                help='Every 30 minutes, add toll plaza crossings of trucks on the road as positions.')
    fastag_all_trips = fields.Boolean(string='For All Trucks Without Recent GPS', default=True,
                                      help='Otherwise only shipments whose tracking is set to FASTag.')
    check_vahan = fields.Boolean(string='Check Vahan on Assignment', default=True)
    state = fields.Selection([('new', 'Not checked'), ('ok', 'Connected'), ('error', 'Problem')], default='new',
                             readonly=True)
    last_message = fields.Char(readonly=True)

    @property
    def _base(self):
        return (self.base_url_override or ULIP_URLS[self.environment]).rstrip('/')

    def _token(self):
        acc = self.sudo()
        now = fields.Datetime.now()
        if acc.token and acc.token_expiry and acc.token_expiry > now:
            return acc.token
        if not (acc.username and acc.password):
            raise UserError(_('Enter the ULIP username and password.'))
        data = self._cp_http('login', 'POST', self._base + '/user/login', headers={'Content-Type': 'application/json'},
                             json_body={'username': acc.username, 'password': acc.password})
        token = (data.get('response') or {}).get('id') if isinstance(data.get('response'), dict) else None
        token = token or data.get('token') or data.get('id')
        if not token:
            raise UserError(_('ULIP did not return a token: %s', data.get('message') or data))
        acc.write({'token': token, 'token_expiry': now + timedelta(hours=20)})
        return token

    def _dataset(self, path, vehicle, record=None):
        self.ensure_one()
        data = self._cp_http(path.lower(), 'POST', '%s/%s' % (self._base, path), record=record,
                             headers={'Content-Type': 'application/json', 'Authorization': 'Bearer %s' % self._token()},
                             json_body={'vehiclenumber': vehicle})
        if str(data.get('error')).lower() == 'true':
            raise UserError(_('ULIP %(p)s error: %(m)s', p=path, m=data.get('message') or data.get('code')))
        items = data.get('response') or []
        return items[0] if items else {}

    def action_test(self):
        self.ensure_one()
        if self.provider == 'simulator':
            msg = _('Simulator ready.')
        else:
            self.sudo().write({'token': False, 'token_expiry': False})
            self._token()
            msg = _('Connected to ULIP.')
        self.write({'state': 'ok', 'last_message': msg})
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'type': 'success', 'title': self.name, 'message': msg}}

    # ------------------------------------------------------------------
    def cp_fastag_events(self, vehicle_number, record=None):
        """[{'time': datetime, 'plaza': str, 'lat': float, 'lon': float, 'direction': str}] newest last."""
        plate = clean_plate(vehicle_number)
        if self.provider == 'simulator':
            return self._simulate_fastag(record)
        item = self._dataset('FASTAG/01', plate, record)
        inner = item.get('response') or {}
        if isinstance(inner, list):
            inner = inner[0] if inner else {}
        vehicle = inner.get('vehicle') or {}
        txns = (vehicle.get('vehltxnList') or {}).get('txn') or []
        events = []
        for txn in txns if isinstance(txns, list) else [txns]:
            geo = (txn.get('tollPlazaGeocode') or '').split(',')
            try:
                lat, lon = float(geo[0]), float(geo[1])
            except (ValueError, IndexError):
                continue
            when = None
            for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
                try:
                    when = datetime.strptime((txn.get('readerReadTime') or '').strip(), fmt)
                    break
                except ValueError:
                    continue
            if when:
                # FASTag times are Indian time; Odoo stores UTC
                events.append({'time': when - timedelta(hours=5, minutes=30), 'plaza': txn.get('tollPlazaName') or '',
                               'lat': lat, 'lon': lon, 'direction': txn.get('laneDirection') or ''})
        return sorted(events, key=lambda e: e['time'])

    def _simulate_fastag(self, trip):
        if not trip or not (trip.origin_id.latitude and trip.destination_id.latitude):
            return []
        o, d = trip.origin_id, trip.destination_id
        start = trip.actual_dispatch or fields.Datetime.now() - timedelta(hours=4)
        hours = max((fields.Datetime.now() - start).total_seconds() / 3600.0, 0.5)
        total = max(trip.lane_id.transit_hours or 24.0, 1.0)
        events = []
        for i in range(1, 6):
            share = i / 6.0
            if share * total > hours:
                break
            events.append({'time': start + timedelta(hours=share * total),
                           'plaza': _('Toll plaza %s', i), 'direction': 'S',
                           'lat': o.latitude + (d.latitude - o.latitude) * share,
                           'lon': o.longitude + (d.longitude - o.longitude) * share})
        return events

    def cp_vahan(self, vehicle_number, record=None):
        plate = clean_plate(vehicle_number)
        if self.provider == 'simulator':
            today = fields.Date.context_today(self)
            return {'owner_name': 'Sim Owner', 'rc_status': 'ACTIVE', 'vehicle_class': 'Goods Carrier',
                    'fitness_upto': today + timedelta(days=200), 'insurance_upto': today + timedelta(days=90),
                    'puc_upto': today + timedelta(days=(-3 if plate.endswith('0') else 60)),
                    'permit_upto': today + timedelta(days=300), 'tax_upto': today + timedelta(days=120)}
        item = self._dataset('VAHAN/01', plate, record)
        raw = item.get('response')
        values = {}
        if isinstance(raw, str) and raw.strip().startswith('<'):
            try:
                root = etree.fromstring(raw.encode())
                values = {el.tag: (el.text or '').strip() for el in root.iter() if el.text and el.text.strip()}
            except etree.XMLSyntaxError:
                values = {}
        elif isinstance(raw, dict):
            values = raw
        result = {'owner_name': values.get('rc_owner_name') or values.get('ownerName') or '',
                  'rc_status': values.get('rc_status') or values.get('status') or '',
                  'vehicle_class': values.get('rc_vh_class_desc') or values.get('vehicleClass') or ''}
        for key, names in VAHAN_DATES.items():
            result[key] = next((_date(values.get(n)) for n in names if values.get(n)), False)
        return result

    @api.model
    def cp_for(self, company):
        return self.search([('company_id', '=', company.id)], limit=1)

    @api.model
    def _cron_fastag(self):
        Trip = self.env['cp.trip']
        stale = fields.Datetime.now() - timedelta(hours=2)
        for acc in self.search([('use_fastag', '=', True)]):
            domain = [('company_id', '=', acc.company_id.id), ('state', '=', 'in_transit'), ('vehicle_number', '!=', False)]
            if acc.fastag_all_trips:
                domain += ['|', ('tracking_method', '=', 'fastag'), '|', ('last_ping_time', '=', False),
                           ('last_ping_time', '<', stale)]
            else:
                domain.append(('tracking_method', '=', 'fastag'))
            for trip in Trip.search(domain, limit=200):
                try:
                    trip._cp_fastag_sync(acc)
                except UserError:
                    continue
        return True


class CpVehicleCheck(models.Model):
    _name = 'cp.vehicle.check'
    _description = 'Vahan Vehicle Check'
    _order = 'checked_at desc, id desc'

    vehicle_number = fields.Char(required=True, index=True)
    checked_at = fields.Datetime(default=fields.Datetime.now, required=True)
    owner_name = fields.Char()
    rc_status = fields.Char(string='RC Status')
    vehicle_class = fields.Char()
    fitness_upto = fields.Date()
    insurance_upto = fields.Date()
    puc_upto = fields.Date(string='PUC Upto')
    permit_upto = fields.Date()
    tax_upto = fields.Date()
    problems = fields.Text(readonly=True)
    ok = fields.Boolean(string='All Valid')
    trip_id = fields.Many2one('cp.trip', ondelete='set null')


class CpTrip(models.Model):
    _inherit = 'cp.trip'

    vahan_check_id = fields.Many2one('cp.vehicle.check', string='Vahan Check', readonly=True, copy=False)
    vahan_ok = fields.Boolean(related='vahan_check_id.ok', string='Vehicle Papers OK')
    fastag_last_sync = fields.Datetime(readonly=True, copy=False)

    def _cp_fastag_sync(self, account=None):
        self.ensure_one()
        account = account or self.env['cp.ulip.account'].cp_for(self.company_id)
        if not account:
            raise UserError(_('Add a ULIP account first (Setup > India Integrations).'))
        if not self.vehicle_number:
            raise UserError(_('%s has no vehicle number.', self.name))
        known = set(self.ping_ids.filtered(lambda p: p.source == 'fastag').mapped('ping_time'))
        added = 0
        since = self.actual_dispatch or self.planned_dispatch
        for event in account.cp_fastag_events(self.vehicle_number, self):
            if event['time'] in known or (since and event['time'] < since - timedelta(hours=1)):
                continue
            self.cp_add_ping(event['lat'], event['lon'], ping_time=event['time'], source='fastag',
                             place=event['plaza'])
            added += 1
        self.fastag_last_sync = fields.Datetime.now()
        if added:
            self._cp_log('tracking', _('%s FASTag toll crossing(s) added.', added))
        return added

    def action_fastag_sync(self):
        added = 0
        for rec in self:
            added += rec._cp_fastag_sync()
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'type': 'info', 'title': _('FASTag'), 'message': _('%s toll crossing(s) added.', added)}}

    def _cp_vahan_check(self, account=None):
        self.ensure_one()
        account = account or self.env['cp.ulip.account'].cp_for(self.company_id)
        if not account or not self.vehicle_number:
            return False
        data = account.cp_vahan(self.vehicle_number, self)
        deadline = fields.Date.to_date(self.promised_delivery or self.planned_dispatch) or fields.Date.context_today(self)
        labels = {'fitness_upto': _('Fitness'), 'insurance_upto': _('Insurance'), 'puc_upto': _('PUC'),
                  'permit_upto': _('Permit'), 'tax_upto': _('Road tax')}
        problems = [_('%(doc)s expired on %(d)s', doc=labels[k], d=data[k]) for k in labels
                    if data.get(k) and data[k] < deadline]
        if data.get('rc_status') and data['rc_status'].upper() not in ('ACTIVE', 'A'):
            problems.append(_('Registration status is %s', data['rc_status']))
        check = self.env['cp.vehicle.check'].sudo().create(dict(
            {k: v for k, v in data.items() if k in self.env['cp.vehicle.check']._fields},
            vehicle_number=clean_plate(self.vehicle_number), trip_id=self.id, problems='\n'.join(problems) or False,
            ok=not problems))
        self.vahan_check_id = check
        if problems:
            self.env['cp.exception'].cp_raise('compliance', self, _('Vehicle papers: %s', self.vehicle_number),
                                              severity='high', detail='\n'.join(problems),
                                              suggestion=_('Ask the transporter for a vehicle with valid papers.'))
        else:
            self.env['cp.exception'].cp_auto_resolve(self, ('compliance',), _('Vahan check passed.'))
        return check

    def action_vahan_check(self):
        for rec in self:
            if not rec._cp_vahan_check():
                raise UserError(_('Add a ULIP account and the vehicle number first.'))
        return True

    def _cp_after_assign(self):
        res = super()._cp_after_assign()
        for rec in self.filtered('vehicle_number'):
            account = self.env['cp.ulip.account'].cp_for(rec.company_id)
            if account and account.check_vahan:
                try:
                    rec._cp_vahan_check(account)
                except UserError as err:
                    rec.message_post(body=_('Vahan check could not run: %s', err.args[0] if err.args else err))
        return res
