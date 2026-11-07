# -*- coding: utf-8 -*-
"""Vendor portal for dispatch: live bid timer, assigned dispatches with gate times,
POD upload and invoice submission (FRD section 6, 3PL Vendor role)."""
import base64
import json
from urllib.parse import quote as url_quote

from odoo import _, http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.cargo_pilot_tms.controllers.portal import CargoPilotPortal

MAX_UPLOAD = 15 * 1024 * 1024


class CargoPilotDispatchPortal(CargoPilotPortal):

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if 'cp_dispatch_count' in counters:
            transporter = self._cp_transporter()
            values['cp_dispatch_count'] = request.env['cp.dispatch.plan'].sudo().search_count(
                [('vendor_id', '=', transporter.id), ('state', 'not in', ('cancel', 'draft'))]) if transporter else 0
        return values

    @http.route('/my/cp/auctions/<int:auction_id>', type='http', auth='user', website=True)
    def cp_auction_detail(self, auction_id, message=None, error=None, **kw):
        transporter = self._cp_transporter()
        if transporter:
            auction = request.env['cp.auction'].sudo().browse(auction_id).exists()
            if auction:
                auction.cp_close_if_expired()
        return super().cp_auction_detail(auction_id, message=message, error=error, **kw)

    @http.route('/my/cp/auctions/<int:auction_id>/live', type='http', auth='user', methods=['GET'])
    def cp_auction_live(self, auction_id, **kw):
        transporter = self._cp_transporter()
        if not transporter:
            raise request.not_found()
        auction = self._cp_get('cp.auction', auction_id, self._cp_auction_domain(transporter))
        return request.make_response(json.dumps(auction.cp_live_status(transporter), default=str),
                                     headers=[('Content-Type', 'application/json'), ('Cache-Control', 'no-store')])

    # ------------------------------------------------------------------
    # Assigned dispatches
    # ------------------------------------------------------------------
    def _cp_dispatch_domain(self, transporter):
        return [('vendor_id', '=', transporter.id), ('state', 'not in', ('cancel', 'draft'))]

    @http.route('/my/cp/dispatches', type='http', auth='user', website=True)
    def cp_dispatches(self, **kw):
        transporter = self._cp_transporter()
        if not transporter:
            return request.redirect('/my')
        plans = request.env['cp.dispatch.plan'].sudo().search(self._cp_dispatch_domain(transporter), limit=200)
        return request.render('cargo_pilot_tms.portal_my_cp_dispatches', {
            'plans': plans, 'page_name': 'cp_dispatches'})

    @http.route('/my/cp/dispatches/<int:plan_id>', type='http', auth='user', website=True)
    def cp_dispatch_detail(self, plan_id, message=None, error=None, **kw):
        transporter = self._cp_transporter()
        if not transporter:
            return request.redirect('/my')
        plan = self._cp_get('cp.dispatch.plan', plan_id, self._cp_dispatch_domain(transporter))
        return request.render('cargo_pilot_tms.portal_my_cp_dispatch', {
            'plan': plan, 'page_name': 'cp_dispatch', 'message': message, 'error': error,
            'bills': plan.move_ids.filtered(lambda m: m.move_type == 'in_invoice')})

    def _cp_attachments(self, files, plan):
        Attachment = request.env['ir.attachment'].sudo()
        attachments = Attachment
        for upload in files:
            if not upload or not upload.filename:
                continue
            data = upload.read()
            if len(data) > MAX_UPLOAD:
                raise UserError(_('%s is larger than 15 MB.', upload.filename))
            attachments |= Attachment.create({'name': upload.filename, 'datas': base64.b64encode(data),
                                              'mimetype': upload.mimetype, 'res_model': 'cp.dispatch.plan',
                                              'res_id': plan.id})
        return attachments

    @http.route('/my/cp/dispatches/<int:plan_id>/pod', type='http', auth='user', methods=['POST'], website=True)
    def cp_dispatch_pod(self, plan_id, receiver_name=None, **kw):
        transporter = self._cp_transporter()
        if not transporter:
            return request.redirect('/my')
        plan = self._cp_get('cp.dispatch.plan', plan_id, self._cp_dispatch_domain(transporter))
        url = '/my/cp/dispatches/%s' % plan.id
        try:
            if plan.state not in ('in_transit', 'delivered', 'pod_verified'):
                raise UserError(_('Upload the POD once the goods are delivered.'))
            files = request.httprequest.files.getlist('pod_files')
            attachments = self._cp_attachments(files, plan)
            if not attachments:
                raise UserError(_('Choose the POD file (photo or PDF).'))
            request.env['cp.dispatch.pod'].sudo().create({
                'plan_id': plan.id, 'source': 'portal', 'receiver_name': receiver_name,
                'attachment_ids': [(6, 0, attachments.ids)]})
            if plan.state == 'in_transit':
                plan.with_context(cp_from_plan=False).action_mark_delivered()
        except (UserError, ValidationError) as exc:
            return request.redirect('%s?error=%s' % (url, url_quote(str(exc.args[0]))))
        return request.redirect('%s?message=%s' % (url, url_quote(_('POD uploaded. We will verify it shortly.'))))

    @http.route('/my/cp/dispatches/<int:plan_id>/invoice', type='http', auth='user', methods=['POST'], website=True)
    def cp_dispatch_invoice(self, plan_id, bill_ref=None, freight=None, detention=None, **kw):
        transporter = self._cp_transporter()
        if not transporter:
            return request.redirect('/my')
        plan = self._cp_get('cp.dispatch.plan', plan_id, self._cp_dispatch_domain(transporter))
        url = '/my/cp/dispatches/%s' % plan.id
        try:
            if not (bill_ref or '').strip():
                raise UserError(_('Enter your invoice number.'))
            if plan.move_ids.filtered(lambda m: m.ref == bill_ref.strip() and m.state != 'cancel'):
                raise UserError(_('Invoice %s was already submitted.', bill_ref))
            freight_amount = float(freight or 0)
            detention_amount = float(detention or 0)
            already_billed = plan.move_ids.filtered(lambda m: m.move_type == 'in_invoice' and m.state != 'cancel')
            if already_billed:
                # Freight is billed; a second invoice is for detention approved later.
                if plan.sudo().detention_unbilled <= 0:
                    raise UserError(_('Your freight invoice is already in and there is no approved detention left to bill.'))
                if detention_amount <= 0:
                    raise UserError(_('Enter the detention amount on your invoice.'))
            elif freight_amount <= 0:
                raise UserError(_('Enter the freight amount on your invoice.'))
            files = request.httprequest.files.getlist('invoice_file')
            attachments = self._cp_attachments(files, plan)
            plan.sudo().cp_create_vendor_bill(bill_ref=bill_ref.strip(), freight_billed=freight_amount,
                                             detention_billed=detention_amount, attachment=attachments)
        except (UserError, ValidationError, ValueError) as exc:
            return request.redirect('%s?error=%s' % (url, url_quote(str(exc.args[0]))))
        return request.redirect('%s?message=%s' % (url, url_quote(_('Invoice submitted.'))))
