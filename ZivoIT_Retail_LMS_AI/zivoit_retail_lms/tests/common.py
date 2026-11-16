# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase, new_test_user


class ZlmsCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, zlms_no_mail=True, mail_notrack=True, tracking_disable=True))
        cls.lnd_user = new_test_user(cls.env, login='zlms_lnd', groups='base.group_user,zivoit_retail_lms.group_zlms_manager')
        cls.sm_user = new_test_user(cls.env, login='zlms_sm', groups='base.group_user,zivoit_retail_lms.group_zlms_store_manager')
        cls.sm_other = new_test_user(cls.env, login='zlms_sm2', groups='base.group_user,zivoit_retail_lms.group_zlms_store_manager')
        cls.portal_user = new_test_user(cls.env, login='zlms_portal', groups='base.group_portal')

        cls.region = cls.env['zlms.region'].create({'name': 'Test South', 'code': 'TS'})
        cls.store = cls.env['zlms.store'].create({'name': 'Test Store A', 'code': 'TST-A', 'region_id': cls.region.id,
                                                  'manager_user_id': cls.sm_user.id})
        cls.store_b = cls.env['zlms.store'].create({'name': 'Test Store B', 'code': 'TST-B', 'region_id': cls.region.id,
                                                    'manager_user_id': cls.sm_other.id})
        cls.role = cls.env['zlms.role'].create({'name': 'Test Associate', 'code': 'TSA'})

        cls.tag_dry = cls.env['zlms.tech.tag'].create({'name': 'Test DryTech', 'description': 'Wicks sweat away'})
        cls.tag_flex = cls.env['zlms.tech.tag'].create({'name': 'Test FlexTech', 'description': 'Stretches'})
        cls.card = cls.env['zlms.product.card'].create({
            'name': 'Test Training Tee', 'activity': 'Gym workouts', 'fabric': 'Poly mesh', 'state': 'published',
            'tech_tag_ids': [(6, 0, cls.tag_dry.ids)],
            'features': 'Quick dry mesh\nFlat seams', 'advantages': 'Sweat evaporates\nNo chafing',
            'benefits': 'You stay dry\nComfortable lifts',
            'objections': 'Too expensive? :: Lasts longer, better value per wear.',
            'keywords': 'tshirt gym',
        })
        cls.card2 = cls.env['zlms.product.card'].create({
            'name': 'Test Joggers', 'activity': 'Travel', 'fabric': 'Cotton spandex', 'state': 'published',
            'tech_tag_ids': [(6, 0, cls.tag_flex.ids)], 'benefits': 'Freedom of movement', 'features': 'Stretch fabric',
        })
        cls.card.cross_sell_ids = cls.card2

        cls.channel = cls.env['slide.channel'].create({'name': 'Test Course', 'enroll': 'invite', 'visibility': 'members',
                                                       'is_published': True})
        cls.slide_article = cls.env['slide.slide'].create({
            'name': 'Lesson', 'channel_id': cls.channel.id, 'slide_category': 'article',
            'html_content': '<p>Hello</p>', 'is_published': True})
        cls.slide_quiz = cls.env['slide.slide'].create({
            'name': 'Quiz', 'channel_id': cls.channel.id, 'slide_category': 'quiz', 'is_published': True,
            'question_ids': [(0, 0, {'question': 'Greet within?', 'answer_ids': [
                (0, 0, {'text_value': '10 seconds', 'is_correct': True}),
                (0, 0, {'text_value': '5 minutes', 'is_correct': False})]})]})
        cls.scenario = cls.env['zlms.roleplay.scenario'].create({
            'name': 'Test discovery', 'skill_area': 'discovery', 'customer_persona': 'Gym beginner',
            'opening_line': 'Just looking', 'customer_script': 'I go to the gym\nI sweat a lot', 'max_turns': 4,
            'product_card_ids': [(6, 0, cls.card.ids)],
        })
        cls.path = cls.env['zlms.learning.path'].create({
            'name': 'Test Onboarding', 'code': 'T-ONB', 'path_type': 'onboarding', 'auto_assign': True,
            'role_ids': [(6, 0, cls.role.ids)], 'duration_days': 30, 'pass_score': 60,
            'line_ids': [
                (0, 0, {'name': 'Course', 'line_type': 'course', 'channel_id': cls.channel.id, 'sequence': 1}),
                (0, 0, {'name': 'Practice', 'line_type': 'roleplay', 'scenario_id': cls.scenario.id, 'sequence': 2,
                        'min_score': 50}),
                (0, 0, {'name': 'Greets customers', 'line_type': 'ojt', 'sequence': 3}),
            ],
        })
        cls.path.action_publish()

    @classmethod
    def _new_learner(cls, name='Test Learner', store=None, **kw):
        vals = {'name': name, 'store_id': (store or cls.store).id, 'role_id': cls.role.id,
                'email': '%s@example.com' % name.replace(' ', '.').lower()}
        vals.update(kw)
        return cls.env['zlms.learner'].create(vals)

    def _complete_course(self, learner, attempts=1):
        SlidePartner = self.env['slide.slide.partner'].sudo()
        for slide in self.channel.slide_ids:
            SlidePartner.create({'slide_id': slide.id, 'partner_id': learner.partner_id.id, 'completed': True,
                                 'quiz_attempts_count': attempts if slide.question_ids else 0})
