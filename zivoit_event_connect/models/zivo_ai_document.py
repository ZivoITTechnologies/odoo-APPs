# -*- coding: utf-8 -*-
import base64
import difflib
import io
import json
import logging
import re
import zipfile
from datetime import datetime

from markupsafe import Markup, escape

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

from .zivo_event_document import DOC_CATEGORIES

_logger = logging.getLogger(__name__)
MAX_CHARS = 15000


def extract_text(raw, filename='', mimetype=''):
    """Best-effort text extraction for txt/csv/md/html/xml/json, PDF and DOCX files."""
    name = (filename or '').lower()
    mimetype = mimetype or ''
    try:
        if name.endswith('.pdf') or 'pdf' in mimetype:
            from odoo.tools.pdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            return '\n'.join((page.extract_text() or '') for page in reader.pages[:30])
        if name.endswith('.docx') or 'wordprocessingml' in mimetype:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                xml = zf.read('word/document.xml').decode('utf-8', 'ignore')
            xml = re.sub(r'</w:p>', '\n', xml)
            return re.sub(r'<[^>]+>', '', xml)
        if name.endswith(('.html', '.htm')) or 'html' in mimetype:
            return html2plaintext(raw.decode('utf-8', 'ignore'))
        if name.endswith(('.txt', '.csv', '.md', '.json', '.xml', '.ics')) or mimetype.startswith('text/'):
            return raw.decode('utf-8', 'ignore')
    except Exception as exc:  # noqa: BLE001
        _logger.info('Text extraction failed for %s: %s', filename, exc)
    return ''


class ZivoEventDocumentDate(models.Model):
    _name = 'zivo.event.document.date'
    _description = 'AI Extracted Key Date'
    _order = 'date'

    document_id = fields.Many2one('zivo.event.document', required=True, ondelete='cascade')
    date = fields.Date(required=True)
    name = fields.Char(string='What', required=True)
    activity_created = fields.Boolean(readonly=True)


class ZivoEventDocument(models.Model):
    _inherit = 'zivo.event.document'

    zivo_ai_summary = fields.Text(string='AI Summary', readonly=True)
    zivo_ai_category = fields.Selection(DOC_CATEGORIES, string='AI Suggested Sub-folder', readonly=True)
    zivo_ai_date_ids = fields.One2many('zivo.event.document.date', 'document_id', string='Key Dates')
    zivo_ai_analysed_version = fields.Integer(readonly=True)

    def _zivo_text(self, version=None):
        self.ensure_one()
        version = version or self.current_version_id
        if not version or not version.datas:
            return ''
        raw = base64.b64decode(version.datas)
        return extract_text(raw, version.filename, version.mimetype)[:MAX_CHARS]

    def action_zivo_ai_analyse(self):
        service = self.env['zivo.ai.service']
        service._ensure_feature('document')
        for doc in self:
            text = doc._zivo_text()
            if not text.strip():
                raise UserError(self.env._('No readable text found in "%s" (supported: PDF, DOCX, TXT, CSV, HTML).',
                                           doc.filename or doc.name))
            prompt = (
                'Document "%s" (file %s) belongs to the event "%s" starting %s.\nAllowed sub-folders: %s.\n'
                'Text:\n"""\n%s\n"""\n\nReturn JSON {"summary": "3-5 sentence summary for an event manager", '
                '"category": one allowed sub-folder key, "key_dates": [{"date": "YYYY-MM-DD", '
                '"what": "deadline / obligation / milestone"}] (only dates explicitly in the text, max 8)}.'
            ) % (doc.name, doc.filename, doc.event_id.name, doc.event_id.date_begin,
                 ', '.join(k for k, _v in DOC_CATEGORIES), text)
            data = service._zivo_ai_complete('document', 'You are an assistant for event operations teams.',
                                             prompt, as_json=True, record=doc)
            dates = []
            for item in (data.get('key_dates') or [])[:8]:
                try:
                    day = datetime.strptime(str(item.get('date')), '%Y-%m-%d').date()
                except (TypeError, ValueError, AttributeError):
                    continue
                if item.get('what'):
                    dates.append((0, 0, {'date': day, 'name': str(item['what'])[:200]}))
            category = data.get('category') if data.get('category') in dict(DOC_CATEGORIES) else False
            doc.zivo_ai_date_ids.unlink()
            doc.with_context(zivo_audit_skip=True).write({
                'zivo_ai_summary': str(data.get('summary') or '')[:3000],
                'zivo_ai_category': category,
                'zivo_ai_date_ids': dates,
                'zivo_ai_analysed_version': doc.version,
            })
        return True

    def action_zivo_ai_apply_category(self):
        for doc in self.filtered('zivo_ai_category'):
            doc.category = doc.zivo_ai_category
        return True

    def action_zivo_ai_create_activities(self):
        todo = self.env.ref('mail.mail_activity_data_todo')
        created = 0
        for doc in self:
            for line in doc.zivo_ai_date_ids.filtered(lambda d: not d.activity_created):
                doc.event_id.activity_schedule(
                    activity_type_id=todo.id, date_deadline=line.date,
                    summary=line.name[:100], note=self.env._('From document "%s"', doc.name),
                    user_id=(doc.event_id.user_id or self.env.user).id)
                line.activity_created = True
                created += 1
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'type': 'success', 'sticky': False,
                       'message': self.env._('%s activity(ies) scheduled on the event.', created)},
        }

    @api.model
    def _zivo_auto_analyse_enabled(self):
        return self.env['zivo.ai.service']._feature_enabled('document') and str(
            self.env['ir.config_parameter'].sudo().get_param(
                'zivoit_event_connect.ai_document_auto', 'False')).lower() in ('1', 'true')

    def _zivo_add_version(self, datas, filename, note=False):
        version = super()._zivo_add_version(datas, filename, note=note)
        if self._zivo_auto_analyse_enabled():
            try:
                with self.env.cr.savepoint():
                    self.action_zivo_ai_analyse()
            except Exception as exc:  # noqa: BLE001
                _logger.info('AI document analysis skipped: %s', exc)
        return version


class ZivoEventDocumentVersion(models.Model):
    _inherit = 'zivo.event.document.version'

    def _zivo_previous(self):
        self.ensure_one()
        return self.document_id.version_ids.filtered(lambda v: v.version_number < self.version_number).sorted(
            'version_number')[-1:]

    def action_zivo_ai_explain_changes(self):
        self.ensure_one()
        service = self.env['zivo.ai.service']
        service._ensure_feature('document')
        previous = self._zivo_previous()
        if not previous:
            raise UserError(self.env._('This is the first version, there is nothing to compare.'))
        doc = self.document_id
        old_text, new_text = doc._zivo_text(previous), doc._zivo_text(self)
        if not old_text.strip() and not new_text.strip():
            raise UserError(self.env._('These files contain no readable text to compare.'))
        diff = '\n'.join(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(),
                                              'v%s' % previous.version_number, 'v%s' % self.version_number,
                                              lineterm='', n=1))[:MAX_CHARS]
        if not diff.strip():
            explanation = self.env._('No text changes were found between version %(a)s and %(b)s.',
                                     a=previous.version_number, b=self.version_number)
        else:
            explanation = service._zivo_ai_complete(
                'document', 'You explain document changes to busy event managers.',
                'Explain in plain language what changed between version %s and %s of "%s". Use 3-6 bullet points '
                '("- "), highlight money, dates and obligations.\nUnified diff:\n%s' % (
                    previous.version_number, self.version_number, doc.name, diff), record=doc)
        doc.message_post(body=Markup('<b>%s</b><br/>%s') % (
            self.env._('AI: changes v%(a)s → v%(b)s', a=previous.version_number, b=self.version_number),
            Markup('<br/>').join(escape(line) for line in str(explanation).splitlines())))
        self.sudo().write({'note': self.note or 'AI compared with v%s' % previous.version_number})
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'type': 'info', 'sticky': True,
                       'title': self.env._('What changed in v%s', self.version_number),
                       'message': str(explanation)[:1500]},
        }
