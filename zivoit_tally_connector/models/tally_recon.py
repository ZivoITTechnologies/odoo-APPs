# -*- coding: utf-8 -*-
"""Party-balance reconciliation between Odoo and Tally."""
from odoo import _, api, fields, models

from ..lib import tally_xml as TX


class TallyReconLine(models.Model):
    _name = 'tally.recon.line'
    _description = 'Tally reconciliation line'
    _order = 'abs_difference desc'

    connection_id = fields.Many2one('tally.connection', required=True, ondelete='cascade',
                                    index=True)
    company_id = fields.Many2one(related='connection_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    run_date = fields.Datetime(default=fields.Datetime.now)
    as_of = fields.Date()
    ledger = fields.Char('Tally ledger')
    partner_id = fields.Many2one('res.partner')
    tally_balance = fields.Monetary(help='Debit positive')
    odoo_balance = fields.Monetary(help='Debit positive')
    difference = fields.Monetary()
    abs_difference = fields.Monetary(store=True, compute='_compute_abs')
    status = fields.Selection([('match', 'Matched'), ('mismatch', 'Mismatch'),
                               ('only_tally', 'Only in Tally'), ('only_odoo', 'Only in Odoo')])
    hint = fields.Char()

    @api.depends('difference')
    def _compute_abs(self):
        for r in self:
            r.abs_difference = abs(r.difference)


class TallyConnectionRecon(models.Model):
    _inherit = 'tally.connection'

    def action_reconcile(self):
        for conn in self:
            if conn.transport == 'file':
                continue
            self.env['tally.request']._enqueue(conn, 'pull_balances',
                                               conn._build_pull('pull_balances'), priority=6)
            self.env['tally.request']._process_queue(conn)
        return {'type': 'ir.actions.act_window', 'name': _('Balance reconciliation'),
                'res_model': 'tally.recon.line', 'view_mode': 'list',
                'domain': [('connection_id', 'in', self.ids)]}

    def _odoo_partner_balances(self, as_of):
        aml = self.env['account.move.line']
        groups = aml._read_group(
            [('company_id', '=', self.company_id.id), ('parent_state', '=', 'posted'),
             ('date', '<=', as_of), ('partner_id', '!=', False),
             ('account_id.account_type', 'in', ('asset_receivable', 'liability_payable'))],
            ['partner_id'], ['balance:sum'])
        out = {}
        for partner, bal in groups:
            cp = partner.commercial_partner_id
            out[cp.id] = out.get(cp.id, 0.0) + bal
        return out

    def _on_pull_balances(self, req, text):
        rows = [TX.normalize_ledger(r) for r in TX.parse_collection(text, 'LEDGER')]
        as_of = fields.Date.today()
        Recon = self.env['tally.recon.line']
        Recon.search([('connection_id', '=', self.id)]).unlink()
        odoo = self._odoo_partner_balances(as_of)
        seen = set()
        vals = []
        tol = self.amount_tolerance or 1.0
        for led in rows:
            kind, _role = self._ledger_kind(led)
            if kind != 'party':
                continue
            m = self.env['tally.mapping']._find(self, 'party', tally_name=led['name'])
            tally_bal = -led['closing_balance']  # Tally: debit negative
            partner = m.partner_id.commercial_partner_id if m and m.partner_id else False
            o = odoo.get(partner.id, 0.0) if partner else 0.0
            if partner:
                seen.add(partner.id)
            diff = round(tally_bal - o, 2)
            status = 'only_tally' if not partner else ('match' if abs(diff) <= tol else 'mismatch')
            hint = ''
            if status == 'mismatch':
                hint = _('Odoo higher: unsynced Odoo vouchers?') if diff < 0 else \
                    _('Tally higher: vouchers not yet imported or opening balance missing?')
            vals.append({'connection_id': self.id, 'as_of': as_of, 'ledger': led['name'],
                         'partner_id': partner.id if partner else False,
                         'tally_balance': tally_bal, 'odoo_balance': o, 'difference': diff,
                         'status': status, 'hint': hint})
        for pid, bal in odoo.items():
            if pid not in seen and abs(bal) > tol:
                vals.append({'connection_id': self.id, 'as_of': as_of,
                             'partner_id': pid, 'odoo_balance': bal, 'tally_balance': 0.0,
                             'difference': -bal, 'status': 'only_odoo',
                             'hint': _('Party not present/mapped in Tally')})
        Recon.create(vals)
        req.records_in = len(rows)
