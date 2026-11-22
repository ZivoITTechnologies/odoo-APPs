# -*- coding: utf-8 -*-
from odoo import fields, models, _


class TenderAgentSearch(models.TransientModel):
    _name = 'zivoit.tender.agent.search'
    _description = 'Ask the Tender Agent'

    query = fields.Char('What are you looking for?', required=True,
                        help='e.g. "open tenders in railways construction in SCR" or '
                             '"road construction tenders in Telangana above 5 crore closing in 15 days"')
    live = fields.Boolean('Search Portals Live', default=True,
                          help='Also query the configured sources (AI web search, feeds, APIs) now.')
    parsed = fields.Text('Understood As', readonly=True)
    engine = fields.Char(readonly=True)
    result_count = fields.Integer(readonly=True)
    tender_ids = fields.Many2many('zivoit.tender', string='Results', readonly=True)
    live_message = fields.Text(readonly=True)
    state = fields.Selection([('ask', 'Ask'), ('done', 'Results')], default='ask')

    def action_search(self):
        self.ensure_one()
        result = self.env['zivoit.tender'].agent_search(self.query, live=self.live, limit=200)
        filters = result['filters']
        lines = []
        for key, label in (('keywords', _('Keywords')), ('exclude_keywords', _('Excluding')), ('sectors', _('Sectors')),
                           ('states', _('States')), ('zones', _('Zones')), ('organisations', _('Buyers'))):
            if filters.get(key):
                lines.append('%s: %s' % (label, ', '.join(filters[key])))
        Tender = self.env['zivoit.tender']
        if filters.get('min_value'):
            lines.append(_('Min value: ₹ %s', Tender._format_inr(filters['min_value'])))
        if filters.get('max_value'):
            lines.append(_('Max value: ₹ %s', Tender._format_inr(filters['max_value'])))
        if filters.get('deadline_days'):
            lines.append(_('Closing within %s days', filters['deadline_days']))
        if filters.get('published_days'):
            lines.append(_('Published in last %s days', filters['published_days']))
        lines.append(_('Open tenders only') if filters.get('open_only', True) else _('Including closed tenders'))
        live = result['live']
        live_message = ''
        if live.get('ran'):
            live_message = _('%s tenders fetched from portals.', live['found'])
            if live.get('errors'):
                live_message += '\n' + '\n'.join(live['errors'])
        self.write({
            'parsed': '\n'.join(lines),
            'engine': _('AI model') if filters.get('engine') == 'ai' else _('Rule engine'),
            'result_count': result['total'],
            'tender_ids': [(6, 0, [row['id'] for row in result['results']])],
            'live_message': live_message,
            'state': 'done',
        })
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new', 'name': _('Tender Agent')}

    def action_open_results(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_tender_ai.action_zivoit_tender')
        action.update({'domain': [('id', 'in', self.tender_ids.ids)], 'name': _('Agent: %s', self.query),
                       'context': {}, 'view_mode': 'list,kanban,form,pivot,graph'})
        action['views'] = [(self.env.ref('zivoit_tender_ai.view_zivoit_tender_list').id, 'list'),
                           (False, 'kanban'), (False, 'form'), (False, 'pivot'), (False, 'graph')]
        return action

    def action_save_agent(self):
        self.ensure_one()
        profile = self.env['zivoit.tender.profile'].create({'name': self.query[:60], 'nl_query': self.query})
        profile.action_parse_query()
        profile._process_matches(self.tender_ids)
        return {'type': 'ir.actions.act_window', 'res_model': 'zivoit.tender.profile', 'res_id': profile.id,
                'view_mode': 'form', 'target': 'current'}

    def action_shortlist_selected(self):
        self.tender_ids.filtered(lambda t: t.stage_type in ('new', 'review')).action_shortlist()
        return self.action_open_results()
