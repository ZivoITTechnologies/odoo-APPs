# ZivoIT Logistics Hub (Odoo 19)

One app for all couriers: Shiprocket, Delhivery, DTDC, Blue Dart, FedEx, Xpressbees, Shree Maruti,
any REST courier (template engine) and an offline sandbox courier, with an AI layer.

## Install
1. Copy `zivoit_logistics_hub` into your addons path (depends on `stock_delivery`, `sale_stock`, `mail`; Python `requests`).
2. Apps > Update Apps List > install "ZivoIT Logistics Hub".
3. Logistics Hub > Configuration > Carrier Accounts: add accounts, Test Connection.
4. Configuration > Delivery Methods: create a method of type "ZivoIT Logistics Hub", choose AI Best (or rules/fixed).
5. Inventory > Configuration > Operation Types > Delivery Orders: tick "Print Label" so validation books the courier.

## Tests
    odoo-bin -d <db> -u zivoit_logistics_hub --test-enable --test-tags zivo_logistics --stop-after-init

## Layout
- `providers/` carrier adapters (pure Python, one contract) + `base.py` engine (HTTP client, logging, retries)
- `ai/` local AI engine (`engine.py`), Indian postal data (`india.py`), LLM client (`llm.py`)
- `models/` Odoo models, `wizard/` Compare Rates, NDR, Ask AI, Bulk Ship, `controllers/` webhooks + tracking page
