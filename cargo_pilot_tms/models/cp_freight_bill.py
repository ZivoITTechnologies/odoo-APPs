# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import format_amount


class CpFreightBill(models.Model):
    _name = 'cp.freight.bill'
    _description = 'Transporter Freight Bill (Audit)'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'portal.mixin']
    _order = 'bill_date desc, id desc'

    name = fields.Char(default=lambda self: _('New'), copy=False, readonly=True)
    transporter_id = fields.Many2one('res.partner', required=True, tracking=True,
                                     domain=[('cp_is_transporter', '=', True)])
    bill_number = fields.Char(string='Transporter Invoice No.', required=True, tracking=True)
    bill_date = fields.Date(default=fields.Date.context_today, required=True)
    line_ids = fields.One2many('cp.freight.bill.line', 'bill_id', string='Trips', copy=False)
    state = fields.Selection([
        ('draft', 'Draft'), ('submitted', 'Submitted'), ('audited', 'Audited'),
        ('approved', 'Approved'), ('cancel', 'Cancelled'),
    ], default='draft', tracking=True)
    billed_total = fields.Monetary(compute='_compute_totals', store=True)
    approved_total = fields.Monetary(compute='_compute_totals', store=True)
    deduction_total = fields.Monetary(compute='_compute_totals', store=True)
    held_count = fields.Integer(compute='_compute_totals', store=True, string='Lines on Hold')
    risk_level = fields.Selection([('low', 'Low'), ('medium', 'Medium'), ('high', 'High')],
                                  compute='_compute_totals', store=True,
                                  help='Highest risk found on any line. Empty until the bill is audited.')
    move_id = fields.Many2one('account.move', string='Vendor Bill', readonly=True, copy=False)
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    _bill_uniq = models.Constraint(
        'unique(transporter_id, bill_number, company_id)',
        'This transporter invoice number was already entered (possible duplicate bill).',
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('cp.freight.bill') or _('New')
        return super().create(vals_list)

    def _compute_access_url(self):
        super()._compute_access_url()
        for rec in self:
            rec.access_url = '/my/cp/bills/%s' % rec.id

    @api.depends('line_ids.billed_amount', 'line_ids.detention_amount', 'line_ids.other_charges',
                 'line_ids.approved_amount', 'line_ids.risk', 'line_ids.on_hold', 'line_ids.audited')
    def _compute_totals(self):
        for rec in self:
            lines = rec.line_ids
            audited = lines.filtered('audited')
            rec.billed_total = sum(l.billed_amount + l.detention_amount + l.other_charges for l in lines)
            rec.approved_total = sum(audited.mapped('approved_amount'))
            rec.deduction_total = sum(audited.mapped('deduction_amount'))
            rec.held_count = len(audited.filtered('on_hold'))
            risks = audited.mapped('risk')
            if not audited:
                rec.risk_level = False
            else:
                rec.risk_level = 'high' if 'high' in risks else ('medium' if 'medium' in risks else 'low')

    def action_load_trips(self):
        """Add delivered, unbilled trips of this transporter."""
        Trip = self.env['cp.trip']
        for rec in self:
            trips = Trip.search([
                ('transporter_id', '=', rec.transporter_id.id),
                ('state', 'in', ('delivered', 'closed')),
                ('is_billed', '=', False),
                ('id', 'not in', rec.line_ids.trip_id.ids),
            ])
            rec.write({'line_ids': [(0, 0, {
                'trip_id': t.id,
                'billed_amount': t.agreed_freight,
                'billed_km': t.contract_line_id.contracted_km or t.lane_id.distance_km,
                'billed_weight_ton': t.loaded_weight_ton,
            }) for t in trips]})

    def action_submit(self):
        for rec in self:
            if not rec.line_ids:
                raise UserError(_('Add the trips covered by this invoice.'))
        self.write({'state': 'submitted'})
        self.action_audit()

    def action_audit(self):
        for rec in self:
            rec.line_ids._cp_audit()
            rec.state = 'audited'
            if rec.risk_level in ('medium', 'high'):
                self.env['cp.exception'].cp_raise(
                    'audit', rec, _('%(t)s billed %(d)s over contract on %(n)s',
                                    t=rec.transporter_id.name, d=rec._cp_money(rec.deduction_total), n=rec.bill_number),
                    severity='high' if rec.risk_level == 'high' else 'low', transporter=rec.transporter_id,
                    detail='\n'.join(l.findings for l in rec.line_ids if l.findings and l.risk != 'low'),
                    suggestion=_('Approve the audited amount; the transporter sees the findings in the portal.'))
            rec.message_post(body=_('Audit complete: billed %(b)s, approved %(a)s, deduction %(d)s, %(h)s line(s) on hold.',
                                    b=rec._cp_money(rec.billed_total), a=rec._cp_money(rec.approved_total),
                                    d=rec._cp_money(rec.deduction_total),
                                    h=rec.held_count))
        return True

    def action_approve(self):
        for rec in self:
            if rec.state != 'audited':
                raise UserError(_('Audit the bill before approving it.'))
            if rec._cp_use_odoo_vendor_bill():
                rec.move_id = rec._cp_create_vendor_bill()
            rec.state = 'approved'
            self.env['cp.webhook'].sudo()._cp_emit('freight_bill.approved', rec)
            self.env['cp.exception'].cp_auto_resolve(rec, ('audit',), _('Bill approved at audited amount.'))
            if rec.transporter_id:
                rec.message_post(
                    body=_('Freight bill %(n)s approved for %(a)s. Deductions: %(d)s.',
                           n=rec.bill_number, a=rec._cp_money(rec.approved_total),
                           d=rec._cp_money(rec.deduction_total)),
                    partner_ids=rec.transporter_id.ids, message_type='comment', subtype_xmlid='mail.mt_comment')
        return self.action_view_move()

    def _cp_money(self, amount):
        return format_amount(self.env, amount or 0.0, self.currency_id or self.env.company.currency_id)

    def _cp_use_odoo_vendor_bill(self):
        """Accounting add-ons (for example Zoho Books) return False to post the bill elsewhere."""
        return True

    def cp_api_dict(self):
        self.ensure_one()
        return {
            'name': self.name, 'transporter': self.transporter_id.name, 'bill_number': self.bill_number,
            'bill_date': self.bill_date, 'state': self.state, 'billed_total': self.billed_total,
            'approved_total': self.approved_total, 'deduction_total': self.deduction_total,
            'currency': self.currency_id.name,
            'lines': [{'shipment': l.trip_id.name, 'billed': l.billed_amount + l.detention_amount + l.other_charges,
                       'approved': l.approved_amount, 'findings': l.findings or ''} for l in self.line_ids],
        }

    def _cp_create_vendor_bill(self):
        self.ensure_one()
        product = self.env.ref('cargo_pilot_tms.product_cp_freight', raise_if_not_found=False)
        lines = []
        for line in self.line_ids.filtered(lambda l: l.approved_amount > 0):
            lines.append((0, 0, {
                'product_id': product.id if product else False,
                'name': _('Freight %(trip)s %(lane)s (%(veh)s)', trip=line.trip_id.name,
                          lane=line.trip_id.lane_id.name, veh=line.trip_id.vehicle_number or ''),
                'quantity': 1.0,
                'price_unit': line.approved_amount,
            }))
        if not lines:
            raise UserError(_('Nothing approved to pay on %s.', self.name))
        return self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.transporter_id.id,
            'ref': self.bill_number,
            'invoice_date': self.bill_date,
            'invoice_origin': self.name,
            'invoice_line_ids': lines,
            'company_id': self.company_id.id,
        })

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            return True
        return {'type': 'ir.actions.act_window', 'res_model': 'account.move',
                'res_id': self.move_id.id, 'view_mode': 'form'}

    def action_cancel(self):
        for rec in self:
            if rec.move_id and rec.move_id.state == 'posted':
                raise UserError(_('Reset or reverse the posted vendor bill first.'))
            rec.move_id.unlink()
        self.write({'state': 'cancel'})

    def action_draft(self):
        self.write({'state': 'draft'})


class CpFreightBillLine(models.Model):
    _name = 'cp.freight.bill.line'
    _description = 'Freight Bill Line'

    bill_id = fields.Many2one('cp.freight.bill', required=True, ondelete='cascade')
    trip_id = fields.Many2one('cp.trip', required=True)
    lane_id = fields.Many2one(related='trip_id.lane_id')
    vehicle_number = fields.Char(related='trip_id.vehicle_number')
    billed_amount = fields.Monetary(required=True)
    billed_km = fields.Float(string='Billed Km')
    billed_weight_ton = fields.Float(string='Billed Weight (t)')
    detention_amount = fields.Monetary(string='Detention Claimed')
    other_charges = fields.Monetary()

    expected_amount = fields.Monetary(readonly=True, string='Contract Amount')
    gps_km = fields.Float(readonly=True, string='GPS Km')
    loaded_weight_ton = fields.Float(readonly=True, string='Loaded (t)')
    delivered_weight_ton = fields.Float(readonly=True, string='Delivered (t)')
    approved_amount = fields.Monetary(readonly=True)
    deduction_amount = fields.Monetary(compute='_compute_deduction', store=True)
    check_rate = fields.Boolean(readonly=True, string='Rate OK')
    check_distance = fields.Boolean(readonly=True, string='Distance OK')
    check_weight = fields.Boolean(readonly=True, string='Weight OK')
    check_duplicate = fields.Boolean(readonly=True, string='Not Duplicate')
    check_epod = fields.Boolean(readonly=True, string='ePOD OK')
    on_hold = fields.Boolean(readonly=True)
    audited = fields.Boolean(readonly=True, copy=False)
    risk = fields.Selection([('low', 'Low'), ('medium', 'Medium'), ('high', 'High')], readonly=True)
    findings = fields.Text(readonly=True)
    currency_id = fields.Many2one(related='bill_id.currency_id')

    @api.depends('billed_amount', 'detention_amount', 'other_charges', 'approved_amount', 'audited')
    def _compute_deduction(self):
        for line in self:
            claimed = line.billed_amount + line.detention_amount + line.other_charges
            line.deduction_amount = (claimed - line.approved_amount) if line.audited else 0.0

    def write(self, vals):
        # Any change to the claimed figures invalidates the previous audit result.
        if {'billed_amount', 'billed_km', 'billed_weight_ton', 'detention_amount', 'other_charges', 'trip_id'} & set(vals):
            vals = dict(vals, audited=False)
        return super().write(vals)

    def _cp_detention_allowed(self):
        """Detention days beyond the free hours between arrival and delivery, priced by contract."""
        self.ensure_one()
        trip, cline = self.trip_id, self.trip_id.contract_line_id
        if not (cline and cline.detention_per_day and trip.actual_arrival and trip.actual_delivery):
            return 0.0
        hours = (trip.actual_delivery - trip.actual_arrival).total_seconds() / 3600.0
        extra = max(hours - (cline.free_detention_hours or 0.0), 0.0)
        return round(extra / 24.0, 2) * cline.detention_per_day

    def _cp_audit(self):
        for line in self:
            trip = line.trip_id
            company = trip.company_id
            dist_tol = (company.cp_audit_distance_tolerance or 5.0) / 100.0
            wt_tol = (company.cp_audit_weight_tolerance or 1.0) / 100.0
            cline = trip.contract_line_id
            findings, risk_points = [], 0

            # 1. Duplicate: the same trip on another live bill
            dup = self.search([('trip_id', '=', trip.id), ('id', '!=', line.id),
                               ('bill_id.state', '!=', 'cancel')], limit=1)
            check_duplicate = not dup
            if dup:
                findings.append(_('Trip already billed on %s.', dup.bill_id.name))
                risk_points += 3

            # 2. ePOD
            check_epod = trip.epod_state == 'verified'
            if not check_epod:
                findings.append(_('No verified ePOD: line held until proof of delivery.'))
                risk_points += 2

            # 3. Weight: billed vs loaded vs delivered
            loaded = trip.loaded_weight_ton
            delivered = trip.delivered_weight_ton or loaded
            candidates = [w for w in (line.billed_weight_ton, loaded, trip.delivered_weight_ton) if w]
            payable_weight = min(candidates) if candidates else 0.0
            check_weight = True
            if line.billed_weight_ton and loaded and line.billed_weight_ton > loaded * (1 + wt_tol):
                check_weight = False
                findings.append(_('Billed %(b)s t but only %(l)s t loaded.', b=line.billed_weight_ton, l=loaded))
                risk_points += 1
            if delivered and loaded and delivered < loaded * (1 - wt_tol):
                check_weight = False
                findings.append(_('Delivered %(d)s t vs loaded %(l)s t (shortage).', d=delivered, l=loaded))
                risk_points += 1

            # 4. Distance: billed km vs GPS (+FASTag) km
            gps_km = trip.gps_distance_km
            payable_km = line.billed_km or (cline.contracted_km if cline else trip.lane_id.distance_km)
            check_distance = True
            if gps_km and line.billed_km and line.billed_km > gps_km * (1 + dist_tol):
                check_distance = False
                payable_km = gps_km * (1 + dist_tol)
                findings.append(_('Billed %(b)s km vs GPS %(g)s km (more than %(t)s%% over).',
                                  b=line.billed_km, g=round(gps_km, 1), t=round(dist_tol * 100, 1)))
                risk_points += 1

            # 5. Rate: billed vs contract (or awarded bid) recomputed on payable quantities
            if cline:
                expected = cline.cp_compute_amount(weight_ton=payable_weight, distance_km=payable_km,
                                                   **trip._cp_rate_kwargs())
            else:
                expected = trip.agreed_freight
            check_rate = line.billed_amount <= expected + 0.01
            if not check_rate:
                findings.append(_('Billed %(b)s above contract %(e)s.', b=line.bill_id._cp_money(line.billed_amount),
                                  e=line.bill_id._cp_money(expected)))
                risk_points += 1

            # 6. Detention
            detention_ok = line._cp_detention_allowed()
            if line.detention_amount > detention_ok + 0.01:
                findings.append(_('Detention claimed %(c)s, allowed %(a)s.', c=line.bill_id._cp_money(line.detention_amount),
                                  a=line.bill_id._cp_money(detention_ok)))

            approved = min(line.billed_amount, expected) + min(line.detention_amount, detention_ok) \
                + line.other_charges
            on_hold = not check_epod or not check_duplicate
            if on_hold:
                approved = 0.0
            line.write({
                'audited': True,
                'expected_amount': expected,
                'gps_km': gps_km,
                'loaded_weight_ton': loaded,
                'delivered_weight_ton': trip.delivered_weight_ton,
                'approved_amount': approved,
                'check_rate': check_rate,
                'check_distance': check_distance,
                'check_weight': check_weight,
                'check_duplicate': check_duplicate,
                'check_epod': check_epod,
                'on_hold': on_hold,
                'risk': 'high' if risk_points >= 3 else ('medium' if risk_points else 'low'),
                'findings': '\n'.join(findings) or _('All checks passed.'),
            })
