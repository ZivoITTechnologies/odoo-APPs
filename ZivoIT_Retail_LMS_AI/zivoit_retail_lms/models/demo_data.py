# -*- coding: utf-8 -*-
"""Demo activity generator: makes the dashboard meaningful right after installing demo data."""
import random
from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models

DEMO_REPLIES = [
    "Hello, welcome to our store! How can I help you today?",
    "What kind of use are you looking for, and how often will you use it?",
    "This one has moisture wicking and responsive cushioning, which means it helps you stay comfortable because it is built for daily use.",
    "I understand the price concern, great question. It lasts much longer and has a full warranty, so it is better value.",
    "It also pairs really well with our matching range - would you like to try it? Shall I pack the complete look along with it?",
]

COMPLAINT_REPLIES = [
    "I'm really sorry about this, I understand how frustrating that is.",
    "Let me take care of this for you right away.",
    "No problem about the bill - I can find it with your mobile number and give you an exchange or a new one.",
    "I will register this with my manager and we will call you to update you.",
    "Thank you for your patience, I appreciate you telling us. Anything else I can help with?",
]


class ZlmsDemo(models.AbstractModel):
    _name = 'zlms.demo'
    _description = 'Retail LMS Demo Generator'

    @api.model
    def _build_product_course(self, card_xmlids, topic):
        cards = self.env['zlms.product.card'].browse([self.env.ref(x).id for x in card_xmlids])
        wizard = self.env['zlms.ai.course.wizard'].create({
            'source': 'product_card', 'product_card_ids': [(6, 0, cards.ids)], 'topic': topic,
            'retail_category': 'product', 'question_count': 8, 'use_ai': False,
        })
        content = wizard._generate_offline(8)
        channel = wizard._create_course(content, 'offline')
        for card in cards:
            card.write({'channel_id': channel.id, 'lesson_html': card._offline_lesson_html(), 'state': 'published'})
        self.env['ir.model.data'].create({
            'name': 'demo_channel_product_knowledge', 'module': 'zivoit_retail_lms', 'model': 'slide.channel',
            'res_id': channel.id, 'noupdate': True,
        })
        return channel

    @api.model
    def _populate_activity(self):
        rng = random.Random(42)
        today = fields.Date.context_today(self)
        ctx = dict(self.env.context, zlms_no_mail=True, mail_notrack=True)
        Learner = self.env['zlms.learner'].with_context(ctx)
        learners = Learner.search([])
        # backdate assignments so that some become overdue
        for learner in learners:
            for assignment in learner.assignment_ids:
                offset = rng.choice([5, 12, 20, 35, 45])
                assignment.write({'assign_date': today - timedelta(days=offset),
                                  'due_date': today - timedelta(days=offset) + timedelta(days=assignment.path_id.duration_days)})
        SlidePartner = self.env['slide.slide.partner'].sudo()
        skill = {l.id: rng.random() for l in learners}
        for learner in learners:
            level = skill[learner.id]
            for assignment in learner.assignment_ids:
                for line in assignment.line_ids:
                    if line.line_type == 'course' and line.channel_id:
                        slides = line.channel_id.slide_ids.filtered(lambda s: not s.is_category and s.is_published)
                        n_done = len(slides) if level > 0.35 else int(len(slides) * level * 2)
                        for slide in slides[:n_done]:
                            if not SlidePartner.search_count([('slide_id', '=', slide.id), ('partner_id', '=', learner.partner_id.id)]):
                                SlidePartner.create({
                                    'slide_id': slide.id, 'partner_id': learner.partner_id.id, 'completed': True,
                                    'quiz_attempts_count': (1 if level > 0.7 else rng.choice([1, 2, 3])) if slide.question_ids else 0,
                                })
                    elif line.line_type == 'ojt' and level > 0.4:
                        signer = assignment.store_id.manager_user_id or self.env.user
                        line.with_user(signer).with_context(ctx).write({'manager_rating': rng.choice(['3', '4', '5'])})
                        line.with_user(signer).with_context(ctx).action_sign_off()
                    elif line.line_type == 'roleplay' and line.scenario_id and level > 0.3:
                        session = self.env['zlms.roleplay.session'].with_context(ctx).create(
                            {'scenario_id': line.scenario_id.id, 'learner_id': learner.id})
                        session.action_start()
                        n_replies = 2 + int(level * 3)
                        replies = COMPLAINT_REPLIES if line.scenario_id.skill_area == 'complaint' else DEMO_REPLIES
                        for text in replies[:n_replies]:
                            if session.state != 'ongoing':
                                break
                            session.send_message(text)
                        if session.state == 'ongoing':
                            session.action_evaluate()
            assignments = learner.assignment_ids
            assignments._sync_progress()
            for a in assignments.filtered(lambda a: a.state == 'completed'):
                a.completion_date = today - timedelta(days=rng.randint(0, 150))
                if a.certificate_id:
                    a.certificate_id.issue_date = a.completion_date
        # Daily Boost history
        Card = self.env['zlms.boost.card'].with_context(ctx)
        Card._seed_for_learners(learners)
        for learner in learners:
            for card in Card._get_due_cards(learner, limit=rng.randint(2, 6)):
                right = card.question_id.answer_ids.filtered('is_correct')[:1]
                wrong = card.question_id.answer_ids.filtered(lambda a: not a.is_correct)[:1]
                pick = right if rng.random() < 0.45 + skill[learner.id] / 2 else (wrong or right)
                card.answer(pick.id, confident=rng.random() > 0.3)
        # Store KPIs: 6 months, conversion correlated with training completion
        Kpi = self.env['zlms.store.kpi']
        for store in self.env['zlms.store'].search([]):
            base_footfall = rng.randint(2200, 5200)
            completion = store.avg_completion
            for i in range(6, 0, -1):
                period = (today - relativedelta(months=i)).replace(day=1)
                if Kpi.search_count([('store_id', '=', store.id), ('period_date', '=', period)]):
                    continue
                train = max(10.0, min(100.0, completion - (i * rng.uniform(2, 6))))
                conv = 0.18 + train / 100.0 * 0.22 + rng.uniform(-0.03, 0.03)
                footfall = int(base_footfall * rng.uniform(0.85, 1.2))
                bills = int(footfall * conv)
                units = int(bills * rng.uniform(1.4, 2.3))
                Kpi.create({
                    'store_id': store.id, 'period_date': period, 'footfall': footfall, 'bills': bills,
                    'units_sold': units, 'net_sales': round(units * rng.uniform(650, 900), 0),
                    'csat_score': round(70 + train * 0.25 + rng.uniform(-5, 5), 1),
                    'mystery_shopper_score': round(60 + train * 0.3 + rng.uniform(-6, 6), 1),
                    'training_completion': round(train, 1),
                })
        return True

    @api.model
    def _populate_wave2(self):
        rng = random.Random(7)
        ctx = dict(self.env.context, zlms_no_mail=True, mail_notrack=True)
        learners = self.env['zlms.learner'].with_context(ctx).search([])
        # bulletins: publish, then most learners read / acknowledge
        for xmlid in ('bulletin_launch', 'bulletin_returns', 'bulletin_recognition'):
            bulletin = self.env.ref('zivoit_retail_lms.%s' % xmlid).with_context(ctx)
            bulletin.action_publish()
            for ack in bulletin.ack_ids:
                roll = rng.random()
                if roll < 0.65:
                    ack.action_acknowledge() if bulletin.require_ack else ack.action_mark_read()
                elif roll < 0.8:
                    ack.action_mark_read()
        # classroom: past session attended, upcoming sessions with registrations
        past = self.env.ref('zivoit_retail_lms.session_service_past').with_context(ctx)
        south = learners.filtered(lambda l: l.region_id == past.region_id)[:8]
        past.register(south)
        for att in past.attendee_ids:
            if rng.random() < 0.8:
                att.write({'state': 'attended', 'feedback_rating': rng.choice(['4', '4', '5', '5', '3']),
                           'feedback_comment': rng.choice(['Great role-plays', 'Very practical', 'Loved the LEARN drill', ''])})
        past.action_done()
        self.env.ref('zivoit_retail_lms.session_vm_upcoming').with_context(ctx).action_invite_due_learners()
        self.env.ref('zivoit_retail_lms.session_service_virtual').with_context(ctx).register(
            learners.filtered(lambda l: l.user_id)[:1] | learners[3:7])
        # coaching observations by store managers
        template = self.env.ref('zivoit_retail_lms.obs_template_floor')
        for learner in learners.filtered(lambda l: l.role_id.code in ('SA', 'CSH', 'ASM')):
            for _i in range(rng.choice([1, 1, 2])):
                observer = learner.store_id.manager_user_id or self.env.user
                obs = self.env['zlms.observation'].with_user(observer).with_context(ctx).create({
                    'learner_id': learner.id, 'template_id': template.id,
                    'observation_date': fields.Date.context_today(self) - timedelta(days=rng.randint(1, 40))})
                base = rng.choice([2, 3, 3, 4])
                for line in obs.line_ids:
                    line.rating = str(max(1, min(5, base + rng.choice([-1, 0, 0, 1, 1]))))
                obs.strengths = 'Friendly and energetic on the floor.'
                obs.next_steps = 'Practise discovery questions before every demo.'
                obs.action_done()
        # seed tenure so some associates can be promotion-ready
        star = self.env.ref('zivoit_retail_lms.learner_06', raise_if_not_found=False)
        if star:
            star.with_context(zlms_no_auto_assign=True).joining_date = fields.Date.context_today(self) - timedelta(days=400)
            for skill in self.env['zlms.skill'].search([]):
                self.env['zlms.learner.skill']._record(star, skill, 4.5, 'manual', weight=1.0)
            star.assignment_ids.filtered(lambda a: a.state != 'completed')._sync_progress()
        learners._check_badges()
        return True
