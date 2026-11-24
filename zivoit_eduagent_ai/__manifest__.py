# -*- coding: utf-8 -*-
{
    "name": "ZivoIT EduAgent AI - Agentic E-Learning Suite",
    "version": "19.0.1.0.0",
    "summary": "End-to-end AI agents for e-learning: admissions, fees & EMI, AI course builder, "
               "SCORM, tutor, practice, project mentor, exam integrity, verifiable certificates, "
               "dropout prediction, support, career and management insights - on Odoo eLearning.",
    "description": """
ZivoIT EduAgent AI
==================
A learner-lifecycle multi-agent system for Odoo 19 (Community & Enterprise).

* AI core: bring-your-own-key LLM gateway (OpenAI, Anthropic Claude, Google Gemini,
  any OpenAI-compatible endpoint incl. Ollama / Azure), model routing, course RAG store,
  PII masking, guardrails, audit log and per-company token budgets.
* 11 agents: Admissions, Fee & EMI, Course Builder, Course Tutor, Practice & Quiz,
  Project Mentor, Integrity, Retention Coach, Support, Career, Insights.
* Learner AI hub on the portal, public admissions chat, SCORM 1.2 player,
  verifiable credentials, REST API for external agents / MCP connectors.
* Human-in-the-loop for every grading / high-risk decision (EU AI Act ready).
    """,
    "author": "Zivo IT Technologies (Shrisampadha Private Limited)",
    "maintainer": "Zivo IT Technologies",
    "website": "https://www.zivoit.com",
    "support": "info@zivoit.com",
    "category": "Website/eLearning",
    "license": "OPL-1",
    "price": 999.0,
    "currency": "USD",
    "depends": [
        "base",
        "mail",
        "portal",
        "website",
        "website_slides",
        "website_slides_survey",
        "crm",
        "sale_management",
        "account",
    ],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/eduagent_security.xml",
        "security/ir.model.access.csv",
        "data/eduagent_agent_data.xml",
        "data/eduagent_data.xml",
        "data/eduagent_cron.xml",
        "views/eduagent_provider_views.xml",
        "views/eduagent_agent_views.xml",
        "views/eduagent_conversation_views.xml",
        "views/eduagent_knowledge_views.xml",
        "views/eduagent_usage_views.xml",
        "views/eduagent_retention_views.xml",
        "views/eduagent_emi_views.xml",
        "views/eduagent_assessment_views.xml",
        "views/eduagent_credential_views.xml",
        "views/eduagent_support_views.xml",
        "views/eduagent_career_views.xml",
        "views/eduagent_scorm_views.xml",
        "views/eduagent_api_views.xml",
        "views/slide_channel_views.xml",
        "views/crm_lead_views.xml",
        "views/sale_order_views.xml",
        "views/res_config_settings_views.xml",
        "wizard/course_builder_wizard_views.xml",
        "wizard/insights_wizard_views.xml",
        "wizard/scorm_import_wizard_views.xml",
        "views/eduagent_menus.xml",
        "views/website_templates.xml",
    ],
    "assets": {
        "web.assets_frontend": [
            "zivoit_eduagent_ai/static/src/css/eduagent_chat.css",
            "zivoit_eduagent_ai/static/src/js/eduagent_chat.js",
        ],
    },
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": True,
    "auto_install": False,
}
