# -*- coding: utf-8 -*-
{
    'name': 'ZivoIT Retail LMS AI',
    'version': '19.0.1.1.1',
    'category': 'Human Resources/eLearning',
    'summary': 'AI-enabled retail LMS for multi-store brands: onboarding journeys, AI role-play sales coach, '
               'AI course builder, certifications, Daily Boost, coaching, skills matrix and store dashboards.',
    'description': """
ZivoIT Retail LMS AI
====================
A retail-first learning layer on top of Odoo eLearning for any multi-store retailer -
fashion, footwear, electronics, beauty, grocery, home, jewellery, pharmacy and more -
running company-owned stores, franchises, shop-in-shops or kiosks.

* Structured, role-based onboarding journeys (30/60/90 day) with auto-assignment
* Product knowledge cards (Feature-Advantage-Benefit) that AI turns into micro-courses and quizzes
* Brand context (name, retail sector, market) so every AI output fits your business
* AI role-play coach for customer service and selling skills, with rubric scoring
* Assessments, quizzes, certifications with validity and renewal tracking
* Daily Boost: adaptive spaced-repetition micro-quizzes (Leitner) for long-term retention
* On-the-job (OJT) checklists signed off by store managers
* Store / region / network dashboards, leaderboards, at-risk learners
* Store KPI capture (conversion, ATV, UPT, CSAT) to correlate training with sales
* Store bulletins (launches, promotions, policy, planograms) with read & acknowledge
* Gamification: badges, Daily Boost streaks, monthly store challenge (points per head)
* Skills matrix with role targets, career ladder and promotion readiness
* Manager coaching observations (floor-walk checklists) with AI coaching plans
* Instructor-led sessions (classroom / virtual / on-the-floor) with attendance and feedback
* AI Learning Path Builder that designs journeys from your content
* Mobile-first learner portal: My Learning, Daily Boost, Ask the Expert, Role-play, News, Sessions
* Pluggable AI: OpenAI, Anthropic Claude, Google Gemini or any OpenAI-compatible endpoint,
  with a built-in offline engine so every feature works without an API key
""",
    'author': 'Zivo IT Technologies',
    'company': 'Shrisampadha Private Limited (Trade Name: Zivo IT Technologies)',
    'maintainer': 'Zivo IT Technologies',
    'website': 'https://www.zivoit.com',
    'support': 'info@zivoit.com',
    'license': 'OPL-1',
    'depends': [
        'mail',
        'portal',
        'hr',
        'product',
        'survey',
        'website_slides',
        'website_slides_survey',
    ],
    'data': [
        'security/zlms_security.xml',
        'security/ir.model.access.csv',
        'data/zlms_sequence_data.xml',
        'data/zlms_master_data.xml',
        'data/zlms_wave2_data.xml',
        'data/zlms_mail_template_data.xml',
        'data/zlms_cron_data.xml',
        'report/zlms_certificate_report.xml',
        'wizards/zlms_ai_course_wizard_views.xml',
        'wizards/zlms_assign_path_wizard_views.xml',
        'wizards/zlms_ask_expert_wizard_views.xml',
        'views/zlms_store_views.xml',
        'views/zlms_learner_views.xml',
        'views/zlms_learning_path_views.xml',
        'views/zlms_assignment_views.xml',
        'views/zlms_certificate_views.xml',
        'views/zlms_product_card_views.xml',
        'views/zlms_roleplay_views.xml',
        'views/zlms_boost_views.xml',
        'views/zlms_ai_views.xml',
        'views/zlms_master_views.xml',
        'views/zlms_wave2_views.xml',
        'views/res_config_settings_views.xml',
        'views/hr_employee_views.xml',
        'views/zlms_portal_templates.xml',
        'views/zlms_menus.xml',
    ],
    'demo': [
        'data/zlms_demo.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'zivoit_retail_lms/static/src/dashboard/*.js',
            'zivoit_retail_lms/static/src/dashboard/*.xml',
            'zivoit_retail_lms/static/src/scss/zlms_backend.scss',
        ],
        'web.assets_frontend': [
            'zivoit_retail_lms/static/src/scss/zlms_portal.scss',
        ],
    },
    'images': ['static/description/banner.png'],
    'price': 659.0,
    'currency': 'USD',
    'installable': True,
    'application': True,
    'auto_install': False,
}
