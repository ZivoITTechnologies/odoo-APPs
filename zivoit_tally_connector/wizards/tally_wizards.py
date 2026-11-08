# -*- coding: utf-8 -*-
import base64
import io
import json
import re
import zipfile

from odoo import _, fields, models
from odoo.exceptions import UserError
from odoo.tools import file_open

from ..lib import tally_xml as TX


class TallyFileImport(models.TransientModel):
    """Offline mode: import an XML exported from TallyPrime (masters and/or Day Book)."""
    _name = 'tally.file.import'
    _description = 'Import Tally XML file'

    connection_id = fields.Many2one('tally.connection', required=True)
    file = fields.Binary(required=True)
    filename = fields.Char()
    summary = fields.Text(readonly=True)

    def action_import(self):
        self.ensure_one()
        raw = base64.b64decode(self.file or b'')
        if self.filename and self.filename.lower().endswith('.zip'):
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                payloads = [zf.read(n) for n in zf.namelist() if n.lower().endswith('.xml')]
        else:
            payloads = [raw]
        conn = self.connection_id
        Req = self.env['tally.request']
        lines = []
        for payload in payloads:
            text = TX.sanitize_response(payload)
            for purpose, tag in (('pull_groups', 'GROUP'), ('pull_vchtypes', 'VOUCHERTYPE'), ('pull_ledgers', 'LEDGER'),
                                 ('pull_items', 'STOCKITEM'), ('pull_vouchers', 'VOUCHER')):
                if not re.search(r'<%s[\s>]' % tag, text):
                    continue
                req = Req._enqueue(conn, purpose, '<!-- file import: %s -->' % (self.filename or ''))
                req.write({'state': 'sent', 'sent_at': fields.Datetime.now()})
                req._handle_response(text)
                lines.append('%s: %s objects (%s)' % (dict(req._fields['purpose'].selection)[
                    purpose], req.records_in, req.state))
        self.env['tally.voucher'].search([('connection_id', '=', conn.id),
                                          ('state', '=', 'draft')])._process()
        if not lines:
            raise UserError(_('No Tally ledgers, stock items or vouchers found in this file. '
                              'Export from TallyPrime with Format = XML (Data Interchange).'))
        self.summary = '\n'.join(lines)
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new'}


class TallyFileExport(models.TransientModel):
    """Offline mode: bundle queued Odoo to Tally requests into Tally-importable XML files."""
    _name = 'tally.file.export'
    _description = 'Export XML for Tally import'

    connection_id = fields.Many2one('tally.connection', required=True)
    include_failed = fields.Boolean()
    mark_done = fields.Boolean('Mark as delivered', default=True)
    file = fields.Binary(readonly=True)
    filename = fields.Char(readonly=True)
    summary = fields.Text(readonly=True)

    def action_export(self):
        self.ensure_one()
        conn = self.connection_id
        states = ['pending'] + (['failed'] if self.include_failed else [])
        reqs = self.env['tally.request'].search([('connection_id', '=', conn.id),
                                                 ('direction', '=', 'out'),
                                                 ('state', 'in', states)])
        if not reqs:
            raise UserError(_('Nothing to export: no queued Odoo to Tally documents.'))
        masters = reqs.filtered(lambda r: r.purpose not in ('push_move', 'cancel_move'))
        vouchers = reqs - masters

        def merge(rs, report):
            msgs = []
            for r in rs.sorted('priority'):
                body = r.request_xml or ''
                start, end = body.find('<REQUESTDATA>'), body.rfind('</REQUESTDATA>')
                if start >= 0 and end > start:
                    msgs.append(body[start + len('<REQUESTDATA>'):end])
            return TX.build_import_envelope(conn.tally_company, report, msgs)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            if masters:
                zf.writestr('1_masters.xml', merge(masters, 'All Masters'))
            if vouchers:
                zf.writestr('2_vouchers.xml', merge(vouchers, 'Vouchers'))
            zf.writestr('README.txt', _(
                'Import into TallyPrime: Gateway of Tally > Import > Masters (1_masters.xml), '
                'then Import > Transactions (2_vouchers.xml). Behaviour: "Combine" for masters.'))
        self.write({'file': base64.b64encode(buf.getvalue()),
                    'filename': 'tally_import_%s.zip' % fields.Date.today(),
                    'summary': _('%(m)s master batches and %(v)s vouchers exported.') % {
                        'm': len(masters), 'v': len(vouchers)}})
        if self.mark_done:
            ok = {'ok': True, 'line_errors': [], 'created': 0, 'altered': 0, 'errors': 0,
                  'last_vch_id': 0}
            for r in reqs:
                handler = getattr(conn, '_on_%s' % r.purpose, None)
                if handler:
                    handler(r, ok)
            reqs.write({'state': 'done', 'done_at': fields.Datetime.now()})
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new'}


class TallyMigration(models.TransientModel):
    """Historical migration in either direction, with a dry run."""
    _name = 'tally.migration'
    _description = 'Tally historical migration'

    connection_id = fields.Many2one('tally.connection', required=True)
    direction = fields.Selection([('pull', 'Tally to Odoo'), ('push', 'Odoo to Tally')],
                                 required=True, default='pull')
    date_from = fields.Date(required=True)
    date_to = fields.Date(required=True, default=fields.Date.today)
    include_masters = fields.Boolean(default=True)
    dry_run = fields.Boolean(default=True)
    summary = fields.Text(readonly=True)

    def action_run(self):
        self.ensure_one()
        conn = self.connection_id
        if self.date_from > self.date_to:
            raise UserError(_('Start date must be before end date.'))
        Req = self.env['tally.request']
        if self.direction == 'push':
            moves = self.env['account.move'].search([
                ('company_id', '=', conn.company_id.id), ('state', '=', 'posted'),
                ('date', '>=', self.date_from), ('date', '<=', self.date_to),
                ('tally_state', 'not in', ('imported', 'synced'))], order='date, id')
            if self.dry_run:
                kinds = {}
                for m in moves:
                    k = conn._move_kind(m)
                    kinds[k] = kinds.get(k, 0) + 1
                self.summary = _('%(n)s posted documents would be pushed: %(k)s') % {
                    'n': len(moves), 'k': json.dumps(kinds)}
            else:
                conn._push_moves(moves, force=True)
                Req._process_queue(conn)
                self.summary = _('%s documents queued for Tally.') % len(moves)
        else:
            if conn.transport == 'file':
                raise UserError(_('Use "Import Tally XML" in offline mode.'))
            if self.include_masters:
                for purpose in ('pull_vchtypes', 'pull_ledgers', 'pull_items'):
                    Req._enqueue(conn, purpose, conn._build_pull(purpose, full=True), priority=3)
            Req._enqueue(conn, 'pull_vouchers', conn._build_pull(
                'pull_vouchers', full=True, from_date=self.date_from, to_date=self.date_to),
                priority=5)
            Req._process_queue(conn)
            staged = self.env['tally.voucher'].search([('connection_id', '=', conn.id),
                                                       ('state', '=', 'draft'),
                                                       ('date', '>=', self.date_from),
                                                       ('date', '<=', self.date_to)])
            if self.dry_run:
                self.summary = _('%s vouchers staged for review (not converted). Untick dry '
                                 'run to convert them.') % len(staged)
            else:
                staged._process()
                done = staged.filtered(lambda s: s.state == 'done')
                self.summary = _('%(d)s of %(n)s vouchers imported. %(r)s need review.') % {
                    'd': len(done), 'n': len(staged), 'r': len(staged) - len(done)}
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new'}


class TallyAgentSetup(models.TransientModel):
    """Generates the bridge agent package with a ready-to-run config."""
    _name = 'tally.agent.setup'
    _description = 'Bridge agent setup'

    connection_id = fields.Many2one('tally.connection', required=True)
    odoo_url = fields.Char(default=lambda s: s.env['ir.config_parameter'].sudo().get_param(
        'web.base.url'))
    database = fields.Char(default=lambda s: s.env.cr.dbname)
    api_user_id = fields.Many2one('res.users', 'Agent API user',
                                  help='Internal user in group "Tally Bridge Agent". Create an '
                                       'API key for it (Preferences > Account Security).')
    file = fields.Binary(readonly=True)
    filename = fields.Char(readonly=True)

    def action_build(self):
        self.ensure_one()
        conn = self.connection_id.sudo()
        if not conn.agent_token:
            conn.action_generate_agent_token()
        cfg = {'odoo_url': (self.odoo_url or '').rstrip('/'), 'database': self.database,
               'api_key': 'PASTE-API-KEY-OF-%s' % (self.api_user_id.login or 'AGENT-USER'),
               'connection_token': conn.agent_token, 'tally_host': conn.host or 'localhost',
               'tally_port': conn.port or 9000, 'poll_seconds': 15}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for name in ('zivoit_tally_agent.py', 'README_AGENT.md', 'install_windows_service.bat'):
                with file_open('zivoit_tally_connector/agent/%s' % name, 'rb') as f:
                    zf.writestr(name, f.read())
            zf.writestr('agent_config.json', json.dumps(cfg, indent=2))
        self.write({'file': base64.b64encode(buf.getvalue()),
                    'filename': 'zivoit_tally_agent_%s.zip' % conn.id})
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new'}
