# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .cp_common import CHANNELS, SHIPMENT_MODES


class CpBooking(models.TransientModel):
    """Book a load in three steps: describe it, compare options, pick one."""
    _name = 'cp.booking'
    _description = 'Book a Load'

    step = fields.Selection([('details', 'Details'), ('options', 'Options')], default='details')
    shipment_mode = fields.Selection(SHIPMENT_MODES, string='How much', default='ftl', required=True)
    channel = fields.Selection(CHANNELS)
    origin_id = fields.Many2one('cp.location', string='From', required=True)
    destination_id = fields.Many2one('cp.location', string='To', required=True)
    vehicle_type_id = fields.Many2one('cp.vehicle.type', string='Truck')
    weight_kg = fields.Float(string='Weight (kg)')
    boxes = fields.Integer(string='Boxes')
    goods_value = fields.Monetary(string='Goods value')
    order_lines = fields.Text(string='Orders on this load',
                              help='One order per line: order no, invoice no, boxes, kg, value. Example:\n'
                                   'SO123, INV-77, 10, 240, 52000')
    vehicle_count = fields.Integer(string='How many trucks', default=1, required=True)
    required_date = fields.Datetime(string='Pickup', required=True,
                                    default=lambda self: fields.Datetime.now() + timedelta(hours=24))
    weight_ton = fields.Float(string='Total weight (t)')
    material = fields.Char(string='What is moving', default=lambda self: _('General cargo'))
    consignee_id = fields.Many2one('res.partner', string='Deliver to')
    reference = fields.Char(string='Your reference', help='PO, sales order or delivery number.')
    option_ids = fields.One2many('cp.booking.option', 'booking_id')
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)

    def _cp_lane(self, create=False):
        self.ensure_one()
        lane = self.env['cp.lane'].search([('origin_id', '=', self.origin_id.id),
                                           ('destination_id', '=', self.destination_id.id)], limit=1)
        if not lane and create:
            lane = self.env['cp.lane'].create({'origin_id': self.origin_id.id,
                                               'destination_id': self.destination_id.id})
            lane._onchange_points()
        return lane

    def _cp_parsed_orders(self):
        """Orders typed as 'order, invoice, boxes, kg, value' one per line."""
        self.ensure_one()
        orders = []
        for raw in (self.order_lines or '').splitlines():
            parts = [p.strip() for p in raw.split(',')]
            if not parts or not parts[0]:
                continue

            def num(i, cast=float):
                try:
                    return cast(parts[i]) if len(parts) > i and parts[i] else cast(0)
                except ValueError:
                    return cast(0)
            orders.append({'order_ref': parts[0], 'invoice_ref': parts[1] if len(parts) > 1 else False,
                           'quantity': num(2), 'weight_kg': num(3), 'order_value': num(4)})
        return orders

    def _cp_totals(self):
        orders = self._cp_parsed_orders()
        weight_kg = self.weight_kg or sum(o['weight_kg'] for o in orders)
        if not weight_kg and self.weight_ton:
            weight_kg = self.weight_ton * 1000.0
        boxes = self.boxes or int(sum(o['quantity'] for o in orders))
        value = self.goods_value or sum(o['order_value'] for o in orders)
        return orders, weight_kg, boxes, value

    def action_compare(self):
        self.ensure_one()
        if self.origin_id == self.destination_id:
            raise UserError(_('Pick different pickup and delivery locations.'))
        if self.shipment_mode == 'ltl':
            return self._cp_compare_ltl()
        if not self.vehicle_type_id:
            raise UserError(_('Choose the truck type.'))
        if self.vehicle_count < 1:
            raise UserError(_('Book at least one truck.'))
        lane = self._cp_lane(create=True)
        per_truck_t = (self.weight_ton / self.vehicle_count) if self.vehicle_count else 0.0
        factor = self.vehicle_type_id.emission_factor or 80.0
        co2 = per_truck_t * (lane.distance_km or 0.0) * factor / 1000.0 * self.vehicle_count
        today = fields.Date.to_date(self.required_date)
        lines = self._cp_allowed(self.env['cp.rate.contract'].cp_find_rates(lane, self.vehicle_type_id, today,
                                                                             mode='ftl'))
        options = []
        _orders, _kg, boxes, value = self._cp_totals()
        for line in lines:
            price = line.cp_compute_amount(weight_ton=per_truck_t, goods_value=value / self.vehicle_count,
                                           boxes=int(boxes / self.vehicle_count), on_date=today,
                                           oda=bool(self.destination_id.is_oda))
            options.append({'kind': 'contract', 'contract_line_id': line.id,
                            'transporter_id': line.contract_id.transporter_id.id, 'price': price,
                            'performance': line.contract_id.transporter_id.cp_score,
                            'transit_hours': lane.transit_hours, 'co2_kg': co2})
        # Market estimate from recent awarded auctions on this lane
        recent = self.env['cp.auction'].search([('lane_id', '=', lane.id), ('state', '=', 'awarded'),
                                                ('date_end', '>=', fields.Datetime.now() - timedelta(days=90))])
        trips = recent.mapped('trip_ids').filtered(lambda t: t.state != 'cancel')
        market = (sum(trips.mapped('agreed_freight')) / len(trips)) if trips else 0.0
        if not market and options:
            market = min(o['price'] for o in options)
        options.append({'kind': 'bidding', 'price': market, 'transit_hours': lane.transit_hours,
                        'co2_kg': co2, 'performance': 0.0})
        # Recommend: cheapest price with a performance tie-breaker
        priced = [o for o in options if o['kind'] == 'contract']
        if priced:
            low = min(o['price'] for o in priced) or 1.0
            best = max(priced, key=lambda o: 0.7 * (low / (o['price'] or low) * 100)
                       + 0.3 * (o['performance'] or 50.0))
            best['recommended'] = True
        else:
            options[-1]['recommended'] = True
        self.option_ids.unlink()
        self.write({'step': 'options', 'option_ids': [(0, 0, o) for o in options]})
        return self._cp_reopen()

    def _cp_allowed(self, lines):
        rule = self.env['cp.channel.rule'].cp_match(self.channel, self.consignee_id)
        if rule and rule.allowed_transporter_ids:
            lines = lines.filtered(lambda l: l.contract_id.transporter_id in rule.allowed_transporter_ids)
        return lines

    def _cp_compare_ltl(self):
        orders, weight_kg, boxes, value = self._cp_totals()
        if not weight_kg and not boxes:
            raise UserError(_('Enter the weight or the number of boxes for the part load.'))
        lane = self._cp_lane(create=True)
        today = fields.Date.to_date(self.required_date)
        lines = self._cp_allowed(self.env['cp.rate.contract'].cp_find_rates(lane, on_date=today, mode='ltl'))
        options = []
        for line in lines:
            breakdown = line.cp_price_breakdown(weight_ton=weight_kg / 1000.0, goods_value=value, boxes=boxes,
                                                on_date=today, oda=bool(self.destination_id.is_oda))
            transit = (line.transit_days or 0.0) * 24.0 or lane.transit_hours * 1.5
            options.append({'kind': 'ltl', 'contract_line_id': line.id,
                            'transporter_id': line.contract_id.transporter_id.id, 'price': breakdown['total'],
                            'performance': line.contract_id.transporter_id.cp_score, 'transit_hours': transit,
                            'co2_kg': weight_kg / 1000.0 * (lane.distance_km or 0.0) * 60.0 / 1000.0,
                            'breakdown': '\n'.join('%s: %.2f' % (lbl, amt) for lbl, amt in breakdown['lines'])})
        if not options:
            raise UserError(_('No LTL rate card covers %s yet. Add per-kg or per-box rates on a rate contract, '
                              'or book this load as a full truck.', lane.name))
        low = min(o['price'] for o in options) or 1.0
        best = max(options, key=lambda o: 0.7 * (low / (o['price'] or low) * 100) + 0.3 * (o['performance'] or 50.0))
        best['recommended'] = True
        self.option_ids.unlink()
        self.write({'step': 'options', 'option_ids': [(0, 0, o) for o in options]})
        return self._cp_reopen()

    def action_back(self):
        self.write({'step': 'details'})
        return self._cp_reopen()

    def _cp_reopen(self):
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new', 'name': _('Book a Load')}

    def _cp_create_indent(self):
        lane = self._cp_lane(create=True)
        orders, weight_kg, boxes, value = self._cp_totals()
        if orders:
            lines = [(0, 0, {'description': self.material or _('General cargo'), 'order_ref': o['order_ref'],
                             'invoice_ref': o['invoice_ref'], 'quantity': o['quantity'] or 1,
                             'uom_label': _('Boxes'), 'weight_ton': o['weight_kg'] / 1000.0,
                             'order_value': o['order_value'], 'customer_id': self.consignee_id.id})
                     for o in orders]
        else:
            weight_t = weight_kg / 1000.0 if self.shipment_mode == 'ltl' else (self.weight_ton or weight_kg / 1000.0)
            lines = [(0, 0, {'description': self.material or _('General cargo'), 'order_ref': self.reference,
                             'quantity': boxes or 1, 'uom_label': _('Boxes') if boxes else _('Lot'),
                             'weight_ton': weight_t, 'order_value': value, 'customer_id': self.consignee_id.id})]
        indent = self.env['cp.indent'].create({
            'lane_id': lane.id,
            'shipment_mode': self.shipment_mode,
            'channel': self.channel,
            'vehicle_type_id': self.vehicle_type_id.id,
            'vehicle_count': 1 if self.shipment_mode == 'ltl' else self.vehicle_count,
            'required_date': self.required_date,
            'consignee_id': self.consignee_id.id,
            'origin': self.reference,
            'source_channel': 'booking',
            'line_ids': lines,
        })
        indent.action_confirm()
        return indent


class CpBookingOption(models.TransientModel):
    _name = 'cp.booking.option'
    _description = 'Booking Option'
    _order = 'recommended desc, kind, price'

    booking_id = fields.Many2one('cp.booking', required=True, ondelete='cascade')
    kind = fields.Selection([('contract', 'Contract rate'), ('ltl', 'LTL rate card'), ('bidding', 'Open for bidding')],
                            required=True)
    breakdown = fields.Text(string='Price Breakdown')
    transporter_id = fields.Many2one('res.partner')
    contract_line_id = fields.Many2one('cp.rate.contract.line')
    price = fields.Monetary(string='Price')
    performance = fields.Float(string='On-time score')
    transit_hours = fields.Float(string='Transit (h)')
    co2_kg = fields.Float(string='CO2e (kg)', digits=(12, 0))
    recommended = fields.Boolean()
    currency_id = fields.Many2one(related='booking_id.currency_id')

    def action_choose(self):
        self.ensure_one()
        booking = self.booking_id
        indent = booking._cp_create_indent()
        if self.kind in ('contract', 'ltl'):
            indent.cp_allocate_to(self.contract_line_id)
            return indent.action_view_trips()
        return indent.action_create_auction()
