# -*- coding: utf-8 -*-
import base64
import csv
import io
import re

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..ai import engine
from ..providers.base import Address, Parcel, ShipmentRequest, parse_datetime
from .wizards import STRATEGY_CHOICES


class RateCalculator(models.TransientModel):
    """Quote any lane against every carrier account, without an order (ShipStation "rate calculator")."""
    _name = 'zivo.logistics.rate.calculator'
    _description = 'Shipping Rate Calculator'

    def _default_carrier(self):
        return self.env['delivery.carrier'].search([('delivery_type', '=', 'zivo_hub'),
                                                    ('company_id', 'in', (False, self.env.company.id))], limit=1)

    carrier_id = fields.Many2one('delivery.carrier', string='Delivery Method', domain=[('delivery_type', '=', 'zivo_hub')],
                                 default=_default_carrier, required=True)
    origin_zip = fields.Char(string='From ZIP / PIN', required=True, default=lambda self: self.env.company.zip)
    origin_city = fields.Char(string='From City', default=lambda self: self.env.company.city)
    origin_country_id = fields.Many2one('res.country', string='From Country', required=True,
                                        default=lambda self: self.env.company.country_id or self.env.ref('base.in'))
    dest_zip = fields.Char(string='To ZIP / PIN', required=True)
    dest_city = fields.Char(string='To City')
    dest_country_id = fields.Many2one('res.country', string='To Country', required=True,
                                      default=lambda self: self.env.company.country_id or self.env.ref('base.in'))
    package_type_id = fields.Many2one('stock.package.type', string='Package')
    weight = fields.Float(string='Weight (kg)', default=1.0, required=True, digits=(10, 3))
    length = fields.Float(string='Length (cm)', default=20.0)
    width = fields.Float(string='Width (cm)', default=15.0)
    height = fields.Float(string='Height (cm)', default=10.0)
    payment_mode = fields.Selection([('prepaid', 'Prepaid'), ('cod', 'Cash on Delivery')], default='prepaid', required=True)
    declared_value = fields.Float(string='Goods Value', default=1000.0)
    strategy = fields.Selection(STRATEGY_CHOICES, default='ai_best', required=True)
    deliver_within_days = fields.Integer(string='Deliver Within (days)', help='0 = no delivery promise')
    line_ids = fields.One2many('zivo.logistics.rate.calculator.line', 'wizard_id')
    summary = fields.Html(readonly=True, sanitize=False)
    errors = fields.Text(readonly=True)
    chargeable_weight = fields.Float(readonly=True, digits=(10, 3))

    @api.onchange('package_type_id')
    def _onchange_package_type(self):
        pt = self.package_type_id
        if pt:
            carrier = self.env['delivery.carrier']
            self.length = carrier._zivo_to_cm(pt.packaging_length) or self.length
            self.width = carrier._zivo_to_cm(pt.width) or self.width
            self.height = carrier._zivo_to_cm(pt.height) or self.height

    def _request(self):
        self.ensure_one()
        company = self.env.company.partner_id
        return ShipmentRequest(
            reference='RATE-CALC',
            pickup=Address(name=company.name or '', company=company.name or '', street=company.street or '',
                           city=self.origin_city or '', zip=(self.origin_zip or '').strip(),
                           state_code=company.state_id.code or '', country_code=self.origin_country_id.code or 'IN',
                           phone=company.phone or ''),
            delivery=Address(name='Customer', city=self.dest_city or '', zip=(self.dest_zip or '').strip(),
                             country_code=self.dest_country_id.code or 'IN'),
            parcels=[Parcel(weight_kg=max(self.weight, 0.01), length_cm=self.length or 10.0, width_cm=self.width or 10.0,
                            height_cm=self.height or 10.0, declared_value=self.declared_value)],
            payment_mode=self.payment_mode, cod_amount=self.declared_value if self.payment_mode == 'cod' else 0.0,
            declared_value=self.declared_value, currency=self.env.company.currency_id.name)

    def action_compute(self):
        self.ensure_one()
        request = self._request()
        carrier = self.carrier_id
        ctx = carrier._zivo_ctx(False, self.env.company, value=self.declared_value)
        ctx['strategy'] = self.strategy
        ctx['deliver_within_days'] = self.deliver_within_days or 0
        ranked, errors = carrier.with_context(zivo_all_services=True)._zivo_ranked_options(request, ctx)
        saving = engine.savings(ranked)
        prices = [o['price'] for o in ranked]
        tats = [o['quote'].tat_days for o in ranked if o['quote'].tat_days]
        now = fields.Datetime.now()
        lines = []
        for o in ranked:
            badges = []
            if o['rank'] == 1:
                badges.append(_('AI pick'))
            if prices and o['price'] == min(prices):
                badges.append(_('Cheapest'))
            if tats and o['quote'].tat_days and o['quote'].tat_days == min(tats):
                badges.append(_('Fastest'))
            lines.append((0, 0, {
                'rank': o['rank'], 'account_id': o['account'].id, 'service_code': o['quote'].service_code,
                'service_name': o['quote'].service_name or o['quote'].service_code, 'price': o['price'],
                'tat_days': o['quote'].tat_days,
                'edd': engine.add_business_days(now, o['quote'].tat_days).date() if o['quote'].tat_days else False,
                'ai_score': o['ai_score'], 'savings': max(saving['max_price'] - o['price'], 0.0),
                'on_time': o.get('on_time', True), 'badges': ' / '.join(badges), 'recommended': o['rank'] == 1,
                'reasons': ', '.join(o.get('reasons') or []), 'rate_source': o['quote'].source}))
        self.line_ids.unlink()
        self.write({'line_ids': lines, 'errors': '\n'.join(errors) or False, 'chargeable_weight': request.chargeable_weight,
                    'summary': self._summary(ranked, saving)})
        return self._reopen()

    def _summary(self, ranked, saving):
        if not ranked:
            return Markup('<p><b>%s</b></p>') % _('No carrier can serve this lane. See the unavailable carriers below.')
        symbol = self.env.company.currency_id.symbol or ''
        best = ranked[0]
        cheapest = min(ranked, key=lambda o: o['price'])
        fastest = min(ranked, key=lambda o: o['quote'].tat_days or 99)

        def name(o):
            return '%s - %s' % (o['account'].name, o['quote'].service_name or o['quote'].service_code)
        rows = [
            (_('AI recommendation'), '%s: %s %.2f, %g day(s)' % (name(best), symbol, best['price'], best['quote'].tat_days or 0)),
            (_('Cheapest'), '%s: %s %.2f' % (name(cheapest), symbol, cheapest['price'])),
            (_('Fastest'), '%s: %g day(s), %s %.2f' % (name(fastest), fastest['quote'].tat_days or 0, symbol, fastest['price'])),
            (_('You save'), _('%(sym)s %(saved).2f (%(pct)s%%) vs the highest quote, %(sym)s %(avg).2f vs the average of '
                              '%(n)s quotes', sym=symbol, saved=saving['saved_vs_max'], pct=saving['saved_pct'],
                              avg=saving['saved_vs_avg'], n=saving['options'])),
        ]
        body = Markup('').join(Markup('<tr><td style="padding:4px 12px 4px 0;color:#0b1f4d;"><b>%s</b></td>'
                                      '<td style="padding:4px 0;color:#111827;">%s</td></tr>') % (k, v) for k, v in rows)
        return Markup('<table>%s</table>') % body

    def _reopen(self):
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id, 'view_mode': 'form',
                'target': 'new', 'name': _('Shipping Rate Calculator'), 'context': {'dialog_size': 'extra-large'}}


class RateCalculatorLine(models.TransientModel):
    _name = 'zivo.logistics.rate.calculator.line'
    _description = 'Rate Calculator Quote'
    _order = 'rank'

    wizard_id = fields.Many2one('zivo.logistics.rate.calculator', required=True, ondelete='cascade')
    rank = fields.Integer(string='#')
    account_id = fields.Many2one('zivo.logistics.account', string='Carrier Account')
    service_code = fields.Char()
    service_name = fields.Char(string='Service')
    price = fields.Float(digits='Product Price')
    tat_days = fields.Float(string='Days', digits=(3, 0))
    edd = fields.Date(string='Est. Delivery')
    ai_score = fields.Float(string='AI Score')
    savings = fields.Float(string='Saves', digits='Product Price')
    on_time = fields.Boolean(string='On Time')
    badges = fields.Char(string='Highlights')
    recommended = fields.Boolean()
    reasons = fields.Char(string='Why')
    rate_source = fields.Char(string='Source')


# ---------------------------------------------------------------------- reconciliation

COLUMN_ALIASES = {
    'awb': ('awb', 'awb_no', 'awb_number', 'awbno', 'tracking', 'tracking_no', 'tracking_number', 'tracking_id',
            'waybill', 'waybill_no', 'airwaybill', 'shipment_number', 'consignment_no'),
    'amount': ('charged_amount', 'billed_amount', 'freight', 'freight_amount', 'freight_charges', 'total_charge',
               'total_charges', 'invoice_amount', 'amount', 'total', 'net_amount', 'shipping_charges'),
    'weight': ('charged_weight', 'billed_weight', 'chargeable_weight', 'weight', 'weight_kg'),
    'cod': ('cod_remitted', 'remitted_amount', 'cod_remittance', 'cod_amount', 'cod_paid', 'remittance_amount'),
    'utr': ('utr', 'utr_no', 'utr_number', 'remittance_ref', 'remittance_id', 'crf_id', 'bank_reference', 'reference'),
    'date': ('remittance_date', 'remitted_on', 'payment_date', 'date'),
}


def _money(value):
    text = re.sub(r'[^0-9.\-]', '', str(value or '').replace(',', ''))
    try:
        return float(text) if text not in ('', '-', '.') else 0.0
    except ValueError:
        return 0.0


class ReconcileImport(models.TransientModel):
    """Import a carrier freight invoice and / or COD remittance file (CSV) and audit every AWB."""
    _name = 'zivo.logistics.reconcile'
    _description = 'Freight & COD Reconciliation'

    file = fields.Binary(string='Carrier File (CSV)', required=True)
    filename = fields.Char()
    account_id = fields.Many2one('zivo.logistics.account', string='Carrier Account',
                                 help='Optional: only match AWBs of this account.')
    state = fields.Selection([('draft', 'Draft'), ('done', 'Done')], default='draft')
    result = fields.Html(readonly=True, sanitize=False)
    matched_ids = fields.Many2many('zivo.logistics.shipment', string='Matched Shipments')

    @staticmethod
    def _parse(raw):
        text = raw.decode('utf-8-sig', errors='replace')
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(io.StringIO(text), dialect)
        rows = [r for r in reader if any((c or '').strip() for c in r)]
        if not rows:
            return {}, []
        header = [re.sub(r'[^a-z0-9]+', '_', (h or '').strip().lower()).strip('_') for h in rows[0]]
        mapping = {}
        for key, aliases in COLUMN_ALIASES.items():
            for alias in aliases:
                if alias in header and header.index(alias) not in mapping.values():
                    mapping[key] = header.index(alias)
                    break
        return mapping, rows[1:]

    def action_import(self):
        self.ensure_one()
        mapping, rows = self._parse(base64.b64decode(self.file or b''))
        if 'awb' not in mapping:
            raise UserError(_('The file needs an AWB / tracking number column (e.g. "awb", "tracking_number", "waybill").'))
        if not any(k in mapping for k in ('amount', 'weight', 'cod')):
            raise UserError(_('The file needs at least one of: charged amount, charged weight or COD remitted columns.'))
        Shipment = self.env['zivo.logistics.shipment']
        base_domain = [('company_id', '=', self.env.company.id)]
        if self.account_id:
            base_domain.append(('account_id', '=', self.account_id.id))
        missing, matched = [], Shipment
        for row in rows:
            def cell(key):
                idx = mapping.get(key)
                return row[idx].strip() if idx is not None and idx < len(row) and row[idx] else ''
            awb = cell('awb')
            if not awb:
                continue
            shipment = Shipment.search(base_domain + [('tracking_number', '=', awb)], limit=1)
            if not shipment:
                missing.append(awb)
                continue
            vals = {}
            if cell('amount'):
                vals['billed_amount'] = _money(cell('amount'))
            if cell('weight'):
                vals['charged_weight'] = _money(cell('weight'))
            if cell('cod'):
                vals['cod_remitted_amount'] = _money(cell('cod'))
                vals['cod_remittance_ref'] = cell('utr') or False
                remitted = parse_datetime(cell('date')) if cell('date') else None
                vals['cod_remitted_date'] = remitted.date() if remitted else fields.Date.today()
            if vals:
                shipment.write(vals)
                matched |= shipment
        over = matched.filtered(lambda s: s.reconcile_state == 'overcharged')
        disputes = matched.filtered('weight_dispute')
        short = matched.filtered(lambda s: s.cod_state == 'short')
        symbol = self.env.company.currency_id.symbol or ''
        stats = [
            (_('Rows read'), str(len(rows))), (_('Shipments matched'), str(len(matched))),
            (_('AWBs not found'), str(len(missing))),
            (_('Overcharged shipments'), '%s (%s %.2f extra)' % (len(over), symbol, sum(over.mapped('billing_variance')))),
            (_('Weight disputes'), str(len(disputes))),
            (_('COD short remitted'), '%s (%s %.2f missing)' % (
                len(short), symbol, sum(s.cod_amount - s.cod_remitted_amount for s in short))),
        ]
        html = Markup('').join(Markup('<tr><td style="padding:3px 14px 3px 0;color:#0b1f4d;"><b>%s</b></td>'
                                      '<td style="color:#111827;">%s</td></tr>') % (k, v) for k, v in stats)
        html = Markup('<table>%s</table>') % html
        if missing:
            html += Markup('<p style="color:#b42318;margin-top:8px;">%s %s</p>') % (
                _('Not found:'), ', '.join(missing[:25]) + (' ...' if len(missing) > 25 else ''))
        self.write({'state': 'done', 'result': html, 'matched_ids': [(6, 0, matched.ids)]})
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id, 'view_mode': 'form',
                'target': 'new', 'name': _('Freight & COD Reconciliation')}

    def action_view_issues(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_logistics_hub.action_zivo_shipment')
        action.update({'domain': [('id', 'in', self.matched_ids.ids), '|', '|',
                                  ('reconcile_state', 'in', ('overcharged', 'undercharged')), ('weight_dispute', '=', True),
                                  ('cod_state', '=', 'short')],
                       'name': _('Reconciliation Issues'), 'context': {},
                       'views': [(self.env.ref('zivoit_logistics_hub.view_zivo_shipment_audit_list').id, 'list'), (False, 'form')]})
        return action


# ---------------------------------------------------------------------- scan to ship

class ScanShip(models.TransientModel):
    """Scan a delivery order / sales order / AWB barcode: validate, book the courier and get the label."""
    _name = 'zivo.logistics.scan.ship'
    _description = 'Scan to Ship'

    barcode = fields.Char(string='Scan Barcode')
    carrier_id = fields.Many2one('delivery.carrier', string='Delivery Method', domain=[('delivery_type', '=', 'zivo_hub')],
                                 help='Used when the transfer has no ZivoIT Logistics Hub delivery method.')
    validate = fields.Boolean(string='Validate Transfer', default=True)
    log_html = fields.Html(string='Scanned', readonly=True, sanitize=False)
    last_label_id = fields.Many2one('ir.attachment', readonly=True)

    def _find(self, code):
        Picking = self.env['stock.picking']
        open_out = [('picking_type_code', '=', 'outgoing'), ('state', 'not in', ('cancel',))]
        picking = Picking.search(open_out + [('name', '=', code)], limit=1)
        if not picking:
            picking = Picking.search(open_out + [('origin', '=', code), ('state', '!=', 'done')], limit=1) or \
                Picking.search(open_out + [('origin', '=', code)], limit=1, order='id desc')
        if not picking:
            shipment = self.env['zivo.logistics.shipment'].search([('tracking_number', '=', code)], limit=1)
            picking = shipment.picking_id
        return picking

    def _ship(self, code):
        picking = self._find(code)
        if not picking:
            return False, _('%s: no delivery order, sales order or AWB found.', code), False
        if self.carrier_id and picking.carrier_id.delivery_type != 'zivo_hub':
            picking.carrier_id = self.carrier_id
        if picking.carrier_id.delivery_type != 'zivo_hub':
            return False, _('%s: set a ZivoIT Logistics Hub delivery method.', picking.name), False
        if picking.state != 'done':
            if not self.validate:
                return False, _('%s: transfer is not validated yet.', picking.name), False
            for move in picking.move_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
                move.quantity = move.product_uom_qty
            picking.move_ids.picked = True
            res = picking.with_context(skip_backorder=True, skip_sms=True, cancel_backorder=False).button_validate()
            if isinstance(res, dict) and picking.state != 'done':
                return False, _('%s needs a manual validation (%s).', picking.name, res.get('name') or res.get('res_model')), False
        if not picking.zivo_shipment_id or picking.zivo_shipment_id.state == 'cancelled':
            picking.send_to_shipper()
        shipment = picking.zivo_shipment_id
        if not shipment:
            return False, _('%s: the courier could not be booked.', picking.name), False
        if not shipment.label_attachment_id:
            try:
                shipment._fetch_label()
            except Exception:  # noqa: BLE001 - label can be printed later
                pass
        return True, _('%(pick)s shipped with %(carrier)s - AWB %(awb)s', pick=picking.name, carrier=shipment.account_id.name,
                       awb=shipment.tracking_number), shipment.label_attachment_id

    def action_scan(self):
        self.ensure_one()
        code = (self.barcode or '').strip()
        if not code:
            raise UserError(_('Scan or type a delivery order, sales order or AWB number.'))
        try:
            with self.env.cr.savepoint():
                ok, message, label = self._ship(code)
        except UserError as exc:
            ok, message, label = False, '%s: %s' % (code, str(exc).splitlines()[0]), False
        color = '#0f7a3a' if ok else '#b42318'
        row = Markup('<div style="padding:4px 0;border-bottom:1px solid #cbd5e1;color:%s;"><b>%s</b> %s%s</div>') % (
            color, 'OK' if ok else 'ERROR', message,
            Markup(' - <a href="/web/content/%s?download=true">%s</a>') % (label.id, _('Download label')) if label else '')
        self.write({'log_html': row + (self.log_html or Markup('')), 'barcode': False,
                    'last_label_id': label.id if label else self.last_label_id.id})
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id, 'view_mode': 'form',
                'target': 'new', 'name': _('Scan to Ship')}


class ManifestWizard(models.AbstractModel):
    _name = 'zivo.logistics.manifest.tools'
    _description = 'Manifest helpers'

    @api.model
    def action_create_end_of_day(self, shipment_ids=None):
        shipments = self.env['zivo.logistics.shipment'].browse(shipment_ids) if shipment_ids else None
        manifests = self.env['zivo.logistics.manifest']._create_end_of_day(shipments)
        if not manifests:
            return {'type': 'ir.actions.client', 'tag': 'display_notification',
                    'params': {'title': _('End-of-day manifest'), 'type': 'warning',
                               'message': _('No booked shipment is waiting for a manifest.')}}
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_logistics_hub.action_zivo_manifest')
        action['domain'] = [('id', 'in', manifests.ids)]
        return action
