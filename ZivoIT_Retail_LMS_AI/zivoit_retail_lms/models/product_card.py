# -*- coding: utf-8 -*-
from markupsafe import Markup, escape

from odoo import _, api, fields, models


class ZlmsProductCard(models.Model):
    _name = 'zlms.product.card'
    _description = 'Product Knowledge Card'
    _inherit = ['mail.thread']
    _order = 'sequence, name'

    name = fields.Char(required=True, tracking=True)
    sequence = fields.Integer(default=10)
    product_tmpl_id = fields.Many2one('product.template', string='Product')
    category = fields.Selection([
        ('apparel', 'Apparel'), ('footwear', 'Footwear'), ('accessories', 'Accessories'),
        ('electronics', 'Electronics'), ('home', 'Home & Kitchen'), ('beauty', 'Beauty & Personal Care'),
        ('food', 'Food & Grocery'), ('health', 'Health & Wellness'), ('services', 'Services & Warranty'),
        ('other', 'Other'),
    ], default='other')
    collection = fields.Char(help='Season / collection, e.g. "Winter 2026 Athleisure".')
    activity = fields.Char(string='Best For / Use Case', help='Who or what situation the product is ideal for.')
    tech_tag_ids = fields.Many2many('zlms.tech.tag', string='Technologies')
    fabric = fields.Char(string='Material / Composition')
    price_band = fields.Char()
    target_customer = fields.Text()
    features = fields.Text(help='One feature per line.')
    advantages = fields.Text(help='What each feature does. One per line.')
    benefits = fields.Text(help='Why the customer cares. One per line.')
    objections = fields.Text(string='Common Objections & Answers',
                             help='Format: "Objection :: Answer", one per line.')
    care_instructions = fields.Text()
    cross_sell_ids = fields.Many2many('zlms.product.card', 'zlms_product_card_cross_rel', 'card_id', 'cross_id',
                                      string='Cross-sell / Complete-the-look')
    keywords = fields.Char(help='Extra search words used by "Ask the Expert".')
    lesson_html = fields.Html(string='Micro-lesson', sanitize_style=True)
    channel_id = fields.Many2one('slide.channel', string='Generated Course', readonly=True, copy=False)
    state = fields.Selection([('draft', 'Draft'), ('published', 'Published')], default='draft', tracking=True)
    active = fields.Boolean(default=True)

    def action_publish(self):
        self.write({'state': 'published'})

    def action_ai_generate_course(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('AI Course Builder'),
            'res_model': 'zlms.ai.course.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_source': 'product_card', 'default_product_card_ids': [(6, 0, self.ids)],
                        'default_topic': self.name},
        }

    def action_view_course(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'slide.channel', 'res_id': self.channel_id.id,
                'view_mode': 'form'}

    # ------------------------------------------------------------------
    # Helpers used by the AI and offline engines
    # ------------------------------------------------------------------
    @staticmethod
    def _lines(text):
        return [l.strip(' -•\t') for l in (text or '').splitlines() if l.strip(' -•\t')]

    def _objection_pairs(self):
        pairs = []
        for line in self._lines(self.objections):
            if '::' in line:
                q, a = line.split('::', 1)
                pairs.append((q.strip(), a.strip()))
        return pairs

    def _to_prompt_text(self):
        parts = []
        for card in self:
            parts.append('\n'.join(filter(None, [
                'PRODUCT: %s' % card.name,
                card.collection and 'Collection: %s' % card.collection,
                card.activity and 'Best for: %s' % card.activity,
                card.fabric and 'Material: %s' % card.fabric,
                card.tech_tag_ids and 'Technologies: %s' % ', '.join(
                    '%s (%s)' % (t.name, t.description or '') for t in card.tech_tag_ids),
                card.price_band and 'Price band: %s' % card.price_band,
                card.target_customer and 'Target customer: %s' % card.target_customer,
                card.features and 'Features:\n%s' % card.features,
                card.advantages and 'Advantages:\n%s' % card.advantages,
                card.benefits and 'Benefits:\n%s' % card.benefits,
                card.objections and 'Objections and answers:\n%s' % card.objections,
                card.care_instructions and 'Care: %s' % card.care_instructions,
                card.cross_sell_ids and 'Cross-sell: %s' % ', '.join(card.cross_sell_ids.mapped('name')),
            ])))
        return '\n\n'.join(parts)

    def _offline_lesson_html(self):
        """Feature-Advantage-Benefit lesson built without AI."""
        self.ensure_one()
        e = escape
        feats, advs, bens = self._lines(self.features), self._lines(self.advantages), self._lines(self.benefits)
        rows = ''
        for i in range(max(len(feats), len(advs), len(bens))):
            rows += '<tr><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                e(feats[i]) if i < len(feats) else '', e(advs[i]) if i < len(advs) else '',
                e(bens[i]) if i < len(bens) else '')
        techs = ''.join('<li><strong>%s</strong> - %s</li>' % (e(t.name), e(t.description or ''))
                        for t in self.tech_tag_ids)
        objections = ''.join('<li><em>"%s"</em><br/>%s</li>' % (e(q), e(a)) for q, a in self._objection_pairs())
        cross = ', '.join(e(n) for n in self.cross_sell_ids.mapped('name'))
        html = '<h2>%s</h2>' % e(self.name)
        if self.activity or self.target_customer:
            html += '<p><strong>Best for:</strong> %s<br/><strong>Who buys it:</strong> %s</p>' % (
                e(self.activity or '-'), e(self.target_customer or '-'))
        if techs:
            html += '<h3>Technology in plain words</h3><ul>%s</ul>' % techs
        if rows:
            html += ('<h3>Feature - Advantage - Benefit</h3><table class="table table-bordered">'
                     '<thead><tr><th>Feature</th><th>Advantage</th><th>Customer Benefit</th></tr></thead>'
                     '<tbody>%s</tbody></table>' % rows)
        if objections:
            html += '<h3>Handling objections</h3><ul>%s</ul>' % objections
        if cross:
            html += '<h3>Complete the look</h3><p>Suggest: %s</p>' % cross
        if self.care_instructions:
            html += '<h3>Care tips to share</h3><p>%s</p>' % e(self.care_instructions)
        html += ('<h3>Say it on the floor</h3><p>"This %s is made for %s. %s - so you %s."</p>' % (
            e(self.name), e(self.activity or 'everyday use'),
            e(feats[0]) if feats else 'It is built for comfort',
            e(bens[0].lower()) if bens else 'stay comfortable all day'))
        return Markup(html)

    def _search_score(self, words):
        self.ensure_one()
        haystack = ' '.join(filter(None, [
            self.name, self.collection, self.activity, self.fabric, self.keywords, self.features,
            self.advantages, self.benefits, self.objections, self.target_customer,
            ' '.join(self.tech_tag_ids.mapped('name'))])).lower()
        return sum(haystack.count(w) * (3 if w in (self.name or '').lower() else 1) for w in words)

    @api.model
    def _find_relevant(self, question, limit=3):
        words = [w for w in ''.join(ch if ch.isalnum() else ' ' for ch in (question or '').lower()).split()
                 if len(w) > 2 and w not in STOPWORDS]
        if not words:
            return self.browse()
        cards = self.search([('state', '=', 'published')])
        scored = sorted(((c._search_score(words), c.id) for c in cards), reverse=True)
        top = scored[0][0] if scored else 0
        return self.browse([cid for score, cid in scored[:limit] if score > 0 and score >= top * 0.5])


STOPWORDS = {
    'the', 'and', 'for', 'what', 'which', 'how', 'does', 'this', 'that', 'with', 'are', 'can', 'you', 'our',
    'your', 'why', 'who', 'customer', 'tell', 'about', 'should', 'have', 'has', 'will', 'from', 'into', 'any',
    'there', 'when', 'best', 'good', 'more', 'than', 'between', 'difference', 'product', 'products',
}
