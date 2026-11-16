# -*- coding: utf-8 -*-
"""AI Course Builder: turns product cards or any SOP text into a micro-course with a quiz."""
import random
import re

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.ai_service import LANGUAGES, ZlmsAiUnavailable

BAD_OBJECTION_ANSWERS = [
    "That's our fixed price, take it or leave it.",
    'You can probably find it cheaper online.',
    "I'm not sure, you should ask someone else.",
    'All brands are the same anyway.',
]


class ZlmsAiCourseWizard(models.TransientModel):
    _name = 'zlms.ai.course.wizard'
    _description = 'AI Course Builder'

    source = fields.Selection([('product_card', 'Product Knowledge Cards'), ('text', 'SOP / Training Text')],
                              required=True, default='product_card')
    product_card_ids = fields.Many2many('zlms.product.card', string='Product Cards')
    source_text = fields.Text(string='Training Content',
                              help='Paste an SOP, a product launch brief, a service policy, a VM guideline...')
    topic = fields.Char(string='Course Title', required=True)
    retail_category = fields.Selection(lambda self: self.env['slide.channel']._fields['zlms_retail_category'].selection,
                                       default='product', required=True)
    role_id = fields.Many2one('zlms.role', string='Audience Role')
    language = fields.Selection(LANGUAGES, default='English', required=True)
    question_count = fields.Integer(string='Quiz Questions', default=5)
    lesson_minutes = fields.Integer(string='Lesson Length (min)', default=4)
    add_to_boost = fields.Boolean(string='Feed Daily Boost', default=True,
                                  help='Questions become spaced-repetition cards for enrolled learners.')
    publish = fields.Boolean(default=True)
    use_ai = fields.Boolean(string='Use AI', default=lambda self: self.env['zlms.ai.service'].is_enabled())
    ai_available = fields.Boolean(default=lambda self: self.env['zlms.ai.service'].is_enabled())

    @api.onchange('product_card_ids')
    def _onchange_cards(self):
        if self.product_card_ids and not self.topic:
            self.topic = _('Product Knowledge: %s', ', '.join(self.product_card_ids.mapped('name')[:3]))

    # ------------------------------------------------------------------
    def action_generate(self):
        self.ensure_one()
        if self.source == 'product_card' and not self.product_card_ids:
            raise UserError(_('Select at least one product card.'))
        if self.source == 'text' and not (self.source_text or '').strip():
            raise UserError(_('Paste the training content first.'))
        count = max(1, min(self.question_count or 5, 20))
        content, engine = None, 'offline'
        if self.use_ai and self.env['zlms.ai.service'].is_enabled():
            try:
                content = self._generate_ai(count)
                engine = 'ai'
            except ZlmsAiUnavailable:
                content = None
        if not content:
            content = self._generate_offline(count)
        channel = self._create_course(content, engine)
        if self.source == 'product_card':
            for card in self.product_card_ids:
                card.write({'channel_id': channel.id,
                            'lesson_html': card.lesson_html or (content['lesson_html'] if len(self.product_card_ids) == 1
                                                                else card._offline_lesson_html())})
        return {'type': 'ir.actions.act_window', 'res_model': 'slide.channel', 'res_id': channel.id,
                'view_mode': 'form', 'target': 'current'}

    def _source_material(self):
        if self.source == 'product_card':
            return self.product_card_ids._to_prompt_text()
        return self.source_text

    def _generate_ai(self, count):
        prompt = (
            '%(brand)s\nCreate a %(minutes)s-minute mobile micro-lesson for its store staff.\n'
            'Course title: %(title)s\nAudience: %(role)s\nCategory: %(cat)s\nWrite everything in %(lang)s.\n'
            'Use only facts from the SOURCE. Use short sentences, the Feature-Advantage-Benefit method, a '
            '"say it on the floor" script and 3 key takeaways.\n\nSOURCE:\n%(src)s\n\n'
            'Return JSON: {"summary": str, "lesson_html": str (simple HTML: h3, p, ul, li, table, strong), '
            '"questions": [{"question": str, "answers": [{"text": str, "correct": bool, "comment": str}]}]} '
            'with exactly %(count)s scenario-based multiple-choice questions, 3-4 answers each, exactly one correct.'
        ) % {'brand': self.env['zlms.ai.service'].brand_context(), 'minutes': self.lesson_minutes or 4, 'title': self.topic, 'role': self.role_id.name or 'Store associates',
             'cat': self.retail_category, 'lang': self.language, 'src': self._source_material(), 'count': count}
        data = self.env['zlms.ai.service'].chat_json(
            prompt, system=self.env['zlms.ai.service'].brand_context() + ' You are an expert retail instructional designer.', feature='course_builder',
            max_tokens=4000)
        questions = []
        for q in data.get('questions') or []:
            answers = [a for a in (q.get('answers') or []) if str(a.get('text') or '').strip()]
            if q.get('question') and len(answers) >= 2 and any(a.get('correct') for a in answers):
                questions.append({'question': str(q['question'])[:250], 'answers': [
                    {'text': str(a['text'])[:250], 'correct': bool(a.get('correct')),
                     'comment': str(a.get('comment') or '')} for a in answers]})
        if not data.get('lesson_html') or not questions:
            raise ZlmsAiUnavailable('Incomplete AI course')
        return {'summary': str(data.get('summary') or ''), 'lesson_html': Markup(data['lesson_html']),
                'questions': questions[:count]}

    # ------------------------------------------------------------------
    # Offline engine
    # ------------------------------------------------------------------
    def _generate_offline(self, count):
        if self.source == 'product_card':
            cards = self.product_card_ids
            lesson = Markup('').join(card._offline_lesson_html() for card in cards)
            questions = self._offline_card_questions(cards)
            summary = _('Product knowledge for %s.', ', '.join(cards.mapped('name')))
        else:
            paragraphs = [p.strip() for p in re.split(r'\n\s*\n', self.source_text or '') if p.strip()]
            lesson = Markup('<h2>%s</h2>') % self.topic + Markup('').join(
                Markup('<p>%s</p>') % p for p in paragraphs)
            questions = self._offline_cloze_questions(self.source_text)
            summary = (paragraphs[0][:200] if paragraphs else '')
        if not questions:
            raise UserError(_('Not enough content to build a quiz offline. Add features, benefits and objections '
                              'to the product cards, or configure an AI provider in Settings.'))
        rng = random.Random(self.topic)
        rng.shuffle(questions)
        return {'summary': summary, 'lesson_html': lesson, 'questions': questions[:count]}

    @staticmethod
    def _mcq(question, correct, wrong, comment=''):
        wrong = [w for w in dict.fromkeys(wrong) if w and w != correct][:3]
        if not correct or not wrong:
            return None
        answers = [{'text': correct, 'correct': True, 'comment': comment}] + \
                  [{'text': w, 'correct': False, 'comment': ''} for w in wrong]
        random.Random(question).shuffle(answers)
        return {'question': question[:250], 'answers': answers}

    def _offline_card_questions(self, cards):
        all_cards = self.env['zlms.product.card'].search([]) | cards
        all_tags = self.env['zlms.tech.tag'].search([]).mapped('name')
        questions = []
        for card in cards:
            others = all_cards - card
            other_benefits = [b for o in others for b in o._lines(o.benefits)][:6] or \
                ['It is cheaper than every other brand', 'It never needs any care', 'It is only for professionals']
            for tag in card.tech_tag_ids[:2]:
                q = self._mcq(_('Which technology is built into the %s?', card.name), tag.name,
                              [t for t in all_tags if t not in card.tech_tag_ids.mapped('name')],
                              tag.description or '')
                questions.append(q)
            benefits = card._lines(card.benefits)
            if benefits:
                questions.append(self._mcq(_('What is the main customer benefit of the %s?', card.name),
                                           benefits[0], other_benefits))
            if card.activity:
                other_acts = [o.activity for o in others if o.activity and o.activity != card.activity] or \
                    [_('Professional industrial use'), _('Occasional decorative use'), _('Children under 3')]
                questions.append(self._mcq(_('The %s is best recommended for:', card.name), card.activity, other_acts))
            for objection, answer in card._objection_pairs()[:2]:
                questions.append(self._mcq(_('A customer says: "%s". What is the best response?', objection),
                                           answer, BAD_OBJECTION_ANSWERS,
                                           _('Acknowledge, explain value, then offer a trial.')))
            if card.cross_sell_ids:
                non_cross = (others - card.cross_sell_ids).mapped('name') or \
                    [_('An unrelated gift card'), _('A random clearance item'), _('Nothing - never suggest add-ons')]
                questions.append(self._mcq(_('Which item best completes the look with the %s?', card.name),
                                           card.cross_sell_ids[0].name, non_cross))
            if card.fabric:
                other_fabrics = [o.fabric for o in others if o.fabric and o.fabric != card.fabric] or \
                    ['Untreated paper', 'Raw cardboard', 'Unknown material']
                questions.append(self._mcq(_('What is the %s made of?', card.name), card.fabric, other_fabrics))
        return [q for q in questions if q]

    def _offline_cloze_questions(self, text):
        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text or '') if len(s.split()) >= 6]
        vocab = sorted({w for w in re.findall(r'[A-Za-z][A-Za-z\-]{5,}', text or '')}, key=str.lower)
        questions = []
        for sentence in sentences:
            words = [w for w in re.findall(r'[A-Za-z][A-Za-z\-]{5,}', sentence)]
            if not words:
                continue
            key = max(words, key=len)
            blanked = re.sub(r'\b%s\b' % re.escape(key), '_____', sentence, count=1)
            distractors = [w for w in vocab if w.lower() != key.lower()]
            random.Random(sentence).shuffle(distractors)
            q = self._mcq(_('Fill in the blank: %s', blanked), key, distractors[:3])
            if q:
                questions.append(q)
        return questions

    # ------------------------------------------------------------------
    def _create_course(self, content, engine):
        Channel = self.env['slide.channel'].sudo()
        e = escape
        description = Markup('<p>%s</p>') % (content.get('summary') or '')
        channel = Channel.create({
            'name': self.topic,
            'channel_type': 'training',
            'enroll': 'invite',
            'visibility': 'members',
            'description': description,
            'description_short': description,
            'zlms_retail_category': self.retail_category,
            'zlms_ai_generated': engine == 'ai',
            'zlms_boost_enabled': self.add_to_boost,
            'zlms_product_card_ids': [(6, 0, self.product_card_ids.ids)],
            'is_published': self.publish,
        })
        Slide = self.env['slide.slide'].sudo()
        Slide.create({
            'name': _('Lesson: %s', self.topic),
            'channel_id': channel.id,
            'slide_category': 'article',
            'html_content': content['lesson_html'],
            'completion_time': (self.lesson_minutes or 4) / 60.0,
            'is_published': True,
            'sequence': 1,
        })
        Slide.create({
            'name': _('Quiz: %s', self.topic),
            'channel_id': channel.id,
            'slide_category': 'quiz',
            'is_published': True,
            'sequence': 2,
            'completion_time': 0.05 * len(content['questions']),
            'question_ids': [(0, 0, {
                'sequence': i,
                'question': q['question'],
                'answer_ids': [(0, 0, {'sequence': j, 'text_value': a['text'], 'is_correct': a['correct'],
                                       'comment': a.get('comment') or False})
                               for j, a in enumerate(q['answers'])],
            }) for i, q in enumerate(content['questions'])],
        })
        channel.message_post(body=Markup('%s') % e(_('Course generated by the %s engine with %s questions.',
                                                     _('AI') if engine == 'ai' else _('offline'),
                                                     len(content['questions']))))
        return channel
