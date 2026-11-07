# -*- coding: utf-8 -*-
{
    'name': 'ZivoIT Tally Connector AI — Two-way TallyPrime Sync',
    'version': '19.0.1.3.0',
    'category': 'Accounting/Accounting',
    'summary': 'Two-way TallyPrime ⇄ Odoo 19 sync with AI: all voucher types, GST guard, '
               'godowns, batches, cost centres, stock moves. Works with Odoo Online via '
               'Bridge Agent.',
    'description': """
ZivoIT Tally Connector AI
=========================
* Two-way sync of ledgers/parties, stock items/units, and all voucher types
  (Sales, Purchase, Credit Note, Debit Note, Receipt, Payment, Journal, Contra).
* Three transports: Direct (LAN), Bridge Agent (cloud Odoo ⇄ office Tally, outbound
  HTTPS only, no port forwarding) and offline XML files.
* Incremental pulls by Tally AlterID, idempotent pushes via REMOTEID, echo-loop
  prevention, conflict detection with policies.
* AI: automatic master matching (GSTIN/PAN/fuzzy + optional LLM), explainable
  anomaly detection (GST place-of-supply, GST math, duplicates, outliers, lock dates),
  Tally error doctor with auto-heal, AI reconciliation narrative.
* Cost centres ↔ analytic accounts (cost categories ↔ analytic plans) with
  allocations on every voucher line, both ways.
* Deletion detection: periodic census of Tally vouchers and masters, applied by
  conflict policy, protected by a mass-deletion guard.
* Party balance reconciliation Odoo vs Tally.
* Inventory: godowns ↔ locations, batches ↔ lots/serials with expiry, and real
  stock moves (deliveries/receipts/returns) for invoices imported from Tally.
* Works with TallyPrime 1.x – 7.x and Tally.ERP 9 (legacy XML schema option).
""",
    'author': 'Zivo IT Technologies',
    'maintainer': 'Zivo IT Technologies',
    'website': 'https://www.zivoit.com',
    'support': 'info@zivoit.com',
    'license': 'OPL-1',
    'depends': ['account', 'product', 'uom', 'mail', 'stock'],
    'data': [
        'security/tally_security.xml',
        'security/ir.model.access.csv',
        'data/tally_data.xml',
        'wizards/tally_wizard_views.xml',
        'views/tally_connection_views.xml',
        'views/tally_mapping_views.xml',
        'views/tally_request_views.xml',
        'views/tally_voucher_views.xml',
        'views/tally_recon_views.xml',
        'views/account_move_views.xml',
        'views/tally_stock_views.xml',
        'views/tally_menus.xml',
    ],
    'external_dependencies': {'python': []},
    'images': ['static/description/banner.png'],   # App Store thumbnail (2:1)
    'installable': True,
    'application': True,
    # Odoo Apps: one-shot price per Odoo version, EUR or USD only; Odoo keeps 30%.
    'price': 299.0,
    'currency': 'USD',
}
