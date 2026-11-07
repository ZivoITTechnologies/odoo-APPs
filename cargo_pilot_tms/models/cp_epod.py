# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .cp_common import split_serials
from .cp_master import haversine_km


class CpEpod(models.Model):
    _name = 'cp.epod'
    _description = 'Electronic Proof of Delivery'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(default=lambda self: _('New'), copy=False, readonly=True)
    trip_id = fields.Many2one('cp.trip', required=True, ondelete='cascade', index=True)
    transporter_id = fields.Many2one(related='trip_id.transporter_id', store=True)
    consignee_id = fields.Many2one(related='trip_id.consignee_id', store=True)
    channel = fields.Selection([
        ('backend', 'Backend'), ('driver', 'Driver Mobile Page'), ('portal', 'Transporter Portal'),
        ('api', 'API'), ('carrier', 'Carrier'),
    ], default='backend', readonly=True)
    receiver_name = fields.Char(string='Received By')
    receiver_phone = fields.Char()
    received_time = fields.Datetime(default=fields.Datetime.now)
    received_weight_ton = fields.Float(string='Received Weight (t)')
    received_packages = fields.Integer()
    shortage_packages = fields.Integer()
    damaged_packages = fields.Integer()
    remarks = fields.Text()
    refused_by_consignee = fields.Boolean(help='Consignee refused or the site was closed.')
    signature = fields.Binary(attachment=True)
    photo_ids = fields.Many2many('ir.attachment', 'cp_epod_photo_rel', 'epod_id', 'attachment_id',
                                 string='Photos / Documents')
    latitude = fields.Float(digits=(10, 7))
    longitude = fields.Float(digits=(10, 7))
    distance_from_site_m = fields.Integer(compute='_compute_distance', store=True,
                                          string='Distance from Site (m)')

    check_signature = fields.Boolean(readonly=True)
    check_photo = fields.Boolean(readonly=True)
    check_location = fields.Boolean(readonly=True)
    check_weight = fields.Boolean(readonly=True)
    check_condition = fields.Boolean(readonly=True, string='Check: No Shortage/Damage')
    scanned_serials = fields.Text(string='Serials Delivered', help='Scanned or typed at delivery, one per line.')
    missing_serials = fields.Text(readonly=True, string='Serials Not Delivered')
    extra_serials = fields.Text(readonly=True, string='Unexpected Serials')
    check_serials = fields.Boolean(readonly=True, string='Check: Serials')
    grn_number = fields.Char(string='GRN No.', help="Retailer's goods receipt note number.")
    check_grn = fields.Boolean(readonly=True, string='Check: GRN')
    fault_party = fields.Selection([
        ('none', 'No Fault'), ('carrier', 'Carrier'), ('customer', 'Customer / Consignee'),
    ], readonly=True, default='none')
    exception_reason = fields.Text(readonly=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('submitted', 'Submitted'), ('verified', 'Verified'),
        ('exception', 'Exception'), ('rejected', 'Rejected'),
    ], default='draft', tracking=True)
    company_id = fields.Many2one(related='trip_id.company_id', store=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('cp.epod') or _('New')
        return super().create(vals_list)

    @api.depends('latitude', 'longitude', 'trip_id.destination_id')
    def _compute_distance(self):
        for rec in self:
            dest = rec.trip_id.destination_id
            if (rec.latitude or rec.longitude) and dest and (dest.latitude or dest.longitude):
                rec.distance_from_site_m = int(haversine_km(rec.latitude, rec.longitude,
                                                            dest.latitude, dest.longitude) * 1000)
            else:
                rec.distance_from_site_m = 0

    def action_submit(self):
        for rec in self:
            if rec.state not in ('draft', 'rejected'):
                continue
            rec.state = 'submitted'
            rec.trip_id._cp_log('epod', _('ePOD %s submitted.', rec.name), rec.latitude, rec.longitude)
            rec._cp_validate()
        return True

    def _cp_validate(self):
        """Rule-based checks of the delivery evidence, with fault classification."""
        for rec in self:
            company = rec.trip_id.company_id
            radius = company.cp_epod_geofence_m or 1000
            tolerance = company.cp_audit_weight_tolerance or 1.0
            loaded = rec.trip_id.loaded_weight_ton
            reasons = []
            has_coords = bool(rec.latitude or rec.longitude)
            dest = rec.trip_id.destination_id
            site_known = bool(dest and (dest.latitude or dest.longitude))
            expected = set(split_serials('\n'.join(rec.trip_id.order_ids.mapped(lambda o: o.serial_numbers or ''))))
            scanned = set(split_serials(rec.scanned_serials))
            need_serials = bool(expected) and (rec.trip_id.serial_scan_required or bool(scanned))
            missing = sorted(expected - scanned) if need_serials else []
            extra = sorted(scanned - expected) if expected and scanned else []
            checks = {
                'check_serials': not missing and not extra,
                'missing_serials': '\n'.join(missing) or False,
                'extra_serials': '\n'.join(extra) or False,
                'check_grn': (not rec.trip_id.grn_required) or bool(rec.grn_number),
                'check_signature': bool(rec.signature),
                'check_photo': bool(rec.photo_ids),
                'check_location': (not site_known) or (has_coords and rec.distance_from_site_m <= radius),
                'check_weight': (not loaded) or abs(rec.received_weight_ton - loaded) <= loaded * tolerance / 100.0,
                'check_condition': not rec.shortage_packages and not rec.damaged_packages,
            }
            if not checks['check_signature']:
                reasons.append(_('Receiver signature missing.'))
            if not checks['check_photo']:
                reasons.append(_('No photo of the signed delivery copy.'))
            if not checks['check_location']:
                reasons.append(_('Captured %(m)s m from the delivery site (limit %(r)s m).',
                                 m=rec.distance_from_site_m if has_coords else '?', r=radius))
            if not checks['check_weight']:
                reasons.append(_('Received %(r)s t vs loaded %(l)s t.', r=rec.received_weight_ton, l=loaded))
            if not checks['check_condition']:
                reasons.append(_('%(s)s short, %(d)s damaged package(s).',
                                 s=rec.shortage_packages, d=rec.damaged_packages))
            if missing:
                reasons.append(_('%(n)s serial number(s) not scanned at delivery: %(s)s',
                                 n=len(missing), s=', '.join(missing[:10])))
            if extra:
                reasons.append(_('%(n)s scanned serial(s) not on this shipment: %(s)s',
                                 n=len(extra), s=', '.join(extra[:10])))
            if not checks['check_grn']:
                reasons.append(_("The retailer's GRN number is missing."))
            if rec.refused_by_consignee:
                fault = 'customer'
                reasons.append(_('Consignee refused delivery or site closed.'))
            elif reasons:
                fault = 'carrier'
            else:
                fault = 'none'
            checks.update({'fault_party': fault, 'exception_reason': '\n'.join(reasons)})
            rec.write(checks)
            if reasons:
                self.env['cp.exception'].cp_raise(
                    'epod', rec, _('Delivery proof issue on %s', rec.trip_id.name),
                    severity='high' if not checks['check_condition'] or missing or fault == 'customer' else 'medium',
                    detail='\n'.join(reasons), trip=rec.trip_id,
                    suggestion=_('Ask the transporter to re-upload, or verify manually after checking.'))
                rec.state = 'exception'
                rec.trip_id._cp_log('exception', _('ePOD exception: %s', reasons[0]))
                owner = rec.trip_id.indent_id.user_id or self.env.user
                rec.activity_schedule(
                    'mail.mail_activity_data_todo', user_id=owner.id,
                    summary=_('ePOD exception (%s fault)', dict(self._fields['fault_party'].selection)[fault]),
                    note='\n'.join(reasons))
                if fault == 'carrier' and rec.transporter_id:
                    rec.message_post(
                        body=_('Please correct ePOD %(name)s for trip %(trip)s: %(why)s',
                               name=rec.name, trip=rec.trip_id.name, why=' '.join(reasons)),
                        partner_ids=rec.transporter_id.ids, message_type='comment',
                        subtype_xmlid='mail.mt_comment')
            else:
                rec._cp_mark_verified()

    def _cp_mark_verified(self):
        Issue = self.env['cp.exception']
        for rec in self:
            rec.state = 'verified'
            for other in rec.trip_id.epod_ids:
                Issue.cp_auto_resolve(other, ('epod',), _('Delivery proof verified.'))
            Issue.cp_auto_resolve(rec.trip_id, ('epod_missing',), _('Delivery proof verified.'))
            trip = rec.trip_id
            vals = {'delivered_weight_ton': rec.received_weight_ton or trip.loaded_weight_ton}
            reference = trip.actual_arrival or trip.actual_delivery
            submitted = rec.create_date or fields.Datetime.now()
            if not trip.epod_tat_hours:
                hours = (submitted - reference).total_seconds() / 3600.0 if reference else 0.0
                vals['epod_tat_hours'] = round(max(hours, 0.1), 1)  # 0.1 h = captured at the door
            trip.sudo().write(vals)
            if trip.state in ('in_transit', 'arrived'):
                trip.sudo().action_mark_delivered()
            trip._cp_log('epod', _('ePOD %s verified.', rec.name))
            trip.sudo().pod_age_bucket = 'done'
            self.env['cp.webhook'].sudo()._cp_emit('pod.verified', trip, {'epod': rec.name})

    def action_verify(self):
        """Manual override by the planner after reviewing an exception."""
        for rec in self:
            if rec.state not in ('submitted', 'exception'):
                raise UserError(_('Only submitted ePODs can be verified.'))
            rec.message_post(body=_('Verified manually by %s.', self.env.user.name))
            rec._cp_mark_verified()

    def action_reject(self):
        self.write({'state': 'rejected'})
        for rec in self:
            if rec.transporter_id:
                rec.message_post(body=_('ePOD %s was rejected. Please resubmit.', rec.name),
                                 partner_ids=rec.transporter_id.ids, message_type='comment',
                                 subtype_xmlid='mail.mt_comment')

    def action_recheck(self):
        self.filtered(lambda e: e.state in ('submitted', 'exception'))._cp_validate()

    @api.model
    def _cron_epod_followup(self):
        """Delivered trips without ePOD: nudge after 12 h, escalate after 48 h."""
        Trip = self.env['cp.trip']
        now = fields.Datetime.now()
        for trip in Trip.search([('state', 'in', ('arrived', 'delivered')), ('epod_state', '=', 'none')]):
            reference = trip.actual_arrival or trip.actual_delivery
            if not reference:
                continue
            age = now - reference
            if age >= timedelta(hours=12):
                self.env['cp.exception'].cp_raise(
                    'epod_missing', trip, _('No delivery proof for %s', trip.name),
                    severity='high' if age >= timedelta(hours=48) else 'medium',
                    suggestion=_('Send the driver link again or ask the transporter to upload.'))
            if age >= timedelta(hours=48):
                summary = _('ePOD overdue 48h+: %s', trip.name)
            elif age >= timedelta(hours=12):
                summary = _('ePOD pending: %s', trip.name)
            else:
                continue
            if not trip.activity_ids.filtered(lambda a: a.summary == summary):
                trip.activity_schedule('mail.mail_activity_data_todo', summary=summary,
                                       user_id=(trip.indent_id.user_id or self.env.user).id)
                if trip.transporter_id:
                    trip.message_post(body=_('Reminder: upload the ePOD for trip %s.', trip.name),
                                      partner_ids=trip.transporter_id.ids, message_type='comment',
                                      subtype_xmlid='mail.mt_comment')
        return True
