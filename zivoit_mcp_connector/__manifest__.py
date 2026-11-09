# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
{
    'name': 'Claude AI MCP Connector | ChatGPT, AnythingMCP, Copilot | Odoo MCP Server',
    'version': '19.0.1.0.0',
    'category': 'Productivity/AI',
    'summary': 'Connect Claude, ChatGPT, Cursor and AnythingMCP to Odoo 19 in one click: built-in MCP server with '
               'Sign in with Odoo (OAuth 2.1), read-only by default, P&L, Balance Sheet, sales, stock and CRM reports, '
               'audit log. MCP, Model Context Protocol, AI assistant, LLM, Claude Desktop, Claude Code.',
    'description': """
Claude AI MCP Connector for Odoo 19
===================================
Turns Odoo 19 (Community, Enterprise, Odoo.sh, on-premise) into a Model Context Protocol server at /mcp.
Paste the address into Claude, ChatGPT, Cursor or any MCP client, log in with Odoo, click Allow: done.

* Sign in with Odoo: built-in OAuth 2.1 server (discovery, dynamic client registration, PKCE, refresh, revoke)
* API keys for AnythingMCP, agents and scripts
* Tools: search, group, read, create/update (opt-in) plus P&L, Balance Sheet, Trial Balance, ageing,
  sales, purchase, stock and CRM pipeline reports when those apps are installed
* Security: user's own access rights, read-only by default, model allow-list, hidden secrets,
  IP allow-list, rate limit, expiry, audit log, session management
""",
    'author': 'ZivoIT Technologies',
    'maintainer': 'ZivoIT Technologies',
    'website': 'https://www.zivoit.com',
    'support': 'info@zivoit.com',
    'license': 'OPL-1',
    'price': 359.0,
    'currency': 'USD',
    'depends': ['base', 'web', 'mail'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/mcp_tool_data.xml',
        'data/mcp_prompt_data.xml',
        'data/ir_config_parameter_data.xml',
        'data/mcp_connector_data.xml',
        'views/mcp_connector_views.xml',
        'views/oauth_views.xml',
        'views/oauth_templates.xml',
        'views/mcp_log_views.xml',
        'views/mcp_tool_prompt_views.xml',
        'views/res_config_settings_views.xml',
        'views/menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'zivoit_mcp_connector/static/src/**/*',
        ],
    },
    'images': ['static/description/banner.png'],
    'installable': True,
    'application': True,
    'auto_install': False,
}
