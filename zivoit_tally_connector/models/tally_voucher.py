# -*- coding: utf-8 -*-
"""Staging area for vouchers received from Tally, and their conversion to Odoo moves."""
import json
import logging

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_is_zero

from ..lib import anomaly

_logger = logging.getLogger(__name__)

KINDS = [('sale', 'Sales'), ('purchase', 'Purchase'), ('credit_note', 'Credit Note'),
         ('debit_note', 'Debit Note'), ('receipt', 'Receipt'), ('payment', 'Payment'),
         ('journal', 'Journal'), ('contra', 'Contra')]
MOVE_TYPE = {'sale': 'out_invoice', 'purchase': 'in_invoice', 'credit_note': 'out_refund',
             'debit_note': 'in_refund'}


class TallyVoucher(models.Model):
    _name = 'tally.voucher'
    _description = 'Voucher received from Tally'
    _inherit = ['mail.thread']
    _order = 'date desc, id desc'
    _rec_name = 'number'

    connection_id = fields.Many2one('tally.connection', required=True, ondelete='cascade',
                                    index=True)
    company_id = fields.Many2one(related='connection_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    guid = fields.Char(index=True, readonly=True)
    master_id = fields.Integer(readonly=True)
    alter_id = fields.Integer(readonly=True)
    date = fields.Date()
    vch_type = fields.Char('Tally voucher type')
    kind = fields.Selection(KINDS, required=True)
    number = fields.Char()
    reference = fields.Char()
    party_name = fields.Char('Party (Tally)')
    partner_id = fields.Many2one('res.partner')
    party_gstin = fields.Char()
    place_of_supply = fields.Char()
    narration = fields.Text()
    amount_total = fields.Monetary()
    cancelled = fields.Boolean()
    irn = fields.Char('IRN')
    state = fields.Selection([('draft', 'To process'), ('done', 'Imported'),
                              ('anomaly', 'Held by AI guard'), ('conflict', 'Conflict'),
                              ('error', 'Needs attention'), ('skipped', 'Skipped'),
                              ('deleted', 'Deleted in Tally'),
                              ('cancelled', 'Cancelled')], default='draft', index=True,
                             tracking=True)
    import_mode = fields.Selection([('invoice', 'Invoice/Bill'), ('entry', 'Journal entry')])
    move_id = fields.Many2one('account.move', readonly=True, index=True)
    payload = fields.Text(readonly=True)
    message = fields.Text()
    line_ids = fields.One2many('tally.voucher.line', 'voucher_id')
    findings = fields.Text()
    findings_html = fields.Html(compute='_compute_findings_html', sanitize=False)
    risk_score = fields.Integer()
    ai_summary = fields.Text('AI summary')

    _guid_uniq = models.Constraint('unique(connection_id, guid)', 'Tally voucher already staged.')

    def _compute_findings_html(self):
        colors = {'block': 'danger', 'warning': 'warning', 'info': 'info'}
        for v in self:
            items = json.loads(v.findings or '[]')
            if not items:
                v.findings_html = False
                continue
            v.findings_html = Markup('').join(
                Markup('<div class="alert alert-%s py-1 my-1" role="status">%s</div>') % (
                    colors.get(f['severity'], 'info'), escape(f['message'])) for f in items)

    # ------------------------------------------------------------------
    @api.model
    def _upsert_from_tally(self, conn, v, kind):
        stage = self.search([('connection_id', '=', conn.id), ('guid', '=', v['guid'])], limit=1)
        vals = {
            'connection_id': conn.id, 'guid': v['guid'], 'master_id': v['master_id'],
            'alter_id': v['alter_id'], 'date': v['date'], 'vch_type': v['vch_type'],
            'kind': kind, 'number': v['number'], 'reference': v['reference'],
            'party_name': v['party'], 'party_gstin': v['party_gstin'],
            'place_of_supply': v['place_of_supply'], 'narration': v['narration'],
            'cancelled': v['cancelled'], 'irn': v.get('irn'),
            'payload': json.dumps(v, default=str),
            'amount_total': self._total_of(v),
            'line_ids': [(5, 0, 0)] + [(0, 0, {
                'line_type': 'ledger', 'ledger': le['ledger'], 'amount': le['amount'],
                'is_party': le['is_party'],
                'bills': ', '.join('%s %s' % (b['type'], b['name']) for b in le['bills'])})
                for le in v['ledger_entries']] + [(0, 0, {
                    'line_type': 'item', 'item': ie['item'], 'ledger': ie['ledger'],
                    'qty': ie['qty'], 'unit': ie['unit'], 'rate': ie['rate'],
                    'discount': ie['discount'], 'amount': ie['amount']})
                for ie in v['inventory_entries']],
        }
        if not stage:
            if v['cancelled']:
                vals['state'] = 'skipped'
            return self.create(vals)
        if v['alter_id'] <= stage.alter_id:
            return stage
        if stage.move_id and stage.state == 'done':
            policy = conn.conflict_policy
            if policy == 'odoo':
                stage.write({'alter_id': v['alter_id']})
                return stage
            vals['state'] = 'conflict' if policy == 'manual' else 'draft'
            vals['message'] = _('Tally altered this voucher (AlterID %(a)s) after it was '
                                'imported into %(m)s.') % {'a': v['alter_id'],
                                                          'm': stage.move_id.name}
            stage.write(vals)
            if policy == 'tally':
                stage._replace_move()
            return stage
        vals['state'] = 'draft'
        stage.write(vals)
        return stage

    @staticmethod
    def _total_of(v):
        party = [le['amount'] for le in v['ledger_entries'] if le['is_party']]
        if party:
            return round(abs(sum(party)), 2)
        return round(sum(-le['amount'] for le in v['ledger_entries'] if le['amount'] < 0), 2)

    def _data(self):
        return json.loads(self.payload or '{}')

    # ------------------------------------------------------------------
    def action_process(self):
        self.filtered(lambda v: v.state in ('draft', 'error', 'anomaly'))._process(force_post=True)
        return True

    def action_apply_tally_version(self):
        for v in self.filtered(lambda v: v.state == 'conflict'):
            v._replace_move()
        return True

    def action_keep_odoo(self):
        self.filtered(lambda v: v.state == 'conflict').write({'state': 'done', 'message': False})
        return True

    def action_view_move(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'account.move',
                'res_id': self.move_id.id, 'view_mode': 'form'}

    def action_skip(self):
        self.write({'state': 'skipped'})
        return True

    def action_ai_review(self):
        for v in self:
            v.ai_summary = v.connection_id._ai_review_voucher(v)
        return True

    def _replace_move(self):
        self.ensure_one()
        old = self.move_id.with_context(tally_no_push=True)
        if old:
            self._reverse_stock(old)
            if old.state == 'posted':
                old.button_draft()
            old.button_cancel()  # keep the audit trail; never delete accounting data
            old.message_post(body=_('Cancelled: replaced by the altered Tally voucher %s.')
                             % self.number)
            if self.cancelled:
                self.write({'state': 'cancelled'})
                return
        self.write({'move_id': False, 'state': 'draft'})
        self._process()

    def _process(self, force_post=False):
        for v in self:
            if v.cancelled:
                v.state = 'cancelled' if v.move_id else 'skipped'
                if v.move_id and v.move_id.state == 'posted':
                    move = v.move_id.with_context(tally_no_push=True)
                    v._reverse_stock(move)
                    move.button_draft()
                    move.button_cancel()
                continue
            try:
                with self.env.cr.savepoint():
                    v._build_move(force_post=force_post)
            except UserError as e:
                v.write({'state': 'error', 'message': str(e)})
            except Exception as e:  # noqa: BLE001 - surfaced to the user
                _logger.exception('Tally voucher %s failed', v.number)
                v.write({'state': 'error', 'message': '%s: %s' % (type(e).__name__, e)})

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Cost centres -> analytic distribution
    # ------------------------------------------------------------------
    def _distribution(self, cost_centres, base_amount):
        conn = self.connection_id
        if not cost_centres or conn.sync_cost_centres == 'none' or not base_amount:
            return False
        Map = self.env['tally.mapping']
        dist, missing = {}, []
        for cc in cost_centres:
            m = Map._find(conn, 'costcentre', tally_name=cc['name'])
            if not m or not m.analytic_account_id or m.state == 'ignored':
                missing.append(cc['name'])
                continue
            key = str(m.analytic_account_id.id)
            pct = round(abs(cc['amount']) / abs(base_amount) * 100.0, 4)
            dist[key] = round(dist.get(key, 0.0) + pct, 4)
        if missing:
            raise UserError(_('Unmapped Tally cost centres: %s. Map them to analytic accounts '
                              'in Tally → Mappings.') % ', '.join(sorted(set(missing))))
        return dist or False

    # ------------------------------------------------------------------
    # Hooks extended by the stock bridge
    # ------------------------------------------------------------------
    def _after_import(self, move, data):
        """Called once the imported move is posted (stock bridge creates pickings)."""
        return True

    def _reverse_stock(self, move):
        """Called before an imported move is cancelled/replaced."""
        return True

    # ------------------------------------------------------------------
    # Deletions detected in Tally
    # ------------------------------------------------------------------
    def _mark_deleted_in_tally(self):
        for v in self:
            policy = v.connection_id.conflict_policy
            msg = _('Voucher %s was deleted in Tally.') % v.number
            if policy == 'tally':
                v._apply_tally_deletion()
            elif policy == 'odoo':
                v.write({'message': msg + ' ' + _('Kept in Odoo (policy: Odoo wins).')})
            else:
                v.write({'state': 'deleted', 'message': msg})
            v.message_post(body=msg)

    def _apply_tally_deletion(self):
        for v in self:
            move = v.move_id.with_context(tally_no_push=True)
            if move and move.state != 'cancel':
                v._reverse_stock(move)
                if move.state == 'posted':
                    move.button_draft()
                move.button_cancel()
                move.message_post(body=_('Cancelled because the voucher was deleted in Tally.'))
            v.write({'state': 'deleted', 'message': _('Deleted in Tally; Odoo entry cancelled.')})

    def action_apply_deletion(self):
        self.filtered(lambda v: v.state == 'deleted')._apply_tally_deletion()
        return True

    def action_keep_after_deletion(self):
        for v in self.filtered(lambda v: v.state == 'deleted'):
            v.write({'state': 'done' if v.move_id else 'skipped',
                     'message': _('Deleted in Tally, kept in Odoo by user.')})
        return True

    def _resolve(self, ledger):
        conn = self.connection_id
        Map = self.env['tally.mapping']
        maps = Map.search([('connection_id', '=', conn.id), ('tally_name', '=ilike', ledger),
                           ('kind', 'in', ('party', 'tax', 'bank', 'ledger')),
                           ('state', 'not in', ('ignored',))])
        for m in maps.sorted(lambda m: (m.state != 'confirmed', m.kind != 'party')):
            if m._target():
                return m
        return maps[:1] or Map

    def _account_for(self, m, role_hint=None):
        if m.kind == 'party':
            p = m.partner_id.with_company(self.company_id)
            if (m.tally_role or role_hint) == 'vendor':
                return p.property_account_payable_id
            return p.property_account_receivable_id
        if m.kind == 'bank':
            return m.journal_id.default_account_id
        if m.kind == 'tax':
            rep = m.tax_id.invoice_repartition_line_ids.filtered(
                lambda r: r.repartition_type == 'tax' and r.account_id)
            return rep[:1].account_id or m.account_id
        return m.account_id

    def _build_move(self, force_post=False):
        self.ensure_one()
        conn = self.connection_id
        if self.move_id and self.move_id.state == 'draft' and not self.move_id.posted_before:
            self.move_id.with_context(force_delete=True).unlink()   # rebuild cleanly
        data = self._data()
        les, ies = data.get('ledger_entries', []), data.get('inventory_entries', [])
        missing, resolved = [], {}
        for le in les:
            m = self._resolve(le['ledger'])
            if not m or not m._target():
                missing.append(le['ledger'])
            resolved[le['ledger']] = m
        for ie in ies:
            if ie.get('ledger') and ie['ledger'] not in resolved:
                m = self._resolve(ie['ledger'])
                resolved[ie['ledger']] = m
        party = next((resolved[le['ledger']] for le in les if le['is_party']), None) or next(
            (resolved[le['ledger']] for le in les if resolved[le['ledger']].kind == 'party'), None)
        if party and party.partner_id:
            self.partner_id = party.partner_id
        if missing and not conn.suspense_account_id:
            raise UserError(_('Unmapped Tally ledgers: %s. Map them in Tally → Mappings (AI '
                              'suggestions are pre-filled) and click Process again.')
                            % ', '.join(sorted(set(missing))))
        move = self.env['account.move']
        if self.kind in MOVE_TYPE and party and party.partner_id and not missing:
            move = self._build_invoice(resolved, les, ies)
        if not move:
            move = self._build_entry(resolved, les, ies, missing)
        self.write({'move_id': move.id, 'import_mode': 'invoice' if move.is_invoice() else 'entry'})
        move.write({'tally_state': 'imported', 'tally_guid': self.guid,
                    'tally_number': self.number, 'tally_connection_id': conn.id,
                    'tally_master_id': self.master_id, 'tally_voucher_id': self.id})
        findings = conn._ai_check_voucher(self, move) if conn.anomaly_check else []
        self.write({'findings': json.dumps(findings), 'risk_score': anomaly.risk_score(findings),
                    'message': False})
        move.tally_risk_score = self.risk_score
        blocked = any(f['severity'] == 'block' for f in findings) and conn.anomaly_block
        if blocked and not force_post:
            self.state = 'anomaly'
            return move
        if conn.auto_post_imported or force_post:
            move.with_context(tally_no_push=True).action_post()
            self._reconcile_bills(move, les)
            self._after_import(move, data)
        self.state = 'done'
        return move

    def _invoice_taxes(self, tax_maps, use):
        """Find the Odoo tax(es) whose components match the Tally duty ledgers used."""
        Tax = self.env['account.tax']
        taxes = Tax.browse([m.tax_id.id for m in tax_maps if m.tax_id])
        if not taxes:
            return Tax
        direct = taxes.filtered(lambda t: t.type_tax_use == use)
        if direct == taxes:
            return direct
        groups = Tax.search([('company_id', '=', self.company_id.id), ('type_tax_use', '=', use),
                             ('amount_type', '=', 'group'),
                             ('children_tax_ids', 'in', taxes.ids)])
        exact = groups.filtered(lambda g: set(g.children_tax_ids.ids) == set(taxes.ids))
        if exact:
            return exact[:1]
        return Tax

    def _build_invoice(self, resolved, les, ies):
        conn = self.connection_id
        move_type = MOVE_TYPE[self.kind]
        sign = 1 if move_type in ('out_invoice', 'in_refund') else -1
        use = 'sale' if move_type in ('out_invoice', 'out_refund') else 'purchase'
        tax_maps = [resolved[le['ledger']] for le in les if resolved[le['ledger']].kind == 'tax']
        taxes = self._invoice_taxes(tax_maps, use)
        if tax_maps and not taxes:
            return self.env['account.move']  # cannot express with Odoo taxes -> entry
        lines = []
        for ie in ies:
            pm = self.env['tally.mapping']._find(conn, 'item', tally_name=ie['item'])
            if not pm or not pm.product_id:
                raise UserError(_('Stock item "%s" is not mapped to a product.') % ie['item'])
            acc_map = resolved.get(ie.get('ledger'))
            line = {'product_id': pm.product_id.id, 'quantity': abs(ie['qty']) or 1.0,
                    'price_unit': abs(ie['rate']) if ie['rate'] else abs(ie['amount']),
                    'discount': ie.get('discount') or 0.0,
                    'tax_ids': [(6, 0, taxes.ids)],
                    'analytic_distribution': self._distribution(ie.get('cost_centres'),
                                                                ie['amount'])}
            if not ie['rate']:
                line['quantity'] = 1.0
            if acc_map and acc_map.account_id:
                line['account_id'] = acc_map.account_id.id
            lines.append(line)
        round_name = (conn.round_off_ledger or '').lower()
        for le in les:
            m = resolved[le['ledger']]
            if le['is_party'] or m.kind in ('party', 'tax'):
                continue
            if any(ie.get('ledger') == le['ledger'] for ie in ies):
                continue  # already represented by inventory allocations
            is_round = le['ledger'].lower() == round_name
            lines.append({'name': le['ledger'], 'account_id': self._account_for(m).id,
                          'analytic_distribution': self._distribution(le.get('cost_centres'),
                                                                      le['amount']),
                          'quantity': 1.0, 'price_unit': sign * le['amount'],
                          'tax_ids': [(6, 0, [] if is_round else taxes.ids)]})
        if not lines:
            return self.env['account.move']
        journal = conn.sale_journal_id if use == 'sale' else conn.purchase_journal_id
        vals = {'move_type': move_type, 'partner_id': self.partner_id.id,
                'invoice_date': self.date, 'date': self.date,
                'ref': self.reference or self.number, 'narration': self.narration or False,
                'invoice_line_ids': [(0, 0, l) for l in lines],
                'company_id': self.company_id.id}
        if use == 'purchase':
            vals['ref'] = self.reference or self.number
        else:
            vals['payment_reference'] = self.number
        if journal:
            vals['journal_id'] = journal.id
        move = self.env['account.move'].with_context(tally_no_push=True).create(vals)
        party_amount = abs(sum(le['amount'] for le in les if le['is_party'])) or self.amount_total
        diff = round(party_amount - abs(move.amount_total), 2)
        if abs(diff) > conn.amount_tolerance:
            move.with_context(force_delete=True).unlink()
            if conn.fallback_to_entry:
                return self.env['account.move']
            raise UserError(_('Odoo taxes give %(o).2f but Tally total is %(t).2f. Check the '
                              'tax mappings.') % {'o': abs(move.amount_total), 't': party_amount})
        if not float_is_zero(diff, precision_digits=2):
            # absorb paise-level rounding so receivables match Tally exactly
            m_round = self.env['tally.mapping']._find(conn, 'ledger',
                                                      tally_name=conn.round_off_ledger or '')
            account = (m_round and m_round.account_id) or move.invoice_line_ids[:1].account_id
            move.write({'invoice_line_ids': [(0, 0, {
                'name': conn.round_off_ledger or _('Round off'), 'account_id': account.id,
                'quantity': 1.0, 'price_unit': diff, 'tax_ids': [(5, 0, 0)]})]})
        return move

    def _build_entry(self, resolved, les, ies, missing):
        conn = self.connection_id
        role_hint = 'vendor' if self.kind in ('purchase', 'debit_note', 'payment') else 'customer'
        lines = []

        def add(account, amount, name, partner=False, cost_centres=None):
            if float_is_zero(amount, precision_digits=2):
                return
            lines.append({'account_id': account.id, 'name': name,
                          'debit': -amount if amount < 0 else 0.0,
                          'credit': amount if amount > 0 else 0.0,
                          'partner_id': partner.id if partner else False,
                          'analytic_distribution': self._distribution(cost_centres, amount)})

        alloc, alloc_cc = {}, {}
        for ie in ies:
            key = ie.get('ledger') or ''
            alloc[key] = alloc.get(key, 0.0) + ie['amount']
            alloc_cc.setdefault(key, []).extend(ie.get('cost_centres') or [])
        bank_journal = self.env['account.journal']
        for le in les:
            m = resolved[le['ledger']]
            if le['ledger'] in missing:
                account = conn.suspense_account_id
            else:
                account = self._account_for(m, role_hint)
            if not account:
                raise UserError(_('No Odoo account for Tally ledger "%s".') % le['ledger'])
            if m and m.kind == 'bank' and not bank_journal:
                bank_journal = m.journal_id
            partner = m.partner_id if m and m.kind == 'party' else False
            add(account, le['amount'], le['ledger'], partner, le.get('cost_centres'))
        for ledger, amount in alloc.items():
            if any(le['ledger'] == ledger for le in les):
                continue
            m = resolved.get(ledger) or self._resolve(ledger)
            account = (m and self._account_for(m)) or conn.suspense_account_id
            if not account:
                raise UserError(_('No Odoo account for Tally ledger "%s".') % ledger)
            add(account, amount, ledger, cost_centres=alloc_cc.get(ledger))
        total = round(sum(l['debit'] - l['credit'] for l in lines), 2)
        if abs(total) > 0.01:
            raise UserError(_('Tally voucher is not balanced (difference %.2f).') % total)
        journal = bank_journal if self.kind in ('receipt', 'payment', 'contra') and bank_journal \
            else (conn.misc_journal_id or self.env['account.journal'].search(
                [('type', '=', 'general'), ('company_id', '=', self.company_id.id)], limit=1))
        return self.env['account.move'].with_context(tally_no_push=True).create({
            'move_type': 'entry', 'journal_id': journal.id, 'date': self.date,
            'ref': '%s %s' % (self.vch_type, self.number),
            'narration': self.narration or False, 'company_id': self.company_id.id,
            'line_ids': [(0, 0, l) for l in lines]})

    def _reconcile_bills(self, move, les):
        """Use Tally bill-wise 'Agst Ref' allocations to reconcile in Odoo."""
        Move = self.env['account.move']
        for le in les:
            for bill in le.get('bills') or []:
                if (bill.get('type') or '').lower() != 'agst ref' or not bill.get('name'):
                    continue
                target = Move.search([('company_id', '=', self.company_id.id),
                                      ('state', '=', 'posted'),
                                      ('commercial_partner_id', '=',
                                       self.partner_id.commercial_partner_id.id),
                                      '|', ('name', '=', bill['name']),
                                      ('tally_number', '=', bill['name'])], limit=1)
                if not target:
                    continue
                pay_lines = move.line_ids.filtered(
                    lambda l: l.account_id.account_type in ('asset_receivable', 'liability_payable')
                    and not l.reconciled)
                inv_lines = target.line_ids.filtered(
                    lambda l: l.account_id in pay_lines.account_id and not l.reconciled)
                if pay_lines and inv_lines:
                    (pay_lines | inv_lines).reconcile()


class TallyVoucherLine(models.Model):
    _name = 'tally.voucher.line'
    _description = 'Tally voucher line'
    _order = 'sequence, id'

    voucher_id = fields.Many2one('tally.voucher', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer()
    line_type = fields.Selection([('ledger', 'Ledger'), ('item', 'Item')])
    ledger = fields.Char()
    item = fields.Char()
    is_party = fields.Boolean()
    qty = fields.Float()
    unit = fields.Char()
    rate = fields.Float()
    discount = fields.Float()
    amount = fields.Float(help='Tally sign: negative = debit')
    debit = fields.Float(compute='_compute_dc')
    credit = fields.Float(compute='_compute_dc')
    bills = fields.Char('Bill allocations')

    @api.depends('amount')
    def _compute_dc(self):
        for l in self:
            l.debit = -l.amount if l.amount < 0 else 0.0
            l.credit = l.amount if l.amount > 0 else 0.0
