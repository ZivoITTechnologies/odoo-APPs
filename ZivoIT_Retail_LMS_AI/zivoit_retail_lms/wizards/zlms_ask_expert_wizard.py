# -*- coding: utf-8 -*-
"""Ask the Expert: answers product questions on the shop floor, grounded on product knowledge cards."""
from markupsafe import Markup, escape

from odoo import _, api, fields, models

from ..models.ai_service import LANGUAGES, ZlmsAiUnavailable


class ZlmsAskExpertWizard(models.TransientModel):
    _name = 'zlms.ask.expert.wizard'
    _description = 'Ask the Product Expert'

    question = fields.Text(required=True)
    language = fields.Selection(LANGUAGES, default='English')
    answer_html = fields.Html(readonly=True, sanitize=True)
    source_card_ids = fields.Many2many('zlms.product.card', string='Sources', readonly=True)
    engine = fields.Selection([('ai', 'AI'), ('offline', 'Offline engine')], readonly=True)

    def action_ask(self):
        self.ensure_one()
        result = self.env['zlms.ask.expert.wizard'].answer_question(self.question, self.language)
        self.write({'answer_html': result['html'], 'source_card_ids': [(6, 0, result['card_ids'])],
                    'engine': result['engine']})
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new'}

    @api.model
    def answer_question(self, question, language='English'):
        cards = self.env['zlms.product.card'].sudo()._find_relevant(question, limit=3)
        e = escape
        if self.env['zlms.ai.service'].is_enabled():
            system = (self.env['zlms.ai.service'].brand_context() + ' You are its product expert, helping a store associate who is '
                      'with a customer right now. Answer ONLY from the product facts given. If the facts do not '
                      'cover it, say so and suggest asking the store manager. Max 120 words, bullet points, end with '
                      'a one-line script the associate can say. Answer in %s.') % (language or 'English')
            prompt = 'PRODUCT FACTS:\n%s\n\nQUESTION: %s' % (cards._to_prompt_text() or 'No matching product.', question)
            try:
                text = self.env['zlms.ai.service'].chat([{'role': 'user', 'content': prompt}], system=system,
                                                        feature='ask_expert', max_tokens=500)
                html = Markup('').join(Markup('<p>%s</p>') % line for line in text.splitlines() if line.strip())
                return {'html': html, 'card_ids': cards.ids, 'engine': 'ai'}
            except ZlmsAiUnavailable:
                pass
        if not cards:
            html = Markup('<p>%s</p>') % _('I could not find this in the product knowledge base. '
                                           'Please check with your store manager.')
            return {'html': html, 'card_ids': [], 'engine': 'offline'}
        parts = []
        for card in cards:
            bullets = card._lines(card.benefits)[:3] + card._lines(card.features)[:2]
            techs = ', '.join(card.tech_tag_ids.mapped('name'))
            body = '<h5>%s</h5>' % e(card.name)
            if techs:
                body += '<p><strong>%s</strong> %s</p>' % (e(_('Technology:')), e(techs))
            if card.activity:
                body += '<p><strong>%s</strong> %s</p>' % (e(_('Best for:')), e(card.activity))
            if bullets:
                body += '<ul>%s</ul>' % ''.join('<li>%s</li>' % e(b) for b in bullets)
            q_lower = (question or '').lower()
            for objection, answer in card._objection_pairs():
                if any(w in q_lower for w in objection.lower().split() if len(w) > 3):
                    body += '<p><em>%s</em> %s</p>' % (e(_('If asked:')), e(answer))
                    break
            parts.append(body)
        return {'html': Markup(''.join(parts)), 'card_ids': cards.ids, 'engine': 'offline'}
