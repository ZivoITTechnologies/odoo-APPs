# -*- coding: utf-8 -*-
import json
import logging
from datetime import datetime, timedelta

from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

from . import tender_utils as tu

_logger = logging.getLogger(__name__)

VALUE_BANDS = [
    ('unknown', 'Not Published'),
    ('upto_50l', 'Up to ₹50 Lakh'),
    ('50l_2cr', '₹50 Lakh - ₹2 Cr'),
    ('2cr_10cr', '₹2 Cr - ₹10 Cr'),
    ('10cr_50cr', '₹10 Cr - ₹50 Cr'),
    ('50cr_100cr', '₹50 Cr - ₹100 Cr'),
    ('above_100cr', 'Above ₹100 Cr'),
]
DEADLINE_BUCKETS = [
    ('unknown', 'No Deadline'),
    ('closed', 'Closed'),
    ('today', 'Closing Today'),
    ('3d', 'Within 3 Days'),
    ('7d', 'Within 7 Days'),
    ('15d', 'Within 15 Days'),
    ('30d', 'Within 30 Days'),
    ('later', 'More than 30 Days'),
]
DURATION_BANDS = [
    ('unknown', 'Not Specified'),
    ('upto_6m', 'Up to 6 Months'),
    ('6_12m', '6 - 12 Months'),
    ('12_24m', '12 - 24 Months'),
    ('24_36m', '24 - 36 Months'),
    ('above_36m', 'Above 36 Months'),
]
WORK_TYPES = [
    ('item_rate', 'Item Rate'),
    ('percentage', 'Percentage Rate'),
    ('lump_sum', 'Lump Sum'),
    ('epc', 'EPC'),
    ('ham', 'Hybrid Annuity (HAM)'),
    ('bot', 'BOT / PPP'),
    ('supply', 'Supply'),
    ('consultancy', 'Consultancy / DPR'),
    ('other', 'Other'),
]


class Tender(models.Model):
    _name = 'zivoit.tender'
    _description = 'Tender / Bid Opportunity'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'priority desc, date_closing asc, id desc'
    _mail_post_access = 'read'

    # ------------------------------------------------------------------
    # identification
    # ------------------------------------------------------------------
    name = fields.Char('Name of Work', required=True, tracking=True)
    code = fields.Char('Internal Ref.', readonly=True, copy=False, default='/', index=True)
    reference = fields.Char('Tender / NIT No.', tracking=True, index='trigram')
    organisation = fields.Char('Organisation / Buyer', tracking=True, index='trigram')
    department = fields.Char('Department / Division')
    partner_id = fields.Many2one('res.partner', 'Buyer Contact')
    zone_id = fields.Many2one('zivoit.tender.zone', 'Railway Zone / PSU', tracking=True)
    country_id = fields.Many2one('res.country', default=lambda self: self.env.ref('base.in', raise_if_not_found=False))
    state_id = fields.Many2one('res.country.state', 'State', tracking=True,
                               domain="[('country_id', '=?', country_id)]")
    location = fields.Char('Location / District')
    sector_ids = fields.Many2many('zivoit.tender.sector', string='Sectors')
    tag_ids = fields.Many2many('zivoit.tender.tag', string='Tags')
    work_type = fields.Selection(WORK_TYPES, 'Contract Type')
    tender_type = fields.Selection([
        ('open', 'Open'), ('limited', 'Limited'), ('single', 'Single'), ('global', 'Global (ICB)'),
        ('eoi', 'EOI / Pre-qualification'), ('auction', 'Reverse Auction'),
    ], default='open')
    cover_system = fields.Selection([('single', 'Single Cover'), ('two', 'Two Cover'), ('three', 'Three Cover')],
                                    'Bid Cover System')
    description = fields.Html(sanitize=True)

    # source & dedup
    source_id = fields.Many2one('zivoit.tender.source', 'Found Via', index=True, ondelete='set null')
    portal = fields.Char('Portal')
    url = fields.Char('Tender Link')
    fingerprint = fields.Char(index=True, copy=False, readonly=True)
    source_payload = fields.Text('Raw Data', readonly=True, copy=False)
    found_date = fields.Datetime('Found On', default=fields.Datetime.now, readonly=True, copy=False)
    seen_count = fields.Integer('Times Seen', default=1, readonly=True, copy=False,
                                help='How many times connectors reported this tender (duplicates merged).')

    # money
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.ref('base.INR', raise_if_not_found=False)
                                  or self.env.company.currency_id, required=True)
    estimated_value = fields.Monetary('Estimated Value', tracking=True)
    value_label = fields.Char('Value (Cr/L)', compute='_compute_inr_labels')
    emd_label = fields.Char('EMD (Cr/L)', compute='_compute_inr_labels')
    zone_code = fields.Char(related='zone_id.code', string='Zone Code')
    emd_amount = fields.Monetary('EMD / Bid Security', tracking=True)
    tender_fee = fields.Monetary('Tender Fee')
    emd_exempt = fields.Boolean('EMD Exempted (MSE/NSIC)')
    performance_security_pct = fields.Float('Performance Security %', default=3.0)

    # dates
    date_published = fields.Datetime('Published On', tracking=True)
    date_prebid = fields.Datetime('Pre-bid Meeting')
    date_closing = fields.Datetime('Bid Submission End', tracking=True, index=True)
    date_opening = fields.Datetime('Bid Opening')
    completion_period = fields.Char('Completion Period (as published)')
    completion_months = fields.Float('Completion (Months)')
    days_left = fields.Float(compute='_compute_days_left', digits=(16, 1))
    days_left_label = fields.Char(compute='_compute_days_left')

    # segregation (stored so they can be grouped / filtered)
    value_band = fields.Selection(VALUE_BANDS, compute='_compute_value_band', store=True)
    deadline_bucket = fields.Selection(DEADLINE_BUCKETS, compute='_compute_time_flags', store=True)
    duration_band = fields.Selection(DURATION_BANDS, compute='_compute_duration_band', store=True)
    is_open = fields.Boolean('Open for Bidding', compute='_compute_time_flags', store=True)
    is_new = fields.Boolean('New (7 days)', compute='_compute_time_flags', store=True)

    # pipeline
    stage_id = fields.Many2one('zivoit.tender.stage', tracking=True, index=True, copy=False,
                               group_expand='_read_group_stage_ids', default=lambda self: self._default_stage())
    stage_type = fields.Selection(related='stage_id.stage_type', store=True)
    priority = fields.Selection([('0', 'Normal'), ('1', 'Warm'), ('2', 'Hot'), ('3', 'Very Hot')], default='0')
    color = fields.Integer()
    kanban_state = fields.Selection([('normal', 'In Progress'), ('done', 'Ready'), ('blocked', 'Blocked')],
                                    default='normal')
    user_id = fields.Many2one('res.users', 'Bid Manager', tracking=True, default=lambda self: self.env.user,
                              domain="[('share', '=', False)]")
    team_member_ids = fields.Many2many('res.users', 'zivoit_tender_team_rel', 'tender_id', 'user_id',
                                       string='Bid Team')
    active = fields.Boolean(default=True)
    shortlisted_date = fields.Datetime(readonly=True, copy=False)

    # matching / AI
    profile_ids = fields.Many2many('zivoit.tender.profile', 'zivoit_tender_profile_rel', 'tender_id', 'profile_id',
                                   string='Matched Agents', copy=False)
    relevance_score = fields.Integer('Relevance', copy=False, aggregator='avg')
    match_reason = fields.Text(copy=False)
    ai_summary = fields.Text('AI Summary', copy=False)
    ai_scope = fields.Text('Scope', copy=False)
    ai_eligibility = fields.Text('Eligibility (AI)', copy=False)
    ai_risks = fields.Text('Risks', copy=False)
    ai_recommendation = fields.Selection([('go', 'Go'), ('review', 'Review'), ('no_go', 'No-Go')],
                                         'AI Recommendation', copy=False, tracking=True)
    ai_score = fields.Integer('AI Score', copy=False)
    ai_reason = fields.Char(copy=False)
    ai_engine = fields.Selection([('ai', 'AI Model'), ('rules', 'Rules')], copy=False)
    ai_analysed_date = fields.Datetime(copy=False)

    # qualification
    checklist_line_ids = fields.One2many('zivoit.tender.checklist.line', 'tender_id', copy=True)
    go_score = fields.Integer('Bid/No-Bid Score', compute='_compute_go_score', store=True)
    go_decision = fields.Selection([('pending', 'Pending'), ('go', 'Bid'), ('no_go', 'No-Bid'), ('review', 'Review')],
                                   compute='_compute_go_score', store=True)
    mandatory_failed = fields.Boolean(compute='_compute_go_score', store=True)

    # costing
    boq_line_ids = fields.One2many('zivoit.tender.boq.line', 'tender_id', copy=True)
    boq_cost = fields.Monetary('BOQ Direct Cost', compute='_compute_costing', store=True)
    boq_estimate_amount = fields.Monetary('BOQ at Department Rates', compute='_compute_costing', store=True)
    overhead_pct = fields.Float('Overheads %', default=8.0)
    contingency_pct = fields.Float('Contingency %', default=2.0)
    margin_pct = fields.Float('Profit Margin %', default=10.0)
    gst_pct = fields.Float('GST % (if quoted extra)', default=0.0)
    quoted_amount = fields.Monetary('Computed Bid Amount', compute='_compute_costing', store=True)
    final_quoted_amount = fields.Monetary('Final Quoted Amount', tracking=True,
                                          help='Amount actually quoted on the portal (leave empty to use the computed amount).')
    bid_amount = fields.Monetary('Bid Amount', compute='_compute_costing', store=True)
    quote_vs_estimate_pct = fields.Float('% Above(+)/Below(-) Estimate', compute='_compute_costing', store=True,
                                         digits=(16, 2))
    expected_margin_amount = fields.Monetary(compute='_compute_costing', store=True)

    # EMD & fees
    emd_mode = fields.Selection([('online', 'Online Payment'), ('bg', 'Bank Guarantee'), ('fdr', 'FDR'),
                                 ('dd', 'Demand Draft'), ('exempt', 'Exempted')], 'EMD Mode')
    emd_status = fields.Selection([('not_required', 'Not Required'), ('to_pay', 'To Pay'), ('paid', 'Paid / Submitted'),
                                   ('refund_due', 'Refund Due'), ('refunded', 'Refunded'), ('adjusted', 'Adjusted to PBG'),
                                   ('forfeited', 'Forfeited')], default='to_pay', tracking=True)
    emd_reference = fields.Char('EMD Ref. / BG No.')
    emd_paid_date = fields.Date('EMD Paid On')
    emd_valid_until = fields.Date('BG / EMD Valid Until')
    emd_refund_date = fields.Date('EMD Refunded On')
    tender_fee_paid = fields.Boolean()

    # submission & result
    submitted_date = fields.Datetime(copy=False, tracking=True)
    submission_reference = fields.Char('Submission Acknowledgement', copy=False)
    corrigendum_ids = fields.One2many('zivoit.tender.corrigendum', 'tender_id')
    corrigendum_count = fields.Integer(compute='_compute_counts')
    competitor_ids = fields.One2many('zivoit.tender.competitor', 'tender_id', string='Bidders / Results')
    our_rank = fields.Integer('Our Rank (L-n)', copy=False, tracking=True)
    l1_amount = fields.Monetary('L1 Amount', copy=False)
    l1_bidder = fields.Char('L1 Bidder', copy=False)
    gap_to_l1_pct = fields.Float('Gap to L1 %', compute='_compute_gap_to_l1', store=True, digits=(16, 2))
    awarded_value = fields.Monetary('Awarded / Contract Value', copy=False, tracking=True)
    result_date = fields.Date(copy=False)
    lost_reason_id = fields.Many2one('zivoit.tender.lost.reason', copy=False, tracking=True)
    result_notes = fields.Text(copy=False)

    # downstream
    lead_id = fields.Many2one('crm.lead', 'Opportunity', copy=False)
    project_id = fields.Many2one('project.project', 'Execution Project', copy=False)
    ra_bill_ids = fields.One2many('zivoit.tender.ra.bill', 'tender_id')
    ra_bill_count = fields.Integer(compute='_compute_counts')
    attachment_count = fields.Integer('Documents', compute='_compute_counts')
    last_reminder_days = fields.Integer(copy=False, readonly=True)

    _fingerprint_company_idx = models.Index('(fingerprint, company_id)')

    # ------------------------------------------------------------------
    # defaults
    # ------------------------------------------------------------------
    @api.model
    def _default_stage(self):
        return self.env['zivoit.tender.stage'].search([('stage_type', '=', 'new')], limit=1)

    @api.model
    def _read_group_stage_ids(self, stages, domain):
        return stages.search([], order=stages._order)

    @api.model
    def _stage(self, stage_type):
        stage = self.env['zivoit.tender.stage'].search([('stage_type', '=', stage_type)], limit=1)
        if not stage:
            raise UserError(_('No pipeline stage of type "%s" is configured.', stage_type))
        return stage

    # ------------------------------------------------------------------
    # computes
    # ------------------------------------------------------------------
    @api.depends('date_closing')
    def _compute_days_left(self):
        now = fields.Datetime.now()
        for tender in self:
            if tender.date_closing:
                days = (tender.date_closing - now).total_seconds() / 86400.0
                tender.days_left = round(days, 1)
                if days < 0:
                    tender.days_left_label = _('Closed')
                elif days < 1:
                    tender.days_left_label = _('%s hrs left', int(days * 24))
                elif days < 2:
                    tender.days_left_label = _('1 day left')
                else:
                    tender.days_left_label = _('%s days left', int(days))
            else:
                tender.days_left = False
                tender.days_left_label = False

    @api.depends('estimated_value', 'emd_amount')
    def _compute_inr_labels(self):
        for tender in self:
            tender.value_label = ('₹ ' + self._format_inr(tender.estimated_value)) if tender.estimated_value else False
            tender.emd_label = ('₹ ' + self._format_inr(tender.emd_amount)) if tender.emd_amount else False

    @api.depends('estimated_value')
    def _compute_value_band(self):
        for tender in self:
            tender.value_band = self._value_band_for(tender.estimated_value)

    @staticmethod
    def _value_band_for(value):
        if not value:
            return 'unknown'
        if value <= 50 * tu.LAKH:
            return 'upto_50l'
        if value <= 2 * tu.CRORE:
            return '50l_2cr'
        if value <= 10 * tu.CRORE:
            return '2cr_10cr'
        if value <= 50 * tu.CRORE:
            return '10cr_50cr'
        if value <= 100 * tu.CRORE:
            return '50cr_100cr'
        return 'above_100cr'

    @api.depends('completion_months')
    def _compute_duration_band(self):
        for tender in self:
            months = tender.completion_months
            if not months:
                band = 'unknown'
            elif months <= 6:
                band = 'upto_6m'
            elif months <= 12:
                band = '6_12m'
            elif months <= 24:
                band = '12_24m'
            elif months <= 36:
                band = '24_36m'
            else:
                band = 'above_36m'
            tender.duration_band = band

    @api.depends('date_closing', 'date_published', 'found_date', 'stage_id.stage_type')
    def _compute_time_flags(self):
        now = fields.Datetime.now()
        for tender in self:
            closing = tender.date_closing
            if not closing:
                bucket = 'unknown'
            else:
                days = (closing - now).total_seconds() / 86400.0
                if days < 0:
                    bucket = 'closed'
                elif closing.date() == now.date():
                    bucket = 'today'
                elif days <= 3:
                    bucket = '3d'
                elif days <= 7:
                    bucket = '7d'
                elif days <= 15:
                    bucket = '15d'
                elif days <= 30:
                    bucket = '30d'
                else:
                    bucket = 'later'
            tender.deadline_bucket = bucket
            stage_type = tender.stage_id.stage_type
            tender.is_open = bucket != 'closed' and stage_type not in ('won', 'lost', 'dropped', 'expired')
            ref = tender.date_published or tender.found_date
            tender.is_new = bool(ref and ref >= now - timedelta(days=7))

    @api.depends('checklist_line_ids.status', 'checklist_line_ids.weight', 'checklist_line_ids.mandatory')
    def _compute_go_score(self):
        for tender in self:
            lines = tender.checklist_line_ids
            if not lines:
                tender.go_score = 0
                tender.go_decision = 'pending'
                tender.mandatory_failed = False
                continue
            total = sum(lines.mapped('weight')) or 1
            earned = 0.0
            for line in lines:
                if line.status == 'yes':
                    earned += line.weight
                elif line.status == 'partial':
                    earned += line.weight / 2.0
            failed = any(l.mandatory and l.status == 'no' for l in lines)
            pending = any(l.status == 'pending' for l in lines)
            score = int(round(earned * 100.0 / total))
            tender.go_score = score
            tender.mandatory_failed = failed
            if failed:
                tender.go_decision = 'no_go'
            elif pending:
                tender.go_decision = 'pending'
            elif score >= 70:
                tender.go_decision = 'go'
            elif score >= 50:
                tender.go_decision = 'review'
            else:
                tender.go_decision = 'no_go'

    @api.depends('boq_line_ids.cost_amount', 'boq_line_ids.estimate_amount', 'overhead_pct', 'contingency_pct',
                 'margin_pct', 'gst_pct', 'final_quoted_amount', 'estimated_value')
    def _compute_costing(self):
        for tender in self:
            cost = sum(tender.boq_line_ids.mapped('cost_amount'))
            estimate = sum(tender.boq_line_ids.mapped('estimate_amount'))
            loaded = cost * (1 + (tender.overhead_pct + tender.contingency_pct) / 100.0)
            quoted = loaded * (1 + tender.margin_pct / 100.0) * (1 + tender.gst_pct / 100.0)
            tender.boq_cost = cost
            tender.boq_estimate_amount = estimate
            tender.quoted_amount = quoted
            bid = tender.final_quoted_amount or quoted
            tender.bid_amount = bid
            base = tender.estimated_value or estimate
            tender.quote_vs_estimate_pct = ((bid - base) / base * 100.0) if base and bid else 0.0
            tender.expected_margin_amount = (bid / (1 + tender.gst_pct / 100.0) - loaded) if bid else 0.0

    @api.depends('l1_amount', 'bid_amount')
    def _compute_gap_to_l1(self):
        for tender in self:
            if tender.l1_amount and tender.bid_amount:
                tender.gap_to_l1_pct = (tender.bid_amount - tender.l1_amount) / tender.l1_amount * 100.0
            else:
                tender.gap_to_l1_pct = 0.0

    def _compute_counts(self):
        att = self.env['ir.attachment']._read_group(
            [('res_model', '=', self._name), ('res_id', 'in', self.ids)], ['res_id'], ['__count'])
        att_map = dict(att)
        for tender in self:
            tender.corrigendum_count = len(tender.corrigendum_ids)
            tender.ra_bill_count = len(tender.ra_bill_ids)
            tender.attachment_count = att_map.get(tender.id, 0)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('code') or vals.get('code') == '/':
                vals['code'] = self.env['ir.sequence'].next_by_code('zivoit.tender') or '/'
            if not vals.get('fingerprint'):
                vals['fingerprint'] = tu.fingerprint(vals.get('reference'), vals.get('name'), vals.get('organisation'))
        tenders = super().create(vals_list)
        for tender in tenders:
            if not tender.sector_ids or not tender.state_id or not tender.zone_id or not tender.work_type:
                tender._auto_classify()
        return tenders

    def write(self, vals):
        stage_changed = 'stage_id' in vals
        if stage_changed:
            new_stage = self.env['zivoit.tender.stage'].browse(vals['stage_id'])
            if new_stage.stage_type == 'shortlist':
                vals.setdefault('shortlisted_date', fields.Datetime.now())
            if new_stage.stage_type == 'submitted':
                vals.setdefault('submitted_date', fields.Datetime.now())
        if any(key in vals for key in ('reference', 'name', 'organisation')) and 'fingerprint' not in vals:
            res = super().write(vals)
            for tender in self:
                tender.fingerprint = tu.fingerprint(tender.reference, tender.name, tender.organisation)
        else:
            res = super().write(vals)
        if stage_changed:
            self._on_stage_changed()
        return res

    @api.constrains('date_closing', 'date_published')
    def _check_dates(self):
        for tender in self:
            if tender.date_closing and tender.date_published and tender.date_closing < tender.date_published:
                raise ValidationError(_('The bid submission end date cannot be before the publication date.'))

    @api.depends('code', 'name')
    def _compute_display_name(self):
        for tender in self:
            tender.display_name = '[%s] %s' % (tender.code, tender.name) if tender.code and tender.code != '/' else tender.name

    # ------------------------------------------------------------------
    # classification & ingestion
    # ------------------------------------------------------------------
    def _classification_text(self):
        self.ensure_one()
        return ' '.join(filter(None, [self.name, self.organisation, self.department, self.location,
                                      tu.clean_text(self.description), self.reference]))

    def _auto_classify(self):
        Sector = self.env['zivoit.tender.sector']
        sector_map = Sector._get_keyword_map()
        for tender in self:
            text = tender._classification_text()
            vals = {}
            if not tender.sector_ids:
                codes = tu.detect_sectors(text, sector_map or None)
                if codes:
                    sectors = Sector.search([('code', 'in', codes)])
                    # exclusions
                    low = text.lower()
                    sectors = sectors.filtered(
                        lambda s: not any(word in low for word in tu.keyword_list(s.exclude_keywords)))
                    if sectors:
                        vals['sector_ids'] = [(6, 0, sectors.ids)]
            if not tender.zone_id:
                zone_code = tu.detect_zone(' '.join(filter(None, [tender.organisation, tender.department, tender.name])))
                if zone_code:
                    zone = self.env['zivoit.tender.zone'].search([('code', '=', zone_code)], limit=1)
                    if zone:
                        vals['zone_id'] = zone.id
            if not tender.state_id:
                state_name = tu.detect_state(text)
                if state_name:
                    state = self.env['res.country.state'].search(
                        [('country_id.code', '=', 'IN'), ('name', '=ilike', state_name)], limit=1)
                    if state:
                        vals['state_id'] = state.id
            if not tender.work_type:
                work_type = tu.detect_work_type(text)
                if work_type:
                    vals['work_type'] = work_type
            if vals:
                tender.with_context(tracking_disable=True).write(vals)

    @api.model
    def _prepare_vals_from_row(self, row, source=False):
        """Normalise one raw connector row into zivoit.tender values."""
        get = lambda *keys: next((row.get(k) for k in keys if row.get(k) not in (None, '', False)), None)  # noqa: E731
        title = tu.clean_text(get('title', 'name', 'work_name', 'name_of_work'))[:1000]
        if not title:
            return None
        vals = {
            'name': title,
            'reference': tu.clean_text(get('reference', 'tender_no', 'tender_id', 'nit_no'))[:200] or False,
            'organisation': tu.clean_text(get('organisation', 'organization', 'buyer')) or (source and source.default_organisation) or False,
            'department': tu.clean_text(get('department', 'division')) or False,
            'location': tu.clean_text(get('location', 'district', 'city')) or False,
            'url': get('url', 'link') or False,
            'portal': tu.clean_text(get('portal')) or (source and dict(source._fields['portal'].selection).get(source.portal)) or False,
            'estimated_value': tu.parse_indian_amount(get('estimated_value', 'value', 'tender_value')),
            'emd_amount': tu.parse_indian_amount(get('emd', 'emd_amount')),
            'tender_fee': tu.parse_indian_amount(get('tender_fee', 'fee')),
            'completion_period': tu.clean_text(get('completion_period', 'period')) or False,
            'completion_months': tu.parse_duration_months(get('completion_period', 'period')),
            'source_payload': json.dumps(row, default=str, ensure_ascii=False)[:20000],
        }
        description = get('description', 'summary')
        if description:
            vals['description'] = Markup('<p>%s</p>') % tu.clean_text(description)[:5000]
        for field, keys in (('date_published', ('published_date', 'publish_date', 'date_published')),
                            ('date_closing', ('closing_date', 'bid_end_date', 'last_date', 'date_closing', 'due_date')),
                            ('date_opening', ('opening_date', 'date_opening'))):
            value = tu.parse_date(get(*keys))
            if value:
                vals[field] = value
        if vals.get('date_published') and vals.get('date_closing') and vals['date_closing'] < vals['date_published']:
            vals.pop('date_published')
        state_name = get('state')
        if state_name:
            state = self.env['res.country.state'].search(
                [('country_id.code', '=', 'IN'), '|', ('name', '=ilike', tu.clean_text(state_name)),
                 ('code', '=ilike', tu.clean_text(state_name))], limit=1)
            if state:
                vals['state_id'] = state.id
        if source:
            vals['source_id'] = source.id
            if source.default_state_id and not vals.get('state_id'):
                vals['state_id'] = source.default_state_id.id
            if source.default_zone_id:
                vals['zone_id'] = source.default_zone_id.id
            if source.default_sector_ids:
                vals['sector_ids'] = [(6, 0, source.default_sector_ids.ids)]
        vals['fingerprint'] = tu.fingerprint(vals.get('reference'), vals['name'], vals.get('organisation'))
        return vals

    @api.model
    def _ingest_rows(self, rows, source=False):
        """Create new tenders or merge duplicates. Returns (tenders, created_count, updated_count)."""
        tenders = self.browse()
        created = updated = 0
        seen = set()
        for row in rows or []:
            vals = self._prepare_vals_from_row(row, source=source)
            if not vals or vals['fingerprint'] in seen:
                continue
            seen.add(vals['fingerprint'])
            existing = self.with_context(active_test=False).search(
                [('fingerprint', '=', vals['fingerprint']), ('company_id', '=', self.env.company.id)], limit=1)
            if existing:
                update = {'seen_count': existing.seen_count + 1}
                # only fill gaps or newer deadlines (corrigendum) - never overwrite user edits
                for field in ('estimated_value', 'emd_amount', 'tender_fee', 'url', 'date_published', 'date_opening',
                              'completion_period', 'completion_months', 'department', 'location', 'state_id'):
                    if vals.get(field) and not existing[field]:
                        update[field] = vals[field]
                if vals.get('date_closing') and existing.date_closing and vals['date_closing'] > existing.date_closing:
                    update['date_closing'] = vals['date_closing']
                    existing.message_post(body=_('Bid submission end date extended from %(old)s to %(new)s (source: %(src)s).',
                                                 old=existing.date_closing, new=vals['date_closing'],
                                                 src=source.name if source else _('import')))
                elif vals.get('date_closing') and not existing.date_closing:
                    update['date_closing'] = vals['date_closing']
                existing.with_context(tracking_disable=True).write(update)
                tenders |= existing
                updated += 1
            else:
                vals.pop('seen_count', None)
                tenders |= self.create(vals)
                created += 1
        return tenders, created, updated

    # ------------------------------------------------------------------
    # matching & scoring
    # ------------------------------------------------------------------
    def _match_score(self, filters):
        """Score 0-100 of how well this tender matches a filter dict. Returns (score, reasons)."""
        self.ensure_one()
        text = self._classification_text().lower()
        title = (self.name or '').lower()
        score = 0
        reasons = []
        keywords = [k.lower() for k in filters.get('keywords') or []]
        if keywords:
            hits = [k for k in keywords if k in text]
            title_hits = [k for k in keywords if k in title]
            score += min(40, 25 * len(title_hits) + 10 * (len(hits) - len(title_hits)))
            if hits:
                reasons.append(_('Keywords: %s', ', '.join(hits)))
        else:
            score += 20
        sectors = filters.get('sectors') or []
        if sectors:
            matched = set(self.sector_ids.mapped('code')) & set(sectors)
            if matched:
                score += 20
                reasons.append(_('Sector: %s', ', '.join(self.sector_ids.filtered(lambda s: s.code in matched).mapped('name'))))
        else:
            score += 10
        states = [s.lower() for s in filters.get('states') or []]
        if states:
            if self.state_id and self.state_id.name.lower() in states:
                score += 15
                reasons.append(_('State: %s', self.state_id.name))
        else:
            score += 8
        zones = [z.upper() for z in filters.get('zones') or []]
        if zones:
            if self.zone_id and self.zone_id.code in zones:
                score += 15
                reasons.append(_('Zone: %s', self.zone_id.code))
        else:
            score += 7
        orgs = [o.lower() for o in filters.get('organisations') or []]
        if orgs and any(o in text for o in orgs):
            score += 5
            reasons.append(_('Buyer match'))
        min_v, max_v = filters.get('min_value') or 0, filters.get('max_value') or 0
        if self.estimated_value and (min_v or max_v):
            if (not min_v or self.estimated_value >= min_v) and (not max_v or self.estimated_value <= max_v):
                score += 10
                reasons.append(_('Value in range'))
        elif self.estimated_value:
            score += 5
        if self.days_left is not False and self.date_closing:
            if self.days_left >= 7:
                score += 5
            elif self.days_left < 2:
                score -= 10
        return max(0, min(100, int(score))), reasons

    # ------------------------------------------------------------------
    # pipeline actions
    # ------------------------------------------------------------------
    def _on_stage_changed(self):
        ICP = self.env['ir.config_parameter'].sudo()
        for tender in self:
            stype = tender.stage_id.stage_type
            if tender.stage_id.mail_template_id:
                tender.message_post_with_source(tender.stage_id.mail_template_id,
                                                subtype_xmlid='mail.mt_comment')
            if stype == 'shortlist':
                if not tender.checklist_line_ids:
                    tender._apply_default_checklist()
                if ICP.get_param('zivoit_tender_ai.auto_lead_on_shortlist') == 'True' and not tender.lead_id:
                    tender.action_create_opportunity()
                if tender.user_id:
                    tender.activity_schedule(
                        'mail.mail_activity_data_todo', user_id=tender.user_id.id,
                        date_deadline=(tender.date_closing - timedelta(days=3)).date()
                        if tender.date_closing and tender.days_left > 3 else fields.Date.context_today(tender),
                        summary=_('Prepare bid & complete Bid/No-Bid checklist'))
            if stype == 'submitted' and tender.emd_status == 'to_pay' and not tender.emd_exempt:
                tender.emd_status = 'paid'
            if stype == 'won':
                if tender.lead_id:
                    tender.lead_id.action_set_won()
                if (ICP.get_param('zivoit_tender_ai.auto_project_on_win', 'True') == 'True' and not tender.project_id
                        and not self.env.context.get('skip_auto_project')):
                    tender.action_create_project()
                if tender.emd_status in ('paid',):
                    tender.emd_status = 'adjusted'
            if stype in ('lost', 'dropped', 'expired'):
                if tender.emd_status == 'paid':
                    tender.emd_status = 'refund_due'
                if tender.lead_id and tender.lead_id.active:
                    tender.lead_id.action_archive()

    def _apply_default_checklist(self, template=False):
        Template = self.env['zivoit.tender.checklist.template']
        for tender in self:
            tmpl = template
            if not tmpl:
                candidates = Template.search([('is_default', '=', True)])
                tmpl = candidates.filtered(lambda t: t.sector_ids & tender.sector_ids)[:1] or \
                    candidates.filtered(lambda t: not t.sector_ids)[:1]
            if not tmpl:
                continue
            existing = set(tender.checklist_line_ids.mapped('name'))
            tender.checklist_line_ids = [(0, 0, {
                'name': line.name, 'category': line.category, 'weight': line.weight,
                'mandatory': line.mandatory, 'sequence': line.sequence,
            }) for line in tmpl.line_ids if line.name not in existing]

    def action_apply_checklist(self):
        self._apply_default_checklist()
        return True

    def action_review(self):
        self.write({'stage_id': self._stage('review').id})

    def action_shortlist(self):
        self.write({'stage_id': self._stage('shortlist').id})

    def action_prepare_bid(self):
        for tender in self:
            if tender.mandatory_failed:
                raise UserError(_('"%s" fails a mandatory qualification criterion. Fix the checklist or drop the tender.',
                                  tender.name))
        self.write({'stage_id': self._stage('prepare').id})

    def action_submit_bid(self):
        for tender in self:
            if not tender.bid_amount:
                raise UserError(_('Enter the BOQ or the final quoted amount of "%s" before submitting.', tender.name))
        self.write({'stage_id': self._stage('submitted').id})

    def action_evaluation(self):
        self.write({'stage_id': self._stage('evaluation').id})

    def action_reopen(self):
        self.write({'stage_id': self._stage('review').id, 'lost_reason_id': False})

    def action_open_result_wizard(self):
        self.ensure_one()
        return {
            'name': _('Record Tender Result'),
            'type': 'ir.actions.act_window',
            'res_model': 'zivoit.tender.result.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_tender_id': self.id,
                        'default_outcome': self.env.context.get('outcome', 'won')},
        }

    def action_drop(self):
        return self.with_context(outcome='dropped').action_open_result_wizard()

    def action_analyse(self):
        AI = self.env['zivoit.tender.ai']
        for tender in self:
            data = AI.analyse_tender(tender)
            tender.write({
                'ai_summary': data.get('summary'),
                'ai_scope': data.get('scope'),
                'ai_eligibility': data.get('eligibility'),
                'ai_risks': data.get('risks'),
                'ai_recommendation': data.get('recommendation'),
                'ai_score': data.get('score'),
                'ai_reason': (data.get('reason') or '')[:250],
                'ai_engine': data.get('engine'),
                'ai_analysed_date': fields.Datetime.now(),
            })
        return True

    def action_open_url(self):
        self.ensure_one()
        if not self.url:
            raise UserError(_('No link is available for this tender.'))
        return {'type': 'ir.actions.act_url', 'url': self.url, 'target': 'new'}

    def action_create_opportunity(self):
        Lead = self.env['crm.lead']
        for tender in self.filtered(lambda t: not t.lead_id):
            partner = tender.partner_id
            if not partner and tender.organisation:
                partner = self.env['res.partner'].search([('name', '=ilike', tender.organisation),
                                                          ('is_company', '=', True)], limit=1)
                if not partner:
                    partner = self.env['res.partner'].create({
                        'name': tender.organisation, 'is_company': True,
                        'state_id': tender.state_id.id, 'country_id': tender.country_id.id})
                tender.partner_id = partner
            lead = Lead.create({
                'name': tender.name[:250],
                'type': 'opportunity',
                'partner_id': partner.id if partner else False,
                'expected_revenue': tender.bid_amount or tender.estimated_value,
                'date_deadline': tender.date_closing.date() if tender.date_closing else False,
                'user_id': tender.user_id.id,
                'description': tender.ai_summary or tu.clean_text(tender.description),
                'tender_id': tender.id,
            })
            tender.lead_id = lead
            tender.message_post(body=Markup(_('Opportunity %s created.')) % lead._get_html_link())
        if len(self) == 1 and self.lead_id:
            return {'type': 'ir.actions.act_window', 'res_model': 'crm.lead', 'res_id': self.lead_id.id,
                    'view_mode': 'form', 'target': 'current'}
        return True

    def action_create_project(self):
        Project = self.env['project.project']
        Task = self.env['project.task']
        for tender in self.filtered(lambda t: not t.project_id):
            project = Project.create({
                'name': '%s - %s' % (tender.code, (tender.name or '')[:150]),
                'partner_id': tender.partner_id.id,
                'user_id': tender.user_id.id,
                'tender_id': tender.id,
                'contract_value': tender.awarded_value or tender.bid_amount or tender.estimated_value,
                'date_start': fields.Date.context_today(tender),
                'date': fields.Date.add(fields.Date.context_today(tender), months=int(tender.completion_months))
                if tender.completion_months else False,
                'description': tender.ai_summary or '',
            })
            for line in tender.boq_line_ids.filtered(lambda l: not l.display_type):
                task = Task.create({
                    'name': ('%s %s' % (line.item_code or '', line.name)).strip()[:250],
                    'project_id': project.id,
                    'description': _('Contract qty: %(qty)s %(uom)s @ %(rate)s', qty=line.quantity,
                                     uom=line.uom or '', rate=line.quoted_rate),
                })
                line.task_id = task
            tender.project_id = project
            tender.message_post(body=Markup(_('Execution project %s created.')) % project._get_html_link())
        if len(self) == 1:
            return self.action_view_project()
        return True

    def action_view_project(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'project.project', 'res_id': self.project_id.id,
                'view_mode': 'form', 'target': 'current'}

    def action_view_ra_bills(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_tender_ai.action_zivoit_ra_bill')
        action['domain'] = [('tender_id', '=', self.id)]
        action['context'] = {'default_tender_id': self.id}
        return action

    def action_view_attachments(self):
        self.ensure_one()
        return {
            'name': _('Tender Documents'), 'type': 'ir.actions.act_window', 'res_model': 'ir.attachment',
            'view_mode': 'kanban,list,form',
            'domain': [('res_model', '=', self._name), ('res_id', '=', self.id)],
            'context': {'default_res_model': self._name, 'default_res_id': self.id},
        }

    def action_view_corrigenda(self):
        self.ensure_one()
        return {
            'name': _('Corrigenda'), 'type': 'ir.actions.act_window', 'res_model': 'zivoit.tender.corrigendum',
            'view_mode': 'list,form', 'domain': [('tender_id', '=', self.id)],
            'context': {'default_tender_id': self.id},
        }

    # ------------------------------------------------------------------
    # mail gateway: tender alert e-mails create tenders
    # ------------------------------------------------------------------
    @api.model
    def message_new(self, msg_dict, custom_values=None):
        body = msg_dict.get('body') or ''
        subject = msg_dict.get('subject') or _('Tender alert')
        rows = self.env['zivoit.tender.ai'].extract_tender_from_text(body, subject)
        source = self.env['zivoit.tender.source'].search([('source_type', '=', 'email')], limit=1)
        values = dict(custom_values or {})
        first = None
        if rows:
            vals = self._prepare_vals_from_row(rows[0], source=source) or {}
            vals.pop('fingerprint', None)
            values = dict(vals, **values)
            others = rows[1:]
        else:
            others = []
        values.setdefault('name', subject)
        first = super().message_new(msg_dict, custom_values=values)
        if others:
            self._ingest_rows(others, source=source)
        self._match_against_profiles(first)
        return first

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _format_inr(amount):
        if not amount:
            return '0'
        if amount >= tu.CRORE:
            return '%.2f Cr' % (amount / tu.CRORE)
        if amount >= tu.LAKH:
            return '%.2f L' % (amount / tu.LAKH)
        return '{:,.0f}'.format(amount)

    def _get_ai_context(self):
        self.ensure_one()
        return '\n'.join(filter(None, [
            'Title: %s' % self.name,
            'Reference: %s' % (self.reference or ''),
            'Buyer: %s %s' % (self.organisation or '', self.department or ''),
            'Zone: %s' % (self.zone_id.display_name or ''),
            'State / Location: %s %s' % (self.state_id.name or '', self.location or ''),
            'Sectors: %s' % ', '.join(self.sector_ids.mapped('name')),
            'Estimated value (INR): %s' % (self.estimated_value or 'not published'),
            'EMD (INR): %s' % (self.emd_amount or ''),
            'Completion period: %s' % (self.completion_period or self.completion_months or ''),
            'Bid closing: %s' % (self.date_closing or ''),
            'Contract type: %s' % (dict(WORK_TYPES).get(self.work_type) or ''),
            'Description: %s' % tu.clean_text(self.description)[:4000],
        ]))

    @api.model
    def _match_against_profiles(self, tenders):
        """Link freshly ingested tenders to every active agent they match (used by e-mail & import)."""
        for profile in self.env['zivoit.tender.profile'].search([('active', '=', True)]):
            profile._process_matches(tenders.filtered_domain(profile._build_domain()))

    # ------------------------------------------------------------------
    # crons
    # ------------------------------------------------------------------
    @api.model
    def _cron_refresh_time_flags(self):
        """Hourly: refresh stored deadline buckets / open flags and expire missed tenders."""
        tenders = self.search(['|', ('is_open', '=', True), ('deadline_bucket', 'not in', ('closed', 'unknown'))])
        tenders._compute_time_flags()
        expired_stage = self.env['zivoit.tender.stage'].search([('stage_type', '=', 'expired')], limit=1)
        if expired_stage:
            missed = self.search([('stage_type', 'in', ('new', 'review')), ('date_closing', '<', fields.Datetime.now())])
            if missed:
                missed.write({'stage_id': expired_stage.id})
        return True

    @api.model
    def _cron_deadline_reminders(self):
        """Daily: remind bid managers before bid submission end dates."""
        ICP = self.env['ir.config_parameter'].sudo()
        days_list = sorted({int(d) for d in (ICP.get_param('zivoit_tender_ai.reminder_days', '7,3,1') or '').split(',')
                            if d.strip().isdigit()}, reverse=True)
        if not days_list:
            return True
        now = fields.Datetime.now()
        tenders = self.search([('stage_type', 'in', ('review', 'shortlist', 'prepare')),
                               ('date_closing', '>', now),
                               ('date_closing', '<=', now + timedelta(days=max(days_list) + 1))])
        for tender in tenders:
            days = tender.days_left
            due = [d for d in days_list if days <= d]
            if not due:
                continue
            threshold = min(due)
            if tender.last_reminder_days and tender.last_reminder_days <= threshold:
                continue
            partners = (tender.user_id | tender.team_member_ids).partner_id
            tender.message_post(
                body=Markup(_('<b>Deadline reminder:</b> bid submission for <b>%(name)s</b> closes on %(date)s '
                              '(%(days)s days left). Stage: %(stage)s.')) % {
                    'name': tender.name, 'date': tender.date_closing, 'days': int(days), 'stage': tender.stage_id.name},
                partner_ids=partners.ids, subtype_xmlid='mail.mt_comment', message_type='notification')
            if tender.user_id:
                tender.activity_schedule('mail.mail_activity_data_todo', user_id=tender.user_id.id,
                                         date_deadline=tender.date_closing.date(),
                                         summary=_('Bid closes in %s days', int(days)))
            tender.last_reminder_days = threshold
        return True

    @api.model
    def _cron_emd_followup(self):
        """Weekly: chase EMD refunds and expiring bank guarantees."""
        today = fields.Date.context_today(self)
        for tender in self.search([('emd_status', '=', 'refund_due')]):
            if tender.user_id and not tender.activity_ids.filtered(lambda a: 'EMD' in (a.summary or '')):
                tender.activity_schedule('mail.mail_activity_data_todo', user_id=tender.user_id.id,
                                         summary=_('Follow up EMD refund'), date_deadline=today)
        for tender in self.search([('emd_status', '=', 'paid'), ('emd_valid_until', '!=', False),
                                   ('emd_valid_until', '<=', today + timedelta(days=15))]):
            if tender.user_id and not tender.activity_ids.filtered(lambda a: 'BG' in (a.summary or '')):
                tender.activity_schedule('mail.mail_activity_data_todo', user_id=tender.user_id.id,
                                         summary=_('EMD BG expiring - extend validity'),
                                         date_deadline=tender.emd_valid_until)
        return True

    # ------------------------------------------------------------------
    # dashboard & agent API (used by the OWL dashboard)
    # ------------------------------------------------------------------
    @api.model
    def get_dashboard_data(self):
        now = fields.Datetime.now()
        open_domain = [('is_open', '=', True)]
        pipeline_types = ('shortlist', 'prepare', 'submitted', 'evaluation')

        def count(domain):
            return self.search_count(domain)

        won = count([('stage_type', '=', 'won')])
        lost = count([('stage_type', '=', 'lost')])
        pipeline = self.search([('stage_type', 'in', pipeline_types)])
        by_sector = self._read_group(open_domain, ['sector_ids'], ['__count', 'estimated_value:sum'])
        by_state = self._read_group(open_domain, ['state_id'], ['__count', 'estimated_value:sum'])
        by_value = self._read_group(open_domain, ['value_band'], ['__count'])
        by_deadline = self._read_group(open_domain, ['deadline_bucket'], ['__count'])
        by_stage = self._read_group([], ['stage_id'], ['__count', 'estimated_value:sum'])
        value_labels = dict(VALUE_BANDS)
        deadline_labels = dict(DEADLINE_BUCKETS)
        closing = self.search(open_domain + [('date_closing', '>=', now), ('date_closing', '<=', now + timedelta(days=7))],
                              order='date_closing asc', limit=8)
        hot = self.search(open_domain + [('stage_type', 'in', ('new', 'review'))],
                          order='relevance_score desc, priority desc, estimated_value desc', limit=8)
        return {
            'kpi': {
                'open': count(open_domain),
                'new_today': count(open_domain + ['|', ('date_published', '>=', now - timedelta(days=1)), '&',
                                                  ('date_published', '=', False), ('found_date', '>=', now - timedelta(days=1))]),
                'new_week': count(open_domain + [('is_new', '=', True)]),
                'closing_week': count(open_domain + [('date_closing', '>=', now), ('date_closing', '<=', now + timedelta(days=7))]),
                'shortlisted': count([('stage_type', '=', 'shortlist')]),
                'in_preparation': count([('stage_type', '=', 'prepare')]),
                'submitted': count([('stage_type', 'in', ('submitted', 'evaluation'))]),
                'won': won,
                'lost': lost,
                'win_rate': round(won * 100.0 / (won + lost), 1) if (won + lost) else 0.0,
                'pipeline_value': self._format_inr(sum(pipeline.mapped('bid_amount')) or sum(pipeline.mapped('estimated_value'))),
                'open_value': self._format_inr(sum(self.search(open_domain).mapped('estimated_value'))),
                'won_value': self._format_inr(sum(self.search([('stage_type', '=', 'won')]).mapped('awarded_value'))),
                'agents': self.env['zivoit.tender.profile'].search_count([]),
            },
            'by_sector': [{'id': s.id, 'name': s.name or _('Unclassified'), 'count': c, 'value': self._format_inr(v)}
                          for s, c, v in sorted(by_sector, key=lambda r: -r[1])],
            'by_state': [{'id': s.id, 'name': s.name or _('Unknown'), 'count': c, 'value': self._format_inr(v)}
                         for s, c, v in sorted(by_state, key=lambda r: -r[1])][:10],
            'by_value': [{'key': k, 'name': value_labels.get(k, k), 'count': c} for k, c in by_value],
            'by_deadline': [{'key': k, 'name': deadline_labels.get(k, k), 'count': c} for k, c in by_deadline],
            'by_stage': [{'id': s.id, 'name': s.name, 'count': c, 'value': self._format_inr(v)} for s, c, v in by_stage],
            'closing': [t._dashboard_row() for t in closing],
            'hot': [t._dashboard_row() for t in hot],
            'ai_enabled': self.env['zivoit.tender.ai']._is_enabled(),
        }

    def _dashboard_row(self):
        self.ensure_one()
        return {
            'id': self.id,
            'code': self.code,
            'name': self.name,
            'organisation': self.organisation or '',
            'state': self.state_id.name or '',
            'zone': self.zone_id.code or '',
            'sectors': ', '.join(self.sector_ids.mapped('name')),
            'value': self._format_inr(self.estimated_value) if self.estimated_value else '-',
            'emd': self._format_inr(self.emd_amount) if self.emd_amount else '-',
            'closing': fields.Datetime.to_string(self.date_closing) if self.date_closing else '',
            'days_left': self.days_left_label or '',
            'stage': self.stage_id.name,
            'score': self.relevance_score,
            'url': self.url or '',
            'completion': self.completion_months or 0,
        }

    @api.model
    def agent_search(self, query, live=False, limit=50):
        """The salesperson's AI agent: natural-language query -> parsed filters -> live fetch -> ranked results."""
        Profile = self.env['zivoit.tender.profile']
        filters = self.env['zivoit.tender.ai'].parse_query(query)
        live_info = {'ran': False, 'found': 0, 'errors': []}
        if live:
            sources = self.env['zivoit.tender.source'].search([('source_type', 'not in', ('email', 'manual'))])
            if sources:
                tenders = sources._run(query=query, filters=filters)
                live_info.update(ran=True, found=len(tenders),
                                 errors=[s.name + ': ' + (s.last_message or '')[:150]
                                         for s in sources if s.last_status == 'error'])
        domain = Profile._domain_from_filters(filters)
        candidates = self.search(domain, limit=500)
        scored = []
        for tender in candidates:
            score, reasons = tender._match_score(filters)
            scored.append((score, tender, reasons))
        scored.sort(key=lambda item: (-item[0], item[1].date_closing or datetime.max))
        results = []
        for score, tender, reasons in scored[:limit]:
            row = tender._dashboard_row()
            row.update(score=score, reasons='; '.join(str(r) for r in reasons))
            results.append(row)
        return {'filters': filters, 'results': results, 'total': len(scored), 'live': live_info,
                'domain': domain}
