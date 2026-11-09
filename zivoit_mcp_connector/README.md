# Claude AI & ChatGPT MCP Connector for Odoo 19 (`zivoit_mcp_connector`)

ZivoIT Technologies · https://www.zivoit.com · info@zivoit.com · License OPL-1 · Depends: base, web, mail

Turns Odoo 19.0 (Community, Enterprise, Odoo.sh, on-premise) into a Model Context Protocol server at
`https://<your-odoo>/mcp` with a built-in OAuth 2.1 "Sign in with Odoo".

## Quick start (people)
1. Install the app. A read-only connector "Claude & ChatGPT – Sign in with Odoo" is created and active.
2. Administrators can connect at once; give others the **AI Connector / User** right.
3. Claude: Settings > Connectors > Add custom connector > URL `https://<your-odoo>/mcp` > Connect.
   ChatGPT (developer mode), Cursor, VS Code: add a remote MCP server with the same URL.
   Claude Code: `claude mcp add --transport http odoo https://<your-odoo>/mcp`, then `/mcp` > Authenticate.
4. Log in to Odoo, click **Allow access**. Sessions: AI Connector > My AI Sessions.

Odoo must be reachable on a public HTTPS address. If AI apps use another address than `web.base.url`,
set **Settings > AI Connector > Public Odoo URL** (it is the OAuth issuer and MCP resource).
Multi-database servers: use `dbfilter` so the host selects the database.

## Systems (API key)
Create a connector with **Integration user + API key**, click **Create Integration User**, **Activate**,
**Generate API Key**. Use `Authorization: Bearer <key>` (AnythingMCP "Odoo (MCP server)": ODOO_URL + key).

## Endpoints
* `POST /mcp` (alias `/zivoit/mcp`): MCP Streamable HTTP, protocol 2024-11-05 … 2025-11-25, JSON responses.
* `/.well-known/oauth-protected-resource[/mcp]`, `/.well-known/oauth-authorization-server[/mcp]`
* `/oauth/register` (RFC 7591), `/oauth/authorize` (login + consent, PKCE S256), `/oauth/token`
  (authorization_code, refresh_token with rotation), `/oauth/revoke` (RFC 7009).

## Tools
Data: `ai_tool_mcp_retrieve_initial_context`, `ai_tool_get_models`, `ai_tool_get_fields`, `ai_tool_search`,
`ai_tool_read_group`, `zivo_read_records`; write (opt-in): `ai_tool_create_records`, `ai_tool_update_records`.
Reports (when the app is installed): `zivo_kpi_snapshot`, `zivo_profit_and_loss`, `zivo_balance_sheet`,
`zivo_trial_balance`, `zivo_aged_receivables`, `zivo_aged_payables`, `zivo_sales_analysis`,
`zivo_purchase_analysis`, `zivo_inventory_snapshot`, `zivo_crm_pipeline`.

## Tests
`odoo-bin -d <db> -i zivoit_mcp_connector --test-tags /zivoit_mcp_connector --stop-after-init`
