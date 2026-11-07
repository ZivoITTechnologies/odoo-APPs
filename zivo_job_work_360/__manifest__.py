# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps).
{
    'name': 'Job Work 360 - Outsourced Manufacturing & Job Work Control Tower',
    'version': '19.0.1.2.0',
    'summary': 'Material custody, vendor execution, production reporting, scrap/yield, quality, costing, '
               'reconciliation and compliance for outsourced manufacturing (subcontracting / job work)',
    'description': """
Job Work 360 for Odoo 19
========================
Complete Outsourced Manufacturing & Job Work Control Tower.

Manufacturing control
* Job Work Orders with configurable job types and multi-stage chains with end-to-end traceability
* Material issue, subcontractor stock, FIFO aging and value held at each job worker
* Customer-owned material tracked with stock owners and kept out of your valuation
* Consumption, scrap, by-products, unprocessed returns and approved losses
* Material reconciliation engine: exceptions with reason codes and approval workflow
* Job-level quality inspection, NCR, acceptance / rejection / rework

Vendor collaboration
* Vendor portal: accept jobs, confirm receipt, report production, request material, upload documents
* Optional labour hours per operation with manager approval and labour cost per unit

Financial & compliance control
* Expected vs actual costing, purchase order for processing charges and vendor bill variance control
* Vendor scorecards and an executive dashboard
* India compliance (switch on in Settings): job work challan with QR code, E-Way Bill JSON data,
  ITC-04 preparation and XLSX export, statutory return-period alerts, Compliance Control Center.
  Prepared data only: it does not file returns or call government APIs.
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
        'data/job_work_reason_data.xml',
        'wizard/job_work_receive_wizard_views.xml',
        'wizard/job_work_return_wizard_views.xml',
        'wizard/job_work_loss_wizard_views.xml',
        'wizard/job_work_exception_resolve_wizard_views.xml',
        'views/job_work_type_views.xml',
        'views/job_work_views.xml',
        'views/job_work_material_views.xml',
        'views/job_work_qc_views.xml',
        'views/job_work_ncr_views.xml',
        'views/job_work_exception_views.xml',
        'views/job_work_request_views.xml',
        'views/job_work_vendor_score_views.xml',
        'views/job_work_reason_views.xml',
        'views/job_work_labour_views.xml',
        'views/res_config_settings_views.xml',
        'views/res_partner_views.xml',
        'views/stock_views.xml',
        'views/purchase_views.xml',
        'views/job_work_portal_templates.xml',
        'views/job_work_menus.xml',
        # India compliance
        'security/india_rules.xml',
        'data/india_sequence_data.xml',
        'data/india_cron_data.xml',
        'report/india_challan_report.xml',
        'wizard/itc04_wizard_views.xml',
        'views/india_challan_views.xml',
        'views/india_product_views.xml',
        'views/india_res_config_settings_views.xml',
        'views/india_job_work_views.xml',
        'views/india_menus.xml',
    ],
    'demo': [
        'demo/job_work_demo.xml',
        'demo/india_demo.xml',
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
    'post_init_hook': 'post_init_hook',
}
