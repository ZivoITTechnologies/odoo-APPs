# -*- coding: utf-8 -*-
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models, _

from . import tender_utils as tu

SEARCH_FIELDS = ('name', 'description', 'organisation', 'department', 'location', 'reference')


class TenderProfile(models.Model):
    _name = 'zivoit.tender.profile'
    _description = 'Tender Agent (Saved Search & Alerts)'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'sequence, name'

    name = fields.Char('Agent Name', required=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    color = fields.Integer()
    user_id = fields.Many2one('res.users', 'Owner', default=lambda self: self.env.user, required=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    shared = fields.Boolean('Shared with Team', default=True)

    # what to look for
    nl_query = fields.Char('Ask the Agent', tracking=True,
                           help='Natural language, e.g. "open railway construction tenders in SCR above 2 crore"')
    keywords = fields.Text(help='Comma separated words / phrases. A tender must contain at least one.')
    exclude_keywords = fields.Text(help='Comma separated words / phrases. Tenders containing any are ignored.')
    sector_ids = fields.Many2many('zivoit.tender.sector', string='Sectors')
    state_ids = fields.Many2many('res.country.state', string='States', domain="[('country_id.code', '=', 'IN')]")
    zone_ids = fields.Many2many('zivoit.tender.zone', string='Railway Zones / PSUs')
    organisation_keywords = fields.Char('Buyer Contains', help='Comma separated, e.g. NHAI, R&B, GHMC')
    currency_id = fields.Many2one('res.currency', default=lambda self: self.env.ref('base.INR', raise_if_not_found=False)
                                  or self.env.company.currency_id)
    min_value = fields.Monetary('Min. Value')
    max_value = fields.Monetary('Max. Value')
    include_unknown_value = fields.Boolean('Include Tenders without Value', default=True)
    max_emd = fields.Monetary('Max. EMD')
    max_completion_months = fields.Float('Max. Completion (Months)')
    min_days_left = fields.Integer('Min. Days to Submit', default=0,
                                   help='Ignore tenders closing sooner than this (not enough time to bid).')
    deadline_days = fields.Integer('Closing Within (Days)', help='Only tenders closing within N days (0 = any).')
    published_days = fields.Integer('Published Within (Days)', help='Only tenders published / found in the last N days.')
    open_only = fields.Boolean('Open Tenders Only', default=True)
    work_types = fields.Char('Contract Types', help='Comma separated technical codes: item_rate, epc, ham, lump_sum ...')

    # where to look
    live_search = fields.Boolean('Search Portals Live', default=True,
                                 help='When running, also query the selected sources (AI web search, feeds, APIs).')
    source_ids = fields.Many2many('zivoit.tender.source', string='Sources',
                                  domain="[('source_type', 'not in', ('email', 'manual'))]")

    # automation
    frequency = fields.Selection([('manual', 'Manual'), ('hourly', 'Every Hour'), ('6h', 'Every 6 Hours'),
                                  ('daily', 'Daily'), ('weekly', 'Weekly')], default='daily', required=True)
    last_run = fields.Datetime(readonly=True, copy=False)
    next_run = fields.Datetime(compute='_compute_next_run')
    auto_shortlist_score = fields.Integer('Auto-Shortlist at Score', default=0,
                                          help='Matches scoring at least this value are moved to Shortlisted (0 = off).')
    auto_assign_user_id = fields.Many2one('res.users', 'Assign Matches To')
    auto_tag_ids = fields.Many2many('zivoit.tender.tag', string='Tag Matches With')
    notify_mode = fields.Selection([('instant', 'Instant Alert'), ('digest', 'Daily Digest'), ('both', 'Instant + Digest'),
                                    ('none', 'No Notification')], default='both', required=True)
    notify_user_ids = fields.Many2many('res.users', 'zivoit_tender_profile_notify_rel', 'profile_id', 'user_id',
                                       string='Notify', default=lambda self: self.env.user)

    # results
    tender_ids = fields.Many2many('zivoit.tender', 'zivoit_tender_profile_rel', 'profile_id', 'tender_id',
                                  string='Matches', copy=False)
    tender_count = fields.Integer(compute='_compute_counts')
    open_count = fields.Integer(compute='_compute_counts')
    last_new_count = fields.Integer('New in Last Run', readonly=True, copy=False)
    log_ids = fields.One2many('zivoit.tender.fetch.log', 'profile_id', readonly=True)
    filter_summary = fields.Text(compute='_compute_filter_summary')

    @api.depends('tender_ids', 'tender_ids.is_open')
    def _compute_counts(self):
        for profile in self:
            profile.tender_count = len(profile.tender_ids)
            profile.open_count = len(profile.tender_ids.filtered('is_open'))

    @api.depends('last_run', 'frequency')
    def _compute_next_run(self):
        delta = {'hourly': timedelta(hours=1), '6h': timedelta(hours=6), 'daily': timedelta(days=1),
                 'weekly': timedelta(days=7)}
        for profile in self:
            if profile.frequency == 'manual':
                profile.next_run = False
            else:
                profile.next_run = (profile.last_run or fields.Datetime.now()) + delta[profile.frequency] \
                    if profile.last_run else fields.Datetime.now()

    @api.depends('keywords', 'exclude_keywords', 'sector_ids', 'state_ids', 'zone_ids', 'min_value', 'max_value',
                 'deadline_days', 'published_days', 'organisation_keywords', 'open_only', 'max_emd',
                 'max_completion_months', 'min_days_left')
    def _compute_filter_summary(self):
        for profile in self:
            parts = []
            if profile.keywords:
                parts.append(_('Keywords: %s', profile.keywords))
            if profile.exclude_keywords:
                parts.append(_('Excluding: %s', profile.exclude_keywords))
            if profile.sector_ids:
                parts.append(_('Sectors: %s', ', '.join(profile.sector_ids.mapped('name'))))
            if profile.state_ids:
                parts.append(_('States: %s', ', '.join(profile.state_ids.mapped('name'))))
            if profile.zone_ids:
                parts.append(_('Zones: %s', ', '.join(profile.zone_ids.mapped('code'))))
            if profile.organisation_keywords:
                parts.append(_('Buyer: %s', profile.organisation_keywords))
            if profile.min_value or profile.max_value:
                parts.append(_('Value: %(min)s - %(max)s', min=self.env['zivoit.tender']._format_inr(profile.min_value),
                               max=self.env['zivoit.tender']._format_inr(profile.max_value) if profile.max_value else '∞'))
            if profile.max_emd:
                parts.append(_('EMD up to %s', self.env['zivoit.tender']._format_inr(profile.max_emd)))
            if profile.max_completion_months:
                parts.append(_('Completion up to %s months', profile.max_completion_months))
            if profile.deadline_days:
                parts.append(_('Closing within %s days', profile.deadline_days))
            if profile.min_days_left:
                parts.append(_('At least %s days to bid', profile.min_days_left))
            if profile.published_days:
                parts.append(_('Published in last %s days', profile.published_days))
            parts.append(_('Open only') if profile.open_only else _('Including closed'))
            profile.filter_summary = '\n'.join(parts)

    # ------------------------------------------------------------------
    # natural language -> fields
    # ------------------------------------------------------------------
    def action_parse_query(self):
        for profile in self.filtered('nl_query'):
            profile._apply_filters(self.env['zivoit.tender.ai'].parse_query(profile.nl_query))
        return True

    @api.onchange('nl_query')
    def _onchange_nl_query(self):
        if self.nl_query:
            self._apply_filters(tu.parse_query(self.nl_query, self.env['zivoit.tender.sector']._get_keyword_map() or None))

    def _apply_filters(self, filters):
        self.ensure_one()
        Sector = self.env['zivoit.tender.sector']
        State = self.env['res.country.state']
        Zone = self.env['zivoit.tender.zone']
        vals = {
            'keywords': ', '.join(filters.get('keywords') or []) or False,
            'exclude_keywords': ', '.join(filters.get('exclude_keywords') or []) or False,
            'sector_ids': [(6, 0, Sector.search([('code', 'in', filters.get('sectors') or [])]).ids)],
            'state_ids': [(6, 0, State.search([('country_id.code', '=', 'IN'),
                                               ('name', 'in', filters.get('states') or [])]).ids)],
            'zone_ids': [(6, 0, Zone.search([('code', 'in', [z.upper() for z in filters.get('zones') or []])]).ids)],
            'organisation_keywords': ', '.join(filters.get('organisations') or []) or False,
            'min_value': filters.get('min_value') or 0.0,
            'max_value': filters.get('max_value') or 0.0,
            'deadline_days': filters.get('deadline_days') or 0,
            'published_days': filters.get('published_days') or 0,
            'open_only': filters.get('open_only', True),
        }
        if isinstance(self.id, int):
            self.write(vals)
        else:
            self.update(vals)

    def _get_filters(self):
        self.ensure_one()
        return {
            'keywords': tu.keyword_list(self.keywords),
            'exclude_keywords': tu.keyword_list(self.exclude_keywords),
            'sectors': self.sector_ids.mapped('code'),
            'states': self.state_ids.mapped('name'),
            'zones': self.zone_ids.mapped('code'),
            'organisations': tu.keyword_list(self.organisation_keywords),
            'min_value': self.min_value,
            'max_value': self.max_value,
            'include_unknown_value': self.include_unknown_value,
            'max_emd': self.max_emd,
            'max_completion_months': self.max_completion_months,
            'min_days_left': self.min_days_left,
            'deadline_days': self.deadline_days,
            'published_days': self.published_days,
            'open_only': self.open_only,
            'work_types': tu.keyword_list(self.work_types),
        }

    def _query_text(self):
        self.ensure_one()
        if self.nl_query:
            return self.nl_query
        bits = tu.keyword_list(self.keywords)[:5] + self.sector_ids.mapped('name') + \
            self.zone_ids.mapped('name') + self.state_ids.mapped('name')
        return ' '.join(bits) + ' tenders'

    # ------------------------------------------------------------------
    # domain building (shared with the dashboard agent)
    # ------------------------------------------------------------------
    @api.model
    def _domain_from_filters(self, filters):
        now = fields.Datetime.now()
        domain = []
        keywords = filters.get('keywords') or []
        if keywords:
            ors = []
            for keyword in keywords:
                for field in SEARCH_FIELDS:
                    ors.append([(field, 'ilike', keyword)])
            domain += ['|'] * (len(ors) - 1) + [leaf for group in ors for leaf in group]
        for word in filters.get('exclude_keywords') or []:
            domain += [('name', 'not ilike', word)]
        if filters.get('sectors'):
            domain += [('sector_ids.code', 'in', filters['sectors'])]
        if filters.get('states'):
            domain += [('state_id.name', 'in', filters['states'])]
        if filters.get('zones'):
            domain += [('zone_id.code', 'in', [z.upper() for z in filters['zones']])]
        orgs = filters.get('organisations') or []
        if orgs:
            ors = []
            for org in orgs:
                ors += [[('organisation', 'ilike', org)], [('department', 'ilike', org)], [('name', 'ilike', org)]]
            domain += ['|'] * (len(ors) - 1) + [leaf for group in ors for leaf in group]
        include_unknown = filters.get('include_unknown_value', True)
        if filters.get('min_value'):
            leaf = [('estimated_value', '>=', filters['min_value'])]
            domain += (['|', ('estimated_value', '=', 0)] + leaf) if include_unknown else leaf
        if filters.get('max_value'):
            domain += [('estimated_value', '<=', filters['max_value'])]
        if filters.get('max_emd'):
            domain += [('emd_amount', '<=', filters['max_emd'])]
        if filters.get('max_completion_months'):
            domain += ['|', ('completion_months', '=', 0), ('completion_months', '<=', filters['max_completion_months'])]
        if filters.get('work_types'):
            domain += [('work_type', 'in', filters['work_types'])]
        if filters.get('open_only', True):
            domain += [('stage_type', 'not in', ('won', 'lost', 'dropped', 'expired')),
                       '|', ('date_closing', '=', False), ('date_closing', '>', fields.Datetime.to_string(now))]
        if filters.get('min_days_left'):
            limit = now + timedelta(days=filters['min_days_left'])
            domain += ['|', ('date_closing', '=', False), ('date_closing', '>=', fields.Datetime.to_string(limit))]
        if filters.get('deadline_days'):
            limit = now + timedelta(days=filters['deadline_days'])
            domain += [('date_closing', '<=', fields.Datetime.to_string(limit))]
        if filters.get('published_days'):
            since = fields.Datetime.to_string(now - timedelta(days=filters['published_days']))
            domain += ['|', ('date_published', '>=', since), '&', ('date_published', '=', False), ('found_date', '>=', since)]
        return domain

    def _build_domain(self):
        self.ensure_one()
        return self._domain_from_filters(self._get_filters())

    # ------------------------------------------------------------------
    # running the agent
    # ------------------------------------------------------------------
    def action_run(self):
        total_new = 0
        for profile in self:
            if profile.live_search:
                sources = profile.source_ids or self.env['zivoit.tender.source'].search(
                    [('source_type', 'not in', ('email', 'manual'))])
                sources._run(query=profile._query_text(), filters=profile._get_filters(), profile=profile)
            matches = self.env['zivoit.tender'].search(profile._build_domain())
            new = profile._process_matches(matches)
            total_new += len(new)
            profile.write({'last_run': fields.Datetime.now(), 'last_new_count': len(new)})
        if len(self) == 1:
            action = self.action_view_tenders()
            action['context'] = dict(action.get('context') or {}, zivoit_new_matches=total_new)
            return action
        return True

    def _process_matches(self, tenders):
        """Link matches, score, tag/assign, auto-shortlist and notify. Returns newly matched tenders."""
        self.ensure_one()
        if not tenders:
            return tenders
        filters = self._get_filters()
        already = self.tender_ids
        new = tenders - already
        shortlist_stage = self.env['zivoit.tender.stage'].search([('stage_type', '=', 'shortlist')], limit=1)
        for tender in tenders:
            score, reasons = tender._match_score(filters)
            vals = {}
            if score > (tender.relevance_score or 0) or tender in new:
                vals['relevance_score'] = max(score, tender.relevance_score or 0)
                vals['match_reason'] = '%s: %s' % (self.name, '; '.join(str(r) for r in reasons))
            if tender in new:
                if self.auto_tag_ids:
                    vals['tag_ids'] = [(4, tag.id) for tag in self.auto_tag_ids]
                if self.auto_assign_user_id:
                    vals['user_id'] = self.auto_assign_user_id.id
                if score >= 80 and tender.priority in ('0', False):
                    vals['priority'] = '2'
                elif score >= 60 and tender.priority in ('0', False):
                    vals['priority'] = '1'
            if vals:
                tender.with_context(tracking_disable=True).write(vals)
            if (tender in new and self.auto_shortlist_score and score >= self.auto_shortlist_score
                    and shortlist_stage and tender.stage_type in ('new', 'review')):
                tender.stage_id = shortlist_stage
        self.tender_ids = [(4, t.id) for t in new]
        if new and self.notify_user_ids:
            new.message_subscribe(partner_ids=self.notify_user_ids.partner_id.ids)
        if new and self.notify_mode in ('instant', 'both'):
            self._notify_new(new)
        return new

    def _notify_new(self, tenders):
        self.ensure_one()
        Tender = self.env['zivoit.tender']
        rows = Markup('').join(
            Markup('<li><a href="/odoo/action-zivoit_tender_ai.action_zivoit_tender/%(id)s">%(name)s</a> - %(org)s '
                   '| %(value)s | closes %(closing)s | score %(score)s</li>') % {
                'id': t.id, 'name': t.name[:140], 'org': t.organisation or '-',
                'value': ('₹ ' + Tender._format_inr(t.estimated_value)) if t.estimated_value else _('value n/a'),
                'closing': t.date_closing or '-', 'score': t.relevance_score,
            } for t in tenders.sorted(lambda t: -t.relevance_score)[:25])
        more = len(tenders) - 25
        body = Markup('<p><b>%s</b></p><ul>%s</ul>') % (
            _('%(count)s new tender(s) match agent "%(agent)s"', count=len(tenders), agent=self.name), rows)
        if more > 0:
            body += Markup('<p>%s</p>') % _('... and %s more.', more)
        self.message_post(body=body, partner_ids=self.notify_user_ids.partner_id.ids,
                          subtype_xmlid='mail.mt_comment', message_type='comment')

    def _get_digest_tenders(self):
        """Tenders for the daily e-mail digest: new matches (24h) and matched tenders closing in 3 days."""
        self.ensure_one()
        now = fields.Datetime.now()
        new = self.tender_ids.filtered(lambda t: t.is_open and t.found_date and t.found_date >= now - timedelta(days=1))
        closing = self.tender_ids.filtered(
            lambda t: t.is_open and t.date_closing and now <= t.date_closing <= now + timedelta(days=3)
            and t.stage_type not in ('dropped',))
        return {'new': new.sorted(lambda t: -t.relevance_score), 'closing': closing.sorted('date_closing')}

    def action_send_digest(self):
        template = self.env.ref('zivoit_tender_ai.mail_template_tender_digest')
        for profile in self:
            digest = profile._get_digest_tenders()
            if not (digest['new'] or digest['closing']) or not profile.notify_user_ids:
                continue
            for user in profile.notify_user_ids.filtered('email'):
                template.with_context(digest_user=user).send_mail(
                    profile.id, email_values={'email_to': user.email, 'recipient_ids': []})
        return True

    def action_view_tenders(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_tender_ai.action_zivoit_tender')
        action['domain'] = [('profile_ids', 'in', self.id)]
        action['context'] = {'search_default_filter_open': 1, 'default_profile_ids': [(4, self.id)]}
        action['name'] = _('Matches: %s', self.name)
        return action

    @api.model
    def _cron_run_agents(self):
        now = fields.Datetime.now()
        for profile in self.search([('frequency', '!=', 'manual')]):
            if profile.next_run and profile.next_run > now + timedelta(minutes=5):
                continue
            try:
                with self.env.cr.savepoint():
                    profile.action_run()
            except Exception as error:  # noqa: BLE001
                profile.message_post(body=_('Agent run failed: %s', error))
        return True

    @api.model
    def _cron_send_digests(self):
        self.search([('notify_mode', 'in', ('digest', 'both'))]).action_send_digest()
        return True
