# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
{
    'name': 'ZivoIT Event Connect AI - Headless Event API, Private Events, Equipment Booking & Survey Resume',
    'version': '19.0.2.0.0',
    'summary': 'AI-enabled (OpenAI, Claude, Gemini, Ollama): event writer, NL search, booking approval assistant, '
               'survey insights, document assistant, anomaly alerts. Headless REST API & signed webhooks for WordPress, IP-range and '
               'invitation-only events, department tagging, equipment time-slot booking with '
               'approvals, versioned event documents, audit trail and survey save & continue later',
    'description': """
ZivoIT Event Connect
====================
* Headless Events REST API (API keys, CORS, filters, pagination, iCal feed, registrations)
* Signed outgoing webhooks (HMAC-SHA256) with queue, retries and delivery log - refresh WordPress
  or any website the moment an event changes
* Event access rules: Public, IP range (CIDR) restricted, Invitation only (tokenised invite links)
* Department tagging for events (website, API and backend filters)
* Equipment booking: time-slot selection, quantity aware availability, no pricing, approval flow
* Event document folders with version history
* Audit log with old/new values, user and IP for events, bookings, documents and invitations
* Survey "Save & continue later" with resume link + resume code, works for anonymous respondents
* Ready-made WordPress plugin (shortcode + webhook receiver) shipped inside the module
* AI (bring your own key - OpenAI, Claude, Gemini or OpenAI-compatible/Ollama): event content writer,
  natural-language website search, booking approval assistant, survey insights, document assistant
  (summary, key dates, version diff) and audit anomaly alerts
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
