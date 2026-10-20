# -*- coding: utf-8 -*-
import base64
import logging
from collections import OrderedDict

from werkzeug.exceptions import NotFound
from werkzeug.urls import url_encode

from odoo import _, http
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError
from odoo.http import request
from odoo.addons.portal.controllers import portal
from odoo.addons.portal.controllers.portal import pager as portal_pager

_logger = logging.getLogger(__name__)

MAX_UPLOAD_SIZE = 20 * 1024 * 1024  # 20 MB per file
ALLOWED_EXTENSIONS = {
    'pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'doc', 'docx', 'xls', 'xlsx', 'csv', 'txt', 'zip', 'dwg', 'dxf',
}
DOC_TYPES = OrderedDict([
    ('inspection', 'Inspection Certificate'),
    ('delivery', 'Delivery Document / Challan'),
    ('photo', 'Photo'),
    ('other', 'Other Document'),
])
OPEN_STATES = ['planned', 'ready', 'issued', 'at_vendor', 'processing', 'qc_pending', 'hold']


class JobWorkPortal(portal.CustomerPortal):

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _jw_domain(self):
        partner = request.env.user.partner_id.commercial_partner_id
        return [('partner_id', 'child_of', partner.id), ('state', 'not in', ('draft', 'cancel'))]

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if 'jw_count' in counters:
            Job = request.env['zivo.job.work']
            values['jw_count'] = Job.search_count(self._jw_domain()) \
                if Job.has_access('read') else 0
        return values

    def _jw_get_job(self, job_id, access_token=None):
        try:
            job_sudo = self._document_check_access('zivo.job.work', job_id, access_token=access_token)
        except (AccessError, MissingError):
            raise NotFound()
        if job_sudo.state in ('draft', 'cancel'):
            raise NotFound()
        return job_sudo

    def _jw_redirect(self, job, access_token=None, **params):
        if access_token:
            params['access_token'] = access_token
        url = '/my/job-works/%s' % job.id
        if params:
            url += '?' + url_encode(params)
        return request.redirect(url)

    @staticmethod
    def _jw_float(value, label):
        try:
            number = float(value or 0.0)
        except (TypeError, ValueError):
            raise ValidationError(_("%s must be a number.", label))
        if number < 0:
            raise ValidationError(_("%s cannot be negative.", label))
        return number

    def _jw_author(self, job):
        """Partner shown as author of portal messages."""
        if request.env.user._is_public():
            return job.partner_id
        return request.env.user.partner_id

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------
    @http.route(['/my/job-works', '/my/job-works/page/<int:page>'], type='http', auth='user', website=True)
    def portal_my_job_works(self, page=1, sortby=None, filterby=None, **kw):
        Job = request.env['zivo.job.work']
        if not Job.has_access('read'):
            return request.redirect('/my')
        domain = self._jw_domain()
        searchbar_sortings = {
            'date': {'label': _('Due Date'), 'order': 'date_expected asc, id desc'},
            'name': {'label': _('Reference'), 'order': 'name desc'},
            'priority': {'label': _('Priority'), 'order': 'priority desc, date_expected asc'},
        }
        searchbar_filters = {
            'open': {'label': _('Open'), 'domain': [('state', 'in', OPEN_STATES)]},
            'action': {'label': _('Awaiting My Action'),
                       'domain': [('vendor_state', 'in', ('new', 'accepted', 'received', 'processing')),
                                  ('state', 'in', OPEN_STATES)]},
            'done': {'label': _('Completed'),
                     'domain': [('state', 'in', ('completed', 'reconciled', 'closed'))]},
            'all': {'label': _('All'), 'domain': []},
        }
        sortby = sortby if sortby in searchbar_sortings else 'date'
        filterby = filterby if filterby in searchbar_filters else 'open'
        domain = domain + searchbar_filters[filterby]['domain']
        count = Job.search_count(domain)
        pager = portal_pager(url='/my/job-works', url_args={'sortby': sortby, 'filterby': filterby},
                             total=count, page=page, step=self._items_per_page)
        jobs = Job.search(domain, order=searchbar_sortings[sortby]['order'], limit=self._items_per_page,
                          offset=pager['offset'])
        request.session['my_job_works_history'] = jobs.ids[:100]
        partner = request.env.user.partner_id.commercial_partner_id
        score = request.env['zivo.job.work.vendor.score'].sudo().search(
            [('partner_id', '=', partner.id)], order='date_to desc, id desc', limit=1)
        values = self._prepare_portal_layout_values()
        values.update({
            'jobs': jobs.sudo(),
            'page_name': 'job_work',
            'pager': pager,
            'default_url': '/my/job-works',
            'searchbar_sortings': searchbar_sortings,
            'searchbar_filters': searchbar_filters,
            'sortby': sortby,
            'filterby': filterby,
            'score': score,
        })
        return request.render('zivo_job_work_360.portal_my_job_works', values)

    # ------------------------------------------------------------------
    # Detail
    # ------------------------------------------------------------------
    @http.route(['/my/job-works/<int:job_id>'], type='http', auth='public', website=True)
    def portal_job_work_page(self, job_id, access_token=None, report_type=None, download=False, **kw):
        job_sudo = self._jw_get_job(job_id, access_token)
        if report_type in ('html', 'pdf', 'text'):
            return self._show_report(model=job_sudo, report_type=report_type,
                                     report_ref='zivo_job_work_360.action_report_job_work', download=download)
        values = self._prepare_portal_layout_values()
        values = self._get_page_view_values(job_sudo, access_token, values, 'my_job_works_history', False, **kw)
        for attachment in job_sudo.shared_attachment_ids:
            attachment.sudo().generate_access_token()
        values.update({
            'job': job_sudo,
            'page_name': 'job_work_detail',
            'doc_types': DOC_TYPES,
            'materials': job_sudo.material_ids,
            'can_act': job_sudo.state not in ('completed', 'reconciled', 'closed', 'cancel', 'hold'),
            'message': kw.get('message'),
        })
        return request.render('zivo_job_work_360.portal_job_work_page', values)

    @http.route(['/my/job-works/<int:job_id>/action'], type='http', auth='public', methods=['POST'],
                website=True)
    def portal_job_work_action(self, job_id, action=None, access_token=None, reason=None, **kw):
        job_sudo = self._jw_get_job(job_id, access_token)
        author = self._jw_author(job_sudo)
        try:
            if action == 'accept':
                job_sudo.action_vendor_accept()
            elif action == 'reject':
                job_sudo.action_vendor_reject(reason=(reason or '').strip()[:2000] or None)
            elif action == 'receipt':
                job_sudo.action_vendor_confirm_receipt()
            elif action == 'start':
                job_sudo.action_vendor_start()
            elif action == 'complete':
                job_sudo.action_vendor_complete()
            else:
                raise UserError(_("Unknown action."))
        except (UserError, ValidationError) as e:
            return self._jw_redirect(job_sudo, access_token, error=str(e.args[0] if e.args else e))
        job_sudo.message_post(body=_("Portal action by %s.", author.display_name), author_id=author.id,
                              message_type='comment', subtype_xmlid='mail.mt_note')
        return self._jw_redirect(job_sudo, access_token, success=_("Your update has been recorded."))

    @http.route(['/my/job-works/<int:job_id>/progress'], type='http', auth='public', methods=['POST'],
                website=True)
    def portal_job_work_progress(self, job_id, access_token=None, **kw):
        job_sudo = self._jw_get_job(job_id, access_token)
        try:
            if job_sudo.state not in ('issued', 'at_vendor', 'processing', 'qc_pending'):
                raise UserError(_("Progress can be reported once material has been issued to you."))
            vals = {
                'job_id': job_sudo.id,
                'qty_completed': self._jw_float(kw.get('qty_completed'), _("Completed quantity")),
                'qty_scrap': self._jw_float(kw.get('qty_scrap'), _("Scrap quantity")),
                'qty_rejected': self._jw_float(kw.get('qty_rejected'), _("Rejected quantity")),
                'qty_rework': self._jw_float(kw.get('qty_rework'), _("Rework quantity")),
                'note': (kw.get('note') or '').strip()[:2000],
                'source': 'portal',
                'reported_by_id': request.env.user.id,
            }
            if not any(vals[k] for k in ('qty_completed', 'qty_scrap', 'qty_rejected', 'qty_rework')):
                raise UserError(_("Enter at least one quantity."))
            request.env['zivo.job.work.progress'].sudo().create(vals)
            if job_sudo.state in ('issued', 'at_vendor'):
                job_sudo.action_vendor_start()
        except (UserError, ValidationError) as e:
            return self._jw_redirect(job_sudo, access_token, error=str(e.args[0] if e.args else e))
        author = self._jw_author(job_sudo)
        job_sudo.message_post(
            body=_("Progress reported: %(done)s completed, %(scrap)s scrap, %(rej)s rejected, %(rew)s rework. %(note)s",
                   done=vals['qty_completed'], scrap=vals['qty_scrap'], rej=vals['qty_rejected'],
                   rew=vals['qty_rework'], note=vals['note']),
            author_id=author.id, message_type='comment', subtype_xmlid='mail.mt_note')
        return self._jw_redirect(job_sudo, access_token, success=_("Progress reported. Thank you!"))

    @http.route(['/my/job-works/<int:job_id>/request'], type='http', auth='public', methods=['POST'],
                website=True)
    def portal_job_work_request(self, job_id, access_token=None, material_id=None, qty=None, reason=None, **kw):
        job_sudo = self._jw_get_job(job_id, access_token)
        try:
            if job_sudo.state not in ('ready', 'issued', 'at_vendor', 'processing', 'qc_pending'):
                raise UserError(_("Additional material can only be requested on an active job."))
            try:
                mat_id = int(material_id or 0)
            except ValueError:
                mat_id = 0
            material = job_sudo.material_ids.filtered(lambda m: m.id == mat_id)
            if not material:
                raise UserError(_("Select a material of this job."))
            quantity = self._jw_float(qty, _("Quantity"))
            if quantity <= 0:
                raise UserError(_("The requested quantity must be positive."))
            reason = (reason or '').strip()[:2000]
            if not reason:
                raise UserError(_("Please explain why additional material is needed."))
            request.env['zivo.job.work.request'].sudo().create({
                'job_id': job_sudo.id,
                'material_id': material.id,
                'product_id': material.product_id.id,
                'product_uom_id': material.product_uom_id.id,
                'qty': quantity,
                'reason': reason,
                'source': 'portal',
                'requested_by_id': request.env.user.id,
            })
        except (UserError, ValidationError) as e:
            return self._jw_redirect(job_sudo, access_token, error=str(e.args[0] if e.args else e))
        return self._jw_redirect(job_sudo, access_token,
                                 success=_("Your request has been sent for approval."))

    @http.route(['/my/job-works/<int:job_id>/upload'], type='http', auth='public', methods=['POST'],
                website=True)
    def portal_job_work_upload(self, job_id, access_token=None, doc_type='other', **kw):
        job_sudo = self._jw_get_job(job_id, access_token)
        files = request.httprequest.files.getlist('attachment')
        files = [f for f in files if f and f.filename]
        if not files:
            return self._jw_redirect(job_sudo, access_token, error=_("Select a file to upload."))
        attachments = request.env['ir.attachment'].sudo()
        for upload in files[:10]:
            name = upload.filename.rsplit('/', 1)[-1].rsplit('\\', 1)[-1]
            ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
            if ext not in ALLOWED_EXTENSIONS:
                return self._jw_redirect(job_sudo, access_token,
                                         error=_("File type .%s is not allowed.", ext or '?'))
            content = upload.read(MAX_UPLOAD_SIZE + 1)
            if len(content) > MAX_UPLOAD_SIZE:
                return self._jw_redirect(job_sudo, access_token, error=_("Files must be smaller than 20 MB."))
            attachments |= attachments.create({
                'name': name,
                'datas': base64.b64encode(content),
                'res_model': 'zivo.job.work',
                'res_id': job_sudo.id,
                'description': DOC_TYPES.get(doc_type, DOC_TYPES['other']),
            })
        author = self._jw_author(job_sudo)
        job_sudo.message_post(
            body=_("%(type)s uploaded by the job worker.", type=DOC_TYPES.get(doc_type, DOC_TYPES['other'])),
            attachment_ids=attachments.ids, author_id=author.id, message_type='comment',
            subtype_xmlid='mail.mt_comment')
        return self._jw_redirect(job_sudo, access_token, success=_("Document uploaded."))
