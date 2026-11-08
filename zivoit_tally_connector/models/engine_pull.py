# -*- coding: utf-8 -*-
"""Tally to Odoo: incremental pulls by AlterID, master upserts, voucher staging."""
import json
import logging
from datetime import timedelta

from odoo import _, fields, models

from ..lib import matching
from ..lib import tally_xml as TX

_logger = logging.getLogger(__name__)

GST_TREATMENT = {'regular': 'regular', 'composition': 'composition',
                 'unregistered/consumer': 'unregistered', 'unregistered': 'unregistered',
                 'consumer': 'consumer', 'overseas': 'overseas',
                 'sez': 'special_economic_zone', 'deemed export': 'deemed_export'}


class TallyConnectionPull(models.Model):
    _inherit = 'tally.connection'

    # ------------------------------------------------------------------
    def _pulls_wanted(self):
        wants = ['pull_groups']
        if any(self._dir_allows(f, 'pull') for f in ('sync_sales', 'sync_purchases',
                                                     'sync_payments', 'sync_journals')):
            wants.append('pull_vchtypes')
        if self._dir_allows('sync_parties', 'pull') or self.sync_ledgers == 'pull' or \
                self._dir_allows('sync_parties', 'push'):
            wants.append('pull_ledgers')
        if self.sync_cost_centres != 'none':
            wants.append('pull_costcentres')
        if self._dir_allows('sync_items', 'pull'):
            wants.append('pull_items')
        if 'pull_vchtypes' in wants:
            wants.append('pull_vouchers')
        return wants

    def _build_pull(self, purpose, full=False, from_date=None, to_date=None):
        obj = {'pull_groups': 'Group', 'pull_vchtypes': 'VoucherType', 'pull_ledgers': 'Ledger',
               'pull_items': 'StockItem', 'pull_vouchers': 'Voucher',
               'pull_balances': 'Ledger', 'pull_costcentres': 'CostCentre',
               'pull_godowns': 'Godown'}[purpose]
        cursor = {'pull_groups': self.last_alter_group, 'pull_vchtypes': self.last_alter_vchtype,
                  'pull_ledgers': self.last_alter_ledger, 'pull_items': self.last_alter_item,
                  'pull_vouchers': self.last_alter_voucher,
                  'pull_costcentres': self.last_alter_costcentre,
                  'pull_godowns': self.last_alter_godown}.get(purpose, 0)
        kw = {'company': self.tally_company, 'alter_id_gt': None if full else cursor}
        if obj == 'Voucher':
            kw['from_date'] = from_date or self.pull_from_date or fields.Date.today().replace(
                month=4, day=1)
            kw['to_date'] = to_date or fields.Date.today() + timedelta(days=366)
        if purpose == 'pull_balances':
            kw['to_date'] = to_date or fields.Date.today()
            kw['alter_id_gt'] = None
            kw['name'] = 'ZivoBalances'
        return TX.build_collection_request(obj, **kw)

    def _queue_pulls(self, only_if_due=False, full=False):
        self.ensure_one()
        if self.transport == 'file':
            return
        Req = self.env['tally.request']
        if Req.search_count([('connection_id', '=', self.id), ('direction', '=', 'in'),
                             ('state', 'in', ('pending', 'sent'))]):
            return
        if only_if_due and self.last_pull_date and \
                self.last_pull_date > fields.Datetime.now() - timedelta(seconds=55):
            return
        prio = {'pull_groups': 1, 'pull_vchtypes': 2, 'pull_godowns': 2, 'pull_costcentres': 2,
                'pull_ledgers': 3, 'pull_items': 4, 'pull_vouchers': 5}
        for purpose in self._pulls_wanted():
            Req._enqueue(self, purpose, self._build_pull(purpose, full=full),
                         priority=prio[purpose])
        self.last_pull_date = fields.Datetime.now()

    def action_pull_now(self, full=False):
        for rec in self:
            rec._queue_pulls(full=full)
            self.env['tally.request']._process_queue(rec)
            self.env['tally.voucher'].search([('connection_id', '=', rec.id),
                                              ('state', '=', 'draft')])._process()
        return self._notify(_('Pull queued/completed.'), 'success')

    def action_full_resync(self):
        self.write({'last_alter_ledger': 0, 'last_alter_item': 0, 'last_alter_voucher': 0,
                    'last_alter_vchtype': 0, 'last_alter_group': 0,
                    'last_alter_costcentre': 0, 'last_alter_godown': 0})
        return self.action_pull_now(full=True)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------
    def _on_test(self, req, text):
        companies = [c.get('@name') or c.get('name') for c in TX.parse_collection(text, 'COMPANY')]
        ok = self.tally_company in companies
        self.write({'state': 'connected' if ok else 'error', 'last_test_message': (
            _('Agent reached TallyPrime. Company "%s" is open.') % self.tally_company) if ok else
            _('Company "%(c)s" is not open in Tally. Open companies: %(l)s') % {
                'c': self.tally_company, 'l': ', '.join(filter(None, companies)) or '-'}})
        req.records_in = len(companies)

    def _on_pull_vchtypes(self, req, text):
        rows = TX.parse_collection(text, 'VOUCHERTYPE')
        Map = self.env['tally.mapping']
        top = 0
        for r in rows:
            name = r.get('@name') or TX._list_name(r)
            if not name:
                continue
            Map._upsert(self, 'vchtype', name, {'tally_parent': r.get('parent') or name,
                                                'tally_guid': r.get('guid'), 'state': 'confirmed',
                                                'origin': 'tally'})
            top = max(top, int(float(r.get('alterid') or 0)))
        req.records_in = len(rows)
        if top > self.last_alter_vchtype:
            self.last_alter_vchtype = top

    def _on_pull_groups(self, req, text):
        rows = TX.parse_collection(text, 'GROUP')
        Map = self.env['tally.mapping']
        top = self.last_alter_group
        for r in rows:
            name = r.get('@name') or TX._list_name(r)
            if not name:
                continue
            Map._upsert(self, 'group', name, {'tally_parent': r.get('parent') or '',
                                              'tally_primary_group': r.get('zprimarygroup') or '',
                                              'tally_guid': r.get('guid'), 'state': 'confirmed',
                                              'origin': 'tally', 'synced_to_tally': True})
            top = max(top, int(float(r.get('alterid') or 0)))
        req.records_in = len(rows)
        self.last_alter_group = top

    def _group_chain(self, group):
        """[group, parent, grand-parent, ...] using pulled Tally groups."""
        Map = self.env['tally.mapping']
        chain, seen = [], set()
        while group and group.lower() not in seen and len(chain) < 25:
            chain.append(group)
            seen.add(group.lower())
            m = Map._find(self, 'group', tally_name=group)
            group = m.tally_parent if m else ''
        return chain

    def _plan_for_category(self, category):
        """Analytic plan for a Tally cost category (mapped, same name, or default)."""
        Map = self.env['tally.mapping']
        m = Map._find(self, 'costcategory', tally_name=category or 'Primary Cost Category')
        if m and m.analytic_plan_id:
            return m.analytic_plan_id
        Plan = self.env['account.analytic.plan']
        plan = Plan.browse()
        if (category or 'Primary Cost Category') == 'Primary Cost Category':
            plan = self.primary_plan_id or Plan.search([('parent_id', '=', False)], limit=1)
        else:
            plan = Plan.search([('name', '=ilike', category), ('parent_id', '=', False)], limit=1)
        if not plan and self._dir_allows('sync_cost_centres', 'pull'):
            plan = Plan.create({'name': category or _('Tally Cost Centres')})
        if plan:
            Map._upsert(self, 'costcategory', category or 'Primary Cost Category', {
                'analytic_plan_id': plan.id, 'state': 'confirmed', 'synced_to_tally': True,
                'origin': 'tally'})
        return plan

    def _on_pull_costcentres(self, req, text):
        rows = [TX.normalize_simple_master(r) for r in TX.parse_collection(text, 'COSTCENTRE')]
        Map = self.env['tally.mapping'].with_context(tally_no_push=True)
        Account = self.env['account.analytic.account']
        top = self.last_alter_costcentre
        for cc in rows:
            if not cc['name']:
                continue
            category = cc['category'] or 'Primary Cost Category'
            m = Map._find(self, 'costcentre', tally_name=cc['name'])
            vals = {'tally_guid': cc['guid'], 'tally_master_id': cc['master_id'],
                    'tally_parent': category, 'synced_to_tally': True,
                    'tally_data': json.dumps(cc, default=str)}
            if m:
                m.write(vals)
            else:
                m = Map.create(dict(vals, connection_id=self.id, kind='costcentre',
                                    tally_name=cc['name'], origin='tally'))
            m.tally_alter_id = max(cc['alter_id'], m.tally_alter_id)
            top = max(top, cc['alter_id'])
            if m.analytic_account_id or m.state == 'ignored':
                continue
            plan = self._plan_for_category(category)
            cands = Account.search([('plan_id', 'child_of', plan.ids)] if plan else [])
            best, score = Account, 0.0
            for a in cands:
                sc = 1.0 if (a.code and a.code.lower() == cc['name'].lower()) else \
                    matching.name_similarity(cc['name'], a.name)
                if sc > score:
                    best, score = a, sc
            if best and score >= (self.ai_match_threshold or 0.92):
                m.write({'analytic_account_id': best.id, 'state': 'confirmed', 'origin': 'ai',
                         'confidence': score, 'match_reason': 'name %.0f%%' % (score * 100)})
            elif best and score >= (self.ai_suggest_threshold or 0.6):
                m.write({'analytic_account_id': best.id, 'state': 'suggested', 'origin': 'ai',
                         'confidence': score, 'match_reason': 'name %.0f%%' % (score * 100)})
            elif plan and self._dir_allows('sync_cost_centres', 'pull'):
                acc = Account.create({'name': cc['name'], 'plan_id': plan.id,
                                      'company_id': self.company_id.id})
                m.write({'analytic_account_id': acc.id, 'state': 'confirmed',
                         'odoo_write_date': acc.write_date})
        req.records_in = len(rows)
        self.last_alter_costcentre = top

    def _base_vch_kind(self, vch_type):
        """Resolve custom voucher types (e.g. 'GST Sales') to their base kind."""
        kinds = self._vch_type_kind()
        lower = {k.lower(): v for k, v in kinds.items() if k}
        seen = set()
        name = vch_type or ''
        Map = self.env['tally.mapping']
        while name and name.lower() not in seen:
            if name.lower() in lower:
                return lower[name.lower()]
            seen.add(name.lower())
            m = Map._find(self, 'vchtype', tally_name=name)
            name = m.tally_parent if m and m.tally_parent != name else ''
        base = {'sales': 'sale', 'purchase': 'purchase', 'credit note': 'credit_note',
                'debit note': 'debit_note', 'receipt': 'receipt', 'payment': 'payment',
                'journal': 'journal', 'contra': 'contra'}
        for k, v in base.items():
            if k in (vch_type or '').lower():
                return v
        return ''

    def _ledger_kind(self, led):
        role = ''
        special = {(self.group_customers or '').lower(): 'customer',
                   (self.group_vendors or '').lower(): 'vendor'}
        for g in self._group_chain(led['parent']):  # closest reserved group wins
            role = special.get(g.lower()) or TX.primary_role(g)
            if role:
                break
        role = role or TX.primary_role(led['primary_group'])
        parent = (led['parent'] or '').lower()
        if not role:
            if parent == (self.group_customers or '').lower():
                role = 'customer'
            elif parent == (self.group_vendors or '').lower():
                role = 'vendor'
            else:
                role = matching.classify_ledger_by_name(led['name'])
        kind = {'customer': 'party', 'vendor': 'party', 'tax': 'tax', 'bank': 'bank',
                'cash': 'bank'}.get(role, 'ledger')
        return kind, role or False

    def _on_pull_ledgers(self, req, text):
        rows = [TX.normalize_ledger(r) for r in TX.parse_collection(text, 'LEDGER')]
        Map = self.env['tally.mapping'].with_context(tally_no_push=True)
        top = self.last_alter_ledger
        touched = Map
        for led in rows:
            if not led['name']:
                continue
            kind, role = self._ledger_kind(led)
            m = Map._find(self, kind, tally_name=led['name'])
            if not m and led['guid']:
                m = Map.search([('connection_id', '=', self.id), ('tally_guid', '=', led['guid'])],
                               limit=1)
            vals = {'tally_guid': led['guid'], 'tally_master_id': led['master_id'],
                    'tally_parent': led['parent'], 'tally_primary_group': led['primary_group'],
                    'tally_role': role, 'tally_gstin': led['gstin'],
                    'tally_balance': -led['closing_balance'],
                    'tally_data': json.dumps(led, default=str), 'synced_to_tally': True}
            changed = not m or led['alter_id'] > (m.tally_alter_id or 0)
            if m:
                if m.kind != kind:
                    vals['kind'] = kind
                if m.tally_name != led['name']:
                    vals['tally_name'] = led['name']
                m.write(vals)
            else:
                m = Map.create(dict(vals, connection_id=self.id, kind=kind,
                                    tally_name=led['name'], origin='tally'))
            if changed and kind == 'party' and m.partner_id:
                self._apply_master_from_tally(m, led)
            m.tally_alter_id = max(led['alter_id'], m.tally_alter_id)
            touched |= m
            top = max(top, led['alter_id'])
        self._ai_match(touched.filtered(lambda m: not m._target() and m.state != 'ignored'))
        # Create contacts for parties still unmatched when pulling parties is enabled
        if self._dir_allows('sync_parties', 'pull'):
            for m in touched.filtered(lambda m: m.kind == 'party' and not m.partner_id
                                      and m.state == 'unmapped'):
                self._apply_master_from_tally(m, json.loads(m.tally_data or '{}'))
        req.records_in = len(rows)
        self.last_alter_ledger = top

    def _on_pull_items(self, req, text):
        rows = [TX.normalize_stock_item(r) for r in TX.parse_collection(text, 'STOCKITEM')]
        Map = self.env['tally.mapping'].with_context(tally_no_push=True)
        top = self.last_alter_item
        touched = Map
        for it in rows:
            if not it['name']:
                continue
            m = Map._find(self, 'item', tally_name=it['name'])
            vals = {'tally_guid': it['guid'], 'tally_master_id': it['master_id'],
                    'tally_parent': it['parent'], 'tally_data': json.dumps(it, default=str),
                    'synced_to_tally': True}
            changed = not m or it['alter_id'] > (m.tally_alter_id or 0)
            if m:
                m.write(vals)
            else:
                m = Map.create(dict(vals, connection_id=self.id, kind='item',
                                    tally_name=it['name'], origin='tally'))
            if changed and m.product_id:
                self._apply_master_from_tally(m, it)
            m.tally_alter_id = max(it['alter_id'], m.tally_alter_id)
            touched |= m
            top = max(top, it['alter_id'])
        self._ai_match(touched.filtered(lambda m: not m.product_id and m.state != 'ignored'))
        for m in touched.filtered(lambda m: not m.product_id and m.state == 'unmapped'):
            self._apply_master_from_tally(m, json.loads(m.tally_data or '{}'))
        req.records_in = len(rows)
        self.last_alter_item = top

    def _on_pull_vouchers(self, req, text):
        rows = [TX.normalize_voucher(r) for r in TX.parse_collection(text, 'VOUCHER')]
        Stage = self.env['tally.voucher']
        top = self.last_alter_voucher
        n = 0
        for v in rows:
            top = max(top, v['alter_id'])
            if self._is_own_remote(v['guid']) or self._is_own_remote(v['remote_id']):
                continue  # echo of a voucher Odoo pushed
            kind = self._base_vch_kind(v['vch_type'])
            if not kind or not self._dir_allows(self._kind_direction_field(kind), 'pull'):
                continue
            if v['optional']:
                continue
            Stage._upsert_from_tally(self, v, kind)
            n += 1
        req.records_in = n
        self.last_alter_voucher = top

    # ------------------------------------------------------------------
    # Master application
    # ------------------------------------------------------------------
    def _partner_vals_from_ledger(self, led):
        Country = self.env['res.country']
        country = Country.search([('name', '=ilike', led.get('country') or 'India')], limit=1) \
            or self.env.ref('base.in', raise_if_not_found=False)
        state = self.env['res.country.state']
        if led.get('state'):
            state = state.search([('name', '=ilike', led['state'])] +
                                 ([('country_id', '=', country.id)] if country else []), limit=1)
        if not state and led.get('gstin'):
            sname = matching.state_from_gstin(led['gstin'])
            if sname:
                state = self.env['res.country.state'].search([('name', '=ilike', sname)], limit=1)
        addr = led.get('address') or []
        vals = {
            'name': led['name'], 'is_company': True,
            'street': addr[0] if len(addr) > 0 else False,
            'street2': ', '.join(addr[1:-1]) if len(addr) > 2 else (addr[1] if len(addr) > 1
                                                                     else False),
            'city': addr[-1] if len(addr) > 2 else False,
            'zip': led.get('pincode') or False, 'state_id': state.id or False,
            'country_id': country.id if country else False,
            'email': led.get('email') or False,
            'phone': led.get('phone') or led.get('mobile') or False,
            'company_id': False,
        }
        if led.get('gstin') and matching.valid_gstin(led['gstin']):
            vals['vat'] = led['gstin']
        if 'l10n_in_gst_treatment' in self.env['res.partner']._fields:
            treat = GST_TREATMENT.get((led.get('registration_type') or '').lower())
            if treat:
                vals['l10n_in_gst_treatment'] = treat
        return {k: v for k, v in vals.items() if v is not False or k in ('company_id',)}

    def _product_vals_from_item(self, it):
        vals = {'name': it['name'], 'type': 'consu', 'sale_ok': True, 'purchase_ok': True}
        if it.get('part_no'):
            vals['default_code'] = it['part_no']
        if it.get('price'):
            vals['list_price'] = it['price']
        if it.get('cost') or it.get('closing_rate'):
            vals['standard_price'] = it.get('cost') or it.get('closing_rate')
        if it.get('hsn') and 'l10n_in_hsn_code' in self.env['product.template']._fields:
            vals['l10n_in_hsn_code'] = it['hsn']
        uom = self._uom_for_unit(it.get('unit'))
        if uom:
            vals['uom_id'] = uom.id
        if it.get('gst_rate') is not None:
            st = self._find_tax_by_rate(it['gst_rate'], 'sale')
            pt = self._find_tax_by_rate(it['gst_rate'], 'purchase')
            if st:
                vals['taxes_id'] = [(6, 0, st.ids)]
            if pt:
                vals['supplier_taxes_id'] = [(6, 0, pt.ids)]
        return vals

    def _uom_for_unit(self, unit):
        if not unit:
            return self.env['uom.uom']
        m = self.env['tally.mapping']._find(self, 'unit', tally_name=unit)
        if m and m.uom_id:
            return m.uom_id
        rev = {'nos': 'Units', 'pcs': 'Units', 'no': 'Units', 'kg': 'kg', 'kgs': 'kg',
               'ltr': 'L', 'mtr': 'm', 'dozen': 'Dozens', 'hrs': 'Hours', 'days': 'Days'}
        name = rev.get(unit.lower(), unit)
        uom = self.env['uom.uom'].search([('name', '=ilike', name)], limit=1)
        if uom:
            self.env['tally.mapping']._upsert(self, 'unit', unit, {
                'uom_id': uom.id, 'state': 'confirmed', 'synced_to_tally': True,
                'origin': 'tally'})
        return uom

    def _find_tax_by_rate(self, rate, use):
        Tax = self.env['account.tax']
        dom = [('company_id', '=', self.company_id.id), ('type_tax_use', '=', use),
               ('active', '=', True)]
        rate = float(rate or 0)
        groups = Tax.search(dom + [('amount_type', '=', 'group')]).sorted(
            lambda t: (matching.tax_rank(t.name), t.sequence, t.id))
        for t in groups:
            if abs(sum(c.amount for c in t.children_tax_ids) - rate) < 0.001:
                if not self.company_id.state_id or 'igst' not in (t.name or '').lower():
                    return t
        return Tax.search(dom + [('amount_type', '=', 'percent'), ('amount', '=', rate)]).sorted(
            lambda t: (matching.tax_rank(t.name), t.sequence, t.id))[:1]

    @staticmethod
    def _norm(rec, field, value):
        cur = rec[field]
        if hasattr(cur, 'ids'):
            if isinstance(value, (list, tuple)) and value and isinstance(value[0], (list, tuple)):
                value = value[0][2] if value[0][0] == 6 else value
            return sorted(cur.ids), sorted(value if isinstance(value, (list, tuple)) else
                                           [value] if value else [])
        if isinstance(cur, float):
            return round(cur, 2), round(float(value or 0), 2)
        return (cur or ''), (value or '')

    def _changed_only(self, rec, vals):
        out = {}
        for k, v in vals.items():
            if k not in rec._fields:
                continue
            a, b = self._norm(rec, k, v)
            if a != b:
                out[k] = v
        return out

    def _differs(self, rec, vals):
        """True when the Tally values really disagree with Odoo's current values."""
        return bool(self._changed_only(rec, {k: v for k, v in vals.items()
                                             if k not in ('company_id', 'is_company', 'type',
                                                          'sale_ok', 'purchase_ok')}))

    def _apply_master_from_tally(self, mapping, data, force=False):
        """Create/update the Odoo record behind a Tally master, honouring conflicts."""
        if not data:
            return
        ctx = dict(self.env.context, tally_no_push=True)
        if mapping.kind == 'party':
            if not (force or self._dir_allows('sync_parties', 'pull')):
                return
            vals = self._partner_vals_from_ledger(data)
            partner = mapping.partner_id
            if partner:
                if not force and mapping._odoo_changed_since_sync() and \
                        self._differs(partner, vals):
                    if self.conflict_policy == 'manual':
                        mapping.write({'state': 'conflict', 'conflict_data': json.dumps(
                            data, default=str)})
                        return
                    if self.conflict_policy == 'odoo':
                        return
                vals.pop('company_id', None)
                vals = self._changed_only(partner, vals)
                if vals:
                    partner.with_context(ctx).write(vals)
            else:
                rank = 'supplier_rank' if mapping.tally_role == 'vendor' else 'customer_rank'
                vals[rank] = 1
                partner = self.env['res.partner'].with_context(ctx).create(vals)
                mapping.write({'partner_id': partner.id, 'state': 'confirmed', 'origin': 'tally'})
            mapping._stamp_synced()
            mapping.last_sync = fields.Datetime.now()
        elif mapping.kind == 'item':
            if not (force or self._dir_allows('sync_items', 'pull')):
                return
            vals = self._product_vals_from_item(data)
            product = mapping.product_id
            if product:
                if not force and mapping._odoo_changed_since_sync() and \
                        self._differs(product, vals):
                    if self.conflict_policy == 'manual':
                        mapping.write({'state': 'conflict', 'conflict_data': json.dumps(
                            data, default=str)})
                        return
                    if self.conflict_policy == 'odoo':
                        return
                vals.pop('type', None)
                if product.product_tmpl_id.product_variant_count > 1:
                    vals.pop('name', None)
                vals = self._changed_only(product, vals)
                if vals:
                    product.with_context(ctx).write(vals)
            else:
                product = self.env['product.product'].with_context(ctx).create(vals)
                mapping.write({'product_id': product.id, 'state': 'confirmed', 'origin': 'tally'})
            mapping._stamp_synced()
            mapping.last_sync = fields.Datetime.now()
