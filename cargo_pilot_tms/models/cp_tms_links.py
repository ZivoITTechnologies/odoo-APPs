# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError

NEW_ISSUES = [
    ('ndr', 'Parcel delivery failed'),
    ('rto', 'Parcel returning'),
    ('parcel_delay', 'Parcel late'),
    ('courier_error', 'Courier problem'),
]


class CpException(models.Model):
    _inherit = 'cp.exception'

    issue_type = fields.Selection(selection_add=NEW_ISSUES,
                                  ondelete={key: 'set default' for key, _label in NEW_ISSUES})


class CpConsignment(models.Model):
    _inherit = 'cp.consignment'

    parcel_ids = fields.One2many('cp.parcel', 'consignment_id', string='Courier Parcels')
    parcel_count = fields.Integer(compute='_compute_parcel_count')
    parcel_awb = fields.Char(compute='_compute_parcel_count', string='Courier AWB')

    @api.depends('parcel_ids.awb', 'parcel_ids.state')
    def _compute_parcel_count(self):
        for rec in self:
            live = rec.parcel_ids.filtered(lambda p: p.state != 'cancelled')
            rec.parcel_count = len(live)
            rec.parcel_awb = ', '.join(p.awb for p in live if p.awb)

    def _cp_parcel_vals(self):
        self.ensure_one()
        sender = self.booking_hub_id.partner_id or self.from_location_id.partner_id or self.consignor_id
        boxes = []
        for line in self.line_ids:
            qty = max(line.packages, 1)
            boxes.append((0, 0, {'name': line.description, 'quantity': qty,
                                 'weight_kg': round((line.actual_weight_kg or 0.5) / qty, 3),
                                 'length_cm': 30, 'width_cm': 25, 'height_cm': 20}))
        cod = self.payment_type == 'to_pay'
        return {
            'reference': self.invoice_ref or self.name, 'consignment_id': self.id,
            'sender_partner_id': sender.id, 'recipient_partner_id': self.consignee_id.id,
            'declared_value': self.goods_value, 'payment_mode': 'cod' if cod else 'prepaid',
            'cod_amount': self.total_amount if cod else 0.0,
            'contents': ', '.join(self.line_ids.mapped('description'))[:60] or _('Goods'),
            'invoice_number': self.invoice_ref, 'eway_bill_no': self.eway_bill_no,
            'package_ids': boxes, 'company_id': self.company_id.id,
        }

    def action_ship_by_courier(self):
        self.ensure_one()
        if self.state == 'cancel':
            raise UserError(_('This booking is cancelled.'))
        draft = self.parcel_ids.filtered(lambda p: p.state == 'draft')[:1]
        parcel = draft or self.env['cp.parcel'].create(self._cp_parcel_vals())
        return {'type': 'ir.actions.act_window', 'res_model': 'cp.parcel', 'res_id': parcel.id,
                'view_mode': 'form', 'target': 'current'}

    def action_view_parcels(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('cargo_pilot_tms.action_cp_parcel')
        action['domain'] = [('consignment_id', '=', self.id)]
        action['context'] = {'default_consignment_id': self.id}
        return action


class CpDashboard(models.AbstractModel):
    _inherit = 'cp.dashboard'

    @api.model
    def get_dashboard_data(self):
        data = super().get_dashboard_data()
        Parcel = self.env['cp.parcel']
        if not Parcel.has_access('read'):
            return data
        active = ('booked', 'picked_up', 'in_transit', 'out_for_delivery', 'ndr', 'rto')
        month_start = fields.Date.context_today(self).replace(day=1)
        closed = Parcel.search_count([('state', 'in', ('delivered', 'returned')), ('delivered_at', '>=', month_start)])
        delivered = Parcel.search_count([('state', '=', 'delivered'), ('delivered_at', '>=', month_start)])
        cod_groups = Parcel._read_group([('cod_state', '=', 'collected')], [], ['cod_amount:sum'])
        data['parcels'] = {
            'enabled': bool(self.env['cp.courier.account'].search_count([])),
            'active': Parcel.search_count([('state', 'in', active)]),
            'to_book': Parcel.search_count([('state', '=', 'draft')]),
            'out_for_delivery': Parcel.search_count([('state', '=', 'out_for_delivery')]),
            'ndr': Parcel.search_count([('state', '=', 'ndr')]),
            'rto': Parcel.search_count([('state', '=', 'rto')]),
            'delayed': Parcel.search_count([('is_delayed', '=', True)]),
            'delivered_month': delivered,
            'success_pct': round(delivered * 100 / closed) if closed else 0,
            'cod_due': round(cod_groups[0][0] or 0.0) if cod_groups else 0,
        }
        return data
