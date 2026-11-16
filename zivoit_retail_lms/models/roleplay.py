# -*- coding: utf-8 -*-
"""AI role-play coach for customer service and selling skills."""
import re

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .ai_service import LANGUAGES, ZlmsAiUnavailable

SKILL_AREAS = [
    ('greeting', 'Greeting & Rapport'),
    ('discovery', 'Need Discovery'),
    ('demo', 'Product Demonstration'),
    ('price_objection', 'Price Objection'),
    ('quality_objection', 'Quality / Comparison Objection'),
    ('cross_sell', 'Cross-sell & Upsell'),
    ('complaint', 'Complaint Handling'),
    ('return', 'Returns & Exchanges'),
    ('loyalty', 'Loyalty & Closing'),
]

DEFAULT_RUBRIC = """Greeting & rapport | welcome, hello, hi, namaste, good morning, good evening, help you
Asked discovery questions | ?, looking for, which, what kind, how often, activity, size
Linked features to benefits | so you, which means, helps you, keeps you, benefit, comfortable, because
Handled the concern with empathy | understand, i hear, fair point, great question, absolutely, sure, no problem
Suggested complementary items | also, pair, match, combo, complete the look, along with, add
Moved to close | try, trial room, billing, shall i, would you like, pack, size for you"""

OFFLINE_CUSTOMER_FALLBACK = {
    'greeting': ['Just looking around for now.', 'Okay, what is new this season?', 'Hmm, nice. What do people usually buy here?'],
    'discovery': ['I need something for daily use.', 'I want it to last, I use things a lot.', 'My budget is flexible if it is worth it.'],
    'demo': ['What is so special about this one?', 'Will it really work the way you say?', 'And how do I take care of it?'],
    'price_objection': ['This looks good but it is a bit expensive.', 'I saw something cheaper online.',
                        'Is there any offer running right now?'],
    'quality_objection': ['How is this better than the big international brands?', 'How long will it last?',
                          'What if something goes wrong after I buy it?'],
    'cross_sell': ["I'll just take this one.", 'Do I really need anything else?', 'Okay, what would go with it?'],
    'complaint': ['The product I bought last week has a defect!', 'I am really disappointed, I paid good money.',
                  'So what will you do about it?'],
    'return': ['I want to return this, it is not what I expected.', 'I lost the bill, is that a problem?',
               'Can I exchange it for something else?'],
    'loyalty': ["Okay, I'm ready to pay.", 'Why should I give my phone number?', 'Fine, what do I get?'],
}


class ZlmsRoleplayScenario(models.Model):
    _name = 'zlms.roleplay.scenario'
    _description = 'Role-play Scenario'
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    skill_area = fields.Selection(SKILL_AREAS, required=True, default='discovery')
    skill_id = fields.Many2one('zlms.skill')
    difficulty = fields.Selection([('1', 'Easy'), ('2', 'Medium'), ('3', 'Hard')], default='2')
    customer_persona = fields.Text(required=True, translate=True,
                                   help='Who the AI customer is: age, need, mood, budget, personality.')
    hidden_goal = fields.Text(string='Customer Hidden Goal', translate=True,
                              help='What would make this customer buy (never revealed directly).')
    opening_line = fields.Char(required=True, translate=True)
    learner_objective = fields.Text(translate=True, help='Shown to the learner before starting.')
    customer_script = fields.Text(translate=True,
                                  help='Offline engine: customer lines used in order, one per line.')
    rubric = fields.Text(default=DEFAULT_RUBRIC,
                         help='One criterion per line: "Criterion | keyword, keyword" (keywords for offline scoring).')
    product_card_ids = fields.Many2many('zlms.product.card', string='Products in Play')
    max_turns = fields.Integer(default=6)
    pass_score = fields.Float(default=60.0)
    language = fields.Selection(LANGUAGES, default='English')
    session_ids = fields.One2many('zlms.roleplay.session', 'scenario_id')
    session_count = fields.Integer(compute='_compute_session_stats')
    avg_score = fields.Float(compute='_compute_session_stats')
    active = fields.Boolean(default=True)

    def _compute_session_stats(self):
        for scenario in self:
            done = scenario.session_ids.filtered(lambda s: s.state == 'evaluated')
            scenario.session_count = len(scenario.session_ids)
            scenario.avg_score = round(sum(done.mapped('score')) / len(done), 1) if done else 0.0

    def _rubric_items(self):
        self.ensure_one()
        items = []
        for line in (self.rubric or DEFAULT_RUBRIC).splitlines():
            if not line.strip():
                continue
            name, _sep, kw = line.partition('|')
            items.append((name.strip(), [k.strip().lower() for k in kw.split(',') if k.strip()]))
        return items

    def action_start_session(self):
        self.ensure_one()
        learner = self.env['zlms.learner'].browse(self.env.context.get('default_learner_id')) \
            or self.env['zlms.learner']._get_current_learner()
        if not learner:
            raise UserError(_('Select a learner to practise this scenario.'))
        session = self.env['zlms.roleplay.session'].create({'scenario_id': self.id, 'learner_id': learner.id})
        session.action_start()
        return {'type': 'ir.actions.act_window', 'res_model': 'zlms.roleplay.session', 'res_id': session.id,
                'view_mode': 'form', 'target': 'current'}


class ZlmsRoleplaySession(models.Model):
    _name = 'zlms.roleplay.session'
    _description = 'Role-play Practice Session'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(compute='_compute_name', store=True)
    scenario_id = fields.Many2one('zlms.roleplay.scenario', required=True, ondelete='restrict')
    skill_area = fields.Selection(related='scenario_id.skill_area', store=True)
    learner_id = fields.Many2one('zlms.learner', required=True, ondelete='cascade', index=True)
    learner_user_id = fields.Many2one(related='learner_id.user_id', store=True)
    store_id = fields.Many2one(related='learner_id.store_id', store=True)
    store_manager_user_id = fields.Many2one(related='learner_id.store_id.manager_user_id', store=True)
    state = fields.Selection([('draft', 'Not Started'), ('ongoing', 'In Conversation'), ('evaluated', 'Evaluated')],
                             default='draft', tracking=True)
    engine = fields.Selection([('ai', 'AI'), ('offline', 'Offline engine')], readonly=True)
    message_line_ids = fields.One2many('zlms.roleplay.message', 'session_id', string='Conversation')
    reply_text = fields.Text(string='Your Reply')
    turn_count = fields.Integer(compute='_compute_turn_count')
    score = fields.Float(readonly=True, aggregator='avg')
    passed = fields.Boolean(readonly=True)
    criterion_ids = fields.One2many('zlms.roleplay.criterion', 'session_id', string='Rubric Scores')
    feedback_html = fields.Html(readonly=True, sanitize=True)
    session_date = fields.Datetime(default=fields.Datetime.now)

    @api.depends('scenario_id', 'learner_id')
    def _compute_name(self):
        for s in self:
            s.name = '%s - %s' % (s.scenario_id.name or '', s.learner_id.name or '')

    @api.depends('message_line_ids.role')
    def _compute_turn_count(self):
        for s in self:
            s.turn_count = len(s.message_line_ids.filtered(lambda m: m.role == 'associate'))

    # ------------------------------------------------------------------
    # Conversation
    # ------------------------------------------------------------------
    def action_start(self):
        for session in self:
            if session.state != 'draft':
                continue
            session.write({
                'state': 'ongoing',
                'engine': 'ai' if self.env['zlms.ai.service'].is_enabled() else 'offline',
                'message_line_ids': [(0, 0, {'role': 'customer', 'content': session.scenario_id.opening_line})],
            })
        return True

    def action_send_reply(self):
        self.ensure_one()
        if not (self.reply_text or '').strip():
            raise UserError(_('Type what you would say to the customer.'))
        self.send_message(self.reply_text)
        self.reply_text = False
        return True

    def send_message(self, text):
        """Store the associate's message and return the customer's reply (or evaluate when finished)."""
        self.ensure_one()
        if self.state == 'draft':
            self.action_start()
        if self.state != 'ongoing':
            raise UserError(_('This role-play is already finished.'))
        text = (text or '').strip()[:2000]
        self.env['zlms.roleplay.message'].create({'session_id': self.id, 'role': 'associate', 'content': text})
        if self.turn_count >= self.scenario_id.max_turns:
            self.action_evaluate()
            return False
        reply, finished = self._customer_reply()
        self.env['zlms.roleplay.message'].create({'session_id': self.id, 'role': 'customer', 'content': reply})
        if finished:
            self.action_evaluate()
        return reply

    def _transcript(self):
        return '\n'.join('%s: %s' % ('CUSTOMER' if m.role == 'customer' else 'ASSOCIATE', m.content)
                         for m in self.message_line_ids)

    def _customer_reply(self):
        scenario = self.scenario_id
        if self.engine == 'ai':
            system = (
                '%s You are role-playing a CUSTOMER in one of its stores, to train a '
                'store associate. Stay in character, never act as the associate and never coach. '
                'Persona: %s\nHidden goal (reveal only if the associate asks good questions): %s\n'
                'Products in the store you may ask about:\n%s\n'
                'Reply in %s with 1-3 short, natural sentences. Raise realistic objections. When you are '
                'convinced to buy, or clearly want to leave, end your message with the token [END].'
            ) % (self.env['zlms.ai.service'].brand_context(), scenario.customer_persona, scenario.hidden_goal or '-',
                 scenario.product_card_ids._to_prompt_text() or '-', scenario.language or 'English')
            messages = [{'role': 'assistant' if m.role == 'customer' else 'user', 'content': m.content}
                        for m in self.message_line_ids]
            try:
                reply = self.env['zlms.ai.service'].chat(messages, system=system, feature='roleplay_customer',
                                                         max_tokens=300)
                finished = '[END]' in reply
                return reply.replace('[END]', '').strip() or _('Okay, thank you.'), finished
            except ZlmsAiUnavailable:
                self.engine = 'offline'
        lines = [l.strip() for l in (scenario.customer_script or '').splitlines() if l.strip()] \
            or OFFLINE_CUSTOMER_FALLBACK.get(scenario.skill_area, OFFLINE_CUSTOMER_FALLBACK['discovery'])
        idx = self.turn_count - 1
        if idx < len(lines):
            return lines[idx], False
        return _('Okay, that sounds good. I will go with your suggestion, thank you!'), True

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    def action_evaluate(self):
        for session in self:
            if not session.message_line_ids.filtered(lambda m: m.role == 'associate'):
                raise UserError(_('Reply to the customer at least once before evaluation.'))
            result = None
            if session.engine == 'ai':
                result = session._evaluate_ai()
            if not result:
                result = session._evaluate_offline()
            session._apply_evaluation(result)
        return True

    def _evaluate_ai(self):
        scenario = self.scenario_id
        criteria = [name for name, _kw in scenario._rubric_items()]
        prompt = (
            'Evaluate this retail store associate in a customer role-play.\nScenario: %s (%s)\n'
            'Learner objective: %s\nProduct facts:\n%s\n\nRubric criteria: %s\n\nTRANSCRIPT\n%s\n\n'
            'Return JSON: {"criteria": [{"name": str, "score": 0-10, "comment": str}], "overall": 0-100, '
            '"strengths": [str], "improvements": [str], "model_answer": str (a better reply the associate could '
            'have given at the weakest moment)}. Be specific, encouraging and practical. Write comments in %s.'
        ) % (scenario.name, dict(SKILL_AREAS).get(scenario.skill_area), scenario.learner_objective or '-',
             scenario.product_card_ids._to_prompt_text() or '-', '; '.join(criteria), self._transcript(),
             scenario.language or 'English')
        try:
            data = self.env['zlms.ai.service'].chat_json(
                prompt, system=self.env['zlms.ai.service'].brand_context() + ' You are an expert retail sales trainer and assessor.', feature='roleplay_eval')
        except ZlmsAiUnavailable:
            return None
        crits = [{'name': str(c.get('name', ''))[:120], 'score': max(0.0, min(10.0, float(c.get('score') or 0))),
                  'comment': str(c.get('comment', ''))} for c in data.get('criteria', []) if c.get('name')]
        if not crits:
            return None
        overall = data.get('overall')
        overall = float(overall) if overall is not None else sum(c['score'] for c in crits) * 10.0 / len(crits)
        return {'criteria': crits, 'overall': max(0.0, min(100.0, overall)),
                'strengths': [str(s) for s in data.get('strengths', [])],
                'improvements': [str(s) for s in data.get('improvements', [])],
                'model_answer': str(data.get('model_answer') or '')}

    def _evaluate_offline(self):
        scenario = self.scenario_id
        said = ' '.join(self.message_line_ids.filtered(lambda m: m.role == 'associate').mapped('content')).lower()
        product_words = set()
        for card in scenario.product_card_ids:
            product_words.update(t.name.lower() for t in card.tech_tag_ids)
            for feat in card._lines(card.features):
                product_words.update(w for w in re.findall(r'[a-z\-]{5,}', feat.lower()))
        crits, strengths, improvements = [], [], []
        for name, keywords in scenario._rubric_items():
            hits = [k for k in keywords if k and k in said]
            score = 3.0 + min(len(hits), 3) * 2.0 + (1.0 if len(hits) >= 4 else 0.0)
            comment = (_('Good: you used "%s".', '", "'.join(hits[:3])) if hits
                       else _('Not observed. Try phrases like "%s".', '", "'.join(keywords[:3])))
            crits.append({'name': name, 'score': min(score, 10.0), 'comment': comment})
            (strengths if hits else improvements).append(name)
        if product_words:
            used = [w for w in product_words if w in said]
            pk = min(10.0, 3.0 + 2.5 * len(used))
            crits.append({'name': _('Product knowledge'), 'score': pk,
                          'comment': _('Mentioned: %s', ', '.join(sorted(used)[:5])) if used
                          else _('Mention the product technology and what it does for the customer.')})
            (strengths if used else improvements).append(_('Product knowledge'))
        overall = sum(c['score'] for c in crits) * 10.0 / len(crits) if crits else 0.0
        return {
            'criteria': crits, 'overall': round(overall, 1),
            'strengths': [_('Showed: %s', s) for s in strengths[:4]],
            'improvements': [_('Work on: %s', s) for s in improvements[:4]],
            'model_answer': _('"I completely understand. Can I ask what you will mainly use it for? This one is built '
                              'for exactly that, which means you get [benefit] every day. Would you like to try it? '
                              'Many customers also pick up [complementary item] with it."'),
        }

    def _apply_evaluation(self, result):
        self.ensure_one()
        self.criterion_ids.unlink()
        e = escape
        html = ''
        if result.get('strengths'):
            html += '<p><strong>%s</strong></p><ul>%s</ul>' % (
                e(_('Strengths')), ''.join('<li>%s</li>' % e(s) for s in result['strengths']))
        if result.get('improvements'):
            html += '<p><strong>%s</strong></p><ul>%s</ul>' % (
                e(_('Improve next time')), ''.join('<li>%s</li>' % e(s) for s in result['improvements']))
        if result.get('model_answer'):
            html += '<p><strong>%s</strong></p><blockquote>%s</blockquote>' % (
                e(_('Try saying')), e(result['model_answer']))
        score = round(result['overall'], 1)
        self.write({
            'state': 'evaluated',
            'score': score,
            'passed': score >= self.scenario_id.pass_score,
            'feedback_html': Markup(html),
            'criterion_ids': [(0, 0, c) for c in result['criteria']],
        })
        self.learner_id._add_points(int(score // 10), reason=_('Role-play practice'))
        if self.scenario_id.skill_id:
            self.env['zlms.learner.skill']._record(self.learner_id, self.scenario_id.skill_id,
                                                   max(1.0, score / 20.0), 'roleplay', weight=0.4)
        assignments = self.env['zlms.assignment'].sudo().search([
            ('learner_id', '=', self.learner_id.id), ('state', 'in', ('assigned', 'in_progress')),
            ('line_ids.scenario_id', '=', self.scenario_id.id)])
        assignments._sync_progress()


class ZlmsRoleplayMessage(models.Model):
    _name = 'zlms.roleplay.message'
    _description = 'Role-play Message'
    _order = 'id'

    session_id = fields.Many2one('zlms.roleplay.session', required=True, ondelete='cascade', index=True)
    role = fields.Selection([('customer', 'Customer (AI)'), ('associate', 'Associate')], required=True)
    content = fields.Text(required=True)


class ZlmsRoleplayCriterion(models.Model):
    _name = 'zlms.roleplay.criterion'
    _description = 'Role-play Rubric Score'
    _order = 'id'

    session_id = fields.Many2one('zlms.roleplay.session', required=True, ondelete='cascade', index=True)
    name = fields.Char(required=True)
    score = fields.Float(help='0 to 10')
    comment = fields.Text()
