# -*- coding: utf-8 -*-
import base64
import io
import json
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .india_gst_utils import is_valid_gstin, partner_state_code

_logger = logging.getLogger(__name__)


def _qr_png_b64(value, report_model):
    """PNG QR code (base64). Uses the ``qrcode`` library shipped with Odoo, with reportlab as fallback."""
    try:
        import qrcode
        img = qrcode.make(value, box_size=6, border=2)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return base64.b64encode(buf.getvalue())
    except Exception:  # noqa: BLE001
        try:
            return base64.b64encode(report_model.barcode('QR', value, width=180, height=180))
        except Exception:  # noqa: BLE001 - never break the challan for a missing imaging backend
            _logger.warning("Job Work challan: QR code could not be generated", exc_info=True)
            return False


TRANSPORT_MODES = [('1', 'Road'), ('2', 'Rail'), ('3', 'Air'), ('4', 'Ship')]


class ZivoJobWorkChallan(models.Model):
    _name = 'zivo.job.work.challan'
    _description = 'Job Work Delivery Challan'
    _inherit = ['mail.thread']
    _order = 'date desc, id desc'
    _check_company_auto = True

    name = fields.Char('Challan No.', required=True, readonly=True, copy=False, default=lambda self: _('New'),
                       index='trigram')
    date = fields.Date('Challan Date', required=True, default=fields.Date.context_today, tracking=True)
    challan_type = fields.Selection([('out', 'Sent to Job Worker')], default='out', required=True)
    state = fields.Selection([('draft', 'Draft'), ('confirmed', 'Confirmed'), ('cancel', 'Cancelled')],
                             default='draft', required=True, tracking=True, index=True)
    job_id = fields.Many2one('zivo.job.work', 'Job Work', required=True, index=True, check_company=True,
                             ondelete='restrict')
    picking_id = fields.Many2one('stock.picking', 'Dispatch Transfer', check_company=True, index='btree_not_null',
                                 groups='stock.group_stock_user')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one(related='company_id.currency_id')
    partner_id = fields.Many2one('res.partner', 'Job Worker', required=True, check_company=True)
    principal_gstin = fields.Char('Principal GSTIN', compute='_compute_gstin', store=True, readonly=False)
    job_worker_gstin = fields.Char('Job Worker GSTIN', compute='_compute_gstin', store=True, readonly=False)
    from_state_code = fields.Integer('From State Code', compute='_compute_gstin', store=True, readonly=False)
    to_state_code = fields.Integer('To State Code', compute='_compute_gstin', store=True, readonly=False)
    nature_of_processing = fields.Char('Nature of Processing', compute='_compute_nature', store=True,
                                       readonly=False)
    source_address = fields.Text('Dispatch From', compute='_compute_addresses', store=True, readonly=False)
    destination_address = fields.Text('Ship To', compute='_compute_addresses', store=True, readonly=False)
    transport_mode = fields.Selection(TRANSPORT_MODES, 'Transport Mode', default='1')
    vehicle_no = fields.Char('Vehicle No.')
    vehicle_type = fields.Selection([('R', 'Regular'), ('O', 'Over Dimensional Cargo')], default='R')
    transporter_name = fields.Char('Transporter')
    transporter_id = fields.Char('Transporter ID / GSTIN')
    transport_doc_no = fields.Char('Transport Doc. No.')
    transport_doc_date = fields.Date('Transport Doc. Date')
    distance_km = fields.Integer('Approx. Distance (km)')
    ewaybill_no = fields.Char('E-Way Bill No.', tracking=True, copy=False)
    ewaybill_date = fields.Datetime('E-Way Bill Date', copy=False)
    ewaybill_valid_until = fields.Datetime('E-Way Bill Valid Until', copy=False)
    line_ids = fields.One2many('zivo.job.work.challan.line', 'challan_id', 'Lines', copy=True)
    amount_total = fields.Monetary('Total Value', compute='_compute_amount', store=True)
    qr_value = fields.Char('QR Content', compute='_compute_qr')
    qr_image = fields.Binary('QR Code', compute='_compute_qr')
    note = fields.Text('Notes')
    is_interstate = fields.Boolean(compute='_compute_gstin', store=True)

    @api.depends('company_id', 'partner_id')
    def _compute_gstin(self):
        for ch in self:
            ch.principal_gstin = ch.company_id.vat or ''
            ch.job_worker_gstin = ch.partner_id.commercial_partner_id.vat or ''
            ch.from_state_code = partner_state_code(ch.company_id.partner_id)
            ch.to_state_code = partner_state_code(ch.partner_id.commercial_partner_id)
            ch.is_interstate = bool(ch.from_state_code and ch.to_state_code and
                                    ch.from_state_code != ch.to_state_code)

    @api.depends('job_id.job_type_id')
    def _compute_nature(self):
        for ch in self:
            ch.nature_of_processing = ch.job_id.job_type_id.name or ''

    @api.depends('picking_id', 'partner_id', 'job_id')
    def _compute_addresses(self):
        for ch in self:
            src = ch.job_id.warehouse_id.partner_id or ch.company_id.partner_id
            ch.source_address = src._display_address() if src else ''
            ch.destination_address = ch.partner_id._display_address() if ch.partner_id else ''

    @api.depends('line_ids.value')
    def _compute_amount(self):
        for ch in self:
            ch.amount_total = sum(ch.line_ids.mapped('value'))

    @api.depends('name', 'date', 'principal_gstin', 'job_worker_gstin', 'amount_total', 'job_id.name')
    def _compute_qr(self):
        Report = self.env['ir.actions.report']
        for ch in self:
            value = '|'.join(str(x or '') for x in (
                'JWC', ch.name, ch.date and ch.date.strftime('%d/%m/%Y'), ch.principal_gstin,
                ch.job_worker_gstin, '%.2f' % ch.amount_total, ch.job_id.name, ch.ewaybill_no))
            ch.qr_value = value
            ch.qr_image = _qr_png_b64(value, Report)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                company = self.env['res.company'].browse(vals.get('company_id')) or self.env.company
                vals['name'] = self.env['ir.sequence'].with_company(company).next_by_code(
                    'zivo.job.work.challan') or _('New')
        return super().create(vals_list)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_confirmed(self):
        if any(ch.state == 'confirmed' for ch in self):
            raise UserError(_("Confirmed challans cannot be deleted; cancel them instead."))

    # ------------------------------------------------------------------
    @api.model
    def _jw_in_load_demo(self):
        """Demo: challans for job work dispatches done before the pack was installed."""
        try:
            hsn = {'SB-100-M': '87089900', 'SB-100-H': '87089900', 'BR-25': '74072110'}
            products = self.env['product.product'].search([('default_code', 'in', list(hsn))])
            for product in products:
                product.product_tmpl_id.jw_hsn_code = hsn[product.default_code]
            pickings = self.env['stock.picking'].search([('jw_order_id', '!=', False), ('state', '=', 'done')])
            for picking in pickings.filtered(lambda p: p.jw_is_issue and not p.jw_challan_ids):
                challan = self._jw_create_from_picking(picking, confirm=True)
                challan.write({'vehicle_no': 'MH12AB%04d' % challan.id, 'transporter_name': 'Speedline Logistics',
                               'distance_km': 150})
        except Exception:  # noqa: BLE001 - demo data must never block the installation
            _logger.exception("Job Work 360 India: demo challans could not be generated")

    @api.model
    def _jw_line_vals_from_picking(self, picking):
        lines = []
        company = picking.company_id
        for move in picking.move_ids.filtered(lambda m: m.jw_move_type == 'issue' and m.state != 'cancel'):
            mat = move.jw_material_id
            qty = move.quantity if move.state == 'done' else move.product_uom_qty
            if mat:
                qty_mat = move.product_uom._compute_quantity(qty, mat.product_uom_id, round=False)
                unit_cost = mat.unit_cost
                uom = mat.product_uom_id
                qty = qty_mat
            else:
                unit_cost = move.product_id.with_company(company).standard_price
                uom = move.product_uom
            if not qty:
                continue
            lines.append((0, 0, {
                'product_id': move.product_id.id,
                'name': move.product_id.display_name,
                'move_id': move.id,
                'qty': qty,
                'product_uom_id': uom.id,
                'unit_value': unit_cost,
                'gst_rate': self._jw_product_gst_rate(move.product_id, company),
            }))
        return lines

    @api.model
    def _jw_product_gst_rate(self, product, company):
        taxes = product.supplier_taxes_id.filtered(lambda t: t.company_id == company and t.amount_type == 'percent')
        if not taxes:
            taxes = product.supplier_taxes_id.filtered(lambda t: t.company_id == company)
        rate = 0.0
        for tax in taxes:
            if tax.amount_type == 'group':
                rate += sum(tax.children_tax_ids.filtered(lambda t: t.amount_type == 'percent').mapped('amount'))
            elif tax.amount_type == 'percent':
                rate += tax.amount
        return rate or company.jw_in_default_gst_rate

    @api.model
    def _jw_create_from_picking(self, picking, confirm=False):
        job = picking.jw_order_id
        lines = self._jw_line_vals_from_picking(picking)
        if not job or not lines:
            return self.browse()
        challan = self.create({
            'job_id': job.id,
            'picking_id': picking.id,
            'company_id': picking.company_id.id,
            'partner_id': job.partner_id.id,
            'date': fields.Date.context_today(self),
            'line_ids': lines,
        })
        if confirm:
            challan.state = 'confirmed'
        job.message_post(body=_("Job work challan %s created.", challan._get_html_link()))
        return challan

    def _jw_sync_from_picking(self):
        """Refresh quantities from the validated dispatch (partial dispatches, backorders)."""
        for ch in self.filtered(lambda c: c.state == 'draft' and c.picking_id):
            ch.line_ids.unlink()
            ch.write({'line_ids': self._jw_line_vals_from_picking(ch.picking_id), 'state': 'confirmed'})

    def action_confirm(self):
        for ch in self:
            if not ch.line_ids:
                raise UserError(_("Add at least one line to the challan."))
        self.filtered(lambda c: c.state == 'draft').write({'state': 'confirmed'})
        return True

    def action_cancel(self):
        self.write({'state': 'cancel'})
        return True

    def action_draft(self):
        self.filtered(lambda c: c.state == 'cancel').write({'state': 'draft'})
        return True

    def action_print(self):
        return self.env.ref('zivo_job_work_360.action_report_job_work_challan').report_action(self)

    # ------------------------------------------------------------------
    # E-Way Bill data
    # ------------------------------------------------------------------
    def _jw_ewaybill_errors(self):
        self.ensure_one()
        errors = []
        company_partner = self.company_id.partner_id
        worker = self.partner_id.commercial_partner_id
        if not is_valid_gstin(self.principal_gstin):
            errors.append(_("Principal GSTIN is missing or invalid."))
        if self.job_worker_gstin and not is_valid_gstin(self.job_worker_gstin):
            errors.append(_("Job worker GSTIN is invalid (leave empty for unregistered: URP)."))
        if not self.from_state_code or not self.to_state_code:
            errors.append(_("State codes are missing: set the GSTIN or the state of the company and job worker."))
        for partner, label in ((company_partner, _('company')), (worker, _('job worker'))):
            if not re.fullmatch(r'\d{6}', (partner.zip or '').strip()):
                errors.append(_("The %s PIN code must have 6 digits.", label))
        if self.transport_mode == '1' and not self.vehicle_no and not self.transporter_id:
            errors.append(_("Enter the vehicle number or the transporter ID for road transport."))
        if self.transport_mode in ('2', '3', '4') and not self.transport_doc_no:
            errors.append(_("Enter the transport document number for rail, air or ship transport."))
        for line in self.line_ids:
            if not re.fullmatch(r'\d{4,8}', line.hsn_code or ''):
                errors.append(_("HSN code missing or invalid for %s.", line.product_id.display_name))
        if not self.line_ids:
            errors.append(_("The challan has no lines."))
        return errors

    def _jw_ewaybill_payload(self):
        self.ensure_one()
        company_partner = self.company_id.partner_id
        worker = self.partner_id.commercial_partner_id
        ship_to = self.partner_id

        def addr(partner):
            return (partner.street or '')[:120], (partner.street2 or '')[:120], (partner.city or '')[:50]

        def pin(partner):
            try:
                return int(re.sub(r'\D', '', partner.zip or '') or 0)
            except ValueError:
                return 0

        f1, f2, fplace = addr(company_partner)
        t1, t2, tplace = addr(ship_to)
        items = []
        for idx, line in enumerate(self.line_ids, start=1):
            rate = line.gst_rate
            items.append({
                'itemNo': idx,
                'productName': (line.product_id.name or '')[:100],
                'productDesc': (line.name or '')[:100],
                'hsnCode': int(line.hsn_code) if (line.hsn_code or '').isdigit() else 0,
                'quantity': round(line.qty, 3),
                'qtyUnit': line.uqc,
                'taxableAmount': round(line.value, 2),
                'sgstRate': 0 if self.is_interstate else round(rate / 2.0, 3),
                'cgstRate': 0 if self.is_interstate else round(rate / 2.0, 3),
                'igstRate': round(rate, 3) if self.is_interstate else 0,
                'cessRate': 0,
                'cessNonAdvol': 0,
            })
        total = round(self.amount_total, 2)
        bill = {
            'userGstin': self.principal_gstin,
            'supplyType': 'O',
            'subSupplyType': 4,
            'subSupplyDesc': '',
            'docType': 'CHL',
            'docNo': self.name,
            'docDate': self.date.strftime('%d/%m/%Y'),
            'fromGstin': self.principal_gstin,
            'fromTrdName': (self.company_id.name or '')[:100],
            'fromAddr1': f1, 'fromAddr2': f2, 'fromPlace': fplace,
            'fromPincode': pin(company_partner),
            'fromStateCode': self.from_state_code,
            'actFromStateCode': self.from_state_code,
            'toGstin': self.job_worker_gstin or 'URP',
            'toTrdName': (worker.name or '')[:100],
            'toAddr1': t1, 'toAddr2': t2, 'toPlace': tplace,
            'toPincode': pin(ship_to),
            'toStateCode': self.to_state_code,
            'actToStateCode': partner_state_code(ship_to) or self.to_state_code,
            'transactionType': 1,
            'totalValue': total,
            'cgstValue': 0, 'sgstValue': 0, 'igstValue': 0, 'cessValue': 0, 'cessNonAdvolValue': 0,
            'otherValue': 0,
            'totInvValue': total,
            'transMode': int(self.transport_mode or 1),
            'transDistance': self.distance_km or 0,
            'transporterName': self.transporter_name or '',
            'transporterId': self.transporter_id or '',
            'transDocNo': self.transport_doc_no or '',
            'transDocDate': self.transport_doc_date.strftime('%d/%m/%Y') if self.transport_doc_date else '',
            'vehicleNo': (self.vehicle_no or '').replace(' ', '').upper(),
            'vehicleType': self.vehicle_type or 'R',
            'itemList': items,
        }
        return bill

    def action_export_ewaybill_json(self):
        errors = []
        for ch in self:
            if ch.state == 'cancel':
                errors.append(_("%s is cancelled.", ch.name))
            errors += ['%s: %s' % (ch.name, e) for e in ch._jw_ewaybill_errors()]
        if errors:
            raise UserError(_("E-Way Bill data cannot be prepared:\n%s", '\n'.join(errors)))
        payload = {'version': '1.0.0621', 'billLists': [ch._jw_ewaybill_payload() for ch in self]}
        content = json.dumps(payload, indent=2, ensure_ascii=False).encode()
        name = 'EWB_%s.json' % ('_'.join(self.mapped('name'))[:80].replace('/', '-'))
        attachment = self.env['ir.attachment'].create({
            'name': name,
            'raw': content,
            'mimetype': 'application/json',
            'res_model': self._name,
            'res_id': self[:1].id,
        })
        for ch in self:
            ch.message_post(body=_("E-Way Bill JSON prepared (%s). Validate it on the E-Way Bill portal.", name))
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % attachment.id,
            'target': 'self',
        }


class ZivoJobWorkChallanLine(models.Model):
    _name = 'zivo.job.work.challan.line'
    _description = 'Job Work Challan Line'
    _order = 'challan_id, sequence, id'

    challan_id = fields.Many2one('zivo.job.work.challan', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(related='challan_id.company_id', store=True)
    currency_id = fields.Many2one(related='challan_id.currency_id')
    product_id = fields.Many2one('product.product', 'Product', required=True)
    name = fields.Char('Description', required=True)
    move_id = fields.Many2one('stock.move', 'Stock Move', readonly=True)
    hsn_code = fields.Char('HSN', compute='_compute_codes', store=True, readonly=False)
    product_uom_id = fields.Many2one('uom.uom', 'Unit', required=True)
    uqc = fields.Char('UQC', compute='_compute_codes', store=True, readonly=False)
    qty = fields.Float('Quantity', digits='Product Unit', required=True)
    unit_value = fields.Float('Unit Value', digits='Product Price')
    value = fields.Monetary('Taxable Value', compute='_compute_value', store=True, readonly=False)
    gst_rate = fields.Float('GST Rate (%)')

    @api.depends('product_id', 'product_uom_id')
    def _compute_codes(self):
        for line in self:
            line.hsn_code = line.product_id.product_tmpl_id._jw_get_hsn() if line.product_id else ''
            line.uqc = line.product_uom_id._jw_get_uqc() if line.product_uom_id else ''

    @api.depends('qty', 'unit_value')
    def _compute_value(self):
        for line in self:
            line.value = line.qty * line.unit_value

    @api.onchange('product_id')
    def _onchange_product_id(self):
        if self.product_id:
            self.name = self.product_id.display_name
            self.product_uom_id = self.product_id.uom_id
            self.unit_value = self.product_id.standard_price
