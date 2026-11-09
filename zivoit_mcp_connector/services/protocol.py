# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
"""Model Context Protocol (JSON-RPC 2.0) message handling.

Transport: MCP Streamable HTTP, answered with ``application/json`` (the server
does not open server-initiated SSE streams), which every MCP client supports.
"""
import logging
import time

from odoo.exceptions import AccessError, MissingError, UserError, ValidationError

from . import registry, utils
from .context import ToolContext, ToolError

_logger = logging.getLogger(__name__)

SUPPORTED_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
LATEST_VERSION = SUPPORTED_VERSIONS[0]
SERVER_NAME = 'zivoit-odoo-mcp'
SERVER_TITLE = 'ZivoIT MCP Connector for Odoo'
SERVER_VERSION = '19.0.1.0.0'

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def error_response(msg_id, code, message, data=None):
    error = {'code': code, 'message': message}
    if data is not None:
        error['data'] = data
    return {'jsonrpc': '2.0', 'id': msg_id, 'error': error}


class McpSession:
    """Handles the JSON-RPC messages of one HTTP request for one connector."""

    def __init__(self, env, connector, request_info=None):
        self.env = env
        self.connector = connector
        self.request_info = request_info or {}
        self.events = []  # one entry per message, consumed by the controller for the audit log

    # ------------------------------------------------------------------
    def handle(self, message):
        started = time.monotonic()
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
            if isinstance(message, dict) and ('result' in message or 'error' in message):
                return None  # a response to a server request: we never send any, ignore
            return error_response(message.get('id') if isinstance(message, dict) else None,
                                  INVALID_REQUEST, 'Invalid JSON-RPC 2.0 request')
        method = message['method']
        msg_id = message.get('id')
        params = message.get('params') or {}
        is_notification = 'id' not in message
        event = {'method': method, 'tool': False, 'model': False, 'status': 'success', 'message': False,
                 'arguments': False, 'count': 0}
        try:
            handler = getattr(self, '_m_' + method.replace('/', '_'), None)
            if handler is None:
                if is_notification:
                    return None
                event.update(status='error', message='Method not found: %s' % method)
                return error_response(msg_id, METHOD_NOT_FOUND, 'Method not found: %s' % method)
            if not isinstance(params, dict):
                return error_response(msg_id, INVALID_PARAMS, 'params must be an object')
            result = handler(params, event)
            if is_notification:
                return None
            return {'jsonrpc': '2.0', 'id': msg_id, 'result': result}
        except _ProtocolError as exc:
            event.update(status='error', message=exc.message)
            return error_response(msg_id, exc.code, exc.message)
        except Exception as exc:  # pragma: no cover - defensive
            _logger.exception('MCP %s failed', method)
            event.update(status='error', message=str(exc))
            return error_response(msg_id, INTERNAL_ERROR, 'Internal error: %s' % exc.__class__.__name__)
        finally:
            event['duration_ms'] = round((time.monotonic() - started) * 1000, 1)
            if method not in ('ping',) and not method.startswith('notifications/'):
                self.events.append(event)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _m_initialize(self, params, event):
        requested = params.get('protocolVersion')
        version = requested if requested in SUPPORTED_VERSIONS else LATEST_VERSION
        client = params.get('clientInfo') or {}
        event['message'] = 'Client %s %s, protocol %s' % (client.get('name', '?'), client.get('version', ''), version)
        return {
            'protocolVersion': version,
            'capabilities': {
                'tools': {'listChanged': False},
                'prompts': {'listChanged': False},
                'logging': {},
                'completions': {},
            },
            'serverInfo': {'name': SERVER_NAME, 'title': SERVER_TITLE, 'version': SERVER_VERSION,
                           'websiteUrl': 'https://www.zivoit.com'},
            'instructions': self.connector._assistant_instructions(),
        }

    def _m_ping(self, params, event):
        return {}

    def _m_notifications_initialized(self, params, event):
        return None

    def _m_notifications_cancelled(self, params, event):
        return None

    def _m_logging_setLevel(self, params, event):
        return {}

    def _m_completion_complete(self, params, event):
        ref = params.get('ref') or {}
        argument = params.get('argument') or {}
        values = []
        if ref.get('type') == 'ref/prompt' and argument.get('name') in ('company', 'entity'):
            prefix = (argument.get('value') or '').lower()
            values = [c.name for c in self.env.companies if c.name.lower().startswith(prefix)][:100]
        return {'completion': {'values': values, 'total': len(values), 'hasMore': False}}

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------
    def _m_tools_list(self, params, event):
        tools = []
        for tool in self.connector._enabled_tools():
            if tool.technical_name in registry.TOOLS:
                tools.append(registry.tool_definition(tool.technical_name, tool))
        event['count'] = len(tools)
        return {'tools': tools}

    def _m_tools_call(self, params, event):
        name = params.get('name')
        arguments = params.get('arguments') or {}
        event['tool'] = name
        event['arguments'] = utils.truncate(utils.dumps(arguments))
        if not isinstance(arguments, dict):
            raise _ProtocolError(INVALID_PARAMS, 'arguments must be an object')
        tool = self.connector._enabled_tools().filtered(lambda t: t.technical_name == name)
        if name not in registry.TOOLS or not tool:
            raise _ProtocolError(INVALID_PARAMS, 'Unknown or disabled tool: %s' % name)
        spec = registry.TOOLS[name]
        ctx = ToolContext(self.env, self.connector, self.request_info.get('base_url'))
        try:
            with self.env.cr.savepoint():
                data = spec['handler'](ctx, arguments)
                self.env.flush_all()
            is_error = False
        except ToolError as exc:
            data, is_error = {'error': str(exc)}, True
        except (AccessError, UserError, ValidationError, MissingError) as exc:
            data, is_error = {'error': '%s: %s' % (exc.__class__.__name__, exc.args[0] if exc.args else exc)}, True
        except ValueError as exc:
            data, is_error = {'error': 'Invalid value: %s' % exc}, True
        except Exception as exc:
            _logger.exception('MCP tool %s failed', name)
            data, is_error = {'error': 'The tool failed unexpectedly (%s). The error is in the Odoo server log.'
                                       % exc.__class__.__name__}, True
        event['model'] = ctx.touched_model or arguments.get('model_name') or False
        event['count'] = ctx.result_count
        if is_error:
            event['status'] = 'error'
            event['message'] = data['error']
        structured = utils.to_jsonable(data if isinstance(data, dict) else {'result': data})
        return {
            'content': [{'type': 'text', 'text': utils.dumps(structured)}],
            'structuredContent': structured,
            'isError': is_error,
        }

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------
    def _m_prompts_list(self, params, event):
        prompts = self.env['zivo.mcp.prompt'].sudo().search([('active', '=', True)])
        result = [p._mcp_definition() for p in prompts]
        event['count'] = len(result)
        return {'prompts': result}

    def _m_prompts_get(self, params, event):
        name = params.get('name')
        prompt = self.env['zivo.mcp.prompt'].sudo().search([('technical_name', '=', name), ('active', '=', True)], limit=1)
        if not prompt:
            raise _ProtocolError(INVALID_PARAMS, 'Unknown prompt: %s' % name)
        event['tool'] = 'prompt:%s' % name
        try:
            return prompt._mcp_render(params.get('arguments') or {})
        except ValueError as exc:
            raise _ProtocolError(INVALID_PARAMS, str(exc)) from None

    # ------------------------------------------------------------------
    # Resources (none published; answered so strict clients do not fail)
    # ------------------------------------------------------------------
    def _m_resources_list(self, params, event):
        return {'resources': []}

    def _m_resources_templates_list(self, params, event):
        return {'resourceTemplates': []}

    def _m_resources_read(self, params, event):
        raise _ProtocolError(-32002, 'Resource not found: %s' % params.get('uri'))


class _ProtocolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
