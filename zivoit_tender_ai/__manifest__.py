# -*- coding: utf-8 -*-
{
    'name': 'ZivoIT Tender AI - Construction Tender Finder & Bid Management',
    'version': '19.0.1.0.0',
    'category': 'Sales/Construction',
    'summary': 'AI agent that finds hot, new & open construction tenders (Roads, Buildings, '
               'Railways, Bridges) from CPPP, IREPS, GeM and state e-procurement portals, '
               'with filtering, alerts, shortlisting, bid/no-bid, BOQ costing, EMD, '
               'results and project handover.',
    'description': """
ZivoIT Tender AI
================
End-to-end tender intelligence and bid management for construction & real estate companies.

* Tender discovery from configurable sources: CPPP, IREPS (Indian Railways), GeM,
  Telangana / AP / state e-procurement, NHAI / MoRTH, RSS, HTML tables, JSON APIs,
  tender-alert e-mails, Excel/CSV import and an AI web-search agent.
* Natural-language "Tender Agents": e.g. "open tenders in railways construction in SCR"
  or "road construction tenders in Telangana above 5 crore".
* Indian amount parsing (Lakh / Crore), duplicate detection, auto-classification
  by sector, state, railway zone and work type.
* Segregation by value band, deadline bucket, completion period and EMD.
* Relevance scoring, AI summaries and Go / No-Go recommendations.
* Notifications: instant match alerts, daily digest e-mails, deadline reminders.
* Bid pipeline: review, shortlisting, bid preparation, submission, evaluation, result.
* Bid/No-Bid qualification checklist, BOQ costing with overheads & margin,
  EMD / tender fee tracking, corrigenda, competitor & L1/L2 analysis.
* Handover of won tenders to CRM and Projects with BOQ-based tasks and
  Running Account (RA) bills for construction execution.
""",
    'author': 'Zivo IT Technologies (Shrisampadha Private Limited)',
    'website': 'https://www.zivoit.com',
    'support': 'info@zivoit.com',
    'license': 'OPL-1',
    'depends': ['base', 'mail', 'web', 'crm', 'project'],
    'external_dependencies': {'python': ['requests', 'lxml']},
    'data': [
        'security/tender_security.xml',
        'security/ir.model.access.csv',
        'data/tender_sequence_data.xml',
        'data/tender_stage_data.xml',
        'data/tender_sector_data.xml',
        'data/tender_source_data.xml',
        'data/tender_checklist_data.xml',
        'data/mail_template_data.xml',
        'data/ir_cron_data.xml',
        'wizard/tender_agent_search_views.xml',
        'wizard/tender_result_wizard_views.xml',
        'views/tender_views.xml',
        'views/tender_profile_views.xml',
        'views/tender_source_views.xml',
        'views/tender_config_views.xml',
        'views/tender_ra_bill_views.xml',
        'views/project_views.xml',
        'views/res_config_settings_views.xml',
        'views/tender_dashboard_action.xml',
        'views/tender_menus.xml',
        'report/tender_report.xml',
    ],
    'demo': [
        'data/tender_demo.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'zivoit_tender_ai/static/src/dashboard/*.js',
            'zivoit_tender_ai/static/src/dashboard/*.xml',
            'zivoit_tender_ai/static/src/scss/*.scss',
        ],
        'web.assets_tests': [
            'zivoit_tender_ai/static/tests/tours/*.js',
        ],
    },
    'images': ['static/description/banner.png'],
    'application': True,
    'installable': True,
    'price': 359.0,
    'currency': 'USD',
}
