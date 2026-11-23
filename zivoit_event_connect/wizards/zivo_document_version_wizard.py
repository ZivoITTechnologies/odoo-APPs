# -*- coding: utf-8 -*-
from odoo import fields, models


class ZivoDocumentVersionWizard(models.TransientModel):
    _name = 'zivo.document.version.wizard'
    _description = 'Upload New Document Version'

    document_id = fields.Many2one('zivo.event.document', required=True)
    datas = fields.Binary(string='File', required=True)
    filename = fields.Char()
    note = fields.Char(string='What changed?')

    def action_upload(self):
        self.ensure_one()
        self.document_id._zivo_add_version(self.datas, self.filename or self.document_id.name, note=self.note)
        return {'type': 'ir.actions.act_window_close'}
