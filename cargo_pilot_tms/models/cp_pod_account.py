# -*- coding: utf-8 -*-
"""FR-08 POD quality gate and FR-09 POD-gated, 3-way matched vendor bills."""
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class CpDispatchPod(models.Model):
    _name = 'cp.dispatch.pod'
    _description = 'Proof of Delivery'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(default=lambda self: _('New'), readonly=True, copy=False)
    plan_id = fields.Many2one('cp.dispatch.plan', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='plan_id.company_id', store=True)
    vendor_id = fields.Many2one(related='plan_id.vendor_id', store=True)
    source = fields.Selection([('portal', 'Vendor portal'), ('driver', 'Driver app ePOD'),
                               ('internal', 'Uploaded by logistics')], default='internal', required=True)
    attachment_ids = fields.Many2many('ir.attachment', 'cp_dispatch_pod_attachment_rel', 'pod_id', 'attachment_id',
                                      string='POD Documents')
    document_count = fields.Integer(compute='_compute_document_count')
    receiver_name = fields.Char(string='Received By')
    received_at = fields.Datetime(string='Received On')
    epod_id = fields.Many2one('cp.epod', string='Driver ePOD', readonly=True)
    check_signature = fields.Boolean(string='Legible consignee signature')
    check_date = fields.Boolean(string='Date / time of receipt')
    check_stamp = fields.Boolean(string='Consignee stamp')
    check_quantity = fields.Boolean(string='Quantity received in full')
    damage_noted = fields.Boolean(string='Shortage or damage noted')
    damage_remarks = fields.Text()
    status = fields.Selection([('pending', 'Pending Verification'), ('verified', 'Verified'),
                               ('rejected', 'Rejected')], default='pending', required=True, tracking=True)
    reject_reason = fields.Char(tracking=True)
    verified_by = fields.Many2one('res.users', readonly=True)
    verified_at = fields.Datetime(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('cp.dispatch.pod') or _('New')
        records = super().create(vals_list)
        for rec in records:
            rec.attachment_ids.filtered(lambda a: not a.res_id).sudo().write({'res_model': rec._name, 'res_id': rec.id})
            rec._cp_to_documents()
            rec.plan_id.message_post(body=_('Proof of delivery %(n)s received (%(s)s).', n=rec.name,
                                            s=dict(rec._fields['source'].selection)[rec.source]))
        return records

    def _compute_document_count(self):
        for rec in self:
            rec.document_count = len(rec.attachment_ids)

    def _cp_to_documents(self):
        """With Odoo Documents (Enterprise) installed, file the POD there as well."""
        if 'documents.document' not in self.env:
            return
        Document = self.env['documents.document'].sudo()
        for rec in self:
            for attachment in rec.attachment_ids:
                try:
                    with self.env.cr.savepoint():
                        Document.create({'attachment_id': attachment.id, 'name': attachment.name,
                                         'res_model': 'cp.dispatch.plan', 'res_id': rec.plan_id.id})
                except Exception:  # noqa: BLE001 - Documents is optional
                    continue

    def action_verify(self):
        if not self.env.su and not self.env.user.has_group('cargo_pilot_tms.group_cp_user'):
            raise UserError(_('Only the logistics team verifies PODs.'))
        for rec in self:
            if rec.status == 'verified':
                continue
            if not rec.attachment_ids:
                raise UserError(_('Attach the signed POD, LR or delivery challan to %s.', rec.name))
            missing = [label for ok, label in ((rec.check_signature, _('signature')), (rec.check_date, _('date')),
                                               (rec.check_stamp, _('stamp'))) if not ok]
            if missing:
                raise UserError(_('Check the %s on the POD before verifying it.', ', '.join(missing)))
            if not rec.check_quantity and not (rec.damage_noted and rec.damage_remarks):
                raise UserError(_('Confirm the full quantity, or record the shortage / damage remarks.'))
            rec.write({'status': 'verified', 'verified_by': self.env.uid, 'verified_at': fields.Datetime.now(),
                       'reject_reason': False})
            plan = rec.plan_id
            plan._cp_check_pod_state()
            self.env['cp.exception'].sudo().cp_auto_resolve(plan, ('epod_missing',), _('POD verified.'))
            if rec.damage_noted:
                self.env['cp.exception'].sudo().cp_raise('epod', plan, _('Shortage / damage on %s', plan.name),
                                                         severity='medium', detail=rec.damage_remarks)
            plan.message_post(body=_('POD %s verified. The vendor bill can now be posted.', rec.name))
        return True

    def action_reject(self):
        for rec in self:
            if not rec.reject_reason:
                raise UserError(_('Say why %s is rejected so the transporter can re-upload it.', rec.name))
            rec.write({'status': 'rejected', 'verified_by': False, 'verified_at': False})
            plan = rec.plan_id
            plan._cp_check_pod_state()
            vendor = plan.vendor_id
            if vendor:
                plan._cp_message_partner(vendor, _('POD rejected: %s', plan.name),
                                         _('The proof of delivery for %(plan)s was rejected: %(why)s. Please upload '
                                           'a clear signed copy at %(url)s',
                                           plan=plan.name, why=rec.reject_reason,
                                           url='%s/my/cp/dispatches/%s' % (plan.get_base_url(), plan.id)))
        return True


class CpEpod(models.Model):
    _inherit = 'cp.epod'

    def action_submit(self):
        res = super().action_submit()
        Pod = self.env['cp.dispatch.pod'].sudo()
        for rec in self:
            plan = rec.trip_id.dispatch_plan_id
            if not plan or Pod.search_count([('epod_id', '=', rec.id)]):
                continue
            attachments = rec.photo_ids
            if rec.signature:
                attachments |= self.env['ir.attachment'].sudo().create({
                    'name': '%s-signature.png' % rec.name, 'datas': rec.signature, 'mimetype': 'image/png'})
            Pod.create({'plan_id': plan.id, 'source': 'driver', 'epod_id': rec.id, 'receiver_name': rec.receiver_name,
                        'received_at': rec.create_date, 'attachment_ids': [(6, 0, attachments.ids)]})
        return res


class CpTrip(models.Model):
    _inherit = 'cp.trip'

    dispatch_plan_id = fields.Many2one('cp.dispatch.plan', string='Dispatch Plan', index=True, ondelete='set null',
                                       readonly=True)

    def write(self, vals):
        res = super().write(vals)
        if 'state' in vals and not self.env.context.get('cp_from_plan'):
            for trip in self.filtered('dispatch_plan_id'):
                plan = trip.dispatch_plan_id.sudo()
                if trip.state == 'in_transit' and plan.state in ('assigned', 'loading'):
                    plan.with_context(cp_from_plan=True).cp_mark_in_transit()
                elif trip.state == 'delivered' and plan.state == 'in_transit':
                    plan.with_context(cp_from_plan=True).write({'state': 'delivered', 'milestone': 'delivered',
                                                                'delivered_at': fields.Datetime.now()})
                    plan._cp_check_pod_state()
        return res

    def _cp_notify_customers(self, event):
        # dispatch plans send their own customer messages with the carrier tracking link
        return super(CpTrip, self.filtered(lambda t: not t.dispatch_plan_id))._cp_notify_customers(event)


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    cp_dispatch_id = fields.Many2one('cp.dispatch.plan', string='Dispatch Plan', index=True, copy=False)


class AccountMove(models.Model):
    _inherit = 'account.move'

    cp_dispatch_id = fields.Many2one('cp.dispatch.plan', string='Dispatch Plan', index=True, copy=False,
                                     compute='_compute_cp_dispatch', store=True, readonly=False)
    cp_pod_status = fields.Selection(related='cp_dispatch_id.pod_status', string='POD Status')
    cp_freight_expected = fields.Monetary(compute='_compute_cp_match', string='Freight per PO')
    cp_freight_billed = fields.Monetary(compute='_compute_cp_match', string='Freight Billed')
    cp_detention_expected = fields.Monetary(compute='_compute_cp_match', string='Approved Detention')
    cp_detention_billed = fields.Monetary(compute='_compute_cp_match', string='Detention Billed')
    cp_match_state = fields.Selection([('ok', 'Matched'), ('exception', 'Mismatch - needs approval'),
                                       ('approved', 'Mismatch approved')], compute='_compute_cp_match',
                                      string='3-Way Match')
    cp_match_note = fields.Char(compute='_compute_cp_match')
    cp_exception_approved_by = fields.Many2one('res.users', readonly=True, copy=False)
    cp_exception_amounts = fields.Char(readonly=True, copy=False, help='Billed amounts when the mismatch was approved.')
    cp_exception_reason = fields.Char(copy=False)

    @api.depends('invoice_line_ids.purchase_line_id.order_id.cp_dispatch_id')
    def _compute_cp_dispatch(self):
        for move in self:
            if move.cp_dispatch_id:
                continue
            plans = move.invoice_line_ids.purchase_line_id.order_id.cp_dispatch_id
            move.cp_dispatch_id = plans[:1]

    def _cp_bill_amounts(self):
        self.ensure_one()
        detention_product = self.env.ref('cargo_pilot_tms.product_cp_detention', raise_if_not_found=False)
        lines = self.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
        detention = lines.filtered(lambda l: detention_product and l.product_id == detention_product)
        freight = lines - detention
        return round(sum(freight.mapped('price_subtotal')), 2), round(sum(detention.mapped('price_subtotal')), 2)

    @api.depends('invoice_line_ids.price_subtotal', 'cp_dispatch_id', 'cp_exception_amounts',
                 'cp_dispatch_id.detention_ids.state', 'cp_dispatch_id.detention_ids.approved_amount',
                 'cp_dispatch_id.move_ids.state')
    def _compute_cp_match(self):
        for move in self:
            plan = move.cp_dispatch_id
            if not plan or move.move_type not in ('in_invoice', 'in_refund'):
                move.update({'cp_freight_expected': 0.0, 'cp_freight_billed': 0.0, 'cp_detention_expected': 0.0,
                             'cp_detention_billed': 0.0, 'cp_match_state': False, 'cp_match_note': False})
                continue
            freight, detention = move._cp_bill_amounts()
            # Freight is billed once: a later bill on the same plan (for example late detention) expects only
            # the freight not yet billed elsewhere.
            others = plan.move_ids.filtered(lambda m: m.move_type == 'in_invoice' and m.state != 'cancel'
                                            and m.id not in (move.id, move._origin.id))
            billed_elsewhere = sum(m._cp_bill_amounts()[0] for m in others)
            expected_freight = round(max(sum(plan.purchase_order_id.order_line.mapped('price_subtotal'))
                                         - billed_elsewhere, 0.0), 2)
            approved = plan.detention_ids.filtered(lambda d: d.state == 'approved')
            expected_detention = round(sum(approved.filtered(lambda d: d.move_id in (move, move._origin)
                                                             or not d.move_id).mapped('approved_amount')), 2)
            notes = []
            if move.move_type == 'in_invoice':
                if abs(freight - expected_freight) > 0.005:
                    notes.append(_('Freight billed %(b)s vs PO %(e)s', b=plan._cp_money(freight),
                                   e=plan._cp_money(expected_freight)))
                if detention - expected_detention > 0.005:
                    notes.append(_('Detention billed %(b)s vs approved %(e)s', b=plan._cp_money(detention),
                                   e=plan._cp_money(expected_detention)))
            state = 'ok'
            if notes:
                state = 'approved' if move.cp_exception_amounts == '%s|%s' % (freight, detention) else 'exception'
            move.update({'cp_freight_expected': expected_freight, 'cp_freight_billed': freight,
                         'cp_detention_expected': expected_detention, 'cp_detention_billed': detention,
                         'cp_match_state': state, 'cp_match_note': '; '.join(notes) or False})

    def _cp_check_dispatch_gate(self):
        for move in self.filtered(lambda m: m.cp_dispatch_id and m.move_type == 'in_invoice'):
            plan = move.cp_dispatch_id
            if plan.pod_status != 'verified':
                raise ValidationError(_('Vendor bill %(bill)s is on hold: the POD of %(plan)s is not verified yet.',
                                        bill=move.ref or move.name or _('draft'), plan=plan.name))
            if move.cp_match_state == 'exception':
                raise ValidationError(_('Vendor bill for %(plan)s does not match: %(note)s. An accounting manager '
                                        'must approve the difference first.', plan=plan.name, note=move.cp_match_note))

    def _post(self, soft=True):
        self._cp_check_dispatch_gate()
        posted = super()._post(soft=soft)
        for move in posted.filtered('cp_dispatch_id'):
            move.cp_dispatch_id.message_post(body=_('Vendor bill %s posted.', move.name))
        return posted

    def action_register_payment(self):
        self._cp_check_dispatch_gate()
        return super().action_register_payment()

    def action_cp_approve_mismatch(self):
        if not self.env.user.has_group('account.group_account_manager'):
            raise UserError(_('Only an accounting manager can approve a freight bill mismatch.'))
        for move in self:
            if move.cp_match_state != 'exception':
                continue
            if not move.cp_exception_reason:
                raise UserError(_('Enter the reason for accepting the difference.'))
            freight, detention = move._cp_bill_amounts()
            move.write({'cp_exception_amounts': '%s|%s' % (freight, detention),
                        'cp_exception_approved_by': self.env.uid})
            move.message_post(body=_('Mismatch approved by %(u)s: %(r)s', u=self.env.user.name,
                                     r=move.cp_exception_reason))
        return True


class AccountPaymentRegister(models.TransientModel):
    _inherit = 'account.payment.register'

    def _create_payments(self):
        self.line_ids.move_id._cp_check_dispatch_gate()
        return super()._create_payments()
