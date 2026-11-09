# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
"""Helpers shared by the MCP protocol layer and the tool implementations."""
import ast
import base64
import datetime
import decimal
import json
import re

from odoo.models import BaseModel

# Models an AI assistant must never reach, whatever the connector says.
HARD_BLOCKED_MODELS = {
    'res.users.apikeys', 'res.users.apikeys.description', 'res.users.apikeys.show',
    'res.users.identitycheck', 'res.users.log', 'res.users.deletion',
    'ir.config_parameter', 'ir.rule', 'ir.model.access', 'ir.cron', 'ir.cron.trigger',
    'ir.actions.server', 'ir.actions.act_window', 'ir.actions.actions', 'ir.ui.view',
    'ir.ui.menu', 'ir.mail_server', 'ir.attachment', 'ir.logging', 'ir.module.module',
    'ir.model.data', 'ir.sequence', 'ir.default', 'ir.profile', 'ir.asset',
    'base.automation', 'fetchmail.server', 'mail.mail', 'iap.account',
    'auth_totp.device', 'auth.totp.wizard', 'payment.provider', 'payment.token',
    'res.groups', 'bus.bus', 'bus.presence', 'mail.presence',
    'zivo.mcp.connector', 'zivo.mcp.model.access', 'zivo.mcp.log', 'zivo.mcp.tool',
    'zivo.mcp.prompt', 'zivo.mcp.prompt.argument', 'zivo.mcp.key.wizard',
}
BLOCKED_MODEL_PREFIXES = ('ir.', 'base_import.', 'base.module.', 'res.config', 'zivo.mcp.', 'web_editor.', 'html_editor.')
# Technical models that are still useful to an assistant.
ALLOWED_TECHNICAL_MODELS = {'ir.model', 'ir.model.fields'}

SENSITIVE_FIELD_RE = re.compile(
    r'(password|passwd|secret|token|api_?key|apikey|totp|private_key|signup_|oauth|'
    r'credential|_pin$|^pin$|iban_hash|session)', re.IGNORECASE)
SENSITIVE_FIELDS = {'password', 'new_password', 'signature', 'api_key_ids', 'totp_secret'}

MAX_TEXT = 4000


def is_model_blocked(model_name):
    if model_name in ALLOWED_TECHNICAL_MODELS:
        return False
    if model_name in HARD_BLOCKED_MODELS:
        return True
    return model_name.startswith(BLOCKED_MODEL_PREFIXES)


def is_field_sensitive(field_name, field=None):
    if field_name in SENSITIVE_FIELDS or SENSITIVE_FIELD_RE.search(field_name or ''):
        return True
    if field is not None and field.type == 'binary':
        return True
    return False


def json_default(value):
    if isinstance(value, datetime.datetime):
        return value.replace(microsecond=0).isoformat(sep=' ')
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    if isinstance(value, BaseModel):
        return [(r.id, r.display_name) for r in value] if len(value) != 1 else [value.id, value.display_name]
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


def dumps(data, indent=None):
    return json.dumps(data, default=json_default, ensure_ascii=False, indent=indent)


def to_jsonable(data):
    """Round-trip through JSON so the result only holds plain JSON types."""
    return json.loads(dumps(data))


def parse_domain(domain):
    """Accept a domain given as a list, a JSON string or a Python literal string."""
    if domain in (None, '', False):
        return []
    if isinstance(domain, str):
        text = domain.strip()
        if not text:
            return []
        try:
            domain = json.loads(text)
        except ValueError:
            try:
                domain = ast.literal_eval(text)
            except (ValueError, SyntaxError) as exc:
                raise ValueError("The domain is not a valid list: %s" % exc) from None
    if isinstance(domain, tuple):
        domain = list(domain)
    if not isinstance(domain, list):
        raise ValueError("The domain must be a list such as [[\"state\", \"=\", \"posted\"]].")
    result = []
    for item in domain:
        if isinstance(item, str):
            if item not in ('&', '|', '!'):
                raise ValueError("Unknown domain operator %r" % item)
            result.append(item)
        elif isinstance(item, (list, tuple)) and len(item) == 3:
            field, operator, value = item
            if not isinstance(field, str) or not isinstance(operator, str):
                raise ValueError("Invalid domain condition %r" % (item,))
            if isinstance(value, list):
                value = list(value)
            result.append((field, operator, value))
        elif isinstance(item, bool) and not result:
            return [] if item else [('id', '=', 0)]
        else:
            raise ValueError("Invalid domain element %r" % (item,))
    return result


def domain_fields(domain):
    for item in domain:
        if isinstance(item, tuple):
            yield item[0]


def truncate(text, size=MAX_TEXT):
    text = text or ''
    return text if len(text) <= size else text[:size] + '…'


def as_list(value):
    if value in (None, False, ''):
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith('['):
            try:
                return json.loads(text)
            except ValueError:
                pass
        return [v.strip() for v in text.split(',') if v.strip()]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def as_int(value, default=None, minimum=None, maximum=None):
    if value in (None, '', False):
        return default
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        raise ValueError("Expected a number, got %r" % (value,)) from None
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def as_bool(value, default=False):
    if value in (None, ''):
        return default
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'y', 'on')
    return bool(value)


def as_date(value, default=None, name='date'):
    if value in (None, '', False):
        return default
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValueError("%s must be a date in YYYY-MM-DD format, got %r" % (name, value)) from None


def money(value, digits=2):
    return round(float(value or 0.0), digits)


def pct(part, whole):
    if not whole:
        return None
    return round(100.0 * float(part) / float(whole), 2)
