# -*- coding: utf-8 -*-
"""Deletion detection.

Tally never exports deleted objects, so incremental (AlterID) pulls cannot see a
deletion.  A periodic lightweight *census* exports only identifiers (GUID,
REMOTEID, name) for the sync window and compares them with what Odoo knows:

* Tally-originated vouchers that vanished -> staging record marked deleted and the
  connection's conflict policy applied (Tally wins = cancel in Odoo).
* Odoo-originated vouchers that vanished -> invoice flagged; Odoo wins = re-pushed.
* Masters (ledgers, stock items) that vanished -> mapping marked deleted; Odoo
  records are never deleted.  Odoo wins = master re-created in Tally.

A mass-deletion guard refuses to apply anything when an implausible share of
objects disappears at once (wrong company open, backup restored, ...).
"""
import logging
from datetime import timedelta

from odoo import _, fields, models

from ..lib import tally_xml as TX

_logger = logging.getLogger(__name__)

CENSUS_FETCH = {
    'Voucher': ['GUID', 'MasterID', 'AlterID', 'Date', 'VoucherNumber', 'VoucherTypeName',
                'IsCancelled'],
    'Ledger': ['GUID', 'MasterID', 'Name', 'Parent'],
    'StockItem': ['GUID', 'MasterID', 'Name'],
}


class TallyConnectionCensus(models.Model):
    _inherit = 'tally.connection'

    def _census_window(self):
        date_from = self.pull_from_date or fields.Date.today().replace(month=4, day=1)
        return date_from, fields.Date.today() + timedelta(days=366)

    def _queue_census(self, only_if_due=False):
        self.ensure_one()
        if self.transport == 'file' or not self.detect_deletions:
            return
        Req = self.env['tally.request']
        if Req.search_count([('connection_id', '=', self.id), ('state', 'in', ('pending', 'sent')),
                             ('purpose', 'in', ('census_vouchers', 'census_ledgers',
                                                'census_items'))]):
            return
        if only_if_due and self.last_census_date and self.last_census_date > \
                fields.Datetime.now() - timedelta(hours=max(1, self.census_interval_hours or 6)):
            return
        date_from, date_to = self._census_window()
        req = Req._enqueue(self, 'census_vouchers', TX.build_collection_request(
            'Voucher', company=self.tally_company, from_date=date_from, to_date=date_to,
            name='ZivoCensusVch', fetch=CENSUS_FETCH['Voucher']), priority=7)
        req.write({'window_from': date_from, 'window_to': date_to})
        Req._enqueue(self, 'census_ledgers', TX.build_collection_request(
            'Ledger', company=self.tally_company, name='ZivoCensusLed',
            fetch=CENSUS_FETCH['Ledger']), priority=7)
        if self.sync_items != 'none':
            Req._enqueue(self, 'census_items', TX.build_collection_request(
                'StockItem', company=self.tally_company, name='ZivoCensusItem',
                fetch=CENSUS_FETCH['StockItem']), priority=7)
        self.last_census_date = fields.Datetime.now()

    def action_detect_deletions(self):
        for conn in self:
            conn.last_census_date = False
            conn._queue_census()
            self.env['tally.request']._process_queue(conn)
        return self._notify(_('Deletion check finished. See Vouchers from Tally / Mappings '
                              '(status "Deleted in Tally").'), 'success')

    # ------------------------------------------------------------------
    def _census_guard(self, req, known, missing, what):
        """Raise ValueError (=> request failed, nothing applied) on implausible deletions."""
        if not known or not missing:
            return
        pct = 100.0 * len(missing) / len(known)
        if len(known) >= 5 and pct > (self.deletion_guard_pct or 30):
            msg = _('Mass-deletion guard: %(m)s of %(k)s %(w)s (%(p).0f%%) seem deleted in Tally. '
                    'Nothing was changed. Check that the right Tally company is open, then run '
                    '"Detect deletions" again or raise the guard on the connection.') % {
                'm': len(missing), 'k': len(known), 'w': what, 'p': pct}
            raise ValueError(msg)

    def _on_census_vouchers(self, req, text):
        rows = TX.parse_collection(text, 'VOUCHER')
        present = set()
        for r in rows:
            present.add(r.get('guid') or '')
            present.add(r.get('@remoteid') or '')
        present.discard('')
        req.records_in = len(rows)
        date_from, date_to = req.window_from, req.window_to
        dom_window = [('date', '>=', date_from), ('date', '<=', date_to)] if date_from else []

        stages = self.env['tally.voucher'].search(
            [('connection_id', '=', self.id), ('guid', '!=', False),
             ('state', 'not in', ('deleted', 'skipped', 'cancelled'))] + dom_window)
        moves = self.env['account.move'].search(
            [('tally_connection_id', '=', self.id), ('tally_guid', '!=', False),
             ('tally_state', 'in', ('synced', 'modified')), ('state', '!=', 'cancel')] +
            dom_window)
        gone_stages = stages.filtered(lambda s: s.guid not in present)
        gone_moves = moves.filtered(lambda m: m.tally_guid not in present)
        self._census_guard(req, stages, gone_stages, _('Tally vouchers'))
        self._census_guard(req, moves, gone_moves, _('vouchers pushed from Odoo'))
        gone_stages._mark_deleted_in_tally()
        self._handle_odoo_moves_deleted(gone_moves)
        req.altered = len(gone_stages) + len(gone_moves)

    def _handle_odoo_moves_deleted(self, moves):
        policy = self.conflict_policy
        for mv in moves:
            mv.message_post(body=_('This document was deleted in Tally (company %s).')
                            % self.tally_company)
            if policy == 'odoo':
                mv.write({'tally_guid': False, 'tally_state': 'deleted'})
                self._push_moves(mv, force=True)
            elif policy == 'tally':
                mv.write({'tally_state': 'deleted'})
                m = mv.with_context(tally_no_push=True)
                m.button_draft()
                m.button_cancel()
            else:
                mv.write({'tally_state': 'deleted', 'tally_error': _(
                    'Deleted in Tally. Use "Push to Tally" to re-create it there, or cancel it '
                    'in Odoo.')})

    def _census_masters(self, req, text, tag, kinds):
        rows = TX.parse_collection(text, tag)
        names, guids = set(), set()
        for r in rows:
            names.add(((r.get('@name') or TX._list_name(r)) or '').lower())
            if r.get('guid'):
                guids.add(r['guid'])
        req.records_in = len(rows)
        maps = self.env['tally.mapping'].search([
            ('connection_id', '=', self.id), ('kind', 'in', kinds),
            ('synced_to_tally', '=', True), ('state', 'not in', ('deleted', 'ignored'))])
        gone = maps.filtered(lambda m: m.tally_name.lower() not in names
                             and (not m.tally_guid or m.tally_guid not in guids)
                             and m.tally_name != 'Primary Cost Category')
        self._census_guard(req, maps, gone, _('masters'))
        repush_partners, repush_products = self.env['res.partner'], self.env['product.product']
        for m in gone:
            m.write({'state': 'deleted', 'synced_to_tally': False, 'tally_guid': False,
                     'match_reason': _('Deleted in Tally on %s') % fields.Date.today()})
            if self.conflict_policy == 'odoo':
                m.write({'state': 'confirmed', 'origin': 'odoo'})
                if m.kind == 'party' and m.partner_id:
                    repush_partners |= m.partner_id
                elif m.kind == 'item' and m.product_id:
                    repush_products |= m.product_id
        if repush_partners or repush_products:
            self._push_masters(partners=repush_partners, products=repush_products)
        req.altered = len(gone)

    def _on_census_ledgers(self, req, text):
        self._census_masters(req, text, 'LEDGER', ('party', 'ledger', 'tax', 'bank'))

    def _on_census_items(self, req, text):
        self._census_masters(req, text, 'STOCKITEM', ('item',))
