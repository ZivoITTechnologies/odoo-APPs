# -*- coding: utf-8 -*-
import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError

KINDS = [
    ('party', 'Party ledger / Contact'),
    ('ledger', 'Ledger / Account'),
    ('tax', 'Duty ledger / Tax'),
    ('bank', 'Bank or cash ledger / Journal'),
    ('item', 'Stock item / Product'),
    ('unit', 'Unit / Unit of measure'),
    ('costcentre', 'Cost centre / Analytic account'),
    ('costcategory', 'Cost category / Analytic plan'),
    ('vchtype', 'Voucher type'),
    ('group', 'Group'),
]


class TallyMapping(models.Model):
    _name = 'tally.mapping'
    _description = 'Tally and Odoo mapping'
    _order = 'kind, tally_name'
    _rec_name = 'tally_name'

    connection_id = fields.Many2one('tally.connection', required=True, ondelete='cascade',
                                    index=True)
    company_id = fields.Many2one(related='connection_id.company_id', store=True)
    kind = fields.Selection(KINDS, required=True, index=True)
    tally_name = fields.Char(required=True, index=True)
    tally_guid = fields.Char(index=True, copy=False)
    tally_master_id = fields.Integer(copy=False)
    tally_alter_id = fields.Integer(copy=False)
    tally_parent = fields.Char()
    tally_primary_group = fields.Char()
    tally_role = fields.Selection([('customer', 'Customer'), ('vendor', 'Vendor'),
                                   ('tax', 'Tax'), ('bank', 'Bank'), ('cash', 'Cash'),
                                   ('income', 'Income'), ('expense', 'Expense'),
                                   ('asset', 'Asset'), ('liability', 'Liability'),
                                   ('equity', 'Equity')])
    tally_gstin = fields.Char('GSTIN (Tally)')
    tally_balance = fields.Float('Tally closing balance', digits=(16, 2),
                                 help='Positive = debit, as in Odoo.')
    tally_data = fields.Text('Last Tally payload (JSON)')

    partner_id = fields.Many2one('res.partner', index=True, ondelete='set null')
    account_id = fields.Many2one('account.account', index=True, ondelete='set null')
    tax_id = fields.Many2one('account.tax', index=True, ondelete='set null')
    journal_id = fields.Many2one('account.journal', index=True, ondelete='set null')
    product_id = fields.Many2one('product.product', index=True, ondelete='set null')
    uom_id = fields.Many2one('uom.uom', ondelete='set null')
    analytic_account_id = fields.Many2one('account.analytic.account', index=True,
                                          ondelete='set null')
    analytic_plan_id = fields.Many2one('account.analytic.plan', ondelete='set null')

    state = fields.Selection([('suggested', 'AI suggestion'), ('confirmed', 'Confirmed'),
                              ('unmapped', 'Unmapped'), ('conflict', 'Conflict'),
                              ('deleted', 'Deleted in Tally'), ('ignored', 'Ignored')],
                             default='unmapped', index=True)
    origin = fields.Selection([('tally', 'From Tally'), ('odoo', 'From Odoo'),
                               ('ai', 'AI match'), ('manual', 'Manual')], default='manual')
    confidence = fields.Float(digits=(3, 2))
    match_reason = fields.Char()
    alternatives = fields.Text('Other candidates')
    synced_to_tally = fields.Boolean(copy=False)
    odoo_hash = fields.Char(copy=False, help='Fingerprint of the synced Odoo values at last sync.')
    pending_name = fields.Char(copy=False, help='New Tally name waiting to be applied (rename).')
    last_sync = fields.Datetime(copy=False)
    odoo_write_date = fields.Datetime('Odoo version synced', copy=False)
    conflict_data = fields.Text(copy=False)
    odoo_record = fields.Char(compute='_compute_odoo_record')

    _uniq = models.Constraint('unique(connection_id, kind, tally_name)',
                              'This Tally master is already mapped on this connection.')

    @api.model
    def _target_fields(self):
        """kind -> Many2one field holding the Odoo record (extended by bridges)."""
        return {'party': 'partner_id', 'ledger': 'account_id', 'tax': 'tax_id',
                'bank': 'journal_id', 'item': 'product_id', 'unit': 'uom_id',
                'costcentre': 'analytic_account_id', 'costcategory': 'analytic_plan_id'}

    @api.depends('partner_id', 'account_id', 'tax_id', 'journal_id', 'product_id', 'uom_id',
                 'analytic_account_id', 'analytic_plan_id')
    def _compute_odoo_record(self):
        for m in self:
            rec = m._target()
            m.odoo_record = rec.display_name if rec else False

    def _target(self):
        self.ensure_one()
        field = self._target_fields().get(self.kind)
        return self[field] if field else self.env['res.partner']

    # ------------------------------------------------------------------
    @api.model
    def _find(self, conn, kind, tally_name=None, record=None):
        dom = [('connection_id', '=', conn.id), ('kind', '=', kind)]
        if tally_name is not None:
            dom.append(('tally_name', '=ilike', tally_name))
        if record is not None:
            field = self._target_fields()[kind]
            dom.append((field, '=', record.id))
            dom.append(('state', '!=', 'ignored'))
        return self.search(dom, limit=1, order='state desc, id')

    @api.model
    def _upsert(self, conn, kind, tally_name, vals):
        m = self._find(conn, kind, tally_name)
        if m:
            m.write(vals)
            return m
        vals = dict(vals, connection_id=conn.id, kind=kind, tally_name=tally_name)
        return self.create(vals)

    # Odoo fields that are actually exchanged with Tally, per master kind.  A change to
    # any other field (customer rank, followers, computed totals...) is not a conflict.
    SYNC_FIELDS = {
        'party': ('name', 'vat', 'street', 'street2', 'city', 'zip', 'state_id', 'country_id',
                  'email', 'phone'),
        'item': ('name', 'default_code', 'list_price', 'uom_id', 'taxes_id'),
    }

    def _odoo_fingerprint(self):
        self.ensure_one()
        rec = self._target()
        names = self.SYNC_FIELDS.get(self.kind)
        if not rec or not names:
            return False
        parts = []
        for f in names:
            if f not in rec._fields:
                continue
            v = rec[f]
            if hasattr(v, 'ids'):
                v = sorted(v.ids)
            parts.append('%s=%s' % (f, v))
        import hashlib
        return hashlib.sha1('|'.join(parts).encode()).hexdigest()

    def _stamp_synced(self):
        for m in self:
            rec = m._target()
            m.write({'odoo_hash': m._odoo_fingerprint(),
                     'odoo_write_date': rec.write_date if rec else fields.Datetime.now()})

    def _odoo_changed_since_sync(self):
        self.ensure_one()
        rec = self._target()
        if not rec:
            return False
        if self.odoo_hash:
            return self._odoo_fingerprint() != self.odoo_hash
        return bool(self.odoo_write_date and rec.write_date
                    and rec.write_date > self.odoo_write_date)

    # ------------------------------------------------------------------
    def action_confirm(self):
        for m in self:
            if not m._target():
                raise UserError(_('Select the Odoo record for "%s" first.') % m.tally_name)
        self.write({'state': 'confirmed'})
        return True

    def action_ignore(self):
        self.write({'state': 'ignored'})
        return True

    def action_push_to_tally(self):
        for conn in self.mapped('connection_id'):
            maps = self.filtered(lambda m: m.connection_id == conn)
            conn._push_masters(partners=maps.mapped('partner_id'),
                               products=maps.mapped('product_id'))
            self.env['tally.request']._process_queue(conn)
        return True

    def action_resolve_keep_tally(self):
        for m in self.filtered(lambda m: m.state == 'conflict'):
            data = json.loads(m.conflict_data or '{}')
            m.connection_id._apply_master_from_tally(m, data, force=True)
            m.write({'state': 'confirmed', 'conflict_data': False})
        return True

    def action_resolve_keep_odoo(self):
        for m in self.filtered(lambda m: m.state == 'conflict'):
            m.write({'state': 'confirmed', 'conflict_data': False})
            m.action_push_to_tally()
        return True

    def action_ai_rematch(self):
        for conn in self.mapped('connection_id'):
            conn._ai_match(self.filtered(lambda m: m.connection_id == conn), force=True)
        return True
