# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
{
    'name': 'Event Connect AI: Headless Events API, WordPress, Private Events and Equipment Booking',
    'version': '19.0.2.0.0',
    'summary': 'AI-powered Odoo Events: AI event writer, natural-language search, booking approval assistant, '
               'survey insights, document assistant and security alerts (OpenAI, Claude, Gemini or Ollama). '
               'Headless REST API with signed webhooks for WordPress, IP-restricted and invitation-only events, '
               'department tagging, equipment booking by time slot with approvals, versioned event folders, '
               'audit log and survey save and continue later.',
    'description': """
ZivoIT Event Connect AI
=======================
* AI assistants (use your own OpenAI, Claude, Gemini or OpenAI-compatible key, including Ollama):
  event content writer, natural-language website search, booking approval assistant,
  survey insights, document assistant (summary, key dates, version comparison) and security alerts
* Headless Events REST API: API keys, CORS, filters, pagination, iCal feed and registrations
* Signed webhooks (HMAC-SHA256) with a queue, retries and a delivery log, to refresh WordPress
  or any website as soon as an event changes
* Event access rules: Open, IP-restricted (CIDR ranges) or Invitation Only (personal invitation links)
* Department tagging for events, with website, API and backend filters
* Equipment booking: time-slot selection, quantity-aware availability, no pricing, approval workflow
* Event document folders with version history
* Audit log with old and new values, user and IP address
* Survey "Save and continue later" with a resume link and resume code, including anonymous respondents
* Ready-made WordPress plugin (shortcodes and webhook receiver) included in the module
""",
    'category': 'Marketing/Events',
    'author': 'ZivoIT Technologies',
    'maintainer': 'ZivoIT Technologies',
    'company': 'ZivoIT Technologies',
    'website': 'https://www.zivoit.com',
    'support': 'info@zivoit.com',
    'license': 'OPL-1',
    'price': 249.00,
    'currency': 'USD',
    'depends': ['event', 'website_event', 'survey', 'mail', 'portal'],
    'data': [
        'security/zivo_event_security.xml',
        'security/ir.model.access.csv',
        'data/ir_sequence_data.xml',
        'data/ir_cron_data.xml',
        'data/mail_template_data.xml',
        'data/ai_data.xml',
        'views/zivo_event_department_views.xml',
        'views/event_event_views.xml',
        'views/zivo_event_invitation_views.xml',
        'views/zivo_api_client_views.xml',
        'views/zivo_webhook_views.xml',
        'views/zivo_equipment_views.xml',
        'views/zivo_equipment_booking_views.xml',
        'views/zivo_event_document_views.xml',
        'views/zivo_audit_log_views.xml',
        'views/survey_survey_views.xml',
        'views/res_config_settings_views.xml',
        'wizards/zivo_booking_reject_wizard_views.xml',
        'wizards/zivo_event_invite_wizard_views.xml',
        'wizards/zivo_document_version_wizard_views.xml',
        'views/menus.xml',
        'views/zivo_ai_views.xml',
        'views/website_templates.xml',
        'views/portal_booking_templates.xml',
        'views/survey_templates.xml',
    ],
    'demo': [
        'data/demo_data.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'zivoit_event_connect/static/src/scss/zivo_frontend.scss',
            'zivoit_event_connect/static/src/js/zivo_booking_slots.js',
        ],
        'survey.survey_assets': [
            'zivoit_event_connect/static/src/js/zivo_survey_save_later.js',
        ],
    },
    'images': ['static/description/banner.png'],
    'installable': True,
    'application': True,
    'auto_install': False,
}
