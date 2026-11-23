# -*- coding: utf-8 -*-
import base64
import hashlib
import mimetypes

from odoo import api, fields, models
from odoo.exceptions import UserError

DOC_CATEGORIES = [
    ('general', 'General'),
    ('agenda', 'Agenda & Programme'),
    ('contract', 'Contracts & Agreements'),
    ('presentation', 'Presentations'),
    ('media', 'Media & Branding'),
    ('logistics', 'Logistics'),
    ('report', 'Reports & Minutes'),
    ('other', 'Other'),
]


class ZivoEventDocument(models.Model):
    _name = 'zivo.event.document'
    _inherit = ['mail.thread', 'zivo.audit.mixin']
    _description = 'Event Document'
    _order = 'category, name'

    _zivo_audit_fields = ['name', 'category', 'event_id', 'owner_id', 'description', 'active']

    name = fields.Char(required=True, tracking=True)
    event_id = fields.Many2one('event.event', required=True, ondelete='cascade', index=True, tracking=True)
    folder_name = fields.Char(string='Folder', compute='_compute_folder_name', store=True)
    category = fields.Selection(DOC_CATEGORIES, default='general', required=True, tracking=True,
                                string='Sub-folder')
    description = fields.Text()
    owner_id = fields.Many2one('res.users', default=lambda self: self.env.user, tracking=True)
    active = fields.Boolean(default=True)
    version_ids = fields.One2many('zivo.event.document.version', 'document_id', string='Versions')
    version_count = fields.Integer(compute='_compute_current')
    current_version_id = fields.Many2one('zivo.event.document.version', compute='_compute_current',
                                         string='Current Version')
    version = fields.Integer(string='Version', compute='_compute_current')
    datas = fields.Binary(string='File', compute='_compute_datas', inverse='_inverse_datas')
    filename = fields.Char()
    mimetype = fields.Char(compute='_compute_current')
    file_size = fields.Integer(compute='_compute_current', string='Size (bytes)')
    last_upload = fields.Datetime(compute='_compute_current')
    company_id = fields.Many2one(related='event_id.company_id', store=True)

    @api.depends('event_id.name', 'event_id.date_begin')
    def _compute_folder_name(self):
        for doc in self:
            ev = doc.event_id
            doc.folder_name = '%s / %s' % (ev.date_begin.strftime('%Y-%m') if ev.date_begin else '----', ev.name or '')

    @api.depends('version_ids')
    def _compute_current(self):
        for doc in self:
            versions = doc.version_ids.sorted('version_number')
            current = versions[-1:] if versions else self.env['zivo.event.document.version']
            doc.current_version_id = current
            doc.version_count = len(versions)
            doc.version = current.version_number or 0
            doc.mimetype = current.mimetype
            doc.file_size = current.file_size
            doc.last_upload = current.create_date

    @api.depends('version_ids')
    def _compute_datas(self):
        for doc in self:
            versions = doc.version_ids.sorted('version_number')
            doc.datas = versions[-1].datas if versions else False

    def _inverse_datas(self):
        for doc in self:
            if doc.datas:
                doc._zivo_add_version(doc.datas, doc.filename or doc.name)

    def _zivo_add_version(self, datas, filename, note=False):
        self.ensure_one()
        raw = base64.b64decode(datas)
        checksum = hashlib.sha1(raw).hexdigest()
        last = self.version_ids.sorted('version_number')[-1:]
        if last and last.checksum == checksum:
            return last  # identical file, no new version
        number = (last.version_number or 0) + 1
        version = self.env['zivo.event.document.version'].create({
            'document_id': self.id,
            'version_number': number,
            'datas': datas,
            'filename': filename,
            'note': note,
            'file_size': len(raw),
            'checksum': checksum,
            'mimetype': mimetypes.guess_type(filename or '')[0] or 'application/octet-stream',
        })
        self.with_context(zivo_audit_skip=True).filename = filename
        self.env['zivo.audit.log']._zivo_log(self, 'version', note='Version %s uploaded: %s%s' % (
            number, filename, (' — %s' % note) if note else ''))
        self.message_post(body=self.env._('Version %(n)s uploaded: %(f)s', n=number, f=filename))
        return version

    def action_upload_version(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Upload New Version'),
            'res_model': 'zivo.document.version.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_document_id': self.id},
        }

    def action_download(self):
        self.ensure_one()
        if not self.current_version_id:
            raise UserError(self.env._('This document has no file yet.'))
        return self.current_version_id.action_download()


class ZivoEventDocumentVersion(models.Model):
    _name = 'zivo.event.document.version'
    _description = 'Event Document Version'
    _order = 'document_id, version_number desc'
    _rec_name = 'filename'

    document_id = fields.Many2one('zivo.event.document', required=True, ondelete='cascade', index=True)
    event_id = fields.Many2one(related='document_id.event_id', store=True)
    version_number = fields.Integer(string='Version', required=True, readonly=True)
    datas = fields.Binary(string='File', attachment=True, required=True, readonly=True)
    filename = fields.Char(readonly=True)
    mimetype = fields.Char(readonly=True)
    file_size = fields.Integer(string='Size (bytes)', readonly=True)
    checksum = fields.Char(readonly=True)
    note = fields.Char(string='Change Note')
    uploaded_by = fields.Many2one('res.users', string='Uploaded By', default=lambda self: self.env.user,
                                  readonly=True)
    is_current = fields.Boolean(compute='_compute_is_current')

    _doc_version_uniq = models.Constraint('unique(document_id, version_number)',
                                          'Version numbers must be unique per document.')

    @api.depends('document_id.version_ids')
    def _compute_is_current(self):
        for v in self:
            v.is_current = v == v.document_id.current_version_id

    def write(self, vals):
        allowed = {'note'}
        if set(vals) - allowed and not self.env.su:
            raise UserError(self.env._('Document versions are immutable. Upload a new version instead.'))
        return super().write(vals)

    def action_download(self):
        self.ensure_one()
        self.env['zivo.audit.log']._zivo_log(self.document_id, 'download',
                                             note='Version %s downloaded: %s' % (self.version_number, self.filename))
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/zivo.event.document.version/%s/datas?download=true&filename_field=filename' % self.id,
            'target': 'self',
        }

    def action_restore(self):
        self.ensure_one()
        self.document_id._zivo_add_version(self.datas, self.filename,
                                           note=self.env._('Restored from version %s', self.version_number))
        return True
