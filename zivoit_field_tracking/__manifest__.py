# -*- coding: utf-8 -*-
# Copyright 2026 ZivoIT Technologies (https://www.zivoit.com)
# License OPL-1 (https://www.odoo.com/documentation/19.0/legal/licenses.html)
{
    'name': 'Offline Field Sales Tracking | Live GPS Tracking Without Network',
    'version': '19.0.1.0.0',
    'category': 'Sales/Sales',
    'sequence': 30,
    'summary': 'Live GPS tracking of field salespeople that keeps recording with no mobile '
               'signal and syncs automatically when back online. Offline PWA, Traccar/OsmAnd '
               'background tracking, live map, route replay, customer visit check-in, geo-fence.',
    'description': """
Offline Field Sales Tracking
============================
Track field salespeople live on a map - even in areas with no mobile signal.

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
    'price': 59.0,
    'currency': 'USD',
    'depends': ['base', 'mail', 'contacts', 'sales_team'],
    'data': [
        'security/zivo_track_security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/zivo_track_point_views.xml',
        'views/zivo_track_session_views.xml',
        'views/zivo_track_visit_views.xml',
        'views/zivo_track_device_views.xml',
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
