# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
{
    'name': 'Field Sales Tracking & Expense Management | Offline GPS, TA/DA, Bills & Claims',
    'version': '19.0.2.0.0',
    'category': 'Sales/Sales',
    'sequence': 30,
    'summary': 'Offline GPS tracking of field staff with automatic travel allowance (per km), daily '
               'allowance, bill photos captured offline, expense claims, approval and reimbursement '
               'through Odoo Expenses. Live map, route replay, customer visit check-in, geo-fence, '
               'fake GPS detection, Traccar/OsmAnd background tracking.',
    'description': """
Field Sales Tracking & Expense Management
=========================================
Track field salespeople live on a map - even in areas with no mobile signal -
and pay their travel expenses from the distance the GPS really measured.

Field expenses
--------------
* Mileage / travel allowance (TA) per km from the GPS day log, rate per vehicle
  type (two-wheeler, car, public transport, company vehicle) with daily km cap
* Daily allowance (DA): full day, half day, outstation, minimum customer visits
* INR / USD currency switch in Settings (phone app, claims and PDF follow it;
  converted with the exchange rate when the company currency differs)
* Bills captured on the phone with a photo, offline, linked to the customer visit
* Expense claims per period (monthly auto-creation), approval by the expense
  manager, rejection with reason, PDF claim with bill photos
* Approved claims become approved Odoo Expenses: post and reimburse in Accounting
* Fraud controls: claimed km vs GPS km, km cap, missing bills, daily limits,
  duplicates, fuel on mileage days, bills away from the tracked route, fake GPS

Tracking
--------
* Offline-first mobile web app (PWA): GPS points are stored on the phone and
  uploaded automatically when the network returns (no data loss, no duplicates)
* Native background tracking (screen locked) through the free Traccar Client
  app or OsmAnd, using the OsmAnd HTTP protocol (query, form and JSON)
* Live map for managers with Live / Delayed / No signal / Day ended status
* Day logs with distance travelled (GPS spike filter), stops and route replay
* Points captured offline are flagged and shown in a different colour
* Customer visit check-in / check-out offline, geo-fence validation
* Mock / fake GPS detection, battery level, accuracy filter, data retention
""",
    'author': 'ZivoIT Technologies',
    'maintainer': 'ZivoIT Technologies',
    'website': 'https://www.zivoit.com',
    'support': 'info@zivoit.com',
    'license': 'OPL-1',
    'price': 299.0,
    'currency': 'USD',
    'depends': ['base', 'mail', 'contacts', 'sales_team', 'hr_expense'],
    'data': [
        'security/zivo_track_security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'data/expense_data.xml',
        'views/zivo_track_point_views.xml',
        'views/zivo_track_session_views.xml',
        'views/zivo_track_visit_views.xml',
        'views/zivo_track_device_views.xml',
        'views/zivo_expense_line_views.xml',
        'views/zivo_expense_claim_views.xml',
        'views/zivo_expense_config_views.xml',
        'report/zivo_expense_claim_report.xml',
        'views/res_users_views.xml',
        'views/res_config_settings_views.xml',
        'views/pwa_templates.xml',
        'views/menus.xml',
    ],
    'demo': [
        'data/demo.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'zivoit_field_tracking/static/lib/leaflet/leaflet.css',
            'zivoit_field_tracking/static/lib/leaflet/leaflet.js',
            'zivoit_field_tracking/static/src/live_map/*.js',
            'zivoit_field_tracking/static/src/live_map/*.xml',
            'zivoit_field_tracking/static/src/live_map/*.scss',
        ],
        'web.assets_tests': [
            'zivoit_field_tracking/static/tests/tours/*.js',
        ],
    },
    'images': [
        'static/description/main_screenshot.png',
        'static/description/banner.png',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
