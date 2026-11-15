# -*- coding: utf-8 -*-
{
    'name': 'ZivoIT Fuel Management System',
    'version': '19.0.2.0.0',
    'category': 'Industries',
    'summary': 'ZivoIT Fuel Management System - AI-enabled petrol pump ERP: nozzle shifts, credit billing with payment status and UPI QR, '
               'tanks and DIP, own tanker fleet, trips, doorstep delivery, maintenance, accounts, payroll '
               'and a customer self-service portal with an AI assistant.',
    'description': """
ZivoIT Fuel Management System (AI enabled)
==========================================
A complete fuel station platform for oil-company retail outlets (BPCL, IOCL, HPCL and private dealers):

* Admin dashboard with AI insights, Ask AI box, fleet status and stock-out forecast
* Shift sheets from nozzle meter readings with card, UPI, fleet-card and cash reconciliation
* Credit customers, vehicles, credit limits, filling slips, indents and period bills
* Clear credit bills: amount payable, payment status (paid / partial / overdue), vehicle-wise summary, UPI QR
* Tanks, DIP chart, DIP variance, tanker receipts and stock ledger
* Own fleet (Odoo Fleet): tankers with compartments, delivery bowsers, depot and doorstep-delivery trips,
  trip expenses, fleet fuel logs with km/L, preventive maintenance, job cards, document expiry alerts
* Double-entry ledger: receipts, payments, journals, contra, card/UPI settlement, day book, trial balance
* Staff roles, attendance and monthly salary sheet
* AI engine: stock-out forecast, suspicious slip detection, credit-risk grade and suggested limit,
  staff and customer assistant (built-in, works offline; optional Anthropic Claude or OpenAI)
* Customer portal: dashboard, ledger, month view, statements, bills, slips, vehicles, indents, rates, AI chat,
  PDF and Excel downloads
""",
    'author': 'ZivoIT Technologies',
    'website': 'https://www.zivoit.com',
    'license': 'OPL-1',
    'depends': ['base', 'mail', 'portal', 'web', 'hr', 'fleet'],
    'external_dependencies': {'python': ['xlsxwriter']},
    'data': [
        'security/pump_security.xml',
        'security/ir.model.access.csv',
        'data/pump_sequence_data.xml',
        'data/pump_master_data.xml',
        'report/report_layout.xml',
        'report/report_statement.xml',
        'report/report_bill.xml',
        'report/report_slip.xml',
        'report/report_shift.xml',
        'report/report_trial_balance.xml',
        'report/report_actions.xml',
        'wizard/pump_wizard_views.xml',
        'views/pump_product_views.xml',
        'views/pump_tank_views.xml',
        'views/pump_partner_views.xml',
        'views/pump_sale_views.xml',
        'views/pump_bill_views.xml',
        'views/pump_shift_views.xml',
        'views/pump_account_views.xml',
        'views/pump_purchase_views.xml',
        'views/pump_employee_views.xml',
        'views/pump_fleet_views.xml',
        'views/pump_company_views.xml',
        'views/pump_dashboard_views.xml',
        'views/pump_menus.xml',
        'views/portal_templates.xml',
        'views/login_templates.xml',
    ],
    'demo': [
        'demo/pump_demo.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'zivoit_fuel_management/static/src/dashboard/**/*',
            'zivoit_fuel_management/static/src/scss/backend.scss',
        ],
        'web.assets_frontend': [
            'zivoit_fuel_management/static/src/scss/portal.scss',
            'zivoit_fuel_management/static/src/scss/login.scss',
            'zivoit_fuel_management/static/src/portal/ai_chat.js',
        ],
        'web.report_assets_common': [
            'zivoit_fuel_management/static/src/scss/report.scss',
        ],
    },
    'images': ['static/description/banner.png'],
    'price': 1599.0,
    'currency': 'USD',
    'support': 'info@zivoit.com',
    'maintainer': 'ZivoIT Technologies',
    'application': True,
    'installable': True,
}
