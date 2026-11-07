# -*- coding: utf-8 -*-
import json

from odoo import api, fields, models, _
from odoo.exceptions import UserError

LENGTH_TO_CM = {'mm': 0.1, 'cm': 1.0, 'm': 100.0, 'in': 2.54, 'ft': 30.48}


class DeliveryCarrier(models.Model):
    _inherit = 'delivery.carrier'

    delivery_type = fields.Selection(selection_add=[('cp_courier', 'Cargo Pilot courier')],
                                     ondelete={'cp_courier': lambda recs: recs.write({'delivery_type': 'fixed',
                                                                                      'fixed_price': 0})})
    cp_account_id = fields.Many2one('cp.courier.account', string='Courier Account', ondelete='restrict')
    cp_provider = fields.Selection(related='cp_account_id.provider', string='Courier')
    cp_choice = fields.Selection([('best', 'Best value'), ('cheapest', 'Cheapest'), ('fastest', 'Fastest')],
                                 string='Pick', default='best',
                                 help='Which option to use when the account returns several couriers or services '
                                      '(Shiprocket, Xpressbees, Delhivery Surface or Express).')
    cp_cod_policy = fields.Selection([('auto', 'When the customer chose cash on delivery'),
                                      ('always', 'Always cash on delivery'), ('never', 'Always prepaid')],
                                     string='COD', default='auto')
    cp_default_weight = fields.Float(string='Weight if unknown (kg)', default=0.5)
    cp_default_dims = fields.Char(string='Box if unknown (cm)', default='30x20x10',
                                  help='Length x width x height used when no package type is set.')

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _cp_dims(self):
        try:
            l, w, h = [float(x) for x in (self.cp_default_dims or '30x20x10').lower().split('x')]
        except ValueError:
            l, w, h = 30.0, 20.0, 10.0
        return l, w, h

    @staticmethod
    def _cp_to_kg(env, weight):
        uom = env['product.template']._get_weight_uom_id_from_ir_config_parameter()
        kg = env.ref('uom.product_uom_kgm')
        return uom._compute_quantity(weight, kg) if uom and uom != kg else weight

    def _cp_is_cod(self, order):
        if self.cp_cod_policy == 'always':
            return True
        if self.cp_cod_policy == 'never' or not order:
            return False
        txs = order.sudo().transaction_ids
        return any(getattr(tx.provider_id, 'custom_mode', '') == 'cash_on_delivery'
                   or tx.payment_method_code == 'cash_on_delivery' for tx in txs)

    def _cp_pick(self, options):
        good = [o for o in options if not o.get('error') and (o.get('price') or 0) > 0]
        if not good:
            errors = '; '.join(o.get('error') for o in options if o.get('error'))
            raise UserError(errors or _('%s returned no price for this address.', self.cp_account_id.name))
        if self.cp_choice == 'cheapest':
            return min(good, key=lambda o: o['price'])
        if self.cp_choice == 'fastest':
            return min(good, key=lambda o: (o.get('days') or 99, o['price']))
        cheapest = min(o['price'] for o in good)
        priciest = max(o['price'] for o in good) or 1.0
        days = [o.get('days') for o in good if o.get('days')]
        fastest, slowest = (min(days), max(days)) if days else (0, 1)

        def score(o):
            p = 1 - (o['price'] - cheapest) / max(priciest - cheapest, 1.0)
            d = (1 - (o['days'] - fastest) / max(slowest - fastest, 1)) if o.get('days') else 0.4
            r = (o.get('rating') or 3.0) / 5.0
            return p * 0.55 + d * 0.3 + r * 0.15
        return max(good, key=score)

    def _cp_parcel_vals_from_order(self, order):
        partner = order.partner_shipping_id
        sender = order.warehouse_id.partner_id or order.company_id.partner_id
        cod = self._cp_is_cod(order)
        weight = self._cp_to_kg(self.env, order._get_estimated_weight()) or self.cp_default_weight
        l, w, h = self._cp_dims()
        amount = order._compute_amount_total_without_delivery()
        return {
            'account_id': self.cp_account_id.id, 'sender_partner_id': sender.id, 'recipient_partner_id': partner.id,
            'reference': order.name, 'declared_value': amount, 'currency_id': order.company_id.currency_id.id,
            'payment_mode': 'cod' if cod else 'prepaid', 'cod_amount': order.amount_total if cod else 0.0,
            'contents': (order.order_line.filtered(lambda x: not x.is_delivery and not x.display_type)[:1].name
                         or _('Goods'))[:60],
            'company_id': order.company_id.id,
            'package_ids': [(0, 0, {'quantity': 1, 'weight_kg': weight, 'length_cm': l, 'width_cm': w,
                                    'height_cm': h})],
        }

    def _cp_parcel_vals_from_picking(self, picking):
        order = picking.sale_id
        sender = picking.picking_type_id.warehouse_id.partner_id or picking.company_id.partner_id
        cod = self._cp_is_cod(order)
        length_uom = self.env['product.template']._get_length_uom_name_from_ir_config_parameter()
        factor = LENGTH_TO_CM.get((length_uom or 'cm').lower(), 1.0)
        boxes = []
        packages = picking.move_line_ids.result_package_id
        for pkg in packages:
            ptype = pkg.package_type_id
            weight = self._cp_to_kg(self.env, pkg.shipping_weight or pkg.with_context(picking_id=picking.id).weight)
            l, w, h = self._cp_dims()
            if ptype and ptype.packaging_length and ptype.width and ptype.height:
                l, w, h = ptype.packaging_length * factor, ptype.width * factor, ptype.height * factor
            boxes.append((0, 0, {'name': pkg.name, 'quantity': 1, 'weight_kg': round(weight or self.cp_default_weight, 3),
                                 'length_cm': l, 'width_cm': w, 'height_cm': h}))
        loose = picking.move_line_ids.filtered(lambda ml: not ml.result_package_id)
        if loose or not boxes:
            weight = sum(ml.quantity * ml.product_id.weight for ml in loose) if packages else \
                (getattr(picking, 'shipping_weight', 0) or picking.weight)
            l, w, h = self._cp_dims()
            boxes.append((0, 0, {'name': _('Loose items') if packages else _('Box'), 'quantity': 1,
                                 'weight_kg': round(self._cp_to_kg(self.env, weight) or self.cp_default_weight, 3),
                                 'length_cm': l, 'width_cm': w, 'height_cm': h}))
        value = sum(m.product_id.lst_price * m.quantity for m in picking.move_ids) if not order else \
            order._compute_amount_total_without_delivery()
        return {
            'account_id': self.cp_account_id.id, 'sender_partner_id': sender.id,
            'recipient_partner_id': picking.partner_id.id, 'reference': order.name if order else picking.name,
            'picking_id': picking.id, 'sale_order_id': order.id if order else False,
            'declared_value': value, 'payment_mode': 'cod' if cod else 'prepaid',
            'cod_amount': order.amount_total if cod and order else 0.0,
            'contents': (picking.move_ids[:1].product_id.name or _('Goods'))[:60],
            'invoice_number': order.invoice_ids[:1].name if order and order.invoice_ids else False,
            'company_id': picking.company_id.id, 'package_ids': boxes,
        }

    # ------------------------------------------------------------------
    # Odoo shipping API
    # ------------------------------------------------------------------
    def cp_courier_rate_shipment(self, order):
        self.ensure_one()
        if not self.cp_account_id:
            return {'success': False, 'price': 0.0, 'error_message': _('Choose a courier account on %s.', self.name),
                    'warning_message': False}
        parcel = self.env['cp.parcel'].new(self._cp_parcel_vals_from_order(order))
        try:
            option = self._cp_pick(self.cp_account_id._cp_provider().rates(parcel))
        except UserError as err:
            return {'success': False, 'price': 0.0, 'error_message': str(err.args[0] if err.args else err),
                    'warning_message': False}
        price = order.company_id.currency_id._convert(option['price'], order.currency_id, order.company_id,
                                                      order.date_order or fields.Date.today())
        warning = False
        if option.get('days'):
            warning = _('%(courier)s, about %(days)s day(s)', courier=option.get('courier_name'), days=option['days'])
        return {'success': True, 'price': price, 'error_message': False, 'warning_message': warning}

    def cp_courier_send_shipping(self, pickings):
        self.ensure_one()
        result = []
        Parcel = self.env['cp.parcel']
        for picking in pickings:
            parcel = Parcel.search([('picking_id', '=', picking.id), ('state', '=', 'draft')], limit=1)
            if not parcel:
                parcel = Parcel.create(self._cp_parcel_vals_from_picking(picking))
            provider = self.cp_account_id._cp_provider()
            option = None
            if self.cp_account_id.can_rate:
                option = self._cp_pick(provider.rates(parcel))
            parcel.action_book(option)
            if parcel.label_attachment_id:
                picking.message_post(body=_('Courier label for %s', parcel.awb),
                                     attachment_ids=parcel.label_attachment_id.copy(
                                         {'res_model': 'stock.picking', 'res_id': picking.id}).ids)
            else:
                pdf, _fmt = self.env['ir.actions.report'].with_context(discard_logo_check=True)._render_qweb_pdf(
                    'cargo_pilot_tms.action_report_parcel_label', parcel.ids)
                picking.message_post(body=_('Shipping label for %s', parcel.awb),
                                     attachments=[('%s-%s.pdf' % (self._get_delivery_label_prefix(), parcel.awb), pdf)])
            result.append({'exact_price': parcel.shipping_cost, 'tracking_number': parcel.awb})
        return result

    def cp_courier_get_tracking_link(self, picking):
        parcel = self.env['cp.parcel'].search([('picking_id', '=', picking.id), ('state', '!=', 'cancelled')],
                                              order='id desc', limit=1)
        return parcel.public_url if parcel else False

    def cp_courier_cancel_shipment(self, pickings):
        parcels = self.env['cp.parcel'].search([('picking_id', 'in', pickings.ids),
                                                ('state', 'not in', ('cancelled', 'draft'))])
        parcels.action_cancel()
        return True
