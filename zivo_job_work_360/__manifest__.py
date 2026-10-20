# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps).
{
    'name': 'Job Work 360 - Subcontracting Control Tower',
    'version': '19.0.1.0.0',
    'summary': 'Job work orders, subcontractor stock, scrap, yield, quality, costing, '
               'reconciliation, vendor portal and vendor scorecards for outsourced manufacturing',
    'description': """
Job Work 360 for Odoo 19
========================
Complete Job Work & Subcontracting management for manufacturing companies.

* Job Work Orders with configurable job types and multi-stage chains
* Material issue to job workers and subcontractor stock (custody) views
* Consumption, scrap, unprocessed returns and approved loss postings
* Material reconciliation engine with automatic exceptions
* Job-level quality inspection, NCR, acceptance / rejection / rework
* Expected vs actual costing (material, processing, scrap, freight, inspection)
* Purchase order for processing charges and vendor bill variance control
* Vendor portal: accept jobs, confirm receipt, report progress, request material, upload documents
* Vendor scorecards with configurable weights and an executive dashboard
""",
    'category': 'Manufacturing/Manufacturing',
    'author': 'ZivoIT Technologies',
    'maintainer': 'ZivoIT Technologies',
    'support': 'info@zivoit.com',
    'license': 'OPL-1',
    'price': 199.0,
    'currency': 'USD',
    'depends': [
        'mrp_subcontracting',
        'purchase_stock',
        'portal',
    ],
    'data': [
        'security/job_work_security.xml',
        'security/ir.model.access.csv',
        'security/job_work_rules.xml',
        'data/ir_sequence_data.xml',
        'report/job_work_report.xml',
        'report/job_work_report_templates.xml',
        'data/job_work_data.xml',
        'data/ir_cron_data.xml',
        'wizard/job_work_receive_wizard_views.xml',
        'wizard/job_work_return_wizard_views.xml',
        'wizard/job_work_loss_wizard_views.xml',
        'views/job_work_type_views.xml',
        'views/job_work_views.xml',
        'views/job_work_material_views.xml',
        'views/job_work_qc_views.xml',
        'views/job_work_ncr_views.xml',
        'views/job_work_exception_views.xml',
        'views/job_work_request_views.xml',
        'views/job_work_vendor_score_views.xml',
        'views/res_config_settings_views.xml',
        'views/res_partner_views.xml',
        'views/stock_views.xml',
        'views/purchase_views.xml',
        'views/job_work_portal_templates.xml',
        'views/job_work_menus.xml',
    ],
    'demo': [
        'demo/job_work_demo.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'zivo_job_work_360/static/src/dashboard/**/*',
        ],
        'web.assets_tests': [
            'zivo_job_work_360/static/tests/tours/**/*',
        ],
    },
    'images': ['static/description/banner.png'],
    'installable': True,
    'application': True,
}
