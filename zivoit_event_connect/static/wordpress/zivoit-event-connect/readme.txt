=== ZivoIT Event Connect for Odoo ===
Contributors: zivoit
Tags: odoo, events, headless, webhook
Requires at least: 6.0
Requires PHP: 7.4
Stable tag: 1.0.0
License: GPLv2 or later

Display events managed in Odoo 19 (ZivoIT Event Connect module) on WordPress and refresh instantly via signed webhooks.

== Installation ==
1. Upload the plugin and activate it.
2. Settings > Odoo Event Connect: enter your Odoo URL, the API key (Odoo > Events > Configuration > Event Connect > Headless API Clients) and the webhook secret.
3. In Odoo create a Webhook pointing to https://your-site/wp-json/zivo/v1/webhook with the same secret.
4. Use [zivo_events limit="6" department="hr"] or [zivo_event id="12"] in any page.
