{
    'name': 'MOOWR Bonded Manufacturing for India Customs',
    'version': '19.0.1.0.0',
    'summary': 'Run a MOOWR bonded factory in Odoo: bills of entry, duty-deferred ledger, '
               'ex-bond clearances, export, scrap, interest and the monthly customs return.',
    'description': """
MOOWR Bonded Manufacturing for India Customs
============================================
Manage manufacturing and other operations in a customs bonded warehouse under
Sections 58 and 65 of the Customs Act, 1962 and the MOOWR Regulations, 2019.
""",
    'category': 'Inventory/Inventory',
    'author': 'ZivoIT Technologies',
    'maintainer': 'ZivoIT Technologies',
    'website': 'https://www.zivoit.com',
    'support': 'info@zivoit.com',
    'license': 'OPL-1',
    'price': 299.0,
    'currency': 'USD',
    'depends': ['purchase_stock', 'sale_stock', 'mrp'],
    'external_dependencies': {'python': ['xlsxwriter']},
    'data': [
        'security/moowr_security.xml',
        'security/ir.model.access.csv',
        'data/moowr_data.xml',
        'report/moowr_reports.xml',
        'report/moowr_return_report.xml',
        'report/moowr_clearance_report.xml',
        'views/moowr_licence_views.xml',
        'views/moowr_boe_views.xml',
        'views/moowr_ledger_views.xml',
        'views/moowr_batch_views.xml',
        'views/moowr_clearance_views.xml',
        'views/moowr_return_views.xml',
        'views/product_views.xml',
        'views/stock_views.xml',
        'views/purchase_views.xml',
        'views/moowr_menus.xml',
    ],
    'assets': {
        'web.assets_backend': ['zivoit_moowr/static/src/scss/moowr.scss'],
    },
    'images': ['static/description/banner.png'],
    'installable': True,
    'application': True,
}
