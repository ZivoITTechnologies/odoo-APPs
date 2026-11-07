# -*- coding: utf-8 -*-
import hmac
import json
import logging

from odoo import http, _
from odoo.http import request

from odoo.addons.portal.controllers.portal import CustomerPortal

_logger = logging.getLogger(__name__)

STEPS = [('booked', 'Booked', 'fa-file-text-o'), ('picked_up', 'Picked up', 'fa-cube'),
         ('in_transit', 'On the way', 'fa-truck'), ('out_for_delivery', 'Out for delivery', 'fa-motorcycle'),
         ('delivered', 'Delivered', 'fa-check')]
ORDER = [s[0] for s in STEPS]


def parcel_steps(parcel):
    state = parcel.state
    if state in ('ndr',):
        reached = ORDER.index('out_for_delivery')
    elif state in ('rto', 'returned', 'lost'):
        reached = ORDER.index('in_transit')
    elif state in ORDER:
        reached = ORDER.index(state)
    else:
        reached = -1
    steps = []
    for idx, (key, label, icon) in enumerate(STEPS):
        status = 'done' if idx < reached or (idx == reached and key == 'delivered') else \
            ('current' if idx == reached else 'todo')
        if idx == reached and state == 'ndr':
            status = 'late'
        steps.append({'key': key, 'label': label, 'icon': icon, 'status': status})
    return steps


class CpCourierController(http.Controller):

    @http.route('/cp/courier/webhook/<int:account_id>/<string:token>', type='http', auth='public',
                methods=['POST'], csrf=False)
    def cp_courier_webhook(self, account_id, token, **kw):
        account = request.env['cp.courier.account'].sudo().browse(account_id).exists()
        if not account or not account.webhook_token or not hmac.compare_digest(account.webhook_token, token):
            return request.make_json_response({'ok': False, 'error': 'unknown endpoint'}, status=404)
        raw = request.httprequest.get_data(as_text=True) or ''
        try:
            payload = json.loads(raw) if raw.strip() else dict(kw)
        except ValueError:
            payload = dict(request.httprequest.form) or dict(kw)
        try:
            results = account._cp_provider().parse_webhook(payload, dict(request.httprequest.headers))
        except Exception:
            _logger.exception('Courier webhook could not be read for account %s', account_id)
            return request.make_json_response({'ok': False, 'error': 'unreadable payload'}, status=400)
        Parcel = request.env['cp.parcel'].sudo()
        updated = 0
        for awb, info in results.items():
            parcel = Parcel.search([('account_id', '=', account.id), ('awb', '=', awb)], limit=1)
            if parcel:
                parcel._cp_apply_tracking(info, source='webhook')
                updated += 1
        return request.make_json_response({'ok': True, 'updated': updated})

    @http.route('/cp/parcel/<string:token>', type='http', auth='public', website=True)
    def cp_parcel_tracking(self, token, **kw):
        parcel = request.env['cp.parcel'].sudo().search([('access_token', '=', token)], limit=1)
        if not parcel or len(token) < 12:
            raise request.not_found()
        return request.render('cargo_pilot_tms.cp_parcel_public', {
            'parcel': parcel, 'steps': parcel_steps(parcel),
            'events': parcel.event_ids.sorted('event_time', reverse=True),
            'company': parcel.company_id,
        })


class CpCourierPortal(CustomerPortal):

    def _cp_parcel_domain(self):
        partner = request.env.user.partner_id.commercial_partner_id
        return ['|', ('recipient_partner_id', 'child_of', partner.id), ('sender_partner_id', 'child_of', partner.id),
                ('state', '!=', 'draft')]

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if 'cp_parcel_count' in counters:
            values['cp_parcel_count'] = request.env['cp.parcel'].sudo().search_count(self._cp_parcel_domain())
        return values

    @http.route('/my/cp/parcels', type='http', auth='user', website=True)
    def cp_portal_parcels(self, **kw):
        parcels = request.env['cp.parcel'].sudo().search(self._cp_parcel_domain(), limit=200)
        values = self._prepare_portal_layout_values()
        values.update({'parcels': parcels, 'page_name': 'cp_parcels'})
        return request.render('cargo_pilot_tms.portal_my_cp_parcels', values)
