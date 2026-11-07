# -*- coding: utf-8 -*-
from collections import defaultdict

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .cp_rate_contract import CHARGE_BASIS


class CpRfq(models.Model):
    _name = 'cp.rfq'
    _description = 'Freight RFQ (Contract Procurement)'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'portal.mixin']
    _order = 'id desc'

    name = fields.Char(default=lambda self: _('New'), copy=False, readonly=True)
    title = fields.Char(required=True, help='e.g. "North zone cement lanes FY27".')
    date_deadline = fields.Datetime(string='Quote Deadline', required=True, tracking=True)
    contract_start = fields.Date(required=True, default=fields.Date.context_today)
    contract_end = fields.Date(required=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('published', 'Open for Quotes'), ('evaluation', 'Evaluation'),
        ('awarded', 'Awarded'), ('cancel', 'Cancelled'),
    ], default='draft', tracking=True)
    line_ids = fields.One2many('cp.rfq.line', 'rfq_id', string='Lanes', copy=True)
    transporter_ids = fields.Many2many('res.partner', string='Invited Transporters',
                                       domain=[('cp_is_transporter', '=', True)])
    quote_ids = fields.One2many('cp.rfq.quote', 'rfq_id', string='Quotes')
    quote_count = fields.Integer(compute='_compute_quote_count')
    contract_ids = fields.One2many('cp.rate.contract', 'rfq_id', string='Contracts')
    price_weight = fields.Integer(string='Price Weight (%)', default=70,
                                  help='Remaining weight goes to transporter performance score.')
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('cp.rfq') or _('New')
        return super().create(vals_list)

    @api.depends('quote_ids')
    def _compute_quote_count(self):
        for rec in self:
            rec.quote_count = len(rec.quote_ids)

    def _compute_access_url(self):
        super()._compute_access_url()
        for rec in self:
            rec.access_url = '/my/cp/rfqs/%s' % rec.id

    def action_publish(self):
        for rec in self:
            if not rec.line_ids or not rec.transporter_ids:
                raise UserError(_('Add lanes and invite transporters before publishing.'))
            rec.state = 'published'
            rec.message_subscribe(partner_ids=rec.transporter_ids.ids)
            rec.message_post(
                body=_('RFQ %(name)s is open for quotes until %(date)s. Submit rates in your transporter portal.',
                       name=rec.name, date=fields.Datetime.to_string(rec.date_deadline)),
                partner_ids=rec.transporter_ids.ids,
                message_type='comment', subtype_xmlid='mail.mt_comment')

    def action_close_quotes(self):
        self.write({'state': 'evaluation'})

    def action_cancel(self):
        self.write({'state': 'cancel'})

    def action_select_best(self):
        """Mark the best-scoring quote of every lane as selected."""
        for rec in self:
            rec.quote_ids.write({'selected': False})
            for line in rec.line_ids:
                best = line.quote_ids.sorted(lambda q: -q.score)[:1]
                best.selected = True

    def action_award(self):
        Contract = self.env['cp.rate.contract']
        for rec in self:
            chosen = rec.quote_ids.filtered('selected')
            if not chosen:
                raise UserError(_('Select at least one quote (or use "Select Best") before awarding.'))
            by_transporter = defaultdict(lambda: self.env['cp.rfq.quote'])
            for quote in chosen:
                by_transporter[quote.transporter_id] |= quote
            for transporter, quotes in by_transporter.items():
                Contract.create({
                    'transporter_id': transporter.id,
                    'date_start': rec.contract_start,
                    'date_end': rec.contract_end,
                    'rfq_id': rec.id,
                    'line_ids': [(0, 0, {
                        'lane_id': q.line_id.lane_id.id,
                        'vehicle_type_id': q.line_id.vehicle_type_id.id,
                        'charge_basis': q.line_id.charge_basis,
                        'rate': q.rate,
                        'contracted_km': q.line_id.lane_id.distance_km,
                    }) for q in quotes],
                }).action_activate()
            rec.state = 'awarded'
            rec.message_post(body=_('Awarded to %s.', ', '.join(p.name for p in by_transporter)))
        return self.action_view_contracts()

    def action_view_contracts(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('cargo_pilot_tms.action_cp_rate_contract')
        action['domain'] = [('rfq_id', '=', self.id)]
        return action

    def action_view_quotes(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Quotes'),
            'res_model': 'cp.rfq.quote',
            'view_mode': 'list,pivot',
            'domain': [('rfq_id', '=', self.id)],
            'context': {'default_rfq_id': self.id, 'search_default_group_line': 1},
        }

    @api.model
    def _cron_close_rfqs(self):
        self.search([('state', '=', 'published'),
                     ('date_deadline', '<=', fields.Datetime.now())]).action_close_quotes()


class CpRfqLine(models.Model):
    _name = 'cp.rfq.line'
    _description = 'RFQ Lane'

    rfq_id = fields.Many2one('cp.rfq', required=True, ondelete='cascade')
    lane_id = fields.Many2one('cp.lane', required=True)
    vehicle_type_id = fields.Many2one('cp.vehicle.type', required=True)
    charge_basis = fields.Selection(CHARGE_BASIS, default='trip', required=True)
    monthly_volume = fields.Integer(string='Est. Trips / Month')
    quote_ids = fields.One2many('cp.rfq.quote', 'line_id')
    lowest_rate = fields.Monetary(compute='_compute_benchmarks')
    average_rate = fields.Monetary(compute='_compute_benchmarks')
    benchmark_rate = fields.Monetary(compute='_compute_benchmarks', string='Last Contract Rate',
                                     help='Rate on the most recent contract for this lane and vehicle type.')
    currency_id = fields.Many2one(related='rfq_id.currency_id')

    def _compute_display_name(self):
        for line in self:
            line.display_name = '%s / %s' % (line.lane_id.name or '', line.vehicle_type_id.name or '')

    @api.depends('quote_ids.rate')
    def _compute_benchmarks(self):
        ContractLine = self.env['cp.rate.contract.line']
        for line in self:
            rates = line.quote_ids.mapped('rate')
            line.lowest_rate = min(rates) if rates else 0.0
            line.average_rate = (sum(rates) / len(rates)) if rates else 0.0
            last = ContractLine.search([
                ('lane_id', '=', line.lane_id.id), ('vehicle_type_id', '=', line.vehicle_type_id.id),
                ('contract_id.state', 'in', ('active', 'expired')),
            ], order='id desc', limit=1)
            line.benchmark_rate = last.rate if last else 0.0


class CpRfqQuote(models.Model):
    _name = 'cp.rfq.quote'
    _description = 'Transporter Quote'
    _order = 'line_id, rate'

    rfq_id = fields.Many2one('cp.rfq', required=True, ondelete='cascade')
    line_id = fields.Many2one('cp.rfq.line', required=True, ondelete='cascade',
                              domain="[('rfq_id', '=', rfq_id)]")
    transporter_id = fields.Many2one('res.partner', required=True, domain=[('cp_is_transporter', '=', True)])
    rate = fields.Monetary(required=True)
    remarks = fields.Char()
    submitted_on = fields.Datetime(default=fields.Datetime.now, readonly=True)
    score = fields.Float(compute='_compute_score', store=True,
                         help='Price competitiveness and transporter performance, out of 100.')
    rank = fields.Integer(compute='_compute_score', store=True)
    selected = fields.Boolean()
    currency_id = fields.Many2one(related='rfq_id.currency_id')

    _quote_uniq = models.Constraint(
        'unique(line_id, transporter_id)',
        'A transporter can quote once per lane; edit the quote instead.',
    )

    @api.depends('rate', 'line_id.quote_ids.rate', 'rfq_id.price_weight')
    def _compute_score(self):
        for line in self.mapped('line_id'):
            quotes = line.quote_ids
            lowest = min(quotes.mapped('rate') or [0.0])
            weight = (line.rfq_id.price_weight or 70) / 100.0
            for q in quotes:
                price_score = (lowest / q.rate * 100.0) if q.rate else 0.0
                perf = q.transporter_id.cp_score or 50.0
                q.score = round(weight * price_score + (1 - weight) * perf, 2)
            for idx, q in enumerate(quotes.sorted(lambda r: -r.score), start=1):
                q.rank = idx
        for q in self.filtered(lambda r: not r.line_id):
            q.score, q.rank = 0.0, 0
