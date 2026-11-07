# -*- coding: utf-8 -*-
"""E-way bill Part-B (vehicle) update, validity extension and consolidated e-way bills through a GSP.

The payloads follow the NIC e-way bill API (fields such as ewbNo, vehicleNo, fromPlace, fromState,
reasonCode, transMode, extnRsnCode, tripSheetEwbBills). Most GSPs (ClearTax, Masters India, IRIS,
Logitax...) accept these fields as plain JSON and add their own authentication and paths, which
are set on the GSP account.
"""
import math
import re
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .cp_integration_base import clean_plate, gst_state_code

REASONS = [('1', 'Breakdown'), ('2', 'Transhipment'), ('3', 'Others'), ('4', 'First time')]
EXTEND_REASONS = [('1', 'Natural calamity'), ('2', 'Law and order'), ('4', 'Transhipment'), ('5', 'Accident'),
                  ('99', 'Others')]


class CpGspAccount(models.Model):
    _name = 'cp.gsp.account'
    _description = 'E-way Bill GSP Account'
    _inherit = ['cp.integration.mixin']
    _cp_service = 'gsp'
    _cp_secret_fields = ('password', 'token', 'client_secret')

    name = fields.Char(required=True, default=lambda self: _('E-way bill GSP'))
    active = fields.Boolean(default=True)
    provider = fields.Selection([('gsp', 'GSP (NIC e-way bill JSON)'), ('simulator', 'Simulator (no real calls)')],
                                default='gsp', required=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    gstin = fields.Char(string='GSTIN', required=True)
    base_url = fields.Char(string='API Address', help='Given by your GSP, for example https://api.gsp.example/ewb')
    auth_style = fields.Selection([('headers', 'ClientCode / UserCode / Password / GSTIN headers'),
                                   ('bearer', 'Bearer token')], default='headers', required=True)
    client_code = fields.Char(string='Client Code / Client ID', groups='cargo_pilot_tms.group_cp_manager')
    client_secret = fields.Char(groups='cargo_pilot_tms.group_cp_manager')
    user_code = fields.Char(string='User Code / Username', groups='cargo_pilot_tms.group_cp_manager')
    password = fields.Char(groups='cargo_pilot_tms.group_cp_manager')
    token = fields.Char(string='Bearer Token', groups='cargo_pilot_tms.group_cp_manager')
    path_update_vehicle = fields.Char(string='Update Part-B Path', default='/updatevehicle')
    path_extend = fields.Char(string='Extend Validity Path', default='/extendvalidity')
    path_consolidated = fields.Char(string='Consolidated EWB Path', default='/consolidated')
    auto_part_b = fields.Boolean(string='Update Part-B When a Truck Is Assigned', default=True)
    auto_extend = fields.Boolean(string='Extend Before Expiry', default=False,
                                 help='Extend e-way bills of trucks still on the road 6 hours before they lapse.')
    state = fields.Selection([('new', 'Not checked'), ('ok', 'Working'), ('error', 'Problem')], default='new',
                             readonly=True)
    last_message = fields.Char(readonly=True)

    def _headers(self):
        acc = self.sudo()
        headers = {'Content-Type': 'application/json', 'GSTIN': acc.gstin or ''}
        if acc.auth_style == 'bearer':
            headers['Authorization'] = 'Bearer %s' % (acc.token or '')
        else:
            headers.update({'ClientCode': acc.client_code or '', 'UserCode': acc.user_code or '',
                            'Password': acc.password or ''})
        return headers

    @staticmethod
    def _parse(payload):
        """NIC style {status: '1', data: {...}} / {status: '0', error: {...}}, or GSP wrappers."""
        if not isinstance(payload, dict):
            return False, {}, str(payload)[:300]
        if 'results' in payload and isinstance(payload['results'], dict):
            res = payload['results']
            ok = str(res.get('status', '')).lower() in ('success', '1', 'true')
            data = res.get('message') if isinstance(res.get('message'), dict) else {}
            return ok, data, '' if ok else str(res.get('errorMessage') or res.get('message') or res)[:300]
        status = payload.get('status', payload.get('success'))
        ok = str(status).lower() in ('1', 'true', 'success')
        data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
        error = payload.get('error') or payload.get('message') or ''
        if isinstance(error, dict):
            error = error.get('message') or error.get('errorCodes') or str(error)
        return ok, data, '' if ok else str(error)[:300]

    def _call(self, path, body, record, operation):
        self.ensure_one()
        if self.provider == 'simulator':
            return self._simulate(operation, body)
        if not self.base_url:
            raise UserError(_('Enter the API address of your GSP on %s.', self.name))
        payload = self._cp_http(operation, 'POST', self.base_url.rstrip('/') + (path or ''), headers=self._headers(),
                                json_body=body, record=record)
        ok, data, error = self._parse(payload)
        if not ok:
            raise UserError(_('The e-way bill system refused %(op)s: %(err)s', op=operation, err=error))
        return data

    def _simulate(self, operation, body):
        now = fields.Datetime.now()
        if operation == 'consolidated':
            return {'cEwbNo': '9%011d' % (sum(int(b['ewbNo']) for b in body['tripSheetEwbBills']) % 10 ** 11),
                    'cEwbDate': now.strftime('%d/%m/%Y %I:%M:%S %p')}
        days = max(1, math.ceil((body.get('remainingDistance') or 200) / 200.0))
        valid = now + timedelta(days=days)
        stamp = now.strftime('%d/%m/%Y %I:%M:%S %p')
        data = {'validUpto': valid.strftime('%d/%m/%Y 11:59:00 PM'), 'vehUpdDate': stamp, 'updatedDate': stamp}
        if operation == 'extend':
            data['ewayBillNo'] = body.get('ewbNo')
        return data

    def action_test(self):
        self.ensure_one()
        if self.provider == 'simulator':
            msg = _('Simulator ready.')
        else:
            if not self.base_url:
                raise UserError(_('Enter the API address first.'))
            msg = _('Settings saved. The first Part-B update will confirm them.')
        self.write({'state': 'ok', 'last_message': msg})
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'type': 'success', 'title': self.name, 'message': msg}}

    @api.model
    def cp_for(self, company):
        return self.search([('company_id', '=', company.id)], limit=1)

    @api.model
    def _cron_extend(self):
        soon = fields.Datetime.now() + timedelta(hours=6)
        for acc in self.search([('auto_extend', '=', True)]):
            trips = self.env['cp.trip'].search([('company_id', '=', acc.company_id.id), ('state', '=', 'in_transit'),
                                                ('eway_bill_no', '!=', False), ('eway_bill_expiry', '!=', False),
                                                ('eway_bill_expiry', '<=', soon)])
            for trip in trips:
                try:
                    trip._cp_ewb_extend('99', _('Vehicle still in transit'))
                except UserError as err:
                    trip._cp_ewb_issue(str(err.args[0] if err.args else err))
        return True


class CpEwaybillAction(models.Model):
    _name = 'cp.ewaybill.action'
    _description = 'E-way Bill Update'
    _order = 'id desc'

    trip_id = fields.Many2one('cp.trip', required=True, ondelete='cascade', index=True)
    action = fields.Selection([('part_b', 'Part-B vehicle update'), ('extend', 'Validity extended'),
                               ('consolidated', 'Consolidated EWB')], required=True)
    ewb_no = fields.Char(string='E-way Bill No.')
    vehicle_number = fields.Char()
    result = fields.Char()
    valid_upto = fields.Char(string='Valid Up To')
    success = fields.Boolean(default=True)


def _parse_nic_date(value):
    if not value:
        return False
    for fmt in ('%d/%m/%Y %I:%M:%S %p', '%d/%m/%Y %I:%M %p', '%d/%m/%Y %H:%M:%S', '%d/%m/%Y'):
        try:
            from datetime import datetime
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return False


class CpTrip(models.Model):
    _inherit = 'cp.trip'

    consolidated_ewb_no = fields.Char(string='Consolidated EWB No.', copy=False, tracking=True)
    partb_updated_at = fields.Datetime(string='Part-B Updated', readonly=True, copy=False)
    ewb_action_ids = fields.One2many('cp.ewaybill.action', 'trip_id', string='E-way Bill Updates')

    def _cp_ewb_numbers(self):
        self.ensure_one()
        numbers = [self.eway_bill_no] + self.order_ids.mapped('eway_bill_no')
        seen, result = set(), []
        for num in numbers:
            digits = re.sub(r'\D', '', num or '')
            if len(digits) == 12 and digits not in seen:
                seen.add(digits)
                result.append(digits)
        return result

    def _cp_ewb_common(self):
        self.ensure_one()
        origin = self.origin_id
        doc_date = (self.actual_dispatch or self.planned_dispatch or fields.Datetime.now()).strftime('%d/%m/%Y')
        return {
            'vehicleNo': clean_plate(self.vehicle_number),
            'fromPlace': (origin.city or origin.name or '')[:50],
            'fromState': int(gst_state_code(origin.state_id) or 0),
            'transDocNo': (self.docket_number or self.name or '')[:15],
            'transDocDate': doc_date,
            'transMode': '1',
        }

    def _cp_ewb_issue(self, message):
        self.env['cp.exception'].cp_raise('integration', self, _('E-way bill update failed on %s', self.name),
                                          severity='high', detail=message,
                                          suggestion=_('Update Part-B on the e-way bill portal, then retry here.'))

    def _cp_ewb_update_part_b(self, reason='4', remark=''):
        Gsp = self.env['cp.gsp.account']
        for rec in self:
            account = Gsp.cp_for(rec.company_id)
            if not account:
                raise UserError(_('Add an e-way bill GSP account first (Setup > India Integrations).'))
            numbers = rec._cp_ewb_numbers()
            if not numbers:
                raise UserError(_('%s has no 12-digit e-way bill number.', rec.name))
            if not rec.vehicle_number:
                raise UserError(_('Enter the vehicle number of %s.', rec.name))
            first = not rec.partb_updated_at
            for ewb in numbers:
                body = dict(rec._cp_ewb_common(), ewbNo=int(ewb), reasonCode=reason if not first else '4',
                            reasonRem=remark or (_('First vehicle entry') if first else _('Vehicle changed')),
                            vehicleType='R')
                data = account._call(account.path_update_vehicle, body, rec, 'part_b')
                valid = data.get('validUpto') or ''
                rec.env['cp.ewaybill.action'].sudo().create({'trip_id': rec.id, 'action': 'part_b', 'ewb_no': ewb,
                                                             'vehicle_number': body['vehicleNo'], 'valid_upto': valid,
                                                             'result': data.get('vehUpdDate') or _('Updated')})
                expiry = _parse_nic_date(valid)
                if expiry and ewb == re.sub(r'\D', '', rec.eway_bill_no or ''):
                    rec.eway_bill_expiry = expiry
            rec.partb_updated_at = fields.Datetime.now()
            rec._cp_log('compliance', _('E-way bill Part-B updated with %(v)s for %(n)s bill(s).',
                                        v=rec.vehicle_number, n=len(numbers)))
            self.env['cp.exception'].cp_auto_resolve(rec, ('integration',), _('Part-B updated.'))
        return True

    def _cp_ewb_extend(self, reason='99', remark=''):
        account = self.env['cp.gsp.account'].cp_for(self.company_id)
        if not account:
            raise UserError(_('Add an e-way bill GSP account first.'))
        for ewb in self._cp_ewb_numbers():
            body = dict(self._cp_ewb_common(), ewbNo=int(ewb), remainingDistance=int(self.remaining_km or
                                                                                    self.lane_id.distance_km or 1),
                        extnRsnCode=int(reason), extnRemarks=remark or _('In transit'),
                        fromPincode=int(re.sub(r'\D', '', self.origin_id.pincode or '') or 0),
                        consignmentStatus='M', transitType='')
            if self.last_ping_time and self.tracking_method != 'driver':
                body['fromPlace'] = (self.ping_ids[:1].place or body['fromPlace'])[:50]
            data = account._call(account.path_extend, body, self, 'extend')
            valid = data.get('validUpto') or ''
            self.env['cp.ewaybill.action'].sudo().create({'trip_id': self.id, 'action': 'extend', 'ewb_no': ewb,
                                                          'vehicle_number': body['vehicleNo'], 'valid_upto': valid,
                                                          'result': data.get('updatedDate') or _('Extended')})
            expiry = _parse_nic_date(valid)
            if expiry and ewb == re.sub(r'\D', '', self.eway_bill_no or ''):
                self.eway_bill_expiry = expiry
        self.env['cp.exception'].cp_auto_resolve(self, ('eway',), _('E-way bill extended.'))
        self._cp_log('compliance', _('E-way bill validity extended.'))
        return True

    def _cp_ewb_consolidate(self):
        account = self.env['cp.gsp.account'].cp_for(self.company_id)
        if not account:
            raise UserError(_('Add an e-way bill GSP account first.'))
        numbers = self._cp_ewb_numbers()
        if len(numbers) < 2:
            raise UserError(_('A consolidated e-way bill needs at least two e-way bills on the shipment.'))
        body = dict(self._cp_ewb_common(), tripSheetEwbBills=[{'ewbNo': int(n)} for n in numbers])
        body.pop('transMode', None)
        body['transMode'] = '1'
        data = account._call(account.path_consolidated, body, self, 'consolidated')
        number = str(data.get('cEwbNo') or '')
        if not number:
            raise UserError(_('The e-way bill system returned no consolidated number.'))
        self.consolidated_ewb_no = number
        self.env['cp.ewaybill.action'].sudo().create({'trip_id': self.id, 'action': 'consolidated', 'ewb_no': number,
                                                      'vehicle_number': body['vehicleNo'],
                                                      'result': _('%s bills', len(numbers))})
        self._cp_log('compliance', _('Consolidated e-way bill %s generated.', number))
        return number

    # Buttons
    def action_ewb_update_part_b(self):
        self._cp_ewb_update_part_b()
        return True

    def action_ewb_extend(self):
        self.ensure_one()
        self._cp_ewb_extend('99', _('Vehicle still in transit'))
        return True

    def action_ewb_consolidate(self):
        self.ensure_one()
        self._cp_ewb_consolidate()
        return True

    def _cp_after_assign(self):
        res = super()._cp_after_assign()
        Gsp = self.env['cp.gsp.account']
        for rec in self:
            account = Gsp.cp_for(rec.company_id)
            if account and account.auto_part_b and rec.vehicle_number and rec._cp_ewb_numbers():
                try:
                    rec._cp_ewb_update_part_b()
                except UserError as err:
                    rec._cp_ewb_issue(str(err.args[0] if err.args else err))
        return res
