# -*- coding: utf-8 -*-
from markupsafe import Markup, escape

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

TONES = [
    ('professional', 'Professional'),
    ('friendly', 'Friendly'),
    ('exciting', 'Exciting / promotional'),
    ('formal', 'Formal / corporate'),
    ('academic', 'Academic'),
]


class ZivoAiEventWriter(models.TransientModel):
    _name = 'zivo.ai.event.writer'
    _description = 'AI Event Content Writer'

    event_id = fields.Many2one('event.event', required=True)
    brief = fields.Text(string='Key points', help='Bullet points: audience, agenda, speakers, benefits…')
    tone = fields.Selection(TONES, default='professional', required=True)
    lang_id = fields.Many2one('res.lang', string='Language',
                              default=lambda self: self.env['res.lang']._lang_get(self.env.lang or 'en_US'))
    state = fields.Selection([('draft', 'Draft'), ('done', 'Generated')], default='draft')
    subtitle = fields.Char()
    description = fields.Html(sanitize=True)
    seo_title = fields.Char(string='SEO Title')
    seo_description = fields.Text(string='Meta Description')
    invite_subject = fields.Char(string='Invitation Subject')
    invite_body = fields.Text(string='Invitation Message')
    social_post = fields.Text(string='Social Media Post')

    @api.model
    def _build_prompt(self, event, brief, tone, lang_name):
        address = event.address_id
        facts = [
            'Event name: %s' % event.name,
            'Current subtitle: %s' % (event.subtitle or '-'),
            'Starts: %s (%s)' % (event._zivo_dt_local(event.date_begin), event.date_tz),
            'Ends: %s' % event._zivo_dt_local(event.date_end),
            'Venue: %s' % (', '.join(filter(None, [address.name, address.city, address.country_id.name])) or 'Online'),
            'Event type: %s' % (event.event_type_id.name or '-'),
            'Departments: %s' % (', '.join(event.zivo_department_ids.mapped('name')) or '-'),
            'Tags: %s' % (', '.join(event.tag_ids.mapped('name')) or '-'),
            'Seats: %s' % (event.seats_max if event.seats_limited else 'unlimited'),
            'Access: %s' % dict(event._fields['zivo_access_mode'].selection).get(event.zivo_access_mode or 'open'),
            'Tickets: %s' % (', '.join(event.event_ticket_ids.mapped('name')) or '-'),
            'Existing description: %s' % (html2plaintext(event.description or '')[:1500] or '-'),
        ]
        return (
            'Write marketing content for this event in %s with a %s tone.\n\n%s\n\nOrganiser key points:\n%s\n\n'
            'Return a JSON object with keys: subtitle (max 90 chars), description_html (2-4 short sections using '
            '<h3>, <p> and <ul><li>, no inline styles, no <html>/<body>), seo_title (max 60 chars), '
            'seo_description (max 155 chars), invite_subject, invite_body (plain text, 80-140 words, personal, '
            'mention that the link is personal when access is invitation only), social_post (max 280 chars, '
            'up to 3 hashtags). Do not invent prices, speakers or facts that are not provided.'
        ) % (lang_name, tone, '\n'.join(facts), brief or '-')

    def action_generate(self):
        self.ensure_one()
        lang_name = self.lang_id.name or 'English'
        prompt = self._build_prompt(self.event_id, self.brief, dict(TONES)[self.tone], lang_name)
        data = self.env['zivo.ai.service']._zivo_ai_complete(
            'writer', 'You are an expert event marketing copywriter.', prompt, as_json=True, record=self.event_id)
        self.write({
            'state': 'done',
            'subtitle': (data.get('subtitle') or '')[:200],
            'description': data.get('description_html') or '',
            'seo_title': (data.get('seo_title') or '')[:120],
            'seo_description': data.get('seo_description') or '',
            'invite_subject': data.get('invite_subject') or '',
            'invite_body': data.get('invite_body') or '',
            'social_post': data.get('social_post') or '',
        })
        return self._reopen()

    def _reopen(self):
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new', 'name': self.env._('AI Event Writer')}

    def action_apply(self):
        self.ensure_one()
        if self.state != 'done':
            raise UserError(self.env._('Generate the content first.'))
        vals = {}
        if self.subtitle:
            vals['subtitle'] = self.subtitle
        if self.description:
            vals['description'] = self.description
        if self.seo_title:
            vals['website_meta_title'] = self.seo_title
        if self.seo_description:
            vals['website_meta_description'] = self.seo_description
        if self.invite_subject:
            vals['zivo_invite_subject'] = self.invite_subject
        if self.invite_body:
            vals['zivo_invite_body'] = self.invite_body
        self.event_id.write(vals)
        if self.social_post:
            self.event_id.message_post(body=Markup('<b>%s</b><br/>%s') % (
                self.env._('AI social post suggestion'), escape(self.social_post)))
        return {'type': 'ir.actions.act_window_close'}
