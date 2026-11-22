# -*- coding: utf-8 -*-
{
    'name': 'ZivoIT Tender AI - Government Tender Finder & Bid Management (All Industries)',
    'version': '19.0.1.0.0',
    'category': 'Sales/Sales',
    'summary': 'AI agent that finds hot, new & open government tenders in every industry - '
               'construction, IT / ERP / software, manpower, telecom, security, facility management, '
               'healthcare, supplies and consultancy - from CPPP, GeM, IREPS and all state e-procurement '
               'portals, with filtering, alerts, shortlisting, bid/no-bid, pricing, EMD, results and handover.',
    'description': """
ZivoIT Tender AI
================
End-to-end tender intelligence and bid management for companies in every industry:
construction, IT / ERP / software, telecom, manpower & staffing, security, facility management,
healthcare, supply of goods, transport, training, media and consultancy.

* Tender discovery from configurable sources: CPPP, GeM, IREPS (Indian Railways), all state
  e-procurement portals (Kerala, Himachal Pradesh, Arunachal Pradesh, Telangana ...), NHAI / MoRTH,
  RSS, HTML tables, JSON APIs, tender-alert e-mails, Excel/CSV import and an AI web-search agent.
* Natural-language "Tender Agents": e.g. "ERP tenders in Kerala", "custom software development
  projects in Himachal Pradesh", "manpower supply for telecom in Arunachal Pradesh" or
  "open tenders in railways construction in SCR".
* Indian amount parsing (Lakh / Crore), duplicate detection, auto-classification
  into 21 sectors across 5 industry groups, all 36 states & UTs, railway zones and contract types.
* Segregation by value band, deadline bucket, completion period and EMD.
* Relevance scoring, AI summaries and Go / No-Go recommendations.
* Notifications: instant match alerts, daily digest e-mails, deadline reminders.
* Bid pipeline: review, shortlisting, bid preparation, submission, evaluation, result.
* Bid/No-Bid checklists for civil works, railways, IT / software and manpower services,
  BOQ / price-schedule costing with overheads & margin,
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
