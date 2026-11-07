{
    "name": "ZivoIT EPC AI - EPC Project Controls with AI",
    "version": "19.0.2.0.2",
    "category": "Services/Project",
    "summary": "AI-enabled EPC (Engineering, Procurement & Construction) project controls: "
               "CPM on working calendars, EVM, delay prediction & Monte Carlo, schedule optimisation, "
               "AI MTO from drawings & BIM, change orders, bid evaluation, ERP material tracking, "
               "DPR, HSE & quality, RA billing, P6 XER and a REST API for SAP/Oracle overlay.",
    "description": """
ZivoIT EPC AI
=============
An Odoo 19 vertical for EPC contractors (power, T&D, renewables, infrastructure,
industrial). It adds the EPC operating model on top of Project, Purchase,
Inventory and Accounting, and an AI layer:

* Bill of Quantities, WBS activities with FS/SS/FF/SF logic and a CPM engine
* Earned value (PV, EV, AC, CPI, SPI, EAC) at activity and project level
* Predictive engine: activity and project delay forecast, delay probability,
  cost-at-completion P50/P80, explained by drivers, learning from completed history
* Early-warning risks and activities raised automatically (daily cron)
* Daily Progress Reports with an AI assistant that turns site notes into DPR lines
* Master Document Register, revisions and transmittals
* Procurement expediting with forecast dates and supplier risk scores
* Running-account (RA) billing from measured quantities, with retention, to invoices
* Contract risk review: clause extraction and risk scoring
* Project AI assistant (Q&A) and AI progress report narrative
* Primavera P6 XER import and export
* Working calendars, Monte Carlo risk simulation and least-cost schedule crashing
* Design optimisation: AI material take-off from drawings, IFC (BIM) import,
  design-vs-BOQ variance and change orders with AI cost/time impact
* Supply chain: technical & commercial bid evaluation with AI offer reading,
  MTO to RFQ, external (SAP/Oracle) material tracking fed into forecasts
* Site operations: HSE permits, incidents, observations, inspections, NCRs,
  punch lists, plant utilisation and AI photo review of site images
* Enterprise integration: API-key secured REST API, CSV import templates,
  portfolio pivot and graph across companies

The AI layer works with OpenAI, Anthropic Claude or any OpenAI-compatible
endpoint (including self-hosted models). Without a provider configured, every
AI feature falls back to a built-in deterministic engine, so the module is
fully usable offline. All AI output is created as a draft for human approval
and every call is logged.
    """,
    "author": "Zivo IT Technologies",
    "website": "https://www.zivoit.com",
    "license": "OPL-1",
    "price": 599.0,
    "currency": "USD",
    "maintainer": "Zivo IT Technologies",
    "images": ["static/description/banner.png"],
    "depends": ["project", "purchase_stock", "account", "mail"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/epc_security.xml",
        "security/ir.model.access.csv",
        "security/epc_rules.xml",
        "data/ir_sequence_data.xml",
        "data/ir_cron_data.xml",
        "wizard/epc_p6_import_views.xml",
        "wizard/epc_ai_assistant_views.xml",
        "wizard/epc_schedule_optimizer_views.xml",
        "views/epc_project_views.xml",
        "views/epc_boq_views.xml",
        "views/epc_activity_views.xml",
        "views/epc_dpr_views.xml",
        "views/epc_risk_views.xml",
        "views/epc_site_views.xml",
        "views/epc_document_views.xml",
        "views/epc_ra_bill_views.xml",
        "views/epc_contract_review_views.xml",
        "views/epc_design_views.xml",
        "views/epc_supply_views.xml",
        "views/epc_integration_views.xml",
        "views/purchase_views.xml",
        "views/res_partner_views.xml",
        "views/epc_ai_log_views.xml",
        "views/epc_prediction_model_views.xml",
        "views/res_config_settings_views.xml",
        "report/epc_reports.xml",
        "report/epc_report_templates.xml",
        "views/epc_menus.xml",
    ],
    "demo": [
        "demo/epc_demo.xml",
    ],
    "application": True,
    "installable": True,
}
