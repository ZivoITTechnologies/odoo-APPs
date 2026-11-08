# -*- coding: utf-8 -*-
{
    'name': 'ZivoIT Logistics Hub - One App for All Couriers (AI)',
    'version': '19.0.1.1.0',
    'summary': 'Single connector platform for Shiprocket, Delhivery, DTDC, Blue Dart, FedEx, Xpressbees, '
               'Shree Maruti & any courier: AI rate shopping, RTO risk, NDR assistant, live tracking',
    'description': """
ZivoIT Logistics Hub
====================
One Odoo app to connect every courier and aggregator from a single screen.

* Common connector engine: carrier accounts, encrypted-at-rest style credential handling, API logs,
  async job queue with retries, token caching, webhooks, status normalisation.
* Ready connectors: Shiprocket, Delhivery, DTDC, Blue Dart, FedEx, Xpressbees, Shree Maruti
  (template), any REST courier (no-code template engine) and an offline sandbox courier.
* AI: carrier recommendation, RTO risk scoring, address intelligence, NDR assistant with customer
  messages, EDD prediction, weight-dispute detection, natural-language shipment search.
    """,
    'category': 'Inventory/Delivery',
    'author': 'ZivoIT Technologies',
    'website': 'https://www.zivoit.com',
    'support': 'info@zivoit.com',
    'license': 'OPL-1',
    'price': 249.0,
    'currency': 'USD',
    'depends': ['stock_delivery', 'sale_stock', 'mail'],
    'external_dependencies': {'python': ['requests']},
    'data': [
        'security/logistics_security.xml',
        'security/ir.model.access.csv',
        'data/ir_sequence_data.xml',
        'data/ir_cron_data.xml',
        'data/status_map_data.xml',
        'data/mail_template_data.xml',
        'views/logistics_account_views.xml',
        'views/shipment_views.xml',
        'views/allocation_rule_views.xml',
        'views/status_map_views.xml',
        'views/technical_views.xml',
        'views/delivery_carrier_views.xml',
        'views/stock_picking_views.xml',
        'views/sale_order_views.xml',
        'views/res_config_settings_views.xml',
        'views/tracking_templates.xml',
        'wizard/rate_shop_views.xml',
        'wizard/ndr_action_views.xml',
        'wizard/ai_query_views.xml',
        'wizard/bulk_ship_views.xml',
        'views/menus.xml',
    ],
    'demo': [
        'demo/logistics_demo.xml',
    ],
    'images': [
        'static/description/zivoit_logistics_hub_cover.png',
        'static/description/screenshot_02_compare_rates_ai.png',
        'static/description/screenshot_01_carrier_accounts.png',
        'static/description/screenshot_08_ndr_queue.png',
        'static/description/screenshot_19_tracking_page.png',
    ],
    'installable': True,
    'application': True,
}
