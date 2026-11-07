# -*- coding: utf-8 -*-
import re
from collections import defaultdict

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class CpConsignment(models.Model):
    _name = 'cp.consignment'
    _description = 'Consignment Note (LR / Bilty)'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'booking_date desc, id desc'

    name = fields.Char(string='LR No.', default=lambda self: _('New'), copy=False, readonly=True)
    booking_date = fields.Date(default=fields.Date.context_today, required=True)
    booking_hub_id = fields.Many2one('cp.location', string='Booking Branch',
                                     domain=[('location_type', 'in', ('hub', 'warehouse'))])
    load_type = fields.Selection([('ftl', 'Full Truck Load'), ('ptl', 'Part Truck Load')],
                                 default='ftl', required=True)
    consignor_id = fields.Many2one('res.partner', string='Consignor', required=True)
    consignee_id = fields.Many2one('res.partner', string='Consignee', required=True)
    billing_partner_id = fields.Many2one('res.partner', string='Billing Party', required=True)
    from_location_id = fields.Many2one('cp.location', string='From', required=True)
    to_location_id = fields.Many2one('cp.location', string='To', required=True)
    line_ids = fields.One2many('cp.consignment.line', 'consignment_id', string='Goods', copy=True)
    total_packages = fields.Integer(compute='_compute_totals', store=True)
    actual_weight_kg = fields.Float(compute='_compute_totals', store=True, string='Actual Weight (kg)')
    charged_weight_kg = fields.Float(compute='_compute_totals', store=True, string='Charged Weight (kg)')
    goods_value = fields.Monetary(string='Declared Goods Value')
    eway_bill_no = fields.Char(string='E-way Bill No.')
    invoice_ref = fields.Char(string='Consignor Invoice No.')

    freight_basis = fields.Selection([
        ('fixed', 'Fixed'), ('kg', 'Per Kg'), ('ton', 'Per Tonne'), ('package', 'Per Package'),
    ], default='fixed', required=True)
    freight_rate = fields.Monetary()
    freight_amount = fields.Monetary(compute='_compute_amounts', store=True)
    loading_charge = fields.Monetary()
    unloading_charge = fields.Monetary()
    door_collection_charge = fields.Monetary(string='Door Collection')
    door_delivery_charge = fields.Monetary(string='Door Delivery')
    other_charge = fields.Monetary(string='Other Charges')
    tax_ids = fields.Many2many('account.tax', string='Taxes', domain=[('type_tax_use', '=', 'sale')])
    untaxed_amount = fields.Monetary(compute='_compute_amounts', store=True)
    tax_amount = fields.Monetary(compute='_compute_amounts', store=True)
    total_amount = fields.Monetary(compute='_compute_amounts', store=True, string='Total')
    advance_received = fields.Monetary()
    balance_amount = fields.Monetary(compute='_compute_amounts', store=True, string='Balance')
    payment_type = fields.Selection([
        ('paid', 'Paid (at booking)'), ('to_pay', 'To Pay (at delivery)'), ('tbb', 'To Be Billed'),
    ], default='tbb', required=True)

    trip_id = fields.Many2one('cp.trip', string='Trip', index=True)
    vehicle_number = fields.Char(related='trip_id.vehicle_number')
    challan_ids = fields.Many2many('cp.challan', 'cp_challan_consignment_rel', 'consignment_id', 'challan_id',
                                   string='Challans', readonly=True)
    current_hub_id = fields.Many2one('cp.location', string='Current Hub', readonly=True, tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('booked', 'Booked'), ('in_transit', 'In Transit'), ('at_hub', 'At Hub'),
        ('delivered', 'Delivered'), ('invoiced', 'Invoiced'), ('cancel', 'Cancelled'),
    ], default='draft', tracking=True)
    invoice_id = fields.Many2one('account.move', string='Customer Invoice', readonly=True, copy=False)
    invoice_state = fields.Selection(related='invoice_id.payment_state', string='Payment')
    pod_received = fields.Boolean(string='POD Received')
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('cp.consignment') or _('New')
        return super().create(vals_list)

    @api.onchange('consignor_id')
    def _onchange_consignor(self):
        if self.consignor_id and not self.billing_partner_id:
            self.billing_partner_id = self.consignor_id

    @api.depends('line_ids.packages', 'line_ids.actual_weight_kg', 'line_ids.charged_weight_kg')
    def _compute_totals(self):
        for rec in self:
            rec.total_packages = sum(rec.line_ids.mapped('packages'))
            rec.actual_weight_kg = sum(rec.line_ids.mapped('actual_weight_kg'))
            rec.charged_weight_kg = sum(rec.line_ids.mapped('charged_weight_kg'))

    @api.depends('freight_basis', 'freight_rate', 'charged_weight_kg', 'total_packages', 'loading_charge',
                 'unloading_charge', 'door_collection_charge', 'door_delivery_charge', 'other_charge',
                 'tax_ids', 'advance_received')
    def _compute_amounts(self):
        for rec in self:
            weight = rec.charged_weight_kg
            if rec.freight_basis == 'kg':
                freight = rec.freight_rate * weight
            elif rec.freight_basis == 'ton':
                freight = rec.freight_rate * weight / 1000.0
            elif rec.freight_basis == 'package':
                freight = rec.freight_rate * rec.total_packages
            else:
                freight = rec.freight_rate
            untaxed = freight + rec.loading_charge + rec.unloading_charge + rec.door_collection_charge \
                + rec.door_delivery_charge + rec.other_charge
            taxes = rec.tax_ids.compute_all(untaxed, currency=rec.currency_id) if rec.tax_ids else None
            total = taxes['total_included'] if taxes else untaxed
            rec.freight_amount = freight
            rec.untaxed_amount = untaxed
            rec.tax_amount = total - untaxed
            rec.total_amount = total
            rec.balance_amount = total - rec.advance_received

    def action_book(self):
        for rec in self:
            if not rec.line_ids:
                raise UserError(_('Add the goods on %s.', rec.name))
            rec.write({'state': 'booked', 'current_hub_id': rec.booking_hub_id.id or rec.from_location_id.id})
        return True

    def action_cancel(self):
        for rec in self:
            if rec.invoice_id and rec.invoice_id.state == 'posted':
                raise UserError(_('Cancel the posted invoice of %s first.', rec.name))
        self.write({'state': 'cancel'})

    def action_draft(self):
        self.write({'state': 'draft'})

    def action_mark_delivered(self):
        self.write({'state': 'delivered', 'current_hub_id': False, 'pod_received': True})

    def action_create_trip(self):
        """Create an FTL trip directly from a booked consignment."""
        self.ensure_one()
        lane = self.env['cp.lane'].search([('origin_id', '=', self.from_location_id.id),
                                           ('destination_id', '=', self.to_location_id.id)], limit=1)
        if not lane:
            lane = self.env['cp.lane'].create({'origin_id': self.from_location_id.id,
                                               'destination_id': self.to_location_id.id})
        trip = self.env['cp.trip'].create({
            'lane_id': lane.id,
            'consignee_id': self.consignee_id.id,
            'loaded_weight_ton': self.actual_weight_kg / 1000.0,
            'eway_bill_no': self.eway_bill_no,
            'agreed_freight': self.total_amount,
        })
        self.trip_id = trip
        return {'type': 'ir.actions.act_window', 'res_model': 'cp.trip', 'res_id': trip.id, 'view_mode': 'form'}

    def action_create_invoice(self):
        """Invoice selected consignments, one invoice per billing party."""
        to_bill = self.filtered(lambda c: c.state in ('booked', 'in_transit', 'at_hub', 'delivered')
                                and not c.invoice_id)
        if not to_bill:
            raise UserError(_('Select booked or delivered consignments that are not invoiced yet.'))
        product = self.env.ref('cargo_pilot_tms.product_cp_freight', raise_if_not_found=False)
        groups = defaultdict(lambda: self.env['cp.consignment'])
        for rec in to_bill:
            groups[(rec.billing_partner_id, rec.company_id)] |= rec
        moves = self.env['account.move']
        for (partner, company), records in groups.items():
            move = self.env['account.move'].with_company(company).create({
                'move_type': 'out_invoice',
                'partner_id': partner.id,
                'invoice_origin': ', '.join(records.mapped('name')),
                'invoice_line_ids': [(0, 0, {
                    'product_id': product.id if product else False,
                    'name': _('LR %(lr)s: %(f)s to %(t)s, %(p)s pkgs, %(w)s kg', lr=rec.name,
                              f=rec.from_location_id.name, t=rec.to_location_id.name,
                              p=rec.total_packages, w=rec.charged_weight_kg),
                    'quantity': 1.0,
                    'price_unit': rec.untaxed_amount,
                    'tax_ids': [(6, 0, rec.tax_ids.ids)],
                }) for rec in records],
            })
            records.write({'invoice_id': move.id, 'state': 'invoiced'})
            moves |= move
        action = self.env['ir.actions.act_window']._for_xml_id('account.action_move_out_invoice_type')
        action['domain'] = [('id', 'in', moves.ids)]
        if len(moves) == 1:
            action.update({'res_id': moves.id, 'views': [(False, 'form')], 'view_mode': 'form'})
        return action


class CpConsignmentLine(models.Model):
    _name = 'cp.consignment.line'
    _description = 'Consignment Goods Line'

    consignment_id = fields.Many2one('cp.consignment', required=True, ondelete='cascade')
    product_id = fields.Many2one('product.product', string='Material')
    description = fields.Char(required=True)
    packing_type = fields.Selection([
        ('box', 'Box / Carton'), ('bag', 'Bag'), ('drum', 'Drum'), ('pallet', 'Pallet'),
        ('loose', 'Loose'), ('roll', 'Roll / Coil'), ('other', 'Other'),
    ], default='box')
    packages = fields.Integer(default=1)
    actual_weight_kg = fields.Float(string='Actual (kg)')
    charged_weight_kg = fields.Float(string='Charged (kg)')

    @api.onchange('actual_weight_kg')
    def _onchange_weight(self):
        for line in self:
            if line.actual_weight_kg and line.charged_weight_kg < line.actual_weight_kg:
                line.charged_weight_kg = line.actual_weight_kg

    @api.onchange('product_id')
    def _onchange_product(self):
        for line in self:
            if line.product_id and not line.description:
                line.description = line.product_id.display_name


class CpConsignmentQuick(models.TransientModel):
    """Smart entry: build a consignment from one typed sentence."""
    _name = 'cp.consignment.quick'
    _description = 'Quick Consignment Entry'

    instruction = fields.Text(
        required=True,
        help='Example: "LR for Acme Steel, 10 tons of steel coils, 12 packages, from Raipur to Chennai, rate 2200 per ton"')
    billing_partner_id = fields.Many2one('res.partner', string='Billing Party')
    consignee_id = fields.Many2one('res.partner')
    from_location_id = fields.Many2one('cp.location', string='From')
    to_location_id = fields.Many2one('cp.location', string='To')
    material = fields.Char()
    packages = fields.Integer(default=1)
    weight_kg = fields.Float(string='Weight (kg)')
    freight_basis = fields.Selection([
        ('fixed', 'Fixed'), ('kg', 'Per Kg'), ('ton', 'Per Tonne'), ('package', 'Per Package'),
    ], default='fixed')
    freight_rate = fields.Float()
    parsed = fields.Boolean()
    parse_notes = fields.Char(readonly=True)

    @staticmethod
    def _cp_find(model, text):
        text = (text or '').strip(' .,;')
        if not text:
            return model.browse()
        return model.search([('name', '=ilike', text)], limit=1) or model.search([('name', 'ilike', text)], limit=1)

    def action_parse(self):
        self.ensure_one()
        text = self.instruction or ''
        Partner, Location = self.env['res.partner'], self.env['cp.location']
        notes = []
        vals = {'parsed': True}
        m = re.search(r'(?:for|party|billing party)\s+([A-Za-z0-9&.\- ]+?)(?:,|$)', text, re.I)
        if m:
            vals['billing_partner_id'] = self._cp_find(Partner, m.group(1)).id or False
            if not vals['billing_partner_id']:
                notes.append(_('Party "%s" not found.', m.group(1).strip()))
        m = re.search(r'from\s+([A-Za-z .\-]+?)\s+to\s+([A-Za-z .\-]+?)(?:,|$|\s+rate)', text, re.I)
        if m:
            vals['from_location_id'] = self._cp_find(Location, m.group(1)).id or False
            vals['to_location_id'] = self._cp_find(Location, m.group(2)).id or False
            if not vals['from_location_id'] or not vals['to_location_id']:
                notes.append(_('A location was not recognised; pick it below.'))
        m = re.search(r'(\d+(?:\.\d+)?)\s*(tons?|tonnes?|t|mt|kgs?|kg)\b(?:\s+of\s+([A-Za-z0-9 \-]+?))?(?:,|$)', text, re.I)
        if m:
            qty, unit = float(m.group(1)), m.group(2).lower()
            vals['weight_kg'] = qty if unit.startswith('kg') else qty * 1000.0
            if m.group(3):
                vals['material'] = m.group(3).strip()
        m = re.search(r'(\d+)\s*(?:packages|pkgs|bags|boxes|cartons|pallets)', text, re.I)
        if m:
            vals['packages'] = int(m.group(1))
        m = re.search(r'rate\s*(?:of\s*)?(\d+(?:\.\d+)?)\s*(?:per\s*(ton|tonne|kg|package|pkg|trip))?', text, re.I)
        if m:
            vals['freight_rate'] = float(m.group(1))
            unit = (m.group(2) or 'trip').lower()
            vals['freight_basis'] = {'ton': 'ton', 'tonne': 'ton', 'kg': 'kg', 'package': 'package',
                                     'pkg': 'package'}.get(unit, 'fixed')
        vals['parse_notes'] = ' '.join(notes) or _('Parsed. Check the fields and create the LR.')
        self.write(vals)
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new'}

    def action_create(self):
        self.ensure_one()
        if not (self.billing_partner_id and self.from_location_id and self.to_location_id):
            raise UserError(_('Billing party, From and To are required.'))
        consignment = self.env['cp.consignment'].create({
            'consignor_id': self.billing_partner_id.id,
            'consignee_id': (self.consignee_id or self.billing_partner_id).id,
            'billing_partner_id': self.billing_partner_id.id,
            'from_location_id': self.from_location_id.id,
            'to_location_id': self.to_location_id.id,
            'freight_basis': self.freight_basis,
            'freight_rate': self.freight_rate,
            'line_ids': [(0, 0, {
                'description': self.material or _('General goods'),
                'packages': self.packages or 1,
                'actual_weight_kg': self.weight_kg,
                'charged_weight_kg': self.weight_kg,
            })],
        })
        return {'type': 'ir.actions.act_window', 'res_model': 'cp.consignment',
                'res_id': consignment.id, 'view_mode': 'form'}
