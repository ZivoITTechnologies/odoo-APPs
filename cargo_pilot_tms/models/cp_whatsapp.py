# -*- coding: utf-8 -*-
"""WhatsApp Business (Meta Cloud API) messages for booking offers, driver links, dispatch,
delivery and POD reminders.

Messages outside a 24-hour chat window must use approved templates, so every event maps to a
template name, a language and the order of its {{1}}, {{2}}... values.
POST https://graph.facebook.com/{version}/{phone_number_id}/messages  (Authorization: Bearer <token>)
Delivery receipts come back to /cp/whatsapp/webhook.
"""
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .cp_integration_base import india_phone

EVENTS = [('offer', 'Booking offer to transporter'), ('assigned', 'Driver link after assignment'),
          ('dispatched', 'Shipment dispatched (customer)'), ('delivered', 'Shipment delivered (customer)'),
          ('pod_missing', 'POD reminder (transporter)')]
PARAMS = {'name', 'route', 'url', 'vehicle', 'transporter', 'eta', 'customer', 'text'}


class CpWhatsappAccount(models.Model):
    _name = 'cp.whatsapp.account'
    _description = 'WhatsApp Business Account'
    _inherit = ['cp.integration.mixin']
    _cp_service = 'whatsapp'
    _cp_secret_fields = ('access_token', 'app_secret')

    name = fields.Char(required=True, default='WhatsApp')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    provider = fields.Selection([('meta', 'Meta WhatsApp Cloud API'), ('simulator', 'Simulator (no real calls)')],
                                default='meta', required=True)
    api_version = fields.Char(default='v21.0', required=True)
    phone_number_id = fields.Char(string='Phone Number ID')
    access_token = fields.Char(groups='cargo_pilot_tms.group_cp_manager')
    app_secret = fields.Char(groups='cargo_pilot_tms.group_cp_manager',
                             help='Checks the signature of delivery receipts (X-Hub-Signature-256).')
    verify_token = fields.Char(help='Any text; enter the same value as the verify token in the Meta app webhook.')
    webhook_url = fields.Char(compute='_compute_webhook_url')
    template_ids = fields.One2many('cp.whatsapp.template', 'account_id', string='Templates')
    message_count = fields.Integer(compute='_compute_message_count')
    state = fields.Selection([('new', 'Not checked'), ('ok', 'Connected'), ('error', 'Problem')], default='new',
                             readonly=True)
    last_message = fields.Char(readonly=True)

    def _compute_webhook_url(self):
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        for acc in self:
            acc.webhook_url = '%s/cp/whatsapp/webhook' % base

    def _compute_message_count(self):
        counts = dict(self.env['cp.whatsapp.message']._read_group([('account_id', 'in', self.ids)], ['account_id'],
                                                                  ['__count']))
        for acc in self:
            acc.message_count = counts.get(acc, 0)

    @api.model
    def cp_for(self, company):
        return self.search([('company_id', '=', company.id)], limit=1)

    def action_test(self):
        self.ensure_one()
        if self.provider == 'simulator':
            msg = _('Simulator ready.')
        else:
            if not (self.phone_number_id and self.sudo().access_token):
                raise UserError(_('Enter the phone number ID and access token.'))
            data = self._cp_http('check', 'GET', 'https://graph.facebook.com/%s/%s' % (self.api_version,
                                                                                    self.phone_number_id),
                                 headers={'Authorization': 'Bearer %s' % self.sudo().access_token},
                                 params={'fields': 'display_phone_number,verified_name'})
            msg = _('Connected as %s.', data.get('verified_name') or data.get('display_phone_number') or '')
        self.write({'state': 'ok', 'last_message': msg})
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'type': 'success', 'title': self.name, 'message': msg}}

    def action_view_messages(self):
        action = self.env['ir.actions.act_window']._for_xml_id('cargo_pilot_tms.action_cp_whatsapp_message')
        action['domain'] = [('account_id', 'in', self.ids)]
        return action

    def action_add_default_templates(self):
        Template = self.env['cp.whatsapp.template']
        for acc in self:
            have = set(acc.template_ids.mapped('event'))
            defaults = {'offer': ('cp_booking_offer', 'name,route,text,url'),
                        'assigned': ('cp_driver_trip', 'name,route,url'),
                        'dispatched': ('cp_shipment_dispatched', 'name,customer,url'),
                        'delivered': ('cp_shipment_delivered', 'name,customer'),
                        'pod_missing': ('cp_pod_reminder', 'name,route,url')}
            for event, (tmpl, params) in defaults.items():
                if event not in have:
                    Template.create({'account_id': acc.id, 'event': event, 'template_name': tmpl, 'params': params})
        return True

    # ------------------------------------------------------------------
    def cp_send(self, event, phone, trip, text='', url='', partner=None):
        """Send the template for ``event``. Returns the message record, or False when nothing is set up."""
        self.ensure_one()
        template = self.template_ids.filtered(lambda t: t.event == event and t.active)[:1]
        number = india_phone(phone)
        if not template or len(number) < 11:
            return False
        values = template._cp_values(trip, text, url, partner)
        message = self.env['cp.whatsapp.message'].sudo().create({
            'account_id': self.id, 'trip_id': trip.id if trip else False, 'partner_id': partner.id if partner else False,
            'phone': number, 'event': event, 'template_name': template.template_name,
            'body': ' | '.join(values), 'state': 'queued'})
        payload = {'messaging_product': 'whatsapp', 'to': number, 'type': 'template',
                   'template': {'name': template.template_name, 'language': {'code': template.language or 'en'},
                                'components': [{'type': 'body',
                                                'parameters': [{'type': 'text', 'text': v} for v in values]}]}}
        try:
            if self.provider == 'simulator':
                wamid = 'wamid.SIM%s' % message.id
            else:
                data = self._cp_http('send', 'POST', 'https://graph.facebook.com/%s/%s/messages'
                                     % (self.api_version, self.phone_number_id),
                                     headers={'Authorization': 'Bearer %s' % (self.sudo().access_token or ''),
                                              'Content-Type': 'application/json'},
                                     json_body=payload, record=trip)
                wamid = ((data.get('messages') or [{}])[0]).get('id')
            message.write({'wamid': wamid, 'state': 'sent', 'sent_at': fields.Datetime.now()})
        except UserError as err:
            message.write({'state': 'failed', 'error': str(err.args[0] if err.args else err)[:500]})
        return message


class CpWhatsappTemplate(models.Model):
    _name = 'cp.whatsapp.template'
    _description = 'WhatsApp Template'
    _order = 'account_id, event'

    account_id = fields.Many2one('cp.whatsapp.account', required=True, ondelete='cascade')
    active = fields.Boolean(default=True)
    event = fields.Selection(EVENTS, required=True)
    template_name = fields.Char(required=True, help='Exact name of the approved template in WhatsApp Manager.')
    language = fields.Char(default='en', required=True, help='Template language code, for example en, en_US, hi.')
    params = fields.Char(string='Values in Order', default='name,route,url', required=True,
                         help='Comma list for {{1}}, {{2}}...: name, route, url, vehicle, transporter, eta, '
                              'customer, text')

    _event_unique = models.Constraint('unique(account_id, event)', 'One template per event.')

    @api.constrains('params')
    def _check_params(self):
        for tmpl in self:
            bad = [p for p in (tmpl.params or '').replace(' ', '').split(',') if p and p not in PARAMS]
            if bad:
                raise UserError(_('Unknown template value(s): %s', ', '.join(bad)))

    def _cp_values(self, trip, text, url, partner):
        self.ensure_one()
        eta = trip.eta if trip and 'eta' in trip._fields else False
        source = {
            'name': trip.name if trip else '',
            'route': '%s to %s' % (trip.origin_id.name, trip.destination_id.name) if trip else '',
            'url': url or '',
            'vehicle': (trip.vehicle_number or '') if trip else '',
            'transporter': trip.transporter_id.name or '' if trip else '',
            'eta': fields.Datetime.to_string(eta)[:16] if eta else '-',
            'customer': partner.name if partner else (trip.consignee_id.name or '' if trip else ''),
            'text': text or '',
        }
        # WhatsApp rejects empty values and new lines inside parameters
        return [(source[p] or '-').replace('\n', ' ')[:900]
                for p in (self.params or '').replace(' ', '').split(',') if p]


class CpWhatsappMessage(models.Model):
    _name = 'cp.whatsapp.message'
    _description = 'WhatsApp Message'
    _order = 'id desc'
    _rec_name = 'phone'

    account_id = fields.Many2one('cp.whatsapp.account', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='account_id.company_id', store=True)
    trip_id = fields.Many2one('cp.trip', ondelete='set null', index=True)
    partner_id = fields.Many2one('res.partner', ondelete='set null')
    phone = fields.Char(required=True)
    event = fields.Selection(EVENTS)
    template_name = fields.Char()
    body = fields.Char(string='Values')
    wamid = fields.Char(string='Message ID', index=True)
    state = fields.Selection([('queued', 'Queued'), ('sent', 'Sent'), ('delivered', 'Delivered'), ('read', 'Read'),
                              ('failed', 'Failed')], default='queued', index=True)
    error = fields.Char()
    sent_at = fields.Datetime()
    delivered_at = fields.Datetime()
    read_at = fields.Datetime()

    @api.model
    def cp_apply_status(self, wamid, status, error=None):
        message = self.sudo().search([('wamid', '=', wamid)], limit=1)
        if not message:
            return False
        order = ['queued', 'sent', 'delivered', 'read']
        vals = {}
        if status == 'failed':
            vals = {'state': 'failed', 'error': (error or '')[:500]}
        elif status in order and order.index(status) > order.index(message.state if message.state in order else 'queued'):
            vals = {'state': status}
            if status == 'delivered':
                vals['delivered_at'] = fields.Datetime.now()
            if status == 'read':
                vals['read_at'] = fields.Datetime.now()
                if not message.delivered_at:
                    vals['delivered_at'] = vals['read_at']
        if vals:
            message.write(vals)
        return message


class CpTrip(models.Model):
    _inherit = 'cp.trip'

    whatsapp_ids = fields.One2many('cp.whatsapp.message', 'trip_id', string='WhatsApp Messages')
    whatsapp_count = fields.Integer(compute='_compute_whatsapp_count', string='WhatsApp Count')

    def _compute_whatsapp_count(self):
        for rec in self:
            rec.whatsapp_count = len(rec.whatsapp_ids)

    def _cp_notify(self, event, partner=None, body=None, url=None):
        res = super()._cp_notify(event, partner=partner, body=body, url=url)
        for rec in self:
            account = self.env['cp.whatsapp.account'].sudo().cp_for(rec.company_id)
            if not account:
                continue
            for person in (partner or self.env['res.partner']):
                phone = getattr(person, 'mobile', False) or person.phone
                if phone:
                    account.cp_send(event, phone, rec, text=body or '', url=url or '', partner=person)
        return res

    def _cp_after_assign(self):
        res = super()._cp_after_assign()
        for rec in self.filtered('driver_phone'):
            account = self.env['cp.whatsapp.account'].sudo().cp_for(rec.company_id)
            if account:
                account.cp_send('assigned', rec.driver_phone, rec, url=rec.driver_url)
        return res

    def action_view_whatsapp(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('cargo_pilot_tms.action_cp_whatsapp_message')
        action['domain'] = [('trip_id', '=', self.id)]
        return action

    @api.model
    def _cron_pod_reminders(self):
        """Daily: remind transporters of deliveries with no proof after 24 hours (once a day per shipment)."""
        since = fields.Datetime.now() - timedelta(hours=20)
        Message = self.env['cp.whatsapp.message'].sudo()
        trips = self.search([('pod_age_bucket', 'in', ('d1', 'd3', 'd7')), ('transporter_id', '!=', False)], limit=500)
        for trip in trips:
            if Message.search_count([('trip_id', '=', trip.id), ('event', '=', 'pod_missing'),
                                     ('create_date', '>=', since)]):
                continue
            base = trip.get_base_url()
            trip._cp_notify('pod_missing', partner=trip.transporter_id,
                            body=_('Please upload the delivery proof of %s.', trip.name),
                            url='%s/my/cp/trips/%s' % (base, trip.id))
        return True
