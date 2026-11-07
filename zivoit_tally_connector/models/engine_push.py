# -*- coding: utf-8 -*-
"""Odoo → Tally: masters and vouchers."""
import logging
import re
from collections import OrderedDict

from odoo import _, fields, models
from odoo.tools import html2plaintext

from ..lib import matching
from ..lib import tally_xml as TX

_logger = logging.getLogger(__name__)

PRIO_UNIT, PRIO_LEDGER, PRIO_ITEM, PRIO_VOUCHER = 10, 20, 30, 50
# bucket, request purpose, priority (masters always sent before vouchers)
MASTER_BUCKETS = [('units', 'push_unit', 10), ('godowns', 'push_godown', 12),
                  ('costcats', 'push_costcat', 14), ('costcentres', 'push_costcentre', 15),
                  ('ledgers', 'push_ledger', 20), ('items', 'push_item', 30)]
PNL_TYPES = ('income', 'income_other', 'expense', 'expense_direct_cost',
             'expense_depreciation', 'expense_other')

ACCOUNT_TYPE_GROUP = {
    'income': 'Sales Accounts', 'income_other': 'Indirect Incomes',
    'expense': 'Indirect Expenses', 'expense_direct_cost': 'Purchase Accounts',
    'expense_depreciation': 'Indirect Expenses', 'expense_other': 'Indirect Expenses',
    'asset_cash': 'Bank Accounts', 'asset_current': 'Current Assets',
    'asset_receivable': 'Sundry Debtors', 'asset_prepayments': 'Current Assets',
    'asset_non_current': 'Fixed Assets', 'asset_fixed': 'Fixed Assets',
    'liability_payable': 'Sundry Creditors', 'liability_current': 'Current Liabilities',
    'liability_credit_card': 'Current Liabilities',
    'liability_non_current': 'Loans (Liability)', 'equity': 'Capital Account',
    'equity_unaffected': 'Reserves & Surplus', 'off_balance': 'Suspense A/c',
}
UOM_TO_TALLY = {'units': 'Nos', 'unit(s)': 'Nos', 'unit': 'Nos', 'dozens': 'Dozen',
                'kg': 'Kg', 'g': 'Gms', 'l': 'Ltr', 'm': 'Mtr', 'hours': 'Hrs', 'days': 'Days'}
DUTY_HEADS = {'CGST': 'Central Tax', 'SGST': 'State Tax', 'UTGST': 'State Tax',
              'IGST': 'Integrated Tax', 'CESS': 'Cess'}


class _Collector:
    """Collects master objects a batch of vouchers depends on."""

    def __init__(self):
        for bucket, _purpose, _prio in MASTER_BUCKETS:
            setattr(self, bucket, OrderedDict())   # mapping -> xml

    def __bool__(self):
        return any(getattr(self, b) for b, _p, _q in MASTER_BUCKETS)


class TallyConnectionPush(models.Model):
    _inherit = 'tally.connection'

    # ------------------------------------------------------------------
    # Master naming
    # ------------------------------------------------------------------
    def _unique_name(self, kind, base, record_field, record):
        Map = self.env['tally.mapping']
        name = (base or '').strip()[:100] or _('Unnamed')
        clash = Map.search([('connection_id', '=', self.id), ('kind', '=', kind),
                            ('tally_name', '=ilike', name), (record_field, '!=', record.id)],
                           limit=1)
        if clash:
            name = '%s (%s)' % (name, getattr(record, 'vat', False) or record.id)
        return name

    def _map_record(self, kind, record, name, field, vals=None):
        """Return the mapping for an Odoo record, creating it (origin odoo) if needed."""
        Map = self.env['tally.mapping']
        m = Map._find(self, kind, record=record)
        if m:
            return m
        # A Tally master with the same name may already be known (pulled): link it.
        m = Map._find(self, kind, tally_name=name)
        if m and not m._target():
            m.write(dict({field: record.id, 'state': 'confirmed'}, **(vals or {})))
            return m
        if m:
            name = self._unique_name(kind, name, field, record)
        return Map.create(dict({'connection_id': self.id, 'kind': kind, 'tally_name': name,
                                field: record.id, 'state': 'confirmed', 'origin': 'odoo'},
                               **(vals or {})))

    def _needs_push(self, mapping, record):
        if not self.auto_create_ledgers and mapping.origin != 'odoo':
            return False
        if not mapping.synced_to_tally:
            return mapping.origin == 'odoo' or not mapping.tally_guid
        if mapping.odoo_hash:
            return mapping._odoo_changed_since_sync()
        return bool(record.write_date and mapping.odoo_write_date
                    and record.write_date > mapping.odoo_write_date)

    # ---- parties ---------------------------------------------------------
    def _party_vals(self, partner, mapping, role):
        p = partner.commercial_partner_id
        lines = [l for l in (p.street, p.street2, p.city) if l]
        return {
            'name': mapping.pending_name or mapping.tally_name,
            'old_name': mapping.tally_name if mapping.synced_to_tally else None,
            'parent': mapping.tally_parent or (self.group_vendors if role == 'vendor'
                                               else self.group_customers),
            'gstin': (p.vat or '').upper() if p.vat and len(p.vat) == 15 else '',
            'state': p.state_id.name or '', 'country': p.country_id.name or 'India',
            'pincode': p.zip or '', 'address': lines, 'email': p.email or '',
            'phone': p.phone or '', 'billwise': True,
            'pan': (p.vat or '')[2:12] if p.vat and len(p.vat) == 15 else '',
        }

    def _ensure_party(self, partner, role, col):
        partner = partner.commercial_partner_id
        m = self._map_record('party', partner, partner.name or partner.display_name, 'partner_id',
                             {'tally_role': role})
        if self._needs_push(m, partner) and m not in col.ledgers:
            if m.synced_to_tally and partner.name and partner.name != m.tally_name:
                m.pending_name = self._unique_name('party', partner.name, 'partner_id', partner)
            action = 'Alter' if m.synced_to_tally else 'Create'
            col.ledgers[m] = TX.build_ledger_xml(self._party_vals(partner, m, role), self.schema,
                                                 action)
        return m.pending_name or m.tally_name

    # ---- GL ledgers --------------------------------------------------------
    def _ensure_account_ledger(self, account, col, group_hint=None):
        m = self._map_record('ledger', account, account.name, 'account_id', {
            'tally_parent': group_hint or ACCOUNT_TYPE_GROUP.get(account.account_type,
                                                                 'Suspense A/c')})
        if self._needs_push(m, account) and m not in col.ledgers:
            col.ledgers[m] = TX.build_ledger_xml({
                'name': m.tally_name, 'parent': m.tally_parent, 'billwise': False,
                'old_name': m.tally_name if m.synced_to_tally else None,
                'cost_centres_on': account.account_type in PNL_TYPES,
            }, self.schema, 'Alter' if m.synced_to_tally else 'Create')
        return m.tally_name

    # ---- cost centres --------------------------------------------------------
    def _cost_centres_enabled(self):
        return self._dir_allows('sync_cost_centres', 'push')

    def _ensure_cost_category(self, plan, col):
        name = plan.name or 'Primary Cost Category'
        if self.primary_plan_id and plan == self.primary_plan_id:
            name = 'Primary Cost Category'   # exists in every Tally company
        m = self._map_record('costcategory', plan, name, 'analytic_plan_id')
        if name == 'Primary Cost Category' and not m.synced_to_tally:
            m.write({'synced_to_tally': True})
        if not m.synced_to_tally and m.origin == 'odoo' and m not in col.costcats:
            col.costcats[m] = TX.build_cost_category_xml(m.tally_name)
        return m.tally_name

    def _ensure_cost_centre(self, account, col):
        category = self._ensure_cost_category(account.plan_id, col)
        m = self._map_record('costcentre', account, account.name, 'analytic_account_id',
                             {'tally_parent': category})
        if self._needs_push(m, account) and m not in col.costcentres:
            col.costcentres[m] = TX.build_cost_centre_xml(
                m.tally_name, m.tally_parent or category,
                action='Alter' if m.synced_to_tally else 'Create',
                old_name=m.tally_name if m.synced_to_tally else None)
        return m.tally_parent or category, m.tally_name

    def _line_cost_centres(self, line, amount, col):
        """Odoo analytic distribution -> Tally cost-centre allocations (signed like amount)."""
        dist = line.analytic_distribution or {}
        if not dist or not self._cost_centres_enabled():
            return []
        Account = self.env['account.analytic.account']
        out = []
        for key, pct in dist.items():
            for aid in str(key).split(','):
                acc = Account.browse(int(aid)).exists()
                if not acc:
                    continue
                category, name = self._ensure_cost_centre(acc, col)
                out.append({'category': category, 'name': name,
                            'amount': round(amount * float(pct) / 100.0, 2)})
        return out

    @staticmethod
    def _merge_cost_centres(target, extra):
        for a in extra:
            for t in target:
                if t['category'] == a['category'] and t['name'] == a['name']:
                    t['amount'] = round(t['amount'] + a['amount'], 2)
                    break
            else:
                target.append(dict(a))

    # ---- hooks extended by the stock bridge ---------------------------------
    def _stock_item_extra(self, product):
        return {}

    def _inventory_batches(self, move, line, qty, amount, kind, col):
        return []

    def _ensure_tax_ledger(self, tax, col, use=None):
        head, rate = matching.gst_head_and_rate(tax.name)
        if rate is None and tax.amount_type == 'percent':
            rate = tax.amount
        if tax.type_tax_use in ('sale', 'purchase'):
            use = tax.type_tax_use
        prefix = _('Output') if use == 'sale' else _('Input')
        base = tax.name if tax.name.lower().startswith(('output', 'input')) else \
            '%s %s' % (prefix, tax.name)
        m = self._map_record('tax', tax, base, 'tax_id', {'tally_parent': 'Duties & Taxes',
                                                         'tally_role': 'tax'})
        if self._needs_push(m, tax) and m not in col.ledgers:
            col.ledgers[m] = TX.build_ledger_xml({
                'name': m.tally_name, 'parent': 'Duties & Taxes', 'billwise': False,
                'tax_type': 'GST' if head else 'Others', 'duty_head': DUTY_HEADS.get(head),
                'rate': rate if head else None,
            }, self.schema, 'Alter' if m.synced_to_tally else 'Create')
        return m.tally_name

    def _ensure_bank_ledger(self, journal, col):
        m = self._map_record('bank', journal, journal.name, 'journal_id', {
            'tally_parent': 'Cash-in-Hand' if journal.type == 'cash' else 'Bank Accounts',
            'tally_role': 'cash' if journal.type == 'cash' else 'bank'})
        if self._needs_push(m, journal) and m not in col.ledgers:
            col.ledgers[m] = TX.build_ledger_xml({
                'name': m.tally_name, 'parent': m.tally_parent, 'billwise': False,
            }, self.schema, 'Alter' if m.synced_to_tally else 'Create')
        return m.tally_name

    def _ensure_round_off(self, col):
        name = self.round_off_ledger or 'Round Off'
        Map = self.env['tally.mapping']
        m = Map._find(self, 'ledger', tally_name=name)
        if not m:
            m = Map.create({'connection_id': self.id, 'kind': 'ledger', 'tally_name': name,
                            'tally_parent': 'Indirect Expenses', 'state': 'confirmed',
                            'origin': 'odoo'})
        if not m.synced_to_tally and m.origin == 'odoo' and m not in col.ledgers:
            col.ledgers[m] = TX.build_ledger_xml({'name': name, 'parent': 'Indirect Expenses',
                                                  'billwise': False}, self.schema)
        return name

    # ---- inventory masters -------------------------------------------------
    def _ensure_unit(self, uom, col):
        default = UOM_TO_TALLY.get((uom.name or '').lower(), uom.name)
        m = self._map_record('unit', uom, default, 'uom_id')
        if not m.synced_to_tally and m.origin == 'odoo' and m not in col.units:
            col.units[m] = TX.build_unit_xml(m.tally_name)
        return m.tally_name

    def _ensure_item(self, product, col):
        name = product.with_context(display_default_code=False).display_name
        m = self._map_record('item', product, name, 'product_id')
        unit = self._ensure_unit(product.uom_id, col)
        if self._needs_push(m, product) and m not in col.items:
            taxes = product.taxes_id.filtered(lambda t: t.company_id == self.company_id)
            rate = sum(t.amount for t in taxes.flatten_taxes_hierarchy()
                       if t.amount_type == 'percent') if taxes else None
            hsn = getattr(product, 'l10n_in_hsn_code', False) or ''
            vals = {
                'name': m.tally_name, 'old_name': m.tally_name if m.synced_to_tally else None,
                'unit': unit, 'hsn': hsn, 'gst_rate': rate, 'part_no': product.default_code,
                'description': product.description_sale or '', 'rate': product.lst_price,
                'supply_type': 'Services' if product.type == 'service' else 'Goods',
            }
            vals.update(self._stock_item_extra(product))
            col.items[m] = TX.build_stock_item_xml(vals, self.schema,
                                                   'Alter' if m.synced_to_tally else 'Create')
        return m.tally_name, unit

    def _queued_mapping_ids(self, states=('pending', 'sent', 'failed')):
        ids = set()
        for r in self.env['tally.request'].search([
                ('connection_id', '=', self.id), ('state', 'in', states),
                ('purpose', 'in', [p for _b, p, _q in MASTER_BUCKETS])]):
            ids.update(r._get_res_ids())
        return ids

    def _flush_masters(self, col, force=False):
        Req = self.env['tally.request']
        batch = max(1, self.batch_size or 50)
        reqs = Req
        queued = self._queued_mapping_ids(('pending', 'sent') if force else
                                          ('pending', 'sent', 'failed'))
        for bucket, purpose, prio in MASTER_BUCKETS:
            coll = getattr(col, bucket)
            for m in [m for m in coll if m.id in queued]:
                coll.pop(m)
            report = 'All Masters'
            maps = list(coll.keys())
            for i in range(0, len(maps), batch):
                chunk = maps[i:i + batch]
                xml = TX.build_import_envelope(self.tally_company, report,
                                               [TX.tally_message(coll[m]) for m in chunk])
                reqs |= Req._enqueue(self, purpose, xml, 'tally.mapping', [m.id for m in chunk],
                                     priority=prio)
        return reqs

    def _push_masters(self, partners=None, products=None):
        """Explicit push of contacts/products (button or automatic on write)."""
        self.ensure_one()
        col = _Collector()
        for p in (partners or self.env['res.partner']):
            role = 'vendor' if p.supplier_rank > p.customer_rank else 'customer'
            m = self._map_record('party', p.commercial_partner_id,
                                 p.commercial_partner_id.name, 'partner_id', {'tally_role': role})
            cp = p.commercial_partner_id
            if m.synced_to_tally and cp.name and cp.name != m.tally_name:
                m.pending_name = self._unique_name('party', cp.name, 'partner_id', cp)
            if m.origin != 'odoo' and not m.synced_to_tally:
                m.origin = 'odoo'
            col.ledgers.pop(m, None)
            col.ledgers[m] = TX.build_ledger_xml(self._party_vals(p, m, m.tally_role or role),
                                                 self.schema,
                                                 'Alter' if m.synced_to_tally else 'Create')
        for prod in (products or self.env['product.product']):
            m = self._map_record('item', prod,
                                 prod.with_context(display_default_code=False).display_name,
                                 'product_id')
            if not m.synced_to_tally:
                m.origin = 'odoo'
            m.odoo_write_date = False
            self._ensure_item(prod, col)
        return self._flush_masters(col)

    # ------------------------------------------------------------------
    # Vouchers
    # ------------------------------------------------------------------
    def _move_kind(self, move):
        mt = move.move_type
        if mt in ('out_invoice', 'out_receipt'):
            return 'sale'
        if mt in ('in_invoice', 'in_receipt'):
            return 'purchase'
        if mt == 'out_refund':
            return 'credit_note'
        if mt == 'in_refund':
            return 'debit_note'
        lines = move.line_ids.filtered(lambda l: not l.currency_id.is_zero(l.amount_currency)
                                       or l.balance)
        liquidity = lines.filtered(lambda l: l.account_id.account_type == 'asset_cash')
        if liquidity and liquidity == lines:
            return 'contra'
        if move.origin_payment_id:
            pay = move.origin_payment_id
            if pay.paired_internal_transfer_payment_id:
                return 'contra'
            return 'receipt' if pay.payment_type == 'inbound' else 'payment'
        if move.statement_line_id or liquidity:
            return 'receipt' if sum(liquidity.mapped('balance')) > 0 else 'payment'
        return 'journal'

    def _kind_direction_field(self, kind):
        return {'sale': 'sync_sales', 'credit_note': 'sync_sales',
                'purchase': 'sync_purchases', 'debit_note': 'sync_purchases',
                'receipt': 'sync_payments', 'payment': 'sync_payments',
                'journal': 'sync_journals', 'contra': 'sync_journals'}[kind]

    def _bills_for_line(self, move, line, kind):
        """Bill-wise allocations so Tally's outstanding reports match Odoo."""
        amount = -line.balance
        if kind in ('sale', 'purchase', 'credit_note', 'debit_note'):
            return [{'name': move.name, 'type': 'New Ref', 'amount': amount}]
        bills, allocated = [], 0.0
        partials = line.matched_debit_ids | line.matched_credit_ids
        for partial in partials:
            other = partial.debit_move_id if partial.credit_move_id == line \
                else partial.credit_move_id
            inv = other.move_id
            if inv == move:
                continue
            amt = partial.amount if line.balance < 0 else -partial.amount
            bills.append({'name': inv.name, 'type': 'Agst Ref', 'amount': amt})
            allocated += amt
        rest = round(amount - allocated, 2)
        if abs(rest) >= 0.01:
            bills.append({'name': move.name, 'type': 'Advance' if kind in ('receipt', 'payment')
                          else 'On Account', 'amount': rest})
        return bills

    def _move_to_voucher(self, move, col):
        kind = self._move_kind(move)
        is_invoice = move.is_invoice(include_receipts=True)
        entries = OrderedDict()   # ledger -> dict
        inventory = []
        party = ''
        item_mode = self.voucher_mode == 'item' and is_invoice and kind in (
            'sale', 'purchase', 'credit_note', 'debit_note')

        def add(ledger, amount, is_party=False, bills=None, line=None):
            e = entries.setdefault(ledger, {'ledger': ledger, 'amount': 0.0,
                                            'is_party': is_party, 'bills': [],
                                            'cost_centres': []})
            e['amount'] = round(e['amount'] + amount, 2)
            if bills:
                e['bills'].extend(bills)
            if line is not None:
                self._merge_cost_centres(e['cost_centres'],
                                         self._line_cost_centres(line, amount, col))

        pay = move.origin_payment_id
        liquidity_accounts = move.journal_id.default_account_id | (
            pay.outstanding_account_id if pay else self.env['account.account'])
        for line in move.line_ids:
            if line.display_type in ('line_section', 'line_note') or (
                    not line.balance and not line.tax_line_id):
                continue
            amount = round(-line.balance, 2)   # Tally: credit positive / debit negative
            atype = line.account_id.account_type
            if atype in ('asset_receivable', 'liability_payable') and line.partner_id:
                role = 'vendor' if atype == 'liability_payable' else 'customer'
                ledger = self._ensure_party(line.partner_id, role, col)
                party = party or ledger
                add(ledger, amount, True, self._bills_for_line(move, line, kind))
            elif line.tax_line_id:
                use = 'sale' if kind in ('sale', 'credit_note') else 'purchase' \
                    if kind in ('purchase', 'debit_note') else None
                add(self._ensure_tax_ledger(line.tax_line_id, col, use), amount)
            elif line.display_type == 'rounding':
                add(self._ensure_round_off(col), amount)
            elif line.journal_id.type in ('bank', 'cash') and line.account_id in liquidity_accounts:
                add(self._ensure_bank_ledger(line.journal_id, col), amount)
            elif item_mode and line.display_type == 'product' and line.product_id \
                    and line.product_id.type != 'service':
                item, unit = self._ensure_item(line.product_id, col)
                group = 'Sales Accounts' if kind in ('sale', 'credit_note') else 'Purchase Accounts'
                ledger = self._ensure_account_ledger(line.account_id, col, group)
                qty = line.quantity or 1.0
                inventory.append({'item': item, 'qty': qty, 'unit': unit,
                                  'rate': line.price_unit, 'discount': line.discount,
                                  'amount': amount, 'ledger': ledger,
                                  'batches': self._inventory_batches(move, line, qty, amount,
                                                                     kind, col),
                                  'cost_centres': self._line_cost_centres(line, amount, col)})
            else:
                hint = None
                if is_invoice and line.display_type == 'product':
                    hint = 'Sales Accounts' if kind in ('sale', 'credit_note') else \
                        'Purchase Accounts'
                add(self._ensure_account_ledger(line.account_id, col, hint), amount, line=line)

        partner = move.commercial_partner_id
        state_name = partner.state_id.name or ''
        pos = getattr(move, 'l10n_in_state_id', False)
        return {
            'remote_id': self._remote_id(move), 'vch_type': self._kind_vch_type(kind),
            'kind': kind, 'date': move.date, 'number': move.name,
            'reference': move.ref or (move.invoice_origin if is_invoice else '') or '',
            'reference_date': move.invoice_date if kind in ('purchase', 'debit_note') else None,
            'party': party, 'party_gstin': partner.vat if partner.vat and len(partner.vat) == 15
            else '',
            'place_of_supply': (pos.name if pos else state_name) if is_invoice else '',
            'narration': (html2plaintext(move.narration) if move.narration else '')[:500]
            or move.ref or '',
            'invoice_mode': item_mode and bool(inventory),
            'is_invoice_doc': is_invoice,
            'ledger_entries': list(entries.values()),
            'inventory_entries': inventory,
        }

    def _push_moves(self, moves, force=False):
        """Queue vouchers (and the masters they need) for Tally."""
        self.ensure_one()
        Req = self.env['tally.request']
        col = _Collector()
        voucher_reqs = Req
        for move in moves:
            if move.state != 'posted' or move.company_id != self.company_id:
                continue
            if move.tally_state == 'imported' and not force:
                continue  # originated in Tally: never echo back
            kind = self._move_kind(move)
            if not force and not self._dir_allows(self._kind_direction_field(kind), 'push'):
                continue
            if self.anomaly_check:
                findings = self._ai_check_move(move)
                if self.anomaly_block and any(f['severity'] == 'block' for f in findings):
                    move.write({'tally_state': 'held', 'tally_error': '\n'.join(
                        f['message'] for f in findings if f['severity'] == 'block')})
                    continue
            vals = self._move_to_voucher(move, col)
            action = 'Alter' if move.tally_guid else 'Create'
            xml = TX.build_import_envelope(self.tally_company, 'Vouchers',
                                           [TX.tally_message(TX.build_voucher_xml(vals, action))])
            # Drop older pending pushes for the same move (latest version wins)
            Req.search([('connection_id', '=', self.id), ('purpose', '=', 'push_move'),
                        ('res_model', '=', 'account.move'), ('res_ids', '=', str(move.id)),
                        ('state', 'in', ('pending', 'failed'))]).write({'state': 'cancelled'})
            voucher_reqs |= Req._enqueue(self, 'push_move', xml, 'account.move', [move.id],
                                         priority=PRIO_VOUCHER)
            move.write({'tally_state': 'queued', 'tally_connection_id': self.id,
                        'tally_error': False})
        self._flush_masters(col)
        return voucher_reqs

    def _refresh_master_request(self, req):
        """Drop masters that are in conflict (the accountant decides first).

        Each object sits in its own TALLYMESSAGE, in the same order as res_ids.
        Returns False when nothing is left to send.
        """
        ids = req._get_res_ids()
        maps = self.env['tally.mapping'].browse(ids)
        held = [m.id for m in maps.exists() if m.state == 'conflict']
        if not held:
            return True
        body = req.request_xml or ''
        start, end = body.find('<REQUESTDATA>'), body.rfind('</REQUESTDATA>')
        msgs = re.findall(r'<TALLYMESSAGE\b.*?</TALLYMESSAGE>', body[start:end], re.S)
        if len(msgs) != len(ids):
            req.write({'state': 'cancelled', 'error': _('Held: a master in this batch is in '
                                                        'conflict.')})
            return False
        keep = [(i, msg) for i, msg in zip(ids, msgs) if i not in held]
        if not keep:
            req.write({'state': 'cancelled', 'error': _('Held: master in conflict — resolve it '
                                                        'in Mappings.')})
            return False
        req.write({'res_ids': ','.join(str(i) for i, _m in keep),
                   'request_xml': TX.build_import_envelope(
                       self.tally_company, 'All Masters', [m for _i, m in keep])})
        return True

    def _auto_heal_extra(self, code, arg, col):
        return False

    def _refresh_move_request(self, req):
        """Rebuild a queued voucher right before sending it.

        Reconciliations made after posting (e.g. the payment wizard reconciles after
        `action_post`) and later master renames are then reflected.  Returns False when
        the voucher must wait for masters that were just queued.
        """
        move = self.env['account.move'].browse(req._get_res_ids()[:1]).exists()
        if not move:
            req.state = 'cancelled'
            return False
        if move.state != 'posted':
            req.write({'state': 'cancelled', 'error': _('Document is no longer posted.')})
            return False
        col = _Collector()
        vals = self._move_to_voucher(move, col)
        action = 'Alter' if move.tally_guid else 'Create'
        req.request_xml = TX.build_import_envelope(
            self.tally_company, 'Vouchers', [TX.tally_message(TX.build_voucher_xml(vals, action))])
        if self._flush_masters(col):
            return False
        return True

    def _push_cancel(self, move):
        kind = self._move_kind(move)
        xml = TX.build_import_envelope(self.tally_company, 'Vouchers', [TX.tally_message(
            TX.build_voucher_cancel_xml(self._kind_vch_type(kind), move.date, move.name))])
        self.env['tally.request']._enqueue(self, 'cancel_move', xml, 'account.move', [move.id],
                                           priority=PRIO_VOUCHER)

    # ------------------------------------------------------------------
    # Response handlers
    # ------------------------------------------------------------------
    def _mark_maps_synced(self, req, res):
        if not res['ok']:
            return
        maps = self.env['tally.mapping'].browse(req._get_res_ids()).exists()
        now = fields.Datetime.now()
        for m in maps:
            target = m._target()
            vals = {'synced_to_tally': True, 'last_sync': now,
                    'odoo_write_date': target.write_date if target else now,
                    'odoo_hash': m._odoo_fingerprint()}
            if m.pending_name:
                vals.update(tally_name=m.pending_name, pending_name=False)
            m.write(vals)

    def _on_push_unit(self, req, res):
        self._mark_maps_synced(req, res)

    def _on_push_ledger(self, req, res):
        self._mark_maps_synced(req, res)

    def _on_push_item(self, req, res):
        self._mark_maps_synced(req, res)

    def _on_push_godown(self, req, res):
        self._mark_maps_synced(req, res)

    def _on_push_costcat(self, req, res):
        self._mark_maps_synced(req, res)

    def _on_push_costcentre(self, req, res):
        self._mark_maps_synced(req, res)

    def _on_push_move(self, req, res):
        moves = self.env['account.move'].browse(req._get_res_ids()).exists()
        if res['ok']:
            for mv in moves:
                mv.write({'tally_state': 'synced', 'tally_guid': self._remote_id(mv),
                          'tally_number': mv.name,
                          'tally_last_sync': fields.Datetime.now(), 'tally_error': False,
                          'tally_master_id': res.get('last_vch_id') or mv.tally_master_id})

    def _on_cancel_move(self, req, res):
        if res['ok']:
            self.env['account.move'].browse(req._get_res_ids()).exists().write(
                {'tally_state': 'cancelled'})

    # ------------------------------------------------------------------
    # Auto-heal
    # ------------------------------------------------------------------
    def _auto_heal(self, req, code, arg, message):
        """Fix what Tally complained about, then retry the request once."""
        Map = self.env['tally.mapping']
        col = _Collector()
        healed = False
        if code == 'missing_ledger' and arg:
            m = Map.search([('connection_id', '=', self.id), ('tally_name', '=ilike', arg),
                            ('kind', 'in', ('party', 'ledger', 'tax', 'bank'))], limit=1)
            if m:
                m.write({'synced_to_tally': False, 'origin': 'odoo', 'tally_guid': False})
                target = m._target()
                if m.kind == 'party' and target:
                    self._ensure_party(target, m.tally_role or 'customer', col)
                elif m.kind == 'ledger':
                    if target:
                        self._ensure_account_ledger(target, col, m.tally_parent)
                    else:
                        col.ledgers[m] = TX.build_ledger_xml(
                            {'name': m.tally_name, 'parent': m.tally_parent or 'Suspense A/c',
                             'billwise': False}, self.schema)
                elif m.kind == 'tax' and target:
                    self._ensure_tax_ledger(target, col)
                elif m.kind == 'bank' and target:
                    self._ensure_bank_ledger(target, col)
                healed = bool(col.ledgers)
        elif code == 'missing_item' and arg:
            m = Map.search([('connection_id', '=', self.id), ('kind', '=', 'item'),
                            ('tally_name', '=ilike', arg)], limit=1)
            if m and m.product_id:
                m.write({'synced_to_tally': False, 'origin': 'odoo', 'tally_guid': False})
                self._ensure_item(m.product_id, col)
                healed = True
        elif code == 'missing_unit' and arg:
            col.units[Map._upsert(self, 'unit', arg, {'origin': 'odoo', 'state': 'confirmed',
                                                      'synced_to_tally': False})] = \
                TX.build_unit_xml(arg)
            healed = True
        elif code == 'missing_costcentre' and arg:
            m = Map.search([('connection_id', '=', self.id), ('kind', '=', 'costcentre'),
                            ('tally_name', '=ilike', arg)], limit=1)
            if m and m.analytic_account_id:
                m.write({'synced_to_tally': False, 'origin': 'odoo', 'tally_guid': False})
                cat = Map.search([('connection_id', '=', self.id), ('kind', '=', 'costcategory'),
                                  ('analytic_plan_id', '=', m.analytic_account_id.plan_id.id)],
                                 limit=1)
                if cat and cat.tally_name != 'Primary Cost Category':
                    cat.write({'synced_to_tally': False, 'origin': 'odoo'})
                self._ensure_cost_centre(m.analytic_account_id, col)
                healed = True
        elif code in ('missing_godown',) and arg:
            healed = self._auto_heal_extra(code, arg, col)
        elif code == 'exists' and req.purpose in ('push_ledger', 'push_item', 'push_godown',
                                                  'push_costcentre'):
            xml = (req.request_xml or '').replace('ACTION="Create"', 'ACTION="Alter"')
            req.write({'request_xml': xml})
            healed = True
        if not healed:
            return False
        self._flush_masters(col, force=True)
        req.write({'state': 'pending', 'next_attempt': fields.Datetime.now(), 'attempts': 0,
                   'auto_healed': True,
                   'ai_hint': (req.ai_hint or '') + '\n' + _('Auto-heal: created the missing '
                                                             'master in Tally and retried.')})
        return True
