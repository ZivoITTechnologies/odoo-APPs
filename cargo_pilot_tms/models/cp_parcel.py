# -*- coding: utf-8 -*-
import base64
import json
import logging
import secrets
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

from .providers import FLOW, TERMINAL, guess_status

_logger = logging.getLogger(__name__)

PARCEL_STATES = [
    ('draft', 'Draft'),
    ('booked', 'Booked'),
    ('picked_up', 'Picked up'),
    ('in_transit', 'In transit'),
    ('out_for_delivery', 'Out for delivery'),
    ('delivered', 'Delivered'),
    ('ndr', 'Delivery failed'),
    ('rto', 'Returning to sender'),
    ('returned', 'Returned'),
    ('cancelled', 'Cancelled'),
    ('lost', 'Lost / damaged'),
]
ACTIVE_STATES = ('booked', 'picked_up', 'in_transit', 'out_for_delivery', 'ndr', 'rto')


class CpParcel(models.Model):
    _name = 'cp.parcel'
    _description = 'Courier Parcel'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc, id desc'
    _rec_names_search = ['name', 'awb', 'reference']

    name = fields.Char(default=lambda self: _('New'), copy=False, readonly=True, index='trigram')
    reference = fields.Char(string='Order Reference', help='Your order, invoice or delivery number.', tracking=True)
    account_id = fields.Many2one('cp.courier.account', string='Courier Account', tracking=True,
                                 domain="[('company_id', '=', company_id)]")
    provider = fields.Selection(related='account_id.provider', store=True)
    can_track = fields.Boolean(related='account_id.can_track')
    can_pickup = fields.Boolean(related='account_id.can_pickup')
    courier_name = fields.Char(string='Courier', tracking=True,
                               help='The courier that carries the parcel (for aggregators such as Shiprocket).')
    service = fields.Char()
    awb = fields.Char(string='AWB / Tracking No.', copy=False, tracking=True, index=True)
    provider_ref = fields.Char(string='Courier Reference', copy=False, readonly=True)
    provider_data = fields.Text(copy=False, readonly=True)

    sender_partner_id = fields.Many2one('res.partner', string='Pick up from', required=True,
                                        default=lambda self: self.env.company.partner_id)
    recipient_partner_id = fields.Many2one('res.partner', string='Deliver to', required=True, tracking=True)
    recipient_phone = fields.Char(related='recipient_partner_id.phone', readonly=False)
    recipient_city = fields.Char(related='recipient_partner_id.city')
    origin_pin = fields.Char(compute='_compute_pins', store=True, string='From PIN')
    dest_pin = fields.Char(compute='_compute_pins', store=True, string='To PIN')

    payment_mode = fields.Selection([('prepaid', 'Prepaid'), ('cod', 'Cash on delivery')], default='prepaid',
                                    required=True, tracking=True)
    cod_amount = fields.Monetary(string='Collect on Delivery')
    declared_value = fields.Monetary(string='Goods Value')
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id, required=True)
    contents = fields.Char(string='Contents', default=lambda self: _('Goods'))
    hsn_code = fields.Char(string='HSN Code')
    invoice_number = fields.Char()
    invoice_date = fields.Date()
    eway_bill_no = fields.Char(string='E-way Bill No.')
    note = fields.Char(string='Delivery Instructions')

    package_ids = fields.One2many('cp.parcel.package', 'parcel_id', string='Boxes', copy=True)
    piece_count = fields.Integer(compute='_compute_weights', store=True, string='Box Count')
    total_weight_kg = fields.Float(compute='_compute_weights', store=True, string='Weight (kg)')
    volumetric_weight_kg = fields.Float(compute='_compute_weights', store=True, string='Volumetric (kg)')
    chargeable_weight_kg = fields.Float(compute='_compute_weights', store=True, string='Charged Weight (kg)')

    state = fields.Selection(PARCEL_STATES, default='draft', required=True, tracking=True, index=True,
                             group_expand='_group_expand_states')
    status_text = fields.Char(string='Courier Status', readonly=True)
    last_location = fields.Char(readonly=True)
    last_event_at = fields.Datetime(readonly=True)
    last_sync_at = fields.Datetime(readonly=True, string='Last Checked')
    booked_at = fields.Datetime(readonly=True, copy=False)
    picked_up_at = fields.Datetime(readonly=True, copy=False)
    delivered_at = fields.Datetime(readonly=True, copy=False)
    expected_delivery = fields.Date(string='Expected Delivery', tracking=True, copy=False)
    is_delayed = fields.Boolean(compute='_compute_is_delayed', store=True)
    transit_days = fields.Float(compute='_compute_transit_days', store=True, string='Days in Transit',
                                aggregator='avg')

    shipping_cost = fields.Monetary(string='Shipping Cost', tracking=True, copy=False)
    label_attachment_id = fields.Many2one('ir.attachment', string='Label', copy=False, readonly=True)
    has_label = fields.Boolean(compute='_compute_has_label')
    tracking_url = fields.Char(compute='_compute_tracking_url', string='Courier Tracking Page')
    access_token = fields.Char(copy=False, default=lambda self: secrets.token_urlsafe(18), readonly=True)
    public_url = fields.Char(compute='_compute_public_url', string='Tracking Link')

    pickup_ref = fields.Char(string='Pickup Request', readonly=True, copy=False)
    pickup_date = fields.Datetime(readonly=True, copy=False)

    ndr_reason = fields.Char(string='Failed Delivery Reason', readonly=True, copy=False)
    ndr_attempts = fields.Integer(string='Failed Attempts', readonly=True, copy=False)
    ndr_action = fields.Selection([('reattempt', 'Re-attempt delivery'), ('rto', 'Return to sender')],
                                  readonly=True, copy=False, tracking=True)

    cod_state = fields.Selection([('none', 'No COD'), ('pending', 'To collect'), ('collected', 'Collected by courier'),
                                  ('remitted', 'Paid to us')], compute='_compute_cod_state', store=True,
                                 readonly=False, string='COD Status', tracking=True)
    cod_remit_ref = fields.Char(string='Remittance Ref.', copy=False)
    cod_remit_date = fields.Date(string='Remitted On', copy=False)

    consignment_id = fields.Many2one('cp.consignment', string='LR / Booking', index=True, copy=False)
    event_ids = fields.One2many('cp.parcel.event', 'parcel_id', string='Tracking History', copy=False)
    event_count = fields.Integer(compute='_compute_event_count')
    issue_count = fields.Integer(compute='_compute_issue_count')
    color = fields.Integer(compute='_compute_color')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)

    _awb_uniq = models.Constraint('unique(account_id, awb)', 'This AWB number is already used on this courier account.')

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.model
    def _group_expand_states(self, states, domain):
        return ['draft', 'booked', 'picked_up', 'in_transit', 'out_for_delivery', 'ndr', 'delivered']

    @api.depends('sender_partner_id.zip', 'recipient_partner_id.zip')
    def _compute_pins(self):
        for rec in self:
            rec.origin_pin = (rec.sender_partner_id.zip or '').replace(' ', '')
            rec.dest_pin = (rec.recipient_partner_id.zip or '').replace(' ', '')

    @api.depends('package_ids.quantity', 'package_ids.weight_kg', 'package_ids.length_cm', 'package_ids.width_cm',
                 'package_ids.height_cm', 'account_id.volumetric_divisor')
    def _compute_weights(self):
        for rec in self:
            divisor = rec.account_id.volumetric_divisor or 5000
            pieces = weight = volumetric = 0.0
            for pkg in rec.package_ids:
                qty = max(pkg.quantity, 1)
                pieces += qty
                weight += pkg.weight_kg * qty
                volumetric += (pkg.length_cm * pkg.width_cm * pkg.height_cm) / divisor * qty
            rec.piece_count = int(pieces)
            rec.total_weight_kg = round(weight, 3)
            rec.volumetric_weight_kg = round(volumetric, 3)
            rec.chargeable_weight_kg = round(max(weight, volumetric), 3)

    @api.depends('expected_delivery', 'state')
    def _compute_is_delayed(self):
        today = fields.Date.context_today(self)
        for rec in self:
            rec.is_delayed = bool(rec.expected_delivery and rec.expected_delivery < today
                                  and rec.state in ACTIVE_STATES)

    @api.depends('picked_up_at', 'booked_at', 'delivered_at')
    def _compute_transit_days(self):
        for rec in self:
            start = rec.picked_up_at or rec.booked_at
            rec.transit_days = round((rec.delivered_at - start).total_seconds() / 86400, 1) \
                if start and rec.delivered_at else 0.0

    @api.depends('payment_mode', 'state')
    def _compute_cod_state(self):
        for rec in self:
            if rec.payment_mode != 'cod':
                rec.cod_state = 'none'
            elif rec.cod_state == 'remitted':
                continue
            elif rec.state == 'delivered':
                rec.cod_state = 'collected'
            else:
                rec.cod_state = 'pending'

    def _compute_has_label(self):
        for rec in self:
            rec.has_label = bool(rec.label_attachment_id)

    @api.depends('awb', 'account_id')
    def _compute_tracking_url(self):
        for rec in self:
            rec.tracking_url = rec.account_id._cp_tracking_url(rec.awb) if rec.account_id else False

    def _compute_public_url(self):
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        for rec in self:
            rec.public_url = '%s/cp/parcel/%s' % (base, rec.access_token) if rec.access_token else False

    def _compute_event_count(self):
        for rec in self:
            rec.event_count = len(rec.event_ids)

    def _compute_issue_count(self):
        Issue = self.env['cp.exception']
        for rec in self:
            rec.issue_count = Issue.search_count([('res_model', '=', self._name), ('res_id', '=', rec.id),
                                                  ('state', '!=', 'resolved')]) if rec.id else 0

    def _compute_color(self):
        colors = {'ndr': 1, 'rto': 2, 'lost': 1, 'delivered': 10, 'returned': 3, 'cancelled': 0}
        for rec in self:
            rec.color = 2 if rec.is_delayed and rec.state in ACTIVE_STATES else colors.get(rec.state, 0)

    @api.constrains('cod_amount', 'payment_mode')
    def _check_cod(self):
        for rec in self:
            if rec.payment_mode == 'cod' and rec.cod_amount <= 0:
                raise ValidationError(_('Enter the amount the courier must collect for COD parcel %s.', rec.name))

    @api.onchange('payment_mode', 'declared_value')
    def _onchange_payment_mode(self):
        if self.payment_mode == 'cod' and not self.cod_amount:
            self.cod_amount = self.declared_value

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('cp.parcel') or _('New')
        records = super().create(vals_list)
        for rec in records.filtered(lambda r: not r.package_ids):
            rec.package_ids = [(0, 0, {'quantity': 1, 'weight_kg': 0.5, 'length_cm': 20, 'width_cm': 15,
                                       'height_cm': 10})]
        return records

    # ------------------------------------------------------------------
    # Helpers for adapters
    # ------------------------------------------------------------------
    def _cp_dims(self):
        """Outer size of the largest box, in whole cm (what couriers ask for on single-box APIs)."""
        self.ensure_one()
        biggest = max(self.package_ids, key=lambda p: p.length_cm * p.width_cm * p.height_cm, default=None)
        if not biggest:
            return 10, 10, 10
        return (max(int(round(biggest.length_cm)), 1), max(int(round(biggest.width_cm)), 1),
                max(int(round(biggest.height_cm)), 1))

    def _cp_items(self):
        self.ensure_one()
        units = max(self.piece_count, 1)
        value = self.cod_amount if self.payment_mode == 'cod' else self.declared_value
        return [{'name': self.contents or _('Goods'), 'sku': self.reference or self.name, 'units': units,
                 'selling_price': round((value or 0.0) / units, 2), 'hsn': self.hsn_code or ''}]

    def _cp_data(self):
        self.ensure_one()
        try:
            return json.loads(self.provider_data or '{}')
        except ValueError:
            return {}

    def _cp_set_data(self, **values):
        data = self._cp_data()
        data.update({k: v for k, v in values.items() if v not in (None, '')})
        self.provider_data = json.dumps(data, default=str)

    def _cp_check_ready(self):
        for rec in self:
            missing = []
            if not rec.account_id:
                missing.append(_('a courier account'))
            if not rec.dest_pin:
                missing.append(_('the receiver PIN code'))
            if not rec.origin_pin:
                missing.append(_('the pickup PIN code'))
            if not rec.recipient_partner_id.phone:
                missing.append(_('the receiver phone number'))
            if not rec.recipient_partner_id.street:
                missing.append(_('the receiver street address'))
            if rec.total_weight_kg <= 0:
                missing.append(_('the box weight'))
            if missing:
                raise UserError(_('%(parcel)s needs %(what)s before it can be booked.',
                                  parcel=rec.name, what=', '.join(missing)))
            if rec.account_id.max_weight_kg and rec.chargeable_weight_kg > rec.account_id.max_weight_kg:
                raise UserError(_('%(parcel)s weighs %(w)s kg, over the %(max)s kg limit of %(acc)s.',
                                  parcel=rec.name, w=rec.chargeable_weight_kg, max=rec.account_id.max_weight_kg,
                                  acc=rec.account_id.name))

    # ------------------------------------------------------------------
    # Main actions
    # ------------------------------------------------------------------
    def action_compare_rates(self):
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_('This parcel is already booked.'))
        wizard = self.env['cp.courier.rate.wizard'].create({'parcel_id': self.id})
        wizard.action_fetch()
        return {
            'type': 'ir.actions.act_window', 'name': _('Choose a courier'), 'res_model': 'cp.courier.rate.wizard',
            'res_id': wizard.id, 'view_mode': 'form', 'target': 'new',
        }

    def action_book(self, option=None):
        """Book with the selected courier account and store AWB, label and first event."""
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_('%s is already booked.', rec.name))
            rec._cp_check_ready()
            provider = rec.account_id._cp_provider()
            booking = provider.create_shipment(rec, option)
            now = fields.Datetime.now()
            vals = {
                'awb': booking['awb'], 'courier_name': booking.get('courier_name') or rec.account_id.name,
                'service': booking.get('service') or (option or {}).get('service') or rec.service,
                'provider_ref': booking.get('provider_ref') or False, 'state': 'booked', 'booked_at': now,
                'status_text': _('Booked'),
            }
            if option and option.get('price') is not None:
                vals['shipping_cost'] = option['price']
            elif not rec.shipping_cost:
                vals['shipping_cost'] = rec.account_id._cp_rate_card_price(rec)
            if booking.get('expected_delivery'):
                vals['expected_delivery'] = fields.Date.to_date(booking['expected_delivery'])
            elif option and option.get('days'):
                vals['expected_delivery'] = fields.Date.context_today(rec) + timedelta(days=int(option['days']))
            if booking.get('pickup_ref'):
                vals.update(pickup_ref=booking['pickup_ref'], pickup_date=now)
            rec.write(vals)
            rec._cp_set_data(label_url=booking.get('label_url'), **(booking.get('data') or {}))
            rec._cp_add_events([{'time': now, 'code': 'BOOKED', 'status': 'booked', 'location': rec.sender_partner_id.city or '',
                                 'description': _('Booked with %(courier)s, AWB %(awb)s',
                                                  courier=vals['courier_name'], awb=vals['awb'])}], source='system')
            if booking.get('label'):
                rec._cp_store_label(booking['label'])
            elif rec.account_id.auto_book_label and rec.account_id.can_label:
                try:
                    rec._cp_download_label()
                except UserError as err:
                    rec.message_post(body=_('Label not ready yet: %s', err.args[0] if err.args else err))
            rec.message_post(body=_('Booked with %(courier)s. AWB %(awb)s.', courier=vals['courier_name'],
                                    awb=vals['awb']))
            rec.env['cp.exception'].cp_auto_resolve(rec, ['courier_error'])
            if rec.account_id.notify_customer and rec.recipient_partner_id.email:
                template = rec.env.ref('cargo_pilot_tms.mail_template_parcel_booked', raise_if_not_found=False)
                if template:
                    template.send_mail(rec.id, force_send=False)
        return True

    def _cp_store_label(self, pdf):
        self.ensure_one()
        if self.label_attachment_id:
            self.label_attachment_id.unlink()
        attachment = self.env['ir.attachment'].create({
            'name': 'Label-%s-%s.pdf' % (self.courier_name or 'courier', self.awb),
            'datas': base64.b64encode(pdf), 'res_model': self._name, 'res_id': self.id,
            'mimetype': 'application/pdf',
        })
        self.label_attachment_id = attachment
        return attachment

    def _cp_download_label(self):
        self.ensure_one()
        pdf = self.account_id._cp_provider().fetch_label(self)
        if pdf:
            return self._cp_store_label(pdf)
        return False

    def action_print_label(self):
        """Courier label when the courier provides one, otherwise our own 4x6 label."""
        courier = self.filtered(lambda p: p.account_id.can_label and p.awb)
        for rec in courier.filtered(lambda p: not p.label_attachment_id):
            try:
                rec._cp_download_label()
            except UserError as err:
                rec.message_post(body=_('Courier label not available, using the Cargo Pilot label: %s',
                                        err.args[0] if err.args else err))
        if len(self) == 1 and self.label_attachment_id:
            return {'type': 'ir.actions.act_url', 'target': 'new',
                    'url': '/web/content/%s?download=false' % self.label_attachment_id.id}
        return self.env.ref('cargo_pilot_tms.action_report_parcel_label').with_context(discard_logo_check=True).report_action(self)

    def action_print_own_label(self):
        return self.env.ref('cargo_pilot_tms.action_report_parcel_label').with_context(discard_logo_check=True).report_action(self)

    def action_track_now(self):
        for rec in self:
            if rec.state == 'draft' or (not rec.awb and rec.state not in ('delivered', 'returned', 'cancelled', 'lost')):
                raise UserError(_('%s is not booked yet. Book it with a courier first, then track it.', rec.name))
        self._cp_sync_tracking(raise_errors=True)
        return True

    def action_cancel(self):
        for rec in self:
            if rec.state in ('delivered', 'returned'):
                raise UserError(_('%s has already been delivered and cannot be cancelled.', rec.name))
            if rec.state not in ('draft',) and rec.awb:
                if rec.state not in ('booked',):
                    raise UserError(_('%s is already with the courier. Ask the courier to return it instead '
                                      '(Delivery failed > Return to sender).', rec.name))
                rec.account_id._cp_provider().cancel(rec)
            rec.write({'state': 'cancelled', 'status_text': _('Cancelled')})
            rec._cp_add_events([{'time': fields.Datetime.now(), 'code': 'CANCEL', 'status': 'cancelled',
                                 'description': _('Shipment cancelled'), 'location': ''}], source='system')
            rec.env['cp.exception'].cp_auto_resolve(rec, ['ndr', 'rto', 'parcel_delay', 'courier_error'])
        return True

    def action_mark_cancelled(self):
        """For couriers without a cancel API: record that it was cancelled on the courier portal."""
        self.write({'state': 'cancelled', 'status_text': _('Cancelled on the courier portal')})
        return True

    def action_reset_draft(self):
        for rec in self:
            if rec.state != 'cancelled':
                raise UserError(_('Only cancelled parcels can go back to draft.'))
        self.write({'state': 'draft', 'awb': False, 'provider_ref': False, 'provider_data': False,
                    'label_attachment_id': False, 'courier_name': False, 'booked_at': False,
                    'expected_delivery': False, 'status_text': False, 'ndr_action': False, 'ndr_attempts': 0})
        return True

    def action_request_pickup(self):
        parcels = self.filtered(lambda p: p.state == 'booked')
        if not parcels:
            raise UserError(_('Select booked parcels that are waiting for pickup.'))
        return {
            'type': 'ir.actions.act_window', 'name': _('Request pickup'), 'res_model': 'cp.courier.pickup.wizard',
            'view_mode': 'form', 'target': 'new', 'context': {'default_parcel_ids': parcels.ids},
        }

    def action_ndr(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Delivery failed: what next?'),
            'res_model': 'cp.parcel.ndr.wizard', 'view_mode': 'form', 'target': 'new',
            'context': {'default_parcel_id': self.id},
        }

    def action_add_update(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Add a tracking update'),
            'res_model': 'cp.parcel.update.wizard', 'view_mode': 'form', 'target': 'new',
            'context': {'default_parcel_id': self.id},
        }

    def action_mark_cod_remitted(self):
        for rec in self:
            if rec.payment_mode != 'cod' or rec.state != 'delivered':
                raise UserError(_('%s is not a delivered COD parcel.', rec.name))
        self.write({'cod_state': 'remitted', 'cod_remit_date': fields.Date.context_today(self)})
        return True

    def action_share_tracking(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_url', 'url': self.public_url, 'target': 'new'}

    def action_open_courier_page(self):
        self.ensure_one()
        if not self.tracking_url:
            raise UserError(_('No courier tracking page is set for %s.', self.account_id.name))
        return {'type': 'ir.actions.act_url', 'url': self.tracking_url, 'target': 'new'}

    def action_view_issues(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('cargo_pilot_tms.action_cp_exception')
        action['domain'] = [('res_model', '=', self._name), ('res_id', '=', self.id)]
        action['context'] = {}
        return action

    def action_duplicate_to_draft(self):
        self.ensure_one()
        new = self.copy({'reference': self.reference})
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': new.id, 'view_mode': 'form'}

    # ------------------------------------------------------------------
    # Tracking
    # ------------------------------------------------------------------
    def _cp_add_events(self, events, source='courier'):
        """Add events that are not stored yet. Returns the created records."""
        self.ensure_one()
        known = {(e.event_time.replace(second=0, microsecond=0) if e.event_time else None,
                  (e.description or '').strip().lower()) for e in self.event_ids}
        vals = []
        for ev in events:
            when = ev.get('time') or fields.Datetime.now()
            key = (when.replace(second=0, microsecond=0), (ev.get('description') or '').strip().lower())
            if key in known:
                continue
            known.add(key)
            vals.append({'parcel_id': self.id, 'event_time': when, 'code': (ev.get('code') or '')[:64],
                         'description': ev.get('description') or ev.get('code') or '',
                         'location': ev.get('location') or '', 'status': ev.get('status') or False,
                         'source': source})
        return self.env['cp.parcel.event'].create(vals) if vals else self.env['cp.parcel.event']

    def _cp_apply_tracking(self, info, source='courier'):
        """Merge a tracking result (see providers.base) into this parcel and react to the new status."""
        self.ensure_one()
        events = info.get('events') or []
        self._cp_add_events(events, source=source)
        new_state = info.get('status') or ''
        if not new_state and events:
            ordered = sorted([e for e in events if e.get('time')], key=lambda e: e['time'])
            new_state = (ordered[-1] if ordered else events[-1]).get('status') or ''
        if not new_state:
            new_state = guess_status(info.get('status_text'))
        vals = {'last_sync_at': fields.Datetime.now()}
        if info.get('status_text'):
            vals['status_text'] = info['status_text'][:250]
        dated = [e for e in events if e.get('time')]
        if dated:
            latest = max(dated, key=lambda e: e['time'])
            vals['last_event_at'] = latest['time']
            if latest.get('location'):
                vals['last_location'] = latest['location'][:120]
        if info.get('expected_delivery'):
            vals['expected_delivery'] = fields.Date.to_date(info['expected_delivery'])
        if info.get('courier_name') and not self.courier_name:
            vals['courier_name'] = info['courier_name']
        old_state = self.state
        if new_state and self._cp_can_move(old_state, new_state):
            vals['state'] = new_state
            when = vals.get('last_event_at') or fields.Datetime.now()
            if new_state == 'picked_up' and not self.picked_up_at:
                vals['picked_up_at'] = when
            if new_state == 'delivered':
                vals['delivered_at'] = when
            if new_state == 'ndr':
                vals['ndr_attempts'] = self.ndr_attempts + 1
                vals['ndr_reason'] = (info.get('status_text') or '')[:120]
                vals['ndr_action'] = False
        self.write(vals)
        if vals.get('state') and vals['state'] != old_state:
            self._cp_on_state_change(old_state, vals['state'])
        self._cp_check_delay()
        return True

    @staticmethod
    def _cp_can_move(old, new):
        if old == new or old in TERMINAL:
            return False
        if new in ('ndr', 'rto', 'returned', 'cancelled', 'lost', 'delivered'):
            return not (old == 'rto' and new in ('ndr', 'delivered'))
        if old in ('ndr',):
            return new in ('out_for_delivery', 'in_transit')
        if old == 'rto':
            return False
        if new in FLOW and old in FLOW:
            return FLOW.index(new) > FLOW.index(old)
        return False

    def _cp_on_state_change(self, old, new):
        Issue = self.env['cp.exception']
        name = self.awb or self.name
        if new == 'ndr':
            Issue.cp_raise('ndr', self, _('Delivery failed: %(awb)s to %(who)s', awb=name,
                                          who=self.recipient_partner_id.name),
                           severity='high', detail=self.ndr_reason,
                           suggestion=_('Call the receiver, then choose Re-attempt or Return to sender.'))
        elif new == 'rto':
            Issue.cp_auto_resolve(self, ['ndr'], note=_('Parcel is on its way back.'))
            Issue.cp_raise('rto', self, _('Parcel %s is coming back', name), severity='medium',
                           detail=self.status_text,
                           suggestion=_('Contact the customer, then plan a refund or a reship.'))
        elif new in ('out_for_delivery', 'in_transit') and old == 'ndr':
            Issue.cp_auto_resolve(self, ['ndr'], note=_('Courier is trying again.'))
        elif new == 'lost':
            Issue.cp_raise('courier_error', self, _('Parcel %s reported lost or damaged', name), severity='critical',
                           suggestion=_('Raise a claim with the courier with the invoice and packing photos.'))
        if new in ('delivered', 'returned', 'cancelled'):
            Issue.cp_auto_resolve(self, ['ndr', 'rto', 'parcel_delay'],
                                  note=_('Parcel %s.', dict(PARCEL_STATES)[new].lower()))
        if self.consignment_id and new == 'delivered' and self.consignment_id.state in ('booked', 'in_transit',
                                                                                         'at_hub'):
            self.consignment_id.sudo().write({'state': 'delivered'})

    def _cp_check_delay(self):
        Issue = self.env['cp.exception']
        for rec in self:
            if rec.is_delayed:
                Issue.cp_raise('parcel_delay', rec, _('Parcel %(awb)s is late (due %(date)s)',
                                                      awb=rec.awb or rec.name, date=rec.expected_delivery),
                               severity='medium', detail=rec.status_text,
                               suggestion=_('Ask the courier for a new delivery date and tell the customer.'))
            elif rec.state not in ACTIVE_STATES:
                Issue.cp_auto_resolve(rec, ['parcel_delay'])

    def _cp_sync_tracking(self, raise_errors=False):
        parcels = self.filtered(lambda p: p.awb and p.state in ACTIVE_STATES and p.account_id.can_track)
        for account in parcels.account_id:
            batch = parcels.filtered(lambda p: p.account_id == account)
            try:
                results = account._cp_provider().track(batch)
            except UserError as err:
                if raise_errors:
                    raise
                _logger.warning('Tracking sync failed for %s: %s', account.name, err)
                continue
            for parcel in batch:
                info = results.get(parcel.awb)
                if info:
                    parcel._cp_apply_tracking(info)
                else:
                    parcel.last_sync_at = fields.Datetime.now()
        if raise_errors and len(self) == 1 and not self.account_id.can_track:
            raise UserError(_('%s has no tracking API. Add updates by hand or open the courier page.',
                              self.account_id.name or self.courier_name or _('This courier')))
        return True

    @api.model
    def _cron_sync_tracking(self, limit=500):
        parcels = self.search([('state', 'in', ACTIVE_STATES), ('awb', '!=', False)],
                              order='last_sync_at asc nulls first', limit=limit)
        parcels._cp_sync_tracking()
        self.search([('state', 'in', ACTIVE_STATES), ('expected_delivery', '<', fields.Date.context_today(self))])\
            ._compute_is_delayed()
        self.search([('is_delayed', '=', True)])._cp_check_delay()
        return True

    def _cp_issue_on_failure(self, message):
        for rec in self:
            self.env['cp.exception'].cp_raise('courier_error', rec, _('Courier booking failed for %s', rec.name),
                                              severity='high', detail=message,
                                              suggestion=_('Check the address and PIN code, or try another courier.'))


class CpParcelPackage(models.Model):
    _name = 'cp.parcel.package'
    _description = 'Parcel Box'

    parcel_id = fields.Many2one('cp.parcel', required=True, ondelete='cascade')
    name = fields.Char(string='Box', default=lambda self: _('Box'))
    quantity = fields.Integer(string='Qty', default=1)
    weight_kg = fields.Float(string='Weight (kg)', default=0.5)
    length_cm = fields.Float(string='L (cm)', default=20)
    width_cm = fields.Float(string='W (cm)', default=15)
    height_cm = fields.Float(string='H (cm)', default=10)


class CpParcelEvent(models.Model):
    _name = 'cp.parcel.event'
    _description = 'Parcel Tracking Event'
    _order = 'event_time desc, id desc'

    parcel_id = fields.Many2one('cp.parcel', required=True, ondelete='cascade', index=True)
    event_time = fields.Datetime(required=True, default=fields.Datetime.now)
    code = fields.Char()
    description = fields.Char()
    location = fields.Char()
    status = fields.Selection(PARCEL_STATES)
    source = fields.Selection([('courier', 'Courier'), ('webhook', 'Courier push'), ('manual', 'Added by user'),
                               ('system', 'Cargo Pilot')], default='courier')
