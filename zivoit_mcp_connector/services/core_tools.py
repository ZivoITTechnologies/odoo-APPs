# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
"""Generic data tools.

Tool names and parameters follow the MCP server that Odoo ships with Odoo AI
(saas-19.4 / Odoo 20), so AnythingMCP's "Odoo (MCP server)" connector and any
prompt written for it work unchanged on Odoo 19.0 Community and Enterprise.
"""
from odoo import fields as odoo_fields

from . import utils
from .context import ToolError

FIELD_ATTRS = ('type', 'string', 'required', 'readonly', 'relation', 'store', 'selection')


def retrieve_initial_context(ctx, args):
    env = ctx.env
    user = env.user
    connector = ctx.connector
    companies = [{
        'id': c.id, 'name': c.name, 'currency': c.currency_id.name,
        'country': c.country_id.name or None,
        'fiscal_year_last_day': '%02d-%02d' % (int(c.fiscalyear_last_month), c.fiscalyear_last_day)
        if 'fiscalyear_last_month' in c._fields else None,
    } for c in env.companies]
    allowed = connector._allowed_model_summary()
    tools = [t.technical_name for t in connector._enabled_tools()]
    return {
        'server': 'ZivoIT MCP Connector for Odoo %s' % env['ir.module.module'].sudo().search(
            [('name', '=', 'base')], limit=1).latest_version,
        'connector': connector.name,
        'entity': connector.entity_label or env.company.name,
        'access_mode': connector.access_mode,
        'user': {'id': user.id, 'name': user.name, 'login': user.login, 'lang': user.lang, 'tz': user.tz or 'UTC'},
        'today': odoo_fields.Date.context_today(user).isoformat(),
        'active_company': {'id': env.company.id, 'name': env.company.name, 'currency': env.company.currency_id.name},
        'companies': companies,
        'models': allowed,
        'tools': tools,
        'instructions': connector._assistant_instructions(),
    }


def get_models(ctx, args):
    search = (args.get('search') or args.get('query') or '').strip().lower()
    result = []
    env = ctx.env
    for name in sorted(env.registry.models):
        model = env[name]
        if model._abstract or model._transient or utils.is_model_blocked(name):
            continue
        if not ctx.connector._is_model_allowed(name, 'read'):
            continue
        if not model.has_access('read'):
            continue
        description = model._description or name
        if search and search not in name.lower() and search not in description.lower():
            continue
        result.append({'model': name, 'name': description})
    return {'models': result, 'count': len(result)}


def get_fields(ctx, args):
    model = ctx.check_model(args.get('model_name'), 'read')
    with_help = utils.as_bool(args.get('include_description'), False)
    only = set(utils.as_list(args.get('fields')))
    allowed = ctx.readable_fields(model)
    described = model.fields_get(list(allowed), attributes=list(FIELD_ATTRS) + (['help'] if with_help else []))
    result = {}
    for name, info in sorted(described.items()):
        if only and name not in only:
            continue
        info = {k: v for k, v in info.items() if v not in (None, False, '') or k in ('required', 'readonly')}
        info['searchable'] = bool(allowed[name].store or allowed[name].search)
        info['groupable'] = bool(allowed[name].store and allowed[name].type not in ('text', 'html', 'one2many', 'many2many'))
        result[name] = info
    return {'model': model._name, 'description': model._description, 'fields': result, 'count': len(result)}


def _clean_record(record_dict, model):
    for key, value in list(record_dict.items()):
        field = model._fields.get(key)
        if field is not None and field.type in ('one2many', 'many2many') and isinstance(value, list) and len(value) > 50:
            record_dict[key] = value[:50] + ['... %s more' % (len(value) - 50)]
    return record_dict


def search(ctx, args):
    model = ctx.check_model(args.get('model_name'), 'read')
    domain = ctx.check_domain(model, args.get('domain'))
    field_names = utils.as_list(args.get('fields')) or ['display_name']
    field_names = [f for f in field_names if f != 'display_name']
    ctx.check_fields(model, field_names)
    field_names = ['display_name'] + field_names
    limit = utils.as_int(args.get('limit'), 50, 1, min(ctx.max_records, 500))
    offset = utils.as_int(args.get('offset'), 0, 0)
    order = args.get('order') or None
    if order:
        for part in order.split(','):
            ctx.check_fields(model, [part.strip().split(' ')[0]], purpose='order')
    try:
        total = model.search_count(domain)
        records = model.search_read(domain, field_names, offset=offset, limit=limit, order=order)
    except ValueError as exc:
        raise ToolError("Odoo rejected the query: %s" % exc) from None
    records = [_clean_record(r, model) for r in records]
    ctx.result_count = len(records)
    result = {'model': model._name, 'records': records, 'total_count': total,
              'offset': offset, 'limit': limit}
    if offset + len(records) < total:
        result['hint'] = ("%s more records match. Call again with offset=%s, or use ai_tool_read_group "
                          "for totals." % (total - offset - len(records), offset + len(records)))
    return result


def read_records(ctx, args):
    model = ctx.check_model(args.get('model_name'), 'read')
    ids = [utils.as_int(i) for i in utils.as_list(args.get('ids'))]
    if not ids:
        raise ToolError("ids is required, e.g. [12, 15].")
    ids = ids[:ctx.max_records]
    allowed = ctx.readable_fields(model)
    names = utils.as_list(args.get('fields'))
    if names:
        ctx.check_fields(model, names)
    else:
        names = [n for n, f in allowed.items() if f.type not in ('one2many', 'html') and not n.startswith('message_')
                 and not n.startswith('activity_') and n not in ('write_uid', 'create_uid', '__last_update')]
    records = model.browse(ids).exists()
    data = records.read(names)
    lines = utils.as_list(args.get('include_lines'))
    for line_field in lines:
        if line_field not in allowed or allowed[line_field].type != 'one2many':
            raise ToolError("%r is not a one2many field of %r." % (line_field, model._name))
        comodel = ctx.check_model(allowed[line_field].comodel_name, 'read')
        sub_names = [n for n, f in ctx.readable_fields(comodel).items()
                     if f.type in ('char', 'float', 'monetary', 'integer', 'many2one', 'date', 'selection', 'boolean')
                     and not n.startswith(('message_', 'activity_')) and f.store][:40]
        for rec_dict, rec in zip(data, records):
            rec_dict[line_field] = rec[line_field][:200].read(sub_names)
    ctx.result_count = len(data)
    return {'model': model._name, 'records': [_clean_record(d, model) for d in data],
            'missing_ids': sorted(set(ids) - set(records.ids))}


def read_group(ctx, args):
    model = ctx.check_model(args.get('model_name'), 'read')
    domain = ctx.check_domain(model, args.get('domain'))
    groupby = utils.as_list(args.get('groupby'))
    aggregates = utils.as_list(args.get('aggregates')) or ['__count']
    ctx.check_fields(model, groupby, purpose='group by')
    ctx.check_fields(model, [a for a in aggregates if a != '__count'], purpose='aggregate')
    having = utils.parse_domain(args.get('having')) if args.get('having') else []
    limit = utils.as_int(args.get('limit'), 100, 1, 500)
    offset = utils.as_int(args.get('offset'), 0, 0)
    order = args.get('order') or None
    try:
        groups = model.formatted_read_group(domain, groupby, aggregates, having=having,
                                            offset=offset, limit=limit, order=order)
    except (ValueError, KeyError) as exc:
        raise ToolError("Odoo rejected the grouping: %s" % exc) from None
    cleaned = []
    for group in groups:
        cleaned.append({k: v for k, v in group.items() if k != '__extra_domain'})
    ctx.result_count = len(cleaned)
    return {'model': model._name, 'groupby': groupby, 'aggregates': aggregates, 'groups': cleaned,
            'group_count': len(cleaned)}


def create_records(ctx, args):
    ctx.require_write_mode()
    model = ctx.check_model(args.get('model_name'), 'create')
    values_list = args.get('values')
    if isinstance(values_list, dict):
        values_list = [values_list]
    if not values_list or not isinstance(values_list, list):
        raise ToolError("values must be a list of objects, one per record to create.")
    if len(values_list) > 50:
        raise ToolError("At most 50 records can be created per call.")
    for values in values_list:
        ctx.check_values(model, values)
    records = model.create(values_list)
    ctx.result_count = len(records)
    return {
        'model': model._name,
        'created': [{'id': r.id, 'display_name': r.display_name, 'url': ctx.record_url(model._name, r.id)}
                    for r in records],
        'explanation': args.get('explanation'),
    }


def update_records(ctx, args):
    ctx.require_write_mode()
    updates = args.get('updates')
    if updates is None and args.get('model_name'):
        updates = [{k: args.get(k) for k in ('model_name', 'ids', 'domain', 'values')}]
    if isinstance(updates, dict):
        updates = [updates]
    if not updates or not isinstance(updates, list):
        raise ToolError("updates must be a list of {model_name, ids or domain, values}.")
    result = []
    for update in updates:
        model = ctx.check_model(update.get('model_name'), 'write')
        values = ctx.check_values(model, update.get('values') or {})
        if not values:
            raise ToolError("values is empty for %r." % model._name)
        if update.get('ids'):
            records = model.browse([utils.as_int(i) for i in utils.as_list(update['ids'])]).exists()
        else:
            domain = ctx.check_domain(model, update.get('domain'))
            if not domain:
                raise ToolError("Refusing to update every %r record: give ids or a non-empty domain." % model._name)
            records = model.search(domain, limit=101)
        if len(records) > 100:
            raise ToolError("The update matches more than 100 %r records; narrow the domain." % model._name)
        if not records:
            raise ToolError("No %r record matches the update." % model._name)
        records.write(values)
        ctx.result_count += len(records)
        result.append({'model': model._name, 'updated_ids': records.ids, 'values': values,
                       'urls': [ctx.record_url(model._name, i) for i in records.ids[:20]]})
    return {'updates': result, 'explanation': args.get('explanation')}
