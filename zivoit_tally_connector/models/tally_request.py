# -*- coding: utf-8 -*-
"""Unified queue + audit log of every exchange with Tally.

The same record life-cycle serves all three transports:
  direct : pending -> (HTTP now) -> done/failed
  agent  : pending -> sent (checked out by agent) -> done/failed
  file   : pending -> exported in an XML bundle -> done (operator confirms)
"""
import logging
from datetime import timedelta

from odoo import _, api, fields, models

from ..lib import error_kb
from ..lib import tally_xml as TX
from ..lib.transport import TallyConnectionError

_logger = logging.getLogger(__name__)

PURPOSES = [
    ('test', 'Connection test'),
    ('pull_groups', 'Pull groups'),
    ('pull_vchtypes', 'Pull voucher types'),
    ('pull_ledgers', 'Pull ledgers'),
    ('pull_items', 'Pull stock items'),
    ('pull_vouchers', 'Pull vouchers'),
    ('pull_balances', 'Pull balances'),
    ('pull_costcentres', 'Pull cost centres'),
    ('pull_godowns', 'Pull godowns'),
    ('census_vouchers', 'Deletion check: vouchers'),
    ('census_ledgers', 'Deletion check: ledgers'),
    ('census_items', 'Deletion check: stock items'),
    ('push_unit', 'Push units'),
    ('push_godown', 'Push godowns'),
    ('push_costcat', 'Push cost categories'),
    ('push_costcentre', 'Push cost centres'),
    ('push_ledger', 'Push ledgers'),
    ('push_item', 'Push stock items'),
    ('push_move', 'Push voucher'),
    ('cancel_move', 'Cancel voucher'),
]
PULL_PURPOSES = ('test', 'pull_groups', 'pull_vchtypes', 'pull_ledgers', 'pull_items',
                 'pull_vouchers', 'pull_balances', 'pull_costcentres', 'pull_godowns',
                 'census_vouchers', 'census_ledgers', 'census_items')
MASTER_PUSH_PURPOSES = ('push_unit', 'push_godown', 'push_costcat', 'push_costcentre',
                        'push_ledger', 'push_item')
SENT_TIMEOUT_MIN = 10


class TallyRequest(models.Model):
    _name = 'tally.request'
    _description = 'Tally Request (queue & log)'
    _order = 'priority, id'
    _rec_name = 'display_label'

    connection_id = fields.Many2one('tally.connection', required=True, ondelete='cascade',
                                    index=True)
    company_id = fields.Many2one(related='connection_id.company_id', store=True)
    purpose = fields.Selection(PURPOSES, required=True, index=True)
    direction = fields.Selection([('in', 'Tally → Odoo'), ('out', 'Odoo → Tally')],
                                 compute='_compute_direction', store=True)
    display_label = fields.Char(compute='_compute_display_label')
    priority = fields.Integer(default=50, index=True)
    state = fields.Selection([('pending', 'Pending'), ('sent', 'With agent'),
                              ('done', 'Done'), ('failed', 'Failed'),
                              ('cancelled', 'Cancelled')], default='pending', index=True)
    res_model = fields.Char(index=True)
    res_ids = fields.Char(help='Comma separated record ids covered by this request.')
    request_xml = fields.Text()
    response_xml = fields.Text()
    attempts = fields.Integer()
    next_attempt = fields.Datetime(default=fields.Datetime.now, index=True)
    sent_at = fields.Datetime()
    done_at = fields.Datetime()
    duration_ms = fields.Integer()
    created = fields.Integer()
    altered = fields.Integer()
    errors = fields.Integer()
    records_in = fields.Integer('Objects received')
    error = fields.Text()
    ai_hint = fields.Text('AI diagnosis')
    error_code = fields.Char()
    auto_healed = fields.Boolean()
    window_from = fields.Date(help='Voucher period covered (census / pulls).')
    window_to = fields.Date()

    @api.depends('purpose')
    def _compute_direction(self):
        for r in self:
            r.direction = 'in' if r.purpose in PULL_PURPOSES else 'out'

    def _compute_display_label(self):
        labels = dict(PURPOSES)
        for r in self:
            r.display_label = '#%s %s' % (r.id or '', labels.get(r.purpose, r.purpose))

    # ------------------------------------------------------------------
    @api.model
    def _enqueue(self, conn, purpose, xml, res_model=None, res_ids=None, priority=50):
        return self.sudo().create({
            'connection_id': conn.id, 'purpose': purpose, 'request_xml': xml,
            'res_model': res_model, 'res_ids': ','.join(str(i) for i in (res_ids or [])),
            'priority': priority,
        })

    def _get_res_ids(self):
        return [int(i) for i in (self.res_ids or '').split(',') if i]

    @api.model
    def _ready_domain(self, conn):
        return [('connection_id', '=', conn.id), ('state', '=', 'pending'),
                ('next_attempt', '<=', fields.Datetime.now())]

    @api.model
    def _process_queue(self, conn, limit=500):
        """Execute pending requests for a DIRECT connection, masters before vouchers."""
        if conn.transport != 'direct':
            return
        for _round in range(4):  # auto-heal may enqueue masters + retry vouchers
            reqs = self.search(self._ready_domain(conn), limit=limit)
            if not reqs:
                break
            for req in reqs:
                if req.state != 'pending':
                    continue
                if req.purpose == 'push_move' and not conn._refresh_move_request(req):
                    continue
                req._execute_direct()
        conn.last_push_date = fields.Datetime.now()

    def _execute_direct(self):
        self.ensure_one()
        conn = self.connection_id
        start = fields.Datetime.now()
        self.write({'state': 'sent', 'sent_at': start, 'attempts': self.attempts + 1})
        try:
            raw = conn._post_direct(self.request_xml)
        except TallyConnectionError as e:
            self._mark_failed(str(e), retry=True)
            return
        self._handle_response(raw)

    @api.model
    def _agent_checkout(self, conn, limit=20):
        # recover jobs an agent took but never answered
        stale = fields.Datetime.now() - timedelta(minutes=SENT_TIMEOUT_MIN)
        self.search([('connection_id', '=', conn.id), ('state', '=', 'sent'),
                     ('sent_at', '<', stale)]).write({'state': 'pending'})
        jobs = self.search(self._ready_domain(conn), limit=limit)
        # Masters first: never hand out vouchers while masters are still queued.
        masters = jobs.filtered(lambda j: j.priority < 50)
        if masters:
            jobs = masters
        else:
            jobs = jobs.filtered(lambda j: j.purpose != 'push_move'
                                 or conn._refresh_move_request(j))
            if self.search_count(self._ready_domain(conn) + [('priority', '<', 50)]):
                return self.browse()  # newly required masters go first
        for j in jobs:
            j.write({'state': 'sent', 'sent_at': fields.Datetime.now(),
                     'attempts': j.attempts + 1})
        return jobs

    # ------------------------------------------------------------------
    def _handle_response(self, raw):
        self.ensure_one()
        if isinstance(raw, bytes):
            text = TX.sanitize_response(raw)
        else:
            text = raw or ''
        vals = {'response_xml': text[:200000], 'done_at': fields.Datetime.now()}
        if self.sent_at:
            vals['duration_ms'] = int((fields.Datetime.now() - self.sent_at).total_seconds() * 1000)
        self.write(vals)
        conn = self.connection_id
        handler = getattr(conn, '_on_%s' % self.purpose, None)
        try:
            with self.env.cr.savepoint():
                if self.direction == 'out':
                    res = TX.parse_import_response(text)
                    self.write({'created': res['created'], 'altered': res['altered'],
                                'errors': res['errors']})
                    if handler:
                        handler(self, res)
                    if not res['ok']:
                        self._mark_failed('\n'.join(res['line_errors']) or _('Tally import error'),
                                          retry=False, import_result=res)
                        return
                elif handler:
                    handler(self, text)
            self.write({'state': 'done', 'error': False})
        except (TX.TallyProtocolError, ValueError) as e:
            self._mark_failed(str(e), retry=self.purpose in PULL_PURPOSES
                              and not self.purpose.startswith('census'))
        except Exception as e:  # keep the queue alive, but log loudly
            _logger.exception('Tally handler %s failed', self.purpose)
            self._mark_failed('%s: %s' % (type(e).__name__, e), retry=False)

    def _mark_failed(self, message, retry=False, import_result=None):
        self.ensure_one()
        conn = self.connection_id
        code, hint, arg = error_kb.diagnose(message, conn.tally_company)
        vals = {'error': message, 'error_code': code, 'ai_hint': hint or False}
        if retry and self.attempts < conn.max_attempts:
            delay = min(60, 2 ** self.attempts)  # exponential back-off in minutes
            vals.update(state='pending', next_attempt=fields.Datetime.now() + timedelta(minutes=delay))
        else:
            vals['state'] = 'failed'
        self.write(vals)
        if vals['state'] == 'failed':
            if conn.auto_heal and self.direction == 'out' and not self.auto_healed:
                healed = conn._auto_heal(self, code, arg, message)
                if healed:
                    return
            if not hint and conn.ai_provider != 'none':
                self.ai_hint = conn._ai_explain_error(message, self)
            self._flag_records(message)

    def _flag_records(self, message):
        if self.res_model == 'account.move':
            moves = self.env['account.move'].browse(self._get_res_ids()).exists()
            moves.write({'tally_state': 'error', 'tally_error': message})

    # ------------------------------------------------------------------
    def action_retry(self):
        self.filtered(lambda r: r.state in ('failed', 'cancelled', 'pending')).write(
            {'state': 'pending', 'next_attempt': fields.Datetime.now(), 'attempts': 0})
        for conn in self.mapped('connection_id'):
            self._process_queue(conn)
        return True

    def action_cancel(self):
        self.filtered(lambda r: r.state in ('pending', 'failed')).write({'state': 'cancelled'})
        return True

    def action_ai_explain(self):
        for r in self.filtered('error'):
            r.ai_hint = r.connection_id._ai_explain_error(r.error, r, force=True)
        return True

    @api.model
    def _gc(self, days=30):
        limit = fields.Datetime.now() - timedelta(days=days)
        self.search([('state', 'in', ('done', 'cancelled')), ('write_date', '<', limit),
                     ('purpose', 'in', ('test', 'pull_groups', 'pull_vchtypes',
                                        'pull_ledgers', 'pull_items', 'pull_balances'))]).unlink()
        self.search([('state', 'in', ('done',)), ('write_date', '<', limit)]).write(
            {'request_xml': False, 'response_xml': False})
