# -*- coding: utf-8 -*-
"""Zoho Books and Zoho Creator connector.

Books (https://www.zohoapis.{dc}/books/v3, header ``Authorization: Zoho-oauthtoken <token>``):
* transporters -> vendors (contacts, contact_type = vendor)
* approved freight bills -> vendor bills, with reverse charge (RCM) and TDS
* freight cost per order -> custom field on the sales invoice (PUT /invoice/{id}/customfields)

Creator (https://www.zohoapis.{dc}/creator/v2.1/data/{owner}/{app}):
* one record per shipment in a form, kept up to date as the shipment moves

Access tokens come from the refresh token (POST https://accounts.zoho.{dc}/oauth/v2/token).
Every change is queued as a sync job and retried with back-off, so a Zoho outage never blocks work.
"""
import json
import logging
import time
from datetime import timedelta

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

DATA_CENTERS = [('in', 'India (zoho.in)'), ('com', 'US (zoho.com)'), ('eu', 'Europe (zoho.eu)'),
                ('com.au', 'Australia (zoho.com.au)'), ('jp', 'Japan (zoho.jp)'), ('sa', 'Saudi Arabia (zoho.sa)')]
KINDS = [('vendor', 'Transporter to vendor'), ('bill', 'Freight bill to vendor bill'),
         ('invoice_freight', 'Freight on sales invoice'), ('creator', 'Shipment to Creator')]
BACKOFF_MINUTES = [5, 15, 60, 240, 720]
DEFAULT_CREATOR_MAP = {
    'Shipment_No': 'name', 'Status': 'status', 'Mode': 'mode', 'Order_Numbers': 'orders',
    'Invoice_Numbers': 'invoices', 'Origin': 'origin', 'Destination': 'destination',
    'Transporter': 'transporter', 'Vehicle_Number': 'vehicle', 'Docket_Number': 'docket',
    'Dispatched_On': 'dispatched', 'ETA': 'eta', 'Delivered_On': 'delivered', 'Freight_Cost': 'freight',
    'POD_Status': 'pod', 'Tracking_Link': 'url',
}


class CpZohoAccount(models.Model):
    _name = 'cp.zoho.account'
    _description = 'Zoho Connection'

    name = fields.Char(required=True, default='Zoho')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    provider = fields.Selection([('zoho', 'Zoho'), ('simulator', 'Simulator (no real calls)')], default='zoho',
                                required=True)
    data_center = fields.Selection(DATA_CENTERS, default='in', required=True)
    client_id = fields.Char(groups='cargo_pilot_tms.group_cp_manager')
    client_secret = fields.Char(groups='cargo_pilot_tms.group_cp_manager')
    refresh_token = fields.Char(groups='cargo_pilot_tms.group_cp_manager',
                                help='Self client in api-console.zoho.in, scopes ZohoBooks.fullaccess.all and '
                                     'ZohoCreator.report.ALL,ZohoCreator.form.CREATE')
    access_token = fields.Char(groups='cargo_pilot_tms.group_cp_manager', readonly=True)
    token_expiry = fields.Datetime(groups='cargo_pilot_tms.group_cp_manager', readonly=True)
    state = fields.Selection([('new', 'Not checked'), ('ok', 'Connected'), ('error', 'Problem')], default='new',
                             readonly=True)
    last_message = fields.Char(readonly=True)

    # Books
    use_books = fields.Boolean(string='Zoho Books', default=True)
    organization_id = fields.Char(string='Books Organization ID')
    skip_odoo_bill = fields.Boolean(string='Post Freight Bills Only in Zoho', default=True,
                                    help='Approved freight bills become vendor bills in Zoho Books instead of Odoo.')
    expense_account_id = fields.Char(string='Freight Expense Account ID', help='Zoho Books account_id for freight.')
    tax_id = fields.Char(string='GST Tax ID', help='Zoho Books tax_id for GST on freight (for example 5% or 12%).')
    reverse_charge = fields.Boolean(string='Reverse Charge (RCM)', default=True,
                                    help='GTA services under RCM: the bill is marked is_reverse_charge_applied.')
    tds_tax_id = fields.Char(string='TDS Tax ID', help='Zoho Books tds_tax_id, for example section 194C.')
    hsn_sac = fields.Char(string='SAC Code', default='996511')
    push_invoice_freight = fields.Boolean(string='Freight on Sales Invoices', default=False)
    invoice_customfield_id = fields.Char(string='Invoice Custom Field ID',
                                         help='customfield_id of a number field on Zoho invoices, for example '
                                              '"Freight cost". Settings > Preferences > Invoices > Field Customization.')
    # Creator
    use_creator = fields.Boolean(string='Zoho Creator', default=False)
    creator_owner = fields.Char(string='Account Owner Name')
    creator_app = fields.Char(string='Application Link Name')
    creator_form = fields.Char(string='Form Link Name', default='Shipment')
    creator_report = fields.Char(string='Report Link Name', default='All_Shipments')
    creator_field_map = fields.Text(string='Field Map', default=lambda self: json.dumps(DEFAULT_CREATOR_MAP, indent=1),
                                    help='JSON: Creator field link name -> name, status, mode, orders, invoices, origin, '
                                         'destination, transporter, vehicle, docket, dispatched, eta, delivered, '
                                         'freight, pod, url')
    job_count = fields.Integer(compute='_compute_job_count')
    failed_job_count = fields.Integer(compute='_compute_job_count')

    def _compute_job_count(self):
        Job = self.env['cp.zoho.sync']
        for acc in self:
            acc.job_count = Job.search_count([('account_id', '=', acc.id)])
            acc.failed_job_count = Job.search_count([('account_id', '=', acc.id), ('state', '=', 'failed')])

    @api.constrains('creator_field_map')
    def _check_map(self):
        for acc in self:
            try:
                value = json.loads(acc.creator_field_map or '{}')
            except ValueError:
                raise UserError(_('The Creator field map must be valid JSON.'))
            if not isinstance(value, dict):
                raise UserError(_('The Creator field map must be a JSON object.'))

    @api.model
    def cp_for(self, company):
        return self.search([('company_id', '=', company.id)], limit=1)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    @property
    def _api_host(self):
        return 'https://www.zohoapis.%s' % self.data_center

    def _token(self):
        acc = self.sudo()
        now = fields.Datetime.now()
        if acc.access_token and acc.token_expiry and acc.token_expiry > now:
            return acc.access_token
        if not (acc.client_id and acc.client_secret and acc.refresh_token):
            raise UserError(_('Enter the Zoho client ID, client secret and refresh token.'))
        try:
            res = requests.request('POST', 'https://accounts.zoho.%s/oauth/v2/token' % acc.data_center,
                                   params={'refresh_token': acc.refresh_token, 'client_id': acc.client_id,
                                           'client_secret': acc.client_secret, 'grant_type': 'refresh_token'},
                                   timeout=30)
            data = res.json()
        except (requests.exceptions.RequestException, ValueError) as err:
            raise UserError(_('Zoho sign-in failed (%s).', err.__class__.__name__)) from err
        if not data.get('access_token'):
            raise UserError(_('Zoho refused the refresh token: %s', data.get('error') or res.status_code))
        acc.write({'access_token': data['access_token'],
                   'token_expiry': now + timedelta(seconds=int(data.get('expires_in') or 3600) - 120)})
        return data['access_token']

    def _request(self, method, url, params=None, body=None, job=None, retry=True):
        """JSON call with the OAuth token; stores the exchange on the job. Returns the decoded answer."""
        self.ensure_one()
        headers = {'Authorization': 'Zoho-oauthtoken %s' % self._token(), 'Content-Type': 'application/json'}
        started = time.time()
        try:
            res = requests.request(method, url, headers=headers, params=params,
                                   json=body if body is not None else None, timeout=40)
        except requests.exceptions.RequestException as err:
            raise UserError(_('Zoho could not be reached (%s).', err.__class__.__name__)) from err
        text = res.text or ''
        if job:
            job.sudo().write({'last_request': '%s %s\n%s' % (method, url, json.dumps(body, default=str)[:8000] if body
                                                              else json.dumps(params or {})),
                              'last_response': '%s (%sms)\n%s' % (res.status_code, int((time.time() - started) * 1000),
                                                                 text[:8000])})
        if res.status_code == 401 and retry:
            self.sudo().write({'access_token': False, 'token_expiry': False})
            return self._request(method, url, params=params, body=body, job=job, retry=False)
        try:
            data = res.json()
        except ValueError:
            raise UserError(_('Zoho sent an unexpected answer (HTTP %(c)s): %(t)s', c=res.status_code, t=text[:200]))
        if res.status_code >= 400:
            raise UserError(_('Zoho refused the request (HTTP %(c)s): %(m)s', c=res.status_code,
                              m=data.get('message') or text[:200]))
        return data

    def _books(self, method, path, params=None, body=None, job=None):
        if not self.organization_id:
            raise UserError(_('Enter the Zoho Books organization ID.'))
        query = dict(params or {}, organization_id=self.organization_id)
        data = self._request(method, '%s/books/v3%s' % (self._api_host, path), params=query, body=body, job=job)
        if data.get('code') not in (0, '0', None):
            raise UserError(_('Zoho Books error %(c)s: %(m)s', c=data.get('code'), m=data.get('message')))
        return data

    def _creator(self, method, path, body=None, job=None):
        if not (self.creator_owner and self.creator_app):
            raise UserError(_('Enter the Creator account owner and application link names.'))
        url = '%s/creator/v2.1/data/%s/%s%s' % (self._api_host, self.creator_owner, self.creator_app, path)
        data = self._request(method, url, body=body, job=job)
        if data.get('code') not in (3000, '3000'):
            raise UserError(_('Zoho Creator error %(c)s: %(m)s', c=data.get('code'),
                              m=data.get('message') or json.dumps(data.get('error') or data)[:300]))
        return data

    def action_test(self):
        self.ensure_one()
        if self.provider == 'simulator':
            msg = _('Simulator ready.')
        else:
            self.sudo().write({'access_token': False, 'token_expiry': False})
            parts = []
            if self.use_books:
                org = self._books('GET', '/organizations/%s' % self.organization_id).get('organization') or {}
                parts.append(_('Books: %s', org.get('name') or self.organization_id))
            else:
                self._token()
            if self.use_creator:
                parts.append(_('Creator: %s', self.creator_app))
            msg = _('Connected. %s', ' · '.join(parts))
        self.write({'state': 'ok', 'last_message': msg})
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'type': 'success', 'title': self.name, 'message': msg}}

    def action_view_jobs(self):
        action = self.env['ir.actions.act_window']._for_xml_id('cargo_pilot_tms.action_cp_zoho_sync')
        action['domain'] = [('account_id', 'in', self.ids)]
        return action

    def action_sync_transporters(self):
        partners = self.env['res.partner'].search([('cp_is_transporter', '=', True), ('zoho_vendor_id', '=', False)])
        for acc in self:
            for partner in partners:
                self.env['cp.zoho.sync'].cp_enqueue(acc, 'vendor', partner)
        return True

    # ------------------------------------------------------------------
    # Operations (each returns the Zoho id)
    # ------------------------------------------------------------------
    def _do_vendor(self, partner, job=None):
        if partner.zoho_vendor_id:
            return partner.zoho_vendor_id
        if self.provider == 'simulator':
            vendor_id = 'SIMV%s' % partner.id
        else:
            found = self._books('GET', '/contacts', params={'contact_name': partner.name, 'contact_type': 'vendor'},
                                job=job).get('contacts') or []
            found = [c for c in found if c.get('contact_type') in (None, 'vendor')]
            if found:
                vendor_id = found[0]['contact_id']
            else:
                body = {'contact_name': partner.name, 'company_name': partner.name, 'contact_type': 'vendor',
                        'gst_treatment': 'business_gst' if partner.vat else 'business_none'}
                if partner.vat:
                    body['gst_no'] = partner.vat
                if partner.state_id.code:
                    body['place_of_contact'] = partner.state_id.code
                if partner.email or partner.phone:
                    body['contact_persons'] = [{'first_name': partner.name[:100], 'email': partner.email or '',
                                                'phone': partner.phone or '', 'is_primary_contact': True}]
                vendor_id = self._books('POST', '/contacts', body=body, job=job)['contact']['contact_id']
        partner.sudo().zoho_vendor_id = vendor_id
        return vendor_id

    def _do_bill(self, bill, job=None):
        if bill.zoho_bill_id:
            return bill.zoho_bill_id
        vendor_id = self._do_vendor(bill.transporter_id, job)
        lines = []
        for line in bill.line_ids.filtered(lambda l: l.approved_amount > 0):
            item = {'description': _('Freight %(trip)s %(lane)s %(veh)s', trip=line.trip_id.name,
                                     lane=line.trip_id.lane_id.name or '', veh=line.trip_id.vehicle_number or '').strip(),
                    'rate': round(line.approved_amount, 2), 'quantity': 1, 'hsn_or_sac': self.hsn_sac or ''}
            if self.expense_account_id:
                item['account_id'] = self.expense_account_id
            if self.tax_id:
                item['tax_id'] = self.tax_id
            lines.append(item)
        if not lines:
            raise UserError(_('Nothing approved to pay on %s.', bill.name))
        body = {'vendor_id': vendor_id, 'bill_number': bill.bill_number, 'reference_number': bill.name,
                'date': fields.Date.to_string(bill.bill_date), 'line_items': lines,
                'is_reverse_charge_applied': bool(self.reverse_charge),
                'notes': _('Audited in Cargo Pilot: billed %(b)s, approved %(a)s, deductions %(d)s.',
                           b=bill.billed_total, a=bill.approved_total, d=bill.deduction_total)}
        if self.tds_tax_id:
            body['tds_tax_id'] = self.tds_tax_id
        if self.provider == 'simulator':
            bill_id = 'SIMB%s' % bill.id
        else:
            bill_id = self._books('POST', '/bills', body=body, job=job)['bill']['bill_id']
        bill.sudo().write({'zoho_bill_id': bill_id})
        bill.message_post(body=_('Vendor bill created in Zoho Books (%s).', bill_id))
        return bill_id

    def _do_invoice_freight(self, order, job=None):
        if not order.invoice_ref:
            return False
        if not self.invoice_customfield_id:
            raise UserError(_('Enter the custom field ID for freight on Zoho invoices.'))
        amount = round(order.freight_cost or 0.0, 2)
        if self.provider == 'simulator':
            invoice_id = 'SIMI%s' % order.id
        else:
            invoices = self._books('GET', '/invoices', params={'invoice_number': order.invoice_ref},
                                   job=job).get('invoices') or []
            if not invoices:
                raise UserError(_('Invoice %s was not found in Zoho Books.', order.invoice_ref))
            invoice_id = invoices[0]['invoice_id']
            self._books('PUT', '/invoice/%s/customfields' % invoice_id,
                        body=[{'customfield_id': self.invoice_customfield_id, 'value': amount}], job=job)
        order.sudo().write({'zoho_invoice_id': invoice_id, 'zoho_freight_synced': amount})
        return invoice_id

    def _creator_values(self, trip):
        orders = trip.order_ids
        fmt = '%d-%b-%Y %H:%M:%S'

        def stamp(value):
            return fields.Datetime.context_timestamp(trip, value).strftime(fmt) if value else ''

        state_label = dict(trip._fields['state']._description_selection(self.env)).get(trip.state, trip.state)
        pod_label = dict(trip._fields['epod_state']._description_selection(self.env)).get(trip.epod_state, '') \
            if 'epod_state' in trip._fields else ''
        source = {
            'name': trip.name, 'status': state_label, 'mode': (trip.shipment_mode or '').upper(),
            'orders': ', '.join(o for o in orders.mapped('order_ref') if o),
            'invoices': ', '.join(i for i in orders.mapped('invoice_ref') if i),
            'origin': trip.origin_id.name or '', 'destination': trip.destination_id.name or '',
            'transporter': trip.transporter_id.name or '', 'vehicle': trip.vehicle_number or '',
            'docket': trip.docket_number or '', 'dispatched': stamp(trip.actual_dispatch), 'eta': stamp(trip.eta),
            'delivered': stamp(trip.actual_delivery), 'freight': round(trip.freight_cost_total or 0.0, 2),
            'pod': pod_label or '', 'url': trip.tracking_url or '',
        }
        mapping = json.loads(self.creator_field_map or '{}')
        return {field: source[key] for field, key in mapping.items() if key in source}

    def _do_creator(self, trip, job=None):
        data = self._creator_values(trip)
        if self.provider == 'simulator':
            record_id = trip.zoho_creator_id or 'SIMC%s' % trip.id
        elif trip.zoho_creator_id:
            self._creator('PATCH', '/report/%s/%s' % (self.creator_report, trip.zoho_creator_id),
                          body={'data': data}, job=job)
            record_id = trip.zoho_creator_id
        else:
            answer = self._creator('POST', '/form/%s' % self.creator_form, body={'data': data}, job=job)
            result = answer.get('data') or {}
            if isinstance(result, list):
                first = result[0] if result else {}
                result = first.get('data') or first
            record_id = str(result.get('ID') or '')
            if not record_id:
                raise UserError(_('Zoho Creator returned no record ID.'))
        trip.sudo().with_context(cp_zoho_skip=True).write({'zoho_creator_id': record_id})
        return record_id


class CpZohoSync(models.Model):
    _name = 'cp.zoho.sync'
    _description = 'Zoho Sync Job'
    _order = 'id desc'

    account_id = fields.Many2one('cp.zoho.account', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='account_id.company_id', store=True)
    kind = fields.Selection(KINDS, required=True)
    res_model = fields.Char(required=True)
    res_id = fields.Integer(required=True)
    record_name = fields.Char(string='Record')
    state = fields.Selection([('pending', 'Waiting'), ('done', 'Done'), ('failed', 'Failed')], default='pending',
                             index=True)
    attempts = fields.Integer(default=0)
    next_try = fields.Datetime(default=fields.Datetime.now, index=True)
    done_at = fields.Datetime()
    zoho_id = fields.Char(string='Zoho ID')
    error = fields.Char()
    last_request = fields.Text()
    last_response = fields.Text()

    @api.model
    def cp_enqueue(self, account, kind, record):
        existing = self.search([('account_id', '=', account.id), ('kind', '=', kind), ('res_model', '=', record._name),
                                ('res_id', '=', record.id), ('state', '=', 'pending')], limit=1)
        if existing:
            existing.next_try = fields.Datetime.now()
            return existing
        return self.sudo().create({'account_id': account.id, 'kind': kind, 'res_model': record._name,
                                   'res_id': record.id, 'record_name': record.display_name})

    def _record(self):
        self.ensure_one()
        return self.env[self.res_model].browse(self.res_id).exists()

    def cp_run(self):
        for job in self:
            record = job._record()
            if not record:
                job.write({'state': 'failed', 'error': _('The record was deleted.')})
                continue
            account = job.account_id
            operation = getattr(account, '_do_%s' % job.kind)
            try:
                with self.env.cr.savepoint():
                    zoho_id = operation(record, job)
                job.write({'state': 'done', 'zoho_id': zoho_id or '', 'error': False, 'done_at': fields.Datetime.now(),
                           'attempts': job.attempts + 1})
            except UserError as err:
                message = str(err.args[0] if err.args else err)[:500]
                attempts = job.attempts + 1
                if attempts > len(BACKOFF_MINUTES):
                    job.write({'state': 'failed', 'attempts': attempts, 'error': message})
                    trip = record if record._name == 'cp.trip' else self.env['cp.trip']
                    self.env['cp.exception'].cp_raise(
                        'integration', record, _('Zoho sync failed: %s', job.record_name), severity='medium',
                        detail=message, trip=trip,
                        suggestion=_('Fix the cause, then press Retry on the job under Setup > Zoho > Sync Jobs.'))
                else:
                    job.write({'attempts': attempts, 'error': message,
                               'next_try': fields.Datetime.now() + timedelta(minutes=BACKOFF_MINUTES[attempts - 1])})
        return True

    def action_retry(self):
        self.write({'state': 'pending', 'next_try': fields.Datetime.now(), 'attempts': 0})
        self.cp_run()
        return True

    def action_open_record(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': self.res_model, 'res_id': self.res_id,
                'view_mode': 'form'}

    @api.model
    def _cron_run(self):
        jobs = self.search([('state', '=', 'pending'), ('next_try', '<=', fields.Datetime.now())], limit=200,
                           order='next_try, id')
        jobs.cp_run()
        return True

    @api.autovacuum
    def _gc_done(self):
        self.sudo().search([('state', '=', 'done'), ('done_at', '<', fields.Datetime.now() - timedelta(days=60))]).unlink()
