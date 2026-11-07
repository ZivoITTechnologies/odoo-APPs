# -*- coding: utf-8 -*-
"""Shared plumbing for the India connectors: HTTP with logging, secrets masked."""
import json
import logging
import re
import time

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.modules import module as odoo_module

_logger = logging.getLogger(__name__)

# GST state codes used by the e-way bill system, keyed by the state's short code.
GST_STATE_CODES = {
    'JK': '01', 'HP': '02', 'PB': '03', 'CH': '04', 'UK': '05', 'UT': '05', 'HR': '06', 'DL': '07', 'RJ': '08',
    'UP': '09', 'BR': '10', 'SK': '11', 'AR': '12', 'NL': '13', 'MN': '14', 'MZ': '15', 'TR': '16', 'ML': '17',
    'AS': '18', 'WB': '19', 'JH': '20', 'OD': '21', 'OR': '21', 'CT': '22', 'CG': '22', 'MP': '23', 'GJ': '24',
    'DN': '26', 'DD': '26', 'MH': '27', 'KA': '29', 'GA': '30', 'LD': '31', 'KL': '32', 'TN': '33', 'PY': '34',
    'AN': '35', 'TS': '36', 'TG': '36', 'AP': '37', 'LA': '38',
}


def gst_state_code(state):
    if not state:
        return ''
    tin = getattr(state, 'l10n_in_tin', False)
    return tin or GST_STATE_CODES.get((state.code or '').upper(), '')


def clean_plate(number):
    return re.sub(r'[^A-Z0-9]', '', (number or '').upper())


def india_phone(phone):
    digits = re.sub(r'\D', '', phone or '')
    if len(digits) == 10:
        digits = '91' + digits
    elif len(digits) == 11 and digits.startswith('0'):
        digits = '91' + digits[1:]
    return digits


class CpIntegrationLog(models.Model):
    _name = 'cp.integration.log'
    _description = 'Integration Request Log'
    _order = 'id desc'

    service = fields.Selection([('gsp', 'E-way bill (GSP)'), ('ulip', 'ULIP'), ('sim', 'SIM tracking'),
                                ('whatsapp', 'WhatsApp')], required=True, index=True)
    operation = fields.Char()
    method = fields.Char()
    url = fields.Char()
    request_body = fields.Text()
    response_body = fields.Text()
    status_code = fields.Integer()
    duration_ms = fields.Integer(string='Time (ms)')
    success = fields.Boolean()
    res_model = fields.Char()
    res_id = fields.Integer()
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    @api.autovacuum
    def _gc_old(self):
        self.sudo().search([('create_date', '<', fields.Datetime.subtract(fields.Datetime.now(), days=30))]).unlink()


class CpIntegrationMixin(models.AbstractModel):
    """Account models for India connectors inherit this for HTTP calls with logging."""
    _name = 'cp.integration.mixin'
    _description = 'Cargo Pilot Integration Account'

    _cp_service = None
    _cp_secret_fields = ()

    def _cp_mask(self, text):
        if text is None:
            return ''
        if not isinstance(text, str):
            text = json.dumps(text, default=str)
        for name in self._cp_secret_fields:
            value = self.sudo()[name]
            if value and len(str(value)) > 3:
                text = text.replace(str(value), '***')
        return text[:20000]

    def _cp_log(self, operation, method, url, body, status, answer, duration, ok, record=None):
        vals = {'service': self._cp_service, 'operation': operation, 'method': method, 'url': self._cp_mask(url),
                'request_body': self._cp_mask(body), 'status_code': status or 0,
                'response_body': self._cp_mask(answer), 'duration_ms': int(duration * 1000), 'success': ok,
                'res_model': record._name if record else False, 'res_id': record.id if record else 0,
                'company_id': self.company_id.id if 'company_id' in self._fields else False}
        if odoo_module.current_test:
            self.env['cp.integration.log'].sudo().create(vals)
            return
        try:
            with self.env.registry.cursor() as cr:
                self.env(cr=cr, su=True)['cp.integration.log'].create(vals)
        except Exception:  # pragma: no cover
            _logger.warning('Could not store integration log')

    def _cp_http(self, operation, method, url, *, headers=None, params=None, json_body=None, data=None,
                 record=None, expect='json', timeout=30):
        started = time.time()
        body = json_body if json_body is not None else data
        try:
            response = requests.request(method, url, headers=headers or {}, params=params, json=json_body,
                                        data=data, timeout=timeout)
        except requests.exceptions.RequestException as err:
            self._cp_log(operation, method, url, body, 0, str(err), time.time() - started, False, record)
            raise UserError(_('%(svc)s could not be reached (%(err)s).', svc=self.display_name,
                              err=err.__class__.__name__)) from err
        ok = 200 <= response.status_code < 300
        self._cp_log(operation, method, url, body, response.status_code, response.text, time.time() - started, ok,
                     record)
        if not ok:
            raise UserError(_('%(svc)s refused the request (HTTP %(code)s): %(text)s', svc=self.display_name,
                              code=response.status_code, text=(response.text or '')[:300]))
        if expect == 'text':
            return response.text
        try:
            return response.json()
        except ValueError:
            raise UserError(_('%(svc)s sent an unexpected answer: %(text)s', svc=self.display_name,
                              text=(response.text or '')[:300]))
