# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
"""Catalogue of the MCP tools served by the connector.

The ``zivo.mcp.tool`` records created from data/mcp_tool_data.xml carry the
same technical names, so administrators can switch every tool on or off per
connector.
"""
from . import core_tools, finance_tools

_COMPANY = {
    'company_ids': {'type': 'array', 'items': {'type': 'string'},
                    'description': "Companies (ids or names) to report on. Omit for every company the connector "
                                   "can see; each company is reported separately in its own currency."},
}
_PERIOD = {
    'date_from': {'type': 'string', 'format': 'date', 'description': 'First day, YYYY-MM-DD. Default: start of the fiscal year.'},
    'date_to': {'type': 'string', 'format': 'date', 'description': 'Last day, YYYY-MM-DD. Default: today.'},
}
_COMPARE = {
    'compare': {'type': 'string', 'enum': ['none', 'previous_period', 'previous_year'],
                'description': 'Add a comparison column against the previous period or the same period last year.'},
    'compare_date_from': {'type': 'string', 'format': 'date', 'description': 'Explicit comparison start (overrides compare).'},
    'compare_date_to': {'type': 'string', 'format': 'date', 'description': 'Explicit comparison end (overrides compare).'},
}
_DRAFT = {'include_draft': {'type': 'boolean', 'description': 'Include draft journal entries. Default false (posted only).'}}
_READ = {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}
_WRITE = {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': False, 'openWorldHint': False}


def _schema(properties=None, required=None):
    schema = {'type': 'object', 'properties': properties or {}}
    if required:
        schema['required'] = required
    return schema


TOOLS = {
    # -------------------------------------------------------------- core (Odoo MCP compatible)
    'ai_tool_mcp_retrieve_initial_context': {
        'title': 'Retrieve initial context',
        'description': ("Returns the signed-in Odoo integration user, today's date, the active company, every company "
                        "this connector can report on, the models it may read and usage instructions. Call it once at "
                        "the start of a conversation, before other Odoo tools."),
        'inputSchema': _schema(),
        'annotations': _READ, 'handler': core_tools.retrieve_initial_context,
    },
    'ai_tool_get_models': {
        'title': 'Get models',
        'description': ("Lists the Odoo models this connector may read, with technical name and description "
                        "(e.g. res.partner, sale.order, account.move). Optional `search` filters by keyword."),
        'inputSchema': _schema({'search': {'type': 'string', 'description': "Keyword such as 'invoice' or 'stock'."}}),
        'annotations': _READ, 'handler': core_tools.get_models,
    },
    'ai_tool_get_fields': {
        'title': 'Get fields',
        'description': ("Lists the fields of one model with type, label, relation and whether they can be searched or "
                        "grouped. Only fields it returns may be used in domains, group-bys and updates."),
        'inputSchema': _schema({
            'model_name': {'type': 'string', 'description': 'Technical model name, e.g. account.move.'},
            'include_description': {'type': 'boolean', 'description': "true to include each field's help text."},
            'fields': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Optional subset of field names.'},
        }, ['model_name']),
        'annotations': _READ, 'handler': core_tools.get_fields,
    },
    'ai_tool_search': {
        'title': 'Search records',
        'description': ("Finds records of a model with an Odoo domain and returns {records, total_count} plus a hint "
                        "when more rows remain. Only display_name comes back unless you list fields; 50 rows by "
                        "default, page with offset. Use ai_tool_read_group for totals."),
        'inputSchema': _schema({
            'model_name': {'type': 'string', 'description': "Technical model name (e.g. 'sale.order', 'res.partner')."},
            'domain': {'type': 'string', 'description': "Odoo domain as a JSON list, e.g. [[\"state\",\"=\",\"posted\"]]. Use [] to match all."},
            'fields': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Fields to return (default display_name only).'},
            'offset': {'type': 'number', 'description': 'Rows to skip (default 0).'},
            'limit': {'type': 'number', 'description': 'Max rows (default 50, capped by the connector).'},
            'order': {'type': 'string', 'description': "Sort, e.g. 'date desc, id desc'."},
        }, ['model_name', 'domain']),
        'annotations': _READ, 'handler': core_tools.search,
    },
    'ai_tool_read_group': {
        'title': 'Group and total records',
        'description': ("Groups records matching a domain by one or more fields (dates by day, week, month, quarter or "
                        "year, e.g. 'invoice_date:month') and computes aggregates such as 'amount_total:sum' or "
                        "'__count'. The tool for totals, statistics and breakdowns."),
        'inputSchema': _schema({
            'model_name': {'type': 'string', 'description': 'Technical model name.'},
            'domain': {'type': 'string', 'description': 'Odoo domain as a JSON list; [] for all records.'},
            'groupby': {'type': 'array', 'items': {'type': 'string'}, 'description': "e.g. ['partner_id', 'date:month']."},
            'aggregates': {'type': 'array', 'items': {'type': 'string'}, 'description': "e.g. ['amount_untaxed:sum', '__count']."},
            'having': {'type': 'string', 'description': "Domain on aggregates, e.g. [['amount_untaxed:sum','>',1000]]."},
            'offset': {'type': 'number'}, 'limit': {'type': 'number', 'description': 'Max groups (default 100, max 500).'},
            'order': {'type': 'string', 'description': "e.g. 'amount_untaxed:sum desc'."},
        }, ['model_name', 'domain']),
        'annotations': _READ, 'handler': core_tools.read_group,
    },
    'zivo_read_records': {
        'title': 'Read records by id',
        'description': ("Reads full details of records by id, optionally with their lines (e.g. include_lines "
                        "['invoice_line_ids'] on account.move or ['order_line'] on sale.order)."),
        'inputSchema': _schema({
            'model_name': {'type': 'string'},
            'ids': {'type': 'array', 'items': {'type': 'integer'}},
            'fields': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Optional; default all readable fields.'},
            'include_lines': {'type': 'array', 'items': {'type': 'string'}, 'description': 'one2many fields to expand.'},
        }, ['model_name', 'ids']),
        'annotations': _READ, 'handler': core_tools.read_records,
    },
    'ai_tool_create_records': {
        'title': 'Create records',
        'description': ("Creates one or more records of a model. Only available on read-write connectors for models an "
                        "administrator allowed. Always confirm the exact values with the user first."),
        'inputSchema': _schema({
            'explanation': {'type': 'string', 'description': 'What will be created and why, ending with a question asking the user to confirm.'},
            'model_name': {'type': 'string'},
            'values': {'type': 'array', 'items': {'type': 'object'}, 'description': 'One object of field values per record.'},
            'preview_menu_id': {'type': 'integer', 'description': 'Accepted for compatibility; ignored.'},
        }, ['explanation', 'model_name', 'values']),
        'annotations': _WRITE, 'handler': core_tools.create_records, 'write': True,
    },
    'ai_tool_update_records': {
        'title': 'Update records',
        'description': ("Changes field values on existing records selected by ids or a domain (max 100 records). "
                        "Only on read-write connectors for allowed models. Confirm the records and values first."),
        'inputSchema': _schema({
            'explanation': {'type': 'string', 'description': 'What will change and why, ending with a question asking the user to confirm.'},
            'updates': {'type': 'array', 'description': 'List of {model_name, ids or domain, values}.', 'items': {
                'type': 'object', 'properties': {
                    'model_name': {'type': 'string'},
                    'ids': {'type': 'array', 'items': {'type': 'integer'}},
                    'domain': {'type': 'string', 'description': 'JSON domain, used when ids is not given.'},
                    'values': {'type': 'object'}},
                'required': ['model_name', 'values']}},
            'preview_menus': {'type': 'array', 'items': {'type': 'object'}, 'description': 'Accepted for compatibility; ignored.'},
        }, ['explanation', 'updates']),
        'annotations': _WRITE, 'handler': core_tools.update_records, 'write': True,
    },
    # -------------------------------------------------------------- finance & analytics
    'zivo_profit_and_loss': {
        'title': 'Profit and Loss statement',
        'description': ("Profit & Loss from posted journal items, per company: revenue, cost of revenue, gross profit and "
                        "margin, operating expenses, depreciation, other income/expenses and net profit, with account "
                        "detail. Optional comparison (previous period or year) with variances, and a monthly or "
                        "quarterly trend (granularity) for margin analysis."),
        'inputSchema': _schema(dict(_PERIOD, **_COMPANY, **_COMPARE, **_DRAFT, **{
            'granularity': {'type': 'string', 'enum': ['month', 'quarter', 'year'], 'description': 'Add a trend per month, quarter or year.'},
            'detail': {'type': 'string', 'enum': ['accounts', 'summary'], 'description': 'accounts (default) or summary totals only.'},
        })),
        'annotations': _READ, 'handler': finance_tools.profit_and_loss, 'requires': ('account.move.line',),
    },
    'zivo_balance_sheet': {
        'title': 'Balance Sheet',
        'description': ("Balance Sheet as of a date, per company: current and non-current assets, current and non-current "
                        "liabilities, equity with current-year and unallocated earnings, working capital and a balance check."),
        'inputSchema': _schema(dict(_COMPANY, **_DRAFT, **{
            'date': {'type': 'string', 'format': 'date', 'description': 'Report date, YYYY-MM-DD. Default today.'},
            'detail': {'type': 'string', 'enum': ['accounts', 'summary']},
        })),
        'annotations': _READ, 'handler': finance_tools.balance_sheet, 'requires': ('account.move.line',),
    },
    'zivo_trial_balance': {
        'title': 'Trial Balance',
        'description': "Trial balance per account for a period: opening balance, debit, credit and closing balance, per company.",
        'inputSchema': _schema(dict(_PERIOD, **_COMPANY, **_DRAFT, **{
            'include_zero': {'type': 'boolean', 'description': 'Include accounts without movement.'},
            'limit': {'type': 'number', 'description': 'Max accounts per company (default 500).'},
        })),
        'annotations': _READ, 'handler': finance_tools.trial_balance, 'requires': ('account.move.line',),
    },
    'zivo_aged_receivables': {
        'title': 'Aged Receivables',
        'description': ("Open customer balances by partner in ageing buckets (not due, 1-30, 31-60, 61-90, 91-120, 120+ "
                        "days) as of a date. Use min_days_overdue and include_documents to list overdue invoices."),
        'inputSchema': _schema(dict(_COMPANY, **{
            'date': {'type': 'string', 'format': 'date', 'description': 'Ageing date, default today.'},
            'min_days_overdue': {'type': 'integer', 'description': 'Only items overdue by at least this many days.'},
            'include_documents': {'type': 'boolean', 'description': 'List the open documents (max 200).'},
            'partner': {'type': 'string', 'description': 'Filter on customer name.'},
            'limit': {'type': 'integer', 'description': 'Max partners (default 25).'},
        })),
        'annotations': _READ, 'handler': finance_tools.aged_receivables, 'requires': ('account.move.line',),
    },
    'zivo_aged_payables': {
        'title': 'Aged Payables',
        'description': ("Open supplier balances by vendor in ageing buckets as of a date. Use min_days_overdue (e.g. 30) "
                        "and include_documents to list overdue vendor bills with amounts and due dates."),
        'inputSchema': _schema(dict(_COMPANY, **{
            'date': {'type': 'string', 'format': 'date'},
            'min_days_overdue': {'type': 'integer'},
            'include_documents': {'type': 'boolean'},
            'partner': {'type': 'string', 'description': 'Filter on vendor name.'},
            'limit': {'type': 'integer'},
        })),
        'annotations': _READ, 'handler': finance_tools.aged_payables, 'requires': ('account.move.line',),
    },
    'zivo_sales_analysis': {
        'title': 'Sales Analysis',
        'description': ("Sales by customer, product, category, salesperson, team, country, company or period (month, "
                        "quarter, year), from confirmed sales orders or posted invoices, with share of total and "
                        "optional comparison and growth (order='growth' ranks the fastest-growing)."),
        'inputSchema': _schema(dict(_PERIOD, **_COMPANY, **_COMPARE, **{
            'group_by': {'type': 'string', 'enum': ['customer', 'product', 'category', 'salesperson', 'team', 'country',
                                                     'company', 'week', 'month', 'quarter', 'year']},
            'source': {'type': 'string', 'enum': ['orders', 'invoices'], 'description': 'Default orders if Sales is installed.'},
            'product': {'type': 'string', 'description': 'Filter on product name.'},
            'partner': {'type': 'string', 'description': 'Filter on customer name.'},
            'order': {'type': 'string', 'enum': ['amount', 'growth']},
            'limit': {'type': 'integer', 'description': 'Max rows (default 20).'},
        })),
        'annotations': _READ, 'handler': finance_tools.sales_analysis, 'requires': ('sale.report', 'account.invoice.report'),
    },
    'zivo_purchase_analysis': {
        'title': 'Purchase Analysis',
        'description': ("Purchases by vendor, product, category, buyer, country or period from confirmed purchase orders "
                        "or posted vendor bills, with share of total and optional comparison."),
        'inputSchema': _schema(dict(_PERIOD, **_COMPANY, **_COMPARE, **{
            'group_by': {'type': 'string', 'enum': ['vendor', 'product', 'category', 'buyer', 'country', 'company',
                                                     'month', 'quarter', 'year']},
            'source': {'type': 'string', 'enum': ['orders', 'bills']},
            'product': {'type': 'string'}, 'partner': {'type': 'string', 'description': 'Filter on vendor name.'},
            'order': {'type': 'string', 'enum': ['amount', 'growth']},
            'limit': {'type': 'integer'},
        })),
        'annotations': _READ, 'handler': finance_tools.purchase_analysis, 'requires': ('purchase.report', 'account.invoice.report'),
    },
    'zivo_inventory_snapshot': {
        'title': 'Inventory Snapshot',
        'description': ("Stock on hand in internal locations by product, category, warehouse or location with reserved "
                        "and available quantities and value at product cost; optional low-stock list."),
        'inputSchema': _schema(dict(_COMPANY, **{
            'group_by': {'type': 'string', 'enum': ['product', 'category', 'warehouse', 'location']},
            'product': {'type': 'string'}, 'warehouse': {'type': 'string'},
            'low_stock_threshold': {'type': 'number', 'description': 'List storable products at or below this quantity.'},
            'limit': {'type': 'integer'},
        })),
        'annotations': _READ, 'handler': finance_tools.inventory_snapshot, 'requires': ('stock.quant',),
    },
    'zivo_kpi_snapshot': {
        'title': 'KPI Snapshot',
        'description': ("Headline KPIs per company: revenue month-to-date and year-to-date, gross and net margin, cash "
                        "and bank, open and overdue receivables and payables, draft invoices and bills, sales to "
                        "invoice and purchases to bill. Ideal first answer to 'how are we doing?'."),
        'inputSchema': _schema(dict(_COMPANY, **{'date': {'type': 'string', 'format': 'date'}})),
        'annotations': _READ, 'handler': finance_tools.kpi_snapshot, 'requires': ('account.move.line',),
    },
    'zivo_crm_pipeline': {
        'title': 'CRM Pipeline',
        'description': ("Sales pipeline from CRM: open opportunities by stage, salesperson, team or expected closing "
                        "month with expected and weighted revenue, plus opportunities won and lost in a period with "
                        "win rate and lost reasons."),
        'inputSchema': _schema(dict(_PERIOD, **_COMPANY, **{
            'group_by': {'type': 'string', 'enum': ['stage', 'salesperson', 'team', 'month', 'customer', 'source']},
            'salesperson': {'type': 'string', 'description': 'Filter on salesperson name.'},
            'limit': {'type': 'integer', 'description': 'Max rows and top deals (default 20).'},
        })),
        'annotations': _READ, 'handler': finance_tools.crm_pipeline, 'requires': ('crm.lead',),
    },
}


def is_available(name, env):
    spec = TOOLS.get(name)
    if not spec:
        return False
    requires = spec.get('requires')
    return not requires or any(model in env.registry for model in requires)


def tool_definition(name, tool_record=None):
    spec = TOOLS[name]
    definition = {
        'name': name,
        'title': spec['title'],
        'description': (tool_record.description_override if tool_record and tool_record.description_override
                        else spec['description']),
        'inputSchema': spec['inputSchema'],
        'annotations': dict(spec['annotations'], title=spec['title']),
    }
    return definition
