# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1 (https://www.odoo.com/documentation/master/legal/licenses.html)
{
    "name": "QuickBooks Online Connector Pro | Two-Way Odoo and QuickBooks Sync",
    "summary": "Real-time two-way QuickBooks Online sync: customers, vendors, items, "
               "invoices, bills, credit notes, payments, estimates, POs, journal entries, "
               "taxes, classes & locations. Webhooks (CloudEvents) + CDC, conflict "
               "resolution, deletion/void detection, smart mapping, integrity check.",
    "description": "QuickBooks Online Connector by ZivoIT Technologies",
    "version": "19.0.1.0.0",
    "category": "Accounting/Accounting",
    "author": "ZivoIT Technologies",
    "maintainer": "ZivoIT Technologies",
    "website": "https://www.zivoit.com",
    "support": "info@zivoit.com",
    "license": "OPL-1",
    "price": 99.0,
    "currency": "USD",
    "depends": [
        "account",
        "sale_management",
        "purchase",
    ],
    "external_dependencies": {"python": ["requests", "cryptography"]},
    "data": [
        "security/qb_security.xml",
        "security/ir.model.access.csv",
        "data/qb_cron.xml",
        
        "views/qb_connection_views.xml",
        "views/qb_entity_config_views.xml",
        "views/qb_mapping_views.xml",
        "views/qb_sync_job_views.xml",
        "views/qb_sync_log_views.xml",
        
        "views/res_partner_views.xml",
        "views/product_views.xml",
        "views/account_move_views.xml",
        "views/account_payment_views.xml",
        "views/sale_purchase_views.xml",
        "wizard/qb_import_wizard_views.xml",
        "wizard/qb_export_wizard_views.xml",
        "wizard/qb_integrity_wizard_views.xml",
        "views/qb_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "zivoit_quickbooks_connector/static/src/scss/qb_dashboard.scss",
        ],
    },
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": True,
    "auto_install": False,
}
