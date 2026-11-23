# -*- coding: utf-8 -*-
import base64

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import ZivoCommon


def b64(text):
    return base64.b64encode(text.encode()).decode()


@tagged('post_install', '-at_install', 'zivo_event_connect')
class TestDocumentsAudit(ZivoCommon, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._zivo_setup()

    def test_versioning(self):
        doc = self.env['zivo.event.document'].with_user(self.user_event).create({
            'name': 'Venue contract', 'event_id': self.ev_open.id, 'category': 'contract',
            'datas': b64('v1 content'), 'filename': 'contract.pdf'})
        self.assertEqual(doc.version, 1)
        self.assertEqual(doc.mimetype, 'application/pdf')
        self.assertEqual(doc.file_size, len('v1 content'))
        self.assertEqual(self.ev_open.zivo_document_count, 1)
        self.assertIn(self.ev_open.name, doc.folder_name)
        wizard = self.env['zivo.document.version.wizard'].with_user(self.user_event).create({
            'document_id': doc.id, 'datas': b64('v2 content!'), 'filename': 'contract-signed.pdf', 'note': 'Signed'})
        wizard.action_upload()
        self.assertEqual(doc.version, 2)
        self.assertEqual(doc.filename, 'contract-signed.pdf')
        self.assertEqual(base64.b64decode(doc.datas), b'v2 content!')
        # identical upload does not create a version
        doc._zivo_add_version(b64('v2 content!'), 'same.pdf')
        self.assertEqual(doc.version_count, 2)
        # restore v1 as v3
        v1 = doc.version_ids.filtered(lambda v: v.version_number == 1)
        v1.action_restore()
        self.assertEqual(doc.version, 3)
        self.assertEqual(base64.b64decode(doc.datas), b'v1 content')
        # versions are immutable
        with self.assertRaises(UserError):
            v1.with_user(self.user_event).write({'filename': 'hack.pdf'})
        v1.with_user(self.user_event).write({'note': 'Original'})
        actions = self.env['zivo.audit.log'].search([('model', '=', 'zivo.event.document'), ('res_id', '=', doc.id)]).mapped('action')
        self.assertIn('create', actions)
        self.assertEqual(actions.count('version'), 3)
        action = v1.action_download()
        self.assertIn('/web/content/zivo.event.document.version/%s/datas' % v1.id, action['url'])
        self.assertIn('download', self.env['zivo.audit.log'].search(
            [('model', '=', 'zivo.event.document'), ('res_id', '=', doc.id)]).mapped('action'))

    def test_document_write_through_form(self):
        doc = self.env['zivo.event.document'].create({'name': 'Agenda', 'event_id': self.ev_open.id,
                                                      'datas': b64('a'), 'filename': 'agenda.docx'})
        doc.write({'datas': b64('b'), 'filename': 'agenda-v2.docx'})
        self.assertEqual(doc.version, 2)
        self.assertEqual(doc.current_version_id.filename, 'agenda-v2.docx')

    def test_event_audit_old_new(self):
        ev = self.ev_open
        ev.with_user(self.user_event).write({'name': 'ZEC Open Day 2', 'zivo_access_mode': 'invite'})
        log = self.env['zivo.audit.log'].search([('model', '=', 'event.event'), ('res_id', '=', ev.id),
                                                 ('action', '=', 'write')], limit=1)
        self.assertEqual(log.user_id, self.user_event)
        self.assertIn('ZEC Open Day → ZEC Open Day 2', log.changes)
        self.assertIn('Invitation Only', log.changes)
        self.assertTrue(log.changes_json)
        self.assertGreaterEqual(ev.zivo_audit_count, 2)
        # untracked field: no log
        count = self.env['zivo.audit.log'].search_count([('model', '=', 'event.event'), ('res_id', '=', ev.id)])
        ev.note = 'just a note'
        self.assertEqual(self.env['zivo.audit.log'].search_count(
            [('model', '=', 'event.event'), ('res_id', '=', ev.id)]), count)
        # m2m change
        ev.zivo_department_ids = [(4, self.dept_hr.id)]
        log = self.env['zivo.audit.log'].search([('model', '=', 'event.event'), ('res_id', '=', ev.id)], limit=1)
        self.assertIn('Human Resources', log.changes)

    def test_audit_unlink_and_readonly(self):
        doc = self.env['zivo.event.document'].create({'name': 'Tmp', 'event_id': self.ev_open.id})
        doc_id = doc.id
        doc.unlink()
        log = self.env['zivo.audit.log'].search([('model', '=', 'zivo.event.document'), ('res_id', '=', doc_id),
                                                 ('action', '=', 'unlink')])
        self.assertTrue(log)
        self.assertFalse(log.action_open_record())
        from odoo.exceptions import AccessError
        with self.assertRaises(AccessError):
            log.with_user(self.user_event).write({'changes': 'tampered'})
        with self.assertRaises(AccessError):
            log.with_user(self.user_event).unlink()
