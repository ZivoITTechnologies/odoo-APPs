# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

CHARGE_BASIS = [
    ('trip', 'Per Trip'),
    ('ton', 'Per Tonne'),
    ('km', 'Per Km'),
    ('ton_km', 'Per Tonne-Km'),
    ('kg', 'Per Kg (LTL)'),
    ('box', 'Per Box / Package (LTL)'),
]
LTL_BASIS = ('kg', 'box')


class CpRateContract(models.Model):
    _name = 'cp.rate.contract'
    _description = 'Freight Rate Contract'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date_start desc, id desc'

    name = fields.Char(default=lambda self: _('New'), copy=False, readonly=True)
    transporter_id = fields.Many2one('res.partner', required=True, tracking=True,
                                     domain=[('cp_is_transporter', '=', True)])
    date_start = fields.Date(required=True, default=fields.Date.context_today, tracking=True)
    date_end = fields.Date(required=True, tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('active', 'Active'), ('expired', 'Expired'), ('cancel', 'Cancelled'),
    ], default='draft', tracking=True)
    priority = fields.Integer(default=10, help='Lower number is offered indents first.')
    line_ids = fields.One2many('cp.rate.contract.line', 'contract_id', copy=True)
    rfq_id = fields.Many2one('cp.rfq', string='Source RFQ', readonly=True)
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    notes = fields.Html()

    # Fuel / diesel escalation clause
    diesel_clause = fields.Boolean(string='Diesel Escalation Clause', tracking=True,
                                   help='Revise freight when the diesel price moves away from the base price.')
    base_diesel_price = fields.Monetary(string='Base Diesel Price / Litre')
    fuel_component_pct = fields.Float(string='Fuel Share of Freight (%)', default=40.0,
                                      help='Freight changes by this share of the diesel price change. '
                                           'Example: 40% share and diesel up 5% = freight up 2%.')
    diesel_threshold_pct = fields.Float(string='Revise When Diesel Moves (%)', default=2.0,
                                        help='Smaller moves are ignored.')
    season_ids = fields.One2many('cp.rate.season', 'contract_id', string='Peak Seasons', copy=True)
    line_count = fields.Integer(compute='_compute_line_count')
    has_ltl = fields.Boolean(compute='_compute_line_count', string='Has LTL Rates')

    @api.depends('line_ids.charge_basis')
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)
            rec.has_ltl = any(l.charge_basis in LTL_BASIS for l in rec.line_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('cp.rate.contract') or _('New')
        return super().create(vals_list)

    @api.constrains('date_start', 'date_end')
    def _check_dates(self):
        for rec in self:
            if rec.date_end and rec.date_start and rec.date_end < rec.date_start:
                raise ValidationError(_('Contract end date must be after its start date.'))

    def action_activate(self):
        for rec in self:
            if not rec.line_ids:
                raise UserError(_('Add at least one lane rate before activating %s.', rec.name))
            if rec.diesel_clause and not rec.base_diesel_price:
                raise UserError(_('Enter the base diesel price for the escalation clause on %s.', rec.name))
        self.write({'state': 'active'})

    def action_cancel(self):
        self.write({'state': 'cancel'})

    def action_draft(self):
        self.write({'state': 'draft'})

    @api.model
    def _cron_expire_contracts(self):
        today = fields.Date.context_today(self)
        self.search([('state', '=', 'active'), ('date_end', '<', today)]).write({'state': 'expired'})

    # ------------------------------------------------------------------
    # Pricing helpers
    # ------------------------------------------------------------------
    def _cp_diesel_factor(self, on_date=None):
        """Multiplier from the diesel escalation clause (1.0 when not applicable)."""
        self.ensure_one()
        if not self.diesel_clause or not self.base_diesel_price:
            return 1.0
        current = self.env['cp.diesel.price'].cp_price_on(on_date, self.company_id)
        if not current:
            return 1.0
        change_pct = (current - self.base_diesel_price) / self.base_diesel_price * 100.0
        if abs(change_pct) < (self.diesel_threshold_pct or 0.0):
            return 1.0
        return 1.0 + change_pct * (self.fuel_component_pct or 0.0) / 100.0 / 100.0

    def _cp_season_pct(self, on_date=None):
        self.ensure_one()
        on_date = fields.Date.to_date(on_date) or fields.Date.context_today(self)
        seasons = self.season_ids.filtered(lambda s: s.date_from <= on_date <= s.date_to)
        return max(seasons.mapped('surcharge_pct') or [0.0])

    @api.model
    def cp_find_rate(self, transporter, lane, vehicle_type, on_date=None, mode='ftl', exclude_transporters=None):
        """Best matching active contract line (empty recordset when none).

        FTL lines match on the lane and truck type. LTL lines (per kg / per box) match on the lane, or on
        the origin and destination states when the line has no lane (zone rates).
        """
        lines = self.cp_find_rates(lane, vehicle_type, on_date=on_date, mode=mode, transporter=transporter,
                                   exclude_transporters=exclude_transporters)
        return lines[:1]

    @api.model
    def cp_find_rates(self, lane, vehicle_type=None, on_date=None, mode='ftl', transporter=None,
                      exclude_transporters=None):
        """All matching lines, best first (contract priority, then rate)."""
        on_date = fields.Date.to_date(on_date) or fields.Date.context_today(self)
        domain = [
            ('contract_id.state', '=', 'active'),
            ('contract_id.date_start', '<=', on_date),
            ('contract_id.date_end', '>=', on_date),
        ]
        if transporter:
            domain.append(('contract_id.transporter_id', '=', transporter.id))
        if exclude_transporters:
            domain.append(('contract_id.transporter_id', 'not in', exclude_transporters.ids))
        if mode == 'ltl':
            domain.append(('charge_basis', 'in', LTL_BASIS))
            dest_state = lane.destination_id.state_id
            orig_state = lane.origin_id.state_id
            domain += ['|', ('lane_id', '=', lane.id), '&', ('lane_id', '=', False),
                       ('dest_state_id', '=', dest_state.id if dest_state else -1)]
            lines = self.env['cp.rate.contract.line'].search(domain, order='id')
            lines = lines.filtered(lambda l: l.lane_id or not l.origin_state_id or l.origin_state_id == orig_state)
            # A lane-specific rate beats a zone rate of the same transporter.
            best = {}
            for line in lines.sorted(lambda l: (0 if l.lane_id else 1)):
                best.setdefault(line.contract_id.transporter_id.id, line)
            lines = self.env['cp.rate.contract.line'].browse([l.id for l in best.values()])
        else:
            domain += [('lane_id', '=', lane.id), ('charge_basis', 'not in', LTL_BASIS)]
            lines = self.env['cp.rate.contract.line'].search(domain, order='id')
            exact = lines.filtered(lambda l: vehicle_type and l.vehicle_type_id == vehicle_type)
            generic = lines.filtered(lambda l: not l.vehicle_type_id)
            lines = exact | generic.filtered(lambda g: g.contract_id.transporter_id not in exact.contract_id.transporter_id)
        return lines.sorted(lambda l: (l.contract_id.priority, l.rate))


class CpRateSeason(models.Model):
    _name = 'cp.rate.season'
    _description = 'Peak Season Surcharge'
    _order = 'date_from'

    contract_id = fields.Many2one('cp.rate.contract', required=True, ondelete='cascade')
    name = fields.Char(required=True, default=lambda self: _('Festive peak'))
    date_from = fields.Date(required=True)
    date_to = fields.Date(required=True)
    surcharge_pct = fields.Float(string='Surcharge (%)', required=True)

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for rec in self:
            if rec.date_to < rec.date_from:
                raise ValidationError(_('The season must end after it starts.'))


class CpDieselPrice(models.Model):
    _name = 'cp.diesel.price'
    _description = 'Diesel Price'
    _order = 'date desc, id desc'

    date = fields.Date(required=True, default=fields.Date.context_today)
    price = fields.Monetary(string='Price / Litre', required=True)
    region = fields.Char(help='Optional: city or state the price is for.')
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    @api.model
    def cp_price_on(self, on_date=None, company=None):
        on_date = fields.Date.to_date(on_date) or fields.Date.context_today(self)
        company = company or self.env.company
        row = self.search([('date', '<=', on_date), '|', ('company_id', '=', company.id),
                           ('company_id', '=', False)], limit=1)
        return row.price


class CpRateContractLine(models.Model):
    _name = 'cp.rate.contract.line'
    _description = 'Contract Lane Rate'

    contract_id = fields.Many2one('cp.rate.contract', required=True, ondelete='cascade')
    transporter_id = fields.Many2one(related='contract_id.transporter_id', store=True)
    lane_id = fields.Many2one('cp.lane', help='Leave empty on an LTL zone rate and use the states instead.')
    origin_state_id = fields.Many2one('res.country.state', string='From State',
                                      help='LTL zone rate: leave empty for any origin.')
    dest_state_id = fields.Many2one('res.country.state', string='To State', help='LTL zone rate.')
    vehicle_type_id = fields.Many2one('cp.vehicle.type', help='Leave empty to apply to any vehicle type.')
    charge_basis = fields.Selection(CHARGE_BASIS, default='trip', required=True)
    mode = fields.Selection([('ftl', 'FTL'), ('ltl', 'LTL')], compute='_compute_mode', store=True)
    rate = fields.Monetary(required=True)
    contracted_km = fields.Float(string='Contracted Km', help='Used for distance validation in freight audit.')
    min_weight_ton = fields.Float(string='Min. Chargeable Weight (t)')
    min_weight_kg = fields.Float(string='Min. Chargeable Weight (kg)', help='LTL: minimum kg charged per docket.')
    min_charge = fields.Monetary(string='Minimum Charge', help='Floor for the total freight of one shipment.')
    detention_per_day = fields.Monetary(string='Detention / Day')
    free_detention_hours = fields.Float(default=24.0)
    loading_charge = fields.Monetary()
    unloading_charge = fields.Monetary()
    docket_charge = fields.Monetary(string='Docket / LR Charge')
    oda_charge = fields.Monetary(string='ODA Charge', help='Added when the destination is an out-of-delivery-area site.')
    fov_pct = fields.Float(string='FOV / Risk (%)', help='Freight on value: percentage of the declared goods value.')
    fuel_surcharge_pct = fields.Float(string='Fuel Surcharge (%)')
    multi_drop_charge = fields.Monetary(string='Extra Drop Charge', help='Per delivery point after the first.')
    transit_days = fields.Float(string='Transit (days)', help='Committed transit time, used for the delivery promise.')
    weekly_capacity = fields.Integer(string='Trucks / Week', help='Capacity the transporter commits on this lane.')
    currency_id = fields.Many2one(related='contract_id.currency_id')

    @api.depends('charge_basis')
    def _compute_mode(self):
        for line in self:
            line.mode = 'ltl' if line.charge_basis in LTL_BASIS else 'ftl'

    @api.constrains('lane_id', 'dest_state_id', 'charge_basis')
    def _check_scope(self):
        for line in self:
            if not line.lane_id and line.charge_basis not in LTL_BASIS:
                raise ValidationError(_('FTL rates need a lane.'))
            if not line.lane_id and not line.dest_state_id:
                raise ValidationError(_('Set a lane, or a destination state for an LTL zone rate.'))

    def _compute_display_name(self):
        basis = dict(CHARGE_BASIS)
        for line in self:
            scope = line.lane_id.name or ('%s → %s' % (line.origin_state_id.name or _('Any'),
                                                        line.dest_state_id.name or ''))
            line.display_name = '%s · %s · %s %s' % (line.contract_id.name or '', scope, line.rate,
                                                     basis.get(line.charge_basis, ''))

    def cp_price_breakdown(self, weight_ton=0.0, distance_km=0.0, goods_value=0.0, boxes=0, drops=1,
                           on_date=None, oda=False):
        """Every component of the freight for one shipment under this line.

        Returns {'lines': [(label, amount)], 'total': float, 'base': float}.
        """
        self.ensure_one()
        lane = self.lane_id
        distance = distance_km or self.contracted_km or lane.distance_km
        weight_t = max(weight_ton or 0.0, self.min_weight_ton or 0.0)
        weight_kg = max((weight_ton or 0.0) * 1000.0, self.min_weight_kg or 0.0)
        basis = self.charge_basis
        if basis == 'ton':
            base, label = self.rate * weight_t, _('%(r)s / t x %(w)s t', r=self.rate, w=round(weight_t, 3))
        elif basis == 'km':
            base, label = self.rate * distance, _('%(r)s / km x %(d)s km', r=self.rate, d=round(distance, 1))
        elif basis == 'ton_km':
            base, label = self.rate * weight_t * distance, _('%(r)s / t-km', r=self.rate)
        elif basis == 'kg':
            base, label = self.rate * weight_kg, _('%(r)s / kg x %(w)s kg', r=self.rate, w=round(weight_kg, 1))
        elif basis == 'box':
            base, label = self.rate * max(boxes or 0, 1), _('%(r)s / box x %(b)s', r=self.rate, b=max(boxes or 0, 1))
        else:
            base, label = self.rate, _('Trip rate')
        lines = [(label, base)]
        contract = self.contract_id
        diesel = contract._cp_diesel_factor(on_date) if contract else 1.0
        if diesel != 1.0:
            lines.append((_('Diesel escalation %s%%', round((diesel - 1) * 100, 2)), base * (diesel - 1)))
        season = contract._cp_season_pct(on_date) if contract else 0.0
        if season:
            lines.append((_('Peak season %s%%', season), base * season / 100.0))
        if self.fuel_surcharge_pct:
            lines.append((_('Fuel surcharge %s%%', self.fuel_surcharge_pct), base * self.fuel_surcharge_pct / 100.0))
        if self.loading_charge:
            lines.append((_('Loading'), self.loading_charge))
        if self.unloading_charge:
            lines.append((_('Unloading'), self.unloading_charge))
        if self.docket_charge:
            lines.append((_('Docket / LR'), self.docket_charge))
        if oda and self.oda_charge:
            lines.append((_('ODA delivery'), self.oda_charge))
        if drops and drops > 1 and self.multi_drop_charge:
            lines.append((_('%s extra drop(s)', drops - 1), (drops - 1) * self.multi_drop_charge))
        if self.fov_pct and goods_value:
            lines.append((_('FOV %s%% of goods value', self.fov_pct), goods_value * self.fov_pct / 100.0))
        total = sum(amount for _label, amount in lines)
        if self.min_charge and total < self.min_charge:
            lines.append((_('Top-up to minimum charge'), self.min_charge - total))
            total = self.min_charge
        return {'lines': lines, 'total': round(total, 2), 'base': round(base, 2)}

    def cp_compute_amount(self, weight_ton=0.0, distance_km=0.0, **kwargs):
        """Freight for one trip or docket under this line."""
        self.ensure_one()
        return self.cp_price_breakdown(weight_ton=weight_ton, distance_km=distance_km, **kwargs)['total']
