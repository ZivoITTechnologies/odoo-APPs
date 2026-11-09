# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
"""Execution context handed to every MCP tool.

A tool never runs as superuser: ``env`` is bound to the connector's integration
user and restricted to the connector's companies, so Odoo access rights and
record rules apply on top of the connector's own allow-list.
"""
from odoo.exceptions import AccessError

from . import utils


class ToolError(Exception):
    """An error the assistant should see and can correct (returned with isError)."""


class ToolContext:

    def __init__(self, env, connector, base_url=None):
        self.env = env
        self.connector = connector  # sudo record of zivo.mcp.connector
        self.base_url = (base_url or connector.get_base_url() or '').rstrip('/')
        self.touched_model = False
        self.result_count = 0

    # ------------------------------------------------------------------
    # Companies
    # ------------------------------------------------------------------
    @property
    def companies(self):
        return self.env.companies

    def resolve_companies(self, company_ids=None, company=None):
        """Return the companies a tool should report on.

        ``company_ids`` may hold ids or names; nothing given means every
        company the connector may see.
        """
        allowed = self.env.companies
        wanted = utils.as_list(company_ids)
        if company not in (None, '', False):
            wanted.append(company)
        if not wanted:
            return allowed
        result = self.env['res.company']
        for item in wanted:
            match = False
            if isinstance(item, int) or (isinstance(item, str) and item.strip().isdigit()):
                match = allowed.filtered(lambda c: c.id == int(item))
            elif isinstance(item, str):
                needle = item.strip().lower()
                match = allowed.filtered(lambda c: c.name.lower() == needle) or \
                    allowed.filtered(lambda c: needle in c.name.lower())
            if not match:
                raise ToolError(
                    "Company %r is not available to this connector. Available companies: %s"
                    % (item, ', '.join('%s (id %s)' % (c.name, c.id) for c in allowed)))
            result |= match
        return result

    # ------------------------------------------------------------------
    # Models and fields
    # ------------------------------------------------------------------
    def has_model(self, model_name):
        return model_name in self.env.registry

    def check_model(self, model_name, operation='read'):
        """Raise ToolError unless the connector *and* Odoo allow ``operation``."""
        if not model_name or not isinstance(model_name, str):
            raise ToolError("model_name is required, e.g. 'res.partner'.")
        if model_name not in self.env.registry:
            raise ToolError("Model %r does not exist in this database. Use ai_tool_get_models to list models." % model_name)
        model = self.env[model_name]
        if model._abstract or model._transient:
            raise ToolError("Model %r is not a regular data model." % model_name)
        if utils.is_model_blocked(model_name):
            raise ToolError("Model %r is blocked for AI access for security reasons." % model_name)
        if not self.connector._is_model_allowed(model_name, operation):
            raise ToolError(
                "This connector does not allow %s on %r. An Odoo administrator can allow it under "
                "AI Connector > Connectors > Model Access." % (operation, model_name))
        odoo_op = {'read': 'read', 'create': 'create', 'write': 'write'}[operation]
        if not model.has_access(odoo_op):
            raise ToolError(
                "The integration user %r has no %s access on %r in Odoo." % (self.env.user.login, odoo_op, model_name))
        self.touched_model = model_name
        return model

    def readable_fields(self, model):
        """Fields the assistant may read or filter on, as {name: field}."""
        excluded = set(self.connector.excluded_field_ids.filtered(lambda f: f.model == model._name).mapped('name'))
        result = {}
        for name, field in model._fields.items():
            if name in excluded or utils.is_field_sensitive(name, field):
                continue
            if field.groups and not self.env.user.has_groups(field.groups):
                continue
            result[name] = field
        return result

    def check_fields(self, model, names, purpose='read'):
        allowed = self.readable_fields(model)
        clean = []
        for name in names or []:
            if not isinstance(name, str):
                raise ToolError("Field names must be strings, got %r" % (name,))
            base = name.split('.')[0].split(':')[0]
            if base not in model._fields:
                raise ToolError("Field %r does not exist on %r. Use ai_tool_get_fields to list fields." % (base, model._name))
            if base not in allowed:
                raise ToolError("Field %r on %r cannot be used by the assistant (%s)." % (base, model._name, purpose))
            clean.append(name)
        return clean

    def check_domain(self, model, domain):
        try:
            domain = utils.parse_domain(domain)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        self.check_fields(model, list(utils.domain_fields(domain)), purpose='filter')
        return domain

    def check_values(self, model, values):
        if not isinstance(values, dict):
            raise ToolError("Field values must be an object of {field: value}.")
        self.check_fields(model, list(values), purpose='write')
        for name in values:
            field = model._fields[name]
            if name in ('id', 'create_uid', 'create_date', 'write_uid', 'write_date'):
                raise ToolError("Field %r on %r is set by Odoo automatically." % (name, model._name))
            if field.compute and not field.inverse and field.readonly:
                # Odoo would silently ignore the value: tell the assistant instead
                raise ToolError("Field %r on %r is computed and read-only." % (name, model._name))
            if name == 'company_id' and values[name]:
                allowed_ids = self.env.companies.ids
                if values[name] not in allowed_ids:
                    raise ToolError("company_id must be one of the connector's companies: %s" % allowed_ids)
        return values

    # ------------------------------------------------------------------
    @property
    def max_records(self):
        return self.connector.max_records or 200

    def record_url(self, model_name, res_id):
        return '%s/odoo/%s/%s' % (self.base_url, model_name, res_id)

    def require_write_mode(self):
        if self.connector.access_mode != 'read_write':
            raise ToolError("This connector is read-only. Write tools are disabled.")

    @staticmethod
    def wrap_access_error(exc):
        if isinstance(exc, AccessError):
            return ToolError("Odoo access rights refused the request: %s" % exc)
        return exc
