# -*- coding: utf-8 -*-
"""Carrier master: regular partners (DHL, Delhivery, FedEx...) with serviceability limits and
tracking links, approved 3PL vendors with their route zones, and vendor detention terms."""
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

CHECK_STATUSES = [
    ('confirmed', 'Confirmed'),
    ('no_vehicle', 'Rejected - No Vehicle'),
    ('over_capacity', 'Rejected - Over Capacity'),
    ('no_service', 'Out of Service Area'),
]


class ResPartner(models.Model):
    _inherit = 'res.partner'

    cp_regular_carrier = fields.Boolean(string='Regular Logistics Partner',
                                        help='Checked first for every dispatch, before any 3PL bidding.')
    cp_carrier_sequence = fields.Integer(string='Carrier Priority', default=10)
    cp_tracking_url_template = fields.Char(
        string='Tracking Link', help='Address of the carrier tracking page; {ref} is replaced by the AWB / booking '
                                     'number, for example https://www.fedex.com/fedextrack/?trknbr={ref}')
    cp_max_weight_kg = fields.Float(string='Max Weight per Shipment (kg)',
                                    help='0 = no limit. Heavier dispatches are pre-marked Over Capacity.')
    cp_max_volume_cbm = fields.Float(string='Max Volume per Shipment (m³)', help='0 = no limit.')
    cp_service_pincodes = fields.Text(
        string='Serviceable PIN Codes',
        help='PIN codes or prefixes separated by commas or new lines (for example 4000, 411001). '
             'Empty = all of India. Other destinations are pre-marked Out of Service Area.')
    cp_is_3pl_vendor = fields.Boolean(string='Approved 3PL Vendor',
                                      help='Invited to time-bound bids for the routes and truck types below.')
    cp_service_state_ids = fields.Many2many('res.country.state', 'cp_partner_service_state_rel', 'partner_id',
                                            'state_id', string='Service Zones (States)',
                                            help='Destination states this vendor serves. Empty = all.')
    cp_detention_term_ids = fields.One2many('cp.detention.term', 'partner_id', string='Detention Terms')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('cp_is_3pl_vendor') or vals.get('cp_regular_carrier'):
                vals['cp_is_transporter'] = True
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('cp_is_3pl_vendor') or vals.get('cp_regular_carrier'):
            vals = dict(vals, cp_is_transporter=True)
        return super().write(vals)

    def cp_tracking_link(self, ref):
        self.ensure_one()
        if not (self.cp_tracking_url_template and ref):
            return False
        return self.cp_tracking_url_template.replace('{ref}', str(ref).strip())

    def cp_precheck(self, plan):
        """Suggested availability for a regular carrier from its limits, or False."""
        self.ensure_one()
        pincode = (plan.dest_pincode or '').replace(' ', '')
        codes = [c.strip() for c in (self.cp_service_pincodes or '').replace('\n', ',').split(',') if c.strip()]
        if codes and pincode and not any(pincode.startswith(c) for c in codes):
            return 'no_service'
        if self.cp_max_weight_kg and plan.gross_weight_kg > self.cp_max_weight_kg:
            return 'over_capacity'
        if self.cp_max_volume_cbm and plan.volume_cbm > self.cp_max_volume_cbm:
            return 'over_capacity'
        return False


class CpDetentionTerm(models.Model):
    """Detention / halting terms agreed with a vendor, optionally per truck type."""
    _name = 'cp.detention.term'
    _description = 'Vendor Detention Terms'
    _order = 'partner_id, vehicle_type_id'

    partner_id = fields.Many2one('res.partner', string='Vendor', required=True, ondelete='cascade', index=True)
    vehicle_type_id = fields.Many2one('cp.vehicle.type', string='Truck Type', help='Empty = any truck type.')
    free_hours = fields.Float(string='Free Waiting (hours)', default=2.0, required=True)
    method = fields.Selection([('hourly', 'Hourly rate'), ('daily', 'Daily halting slab')], default='hourly',
                              required=True)
    hourly_rate = fields.Monetary(string='Rate per Hour')
    daily_rate = fields.Monetary(string='Rate per Day')
    partial_day = fields.Selection([('full', 'Any part of a day counts as a full day'),
                                    ('prorata', 'Pro rata by hours')], default='full', string='Part Days')
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    note = fields.Char()

    @api.depends('partner_id', 'vehicle_type_id', 'method', 'free_hours')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = '%s · %s · %s' % (
                rec.partner_id.name or _('New'), rec.vehicle_type_id.name or _('Any truck'),
                _('%(h)s h free, %(m)s', h=('%g' % rec.free_hours), m=dict(rec._fields['method'].selection).get(rec.method, '')))

    @api.constrains('free_hours', 'hourly_rate', 'daily_rate')
    def _check_values(self):
        for rec in self:
            if min(rec.free_hours, rec.hourly_rate, rec.daily_rate) < 0:
                raise ValidationError(_('Detention terms cannot be negative.'))

    @api.model
    def cp_for(self, partner, vehicle_type=None):
        terms = self.search([('partner_id', '=', partner.commercial_partner_id.id)])
        return terms.filtered(lambda t: vehicle_type and t.vehicle_type_id == vehicle_type)[:1] or \
            terms.filtered(lambda t: not t.vehicle_type_id)[:1]
