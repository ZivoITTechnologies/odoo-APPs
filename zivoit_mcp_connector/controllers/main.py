# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
"""MCP Streamable HTTP endpoint.

``POST /mcp`` is the address AnythingMCP's "Odoo (MCP server)" connector calls
(``ODOO_URL/mcp``), so Odoo 19.0 Community, Enterprise and Odoo.sh databases
work with it exactly like Odoo Online databases that ship Odoo's own MCP server.
``/zivoit/mcp`` is an alias, and ``/zivoit/mcp/<key>`` serves clients that cannot
send headers (only when the connector allows keys in the URL).
"""
import json
import logging
from urllib.parse import urlparse


from odoo import http
from odoo.http import request

from ..models.mcp_oauth import ACCESS_PREFIX
from ..services import protocol, utils
from .oauth import resource_metadata_url

_logger = logging.getLogger(__name__)

ALLOWED_METHODS = 'GET, POST, DELETE, OPTIONS'


def _json(data, status=200, headers=None):
    body = json.dumps(data, default=utils.json_default, ensure_ascii=False)
    hdrs = [('Content-Type', 'application/json; charset=utf-8'), ('Cache-Control', 'no-store')]
    hdrs += list((headers or {}).items())
    return request.make_response(body, headers=hdrs, status=status)


class ZivoMcpController(http.Controller):

    @http.route(['/mcp', '/zivoit/mcp', '/zivoit/mcp/<string:url_key>'], type='http', auth='none',
                methods=['GET', 'POST', 'DELETE', 'OPTIONS'], csrf=False, save_session=False,
                readonly=False)
    def mcp_endpoint(self, url_key=None, **kwargs):
        httpreq = request.httprequest
        method = httpreq.method
        if method == 'OPTIONS':
            return request.make_response('', headers=[('Allow', ALLOWED_METHODS)], status=204)
        if method in ('GET', 'DELETE'):
            # Stateless server: no server-initiated SSE stream and no session to delete.
            return _json({'name': protocol.SERVER_TITLE, 'transport': 'streamable-http',
                          'message': 'Send MCP JSON-RPC messages with POST.'}, status=405,
                         headers={'Allow': 'POST, OPTIONS'})

        env = request.env(su=True)
        ICP = env['ir.config_parameter']
        ip = httpreq.remote_addr
        user_agent = (httpreq.headers.get('User-Agent') or '')[:250]
        Log = env['zivo.mcp.log']

        if utils.as_bool(ICP.get_param('zivoit_mcp.endpoint_disabled'), False):
            return _json(protocol.error_response(None, -32000, 'The MCP endpoint is disabled on this database.'), 503)

        # DNS-rebinding protection required by the MCP specification
        origin = httpreq.headers.get('Origin')
        if origin and not self._origin_allowed(origin, env):
            Log._log_simple(False, 'denied', 'Origin %s not allowed' % origin, ip, user_agent)
            return _json(protocol.error_response(None, -32000, 'Origin not allowed'), 403)

        # ---------------------------------------------------------- authentication
        key, key_source = self._extract_key(httpreq, url_key)
        metadata = 'resource_metadata="%s"' % resource_metadata_url()
        if not key:
            # Starts the "Sign in with Odoo" flow in Claude, ChatGPT and other OAuth-capable MCP clients
            Log._log_simple(False, 'auth_failed', 'Missing credentials (sign-in required)', ip, user_agent)
            return _json(protocol.error_response(None, -32001, 'Authentication required: sign in with Odoo (OAuth) '
                                                 'or send "Authorization: Bearer <Odoo API key>".'),
                         401, {'WWW-Authenticate': 'Bearer realm="Odoo MCP", %s' % metadata})
        challenge = {'WWW-Authenticate': 'Bearer realm="Odoo MCP", error="invalid_token", %s' % metadata}
        oauth_token = None
        if key.startswith(ACCESS_PREFIX):
            oauth_token = env['zivo.mcp.oauth.token']._from_access_token(key)
            if not oauth_token:
                Log._log_simple(False, 'auth_failed', 'Invalid or expired access token', ip, user_agent)
                return _json(protocol.error_response(None, -32001, 'Invalid or expired access token.'), 401, challenge)
            uid = oauth_token.user_id.id
            connector = oauth_token.connector_id
        else:
            uid = env['res.users.apikeys']._check_credentials(scope='mcp', key=key)
            if not uid:
                Log._log_simple(False, 'auth_failed', 'Invalid or expired API key %s...' % key[:4], ip, user_agent)
                return _json(protocol.error_response(None, -32001, 'Invalid or expired API key.'), 401, challenge)
            connector = env['zivo.mcp.connector'].search(
                [('user_id', '=', uid), ('auth_mode', '=', 'api_key'), ('state', '!=', 'draft')], limit=1)
        denial = self._check_connector(connector, uid, key_source, ip, oauth_token)
        if denial:
            Log._log_simple(connector.id or False, 'denied', denial, ip, user_agent, uid)
            return _json(protocol.error_response(None, -32003, denial), 403)
        if oauth_token:
            oauth_token._touch(ip)
        if connector._rate_limited(uid if oauth_token else None):
            Log._log_simple(connector.id, 'rate_limited', 'Rate limit of %s calls/minute reached' % connector.rate_limit,
                            ip, user_agent, uid)
            return _json(protocol.error_response(None, -32029, 'Rate limit reached, retry in a minute.'), 429,
                         {'Retry-After': '60'})

        # ---------------------------------------------------------- payload
        try:
            payload = json.loads(httpreq.get_data(as_text=True) or 'null')
        except ValueError:
            return _json(protocol.error_response(None, protocol.PARSE_ERROR, 'Parse error: body is not JSON'), 400)
        if payload is None or (isinstance(payload, list) and not payload):
            return _json(protocol.error_response(None, protocol.INVALID_REQUEST, 'Empty request'), 400)

        # ---------------------------------------------------------- run as the integration user
        user = env['res.users'].browse(uid)
        company_ids = connector._effective_company_ids(user)
        context = dict(request.env.context, allowed_company_ids=company_ids, lang=user.lang or 'en_US',
                       tz=user.tz or 'UTC', zivo_mcp_connector_id=connector.id)
        request.update_env(user=uid, context=context)
        user_env = request.env
        session = protocol.McpSession(user_env, connector.with_env(user_env).sudo(), {
            'base_url': connector._public_base_url(),
        })
        messages = payload if isinstance(payload, list) else [payload]
        responses = [r for r in (session.handle(m) for m in messages[:50]) if r is not None]

        log_payloads = not utils.as_bool(ICP.get_param('zivoit_mcp.hide_payloads'), False)
        Log._log_events(connector, uid, session.events, ip, user_agent, log_payloads)

        if not responses:
            return request.make_response('', status=202)
        return _json(responses if isinstance(payload, list) else responses[0])

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_key(httpreq, url_key):
        auth = httpreq.headers.get('Authorization') or ''
        if auth.lower().startswith('bearer '):
            return auth[7:].strip(), 'header'
        api_key = httpreq.headers.get('X-API-Key') or httpreq.headers.get('X-Odoo-Api-Key')
        if api_key:
            return api_key.strip(), 'header'
        if url_key:
            return url_key.strip(), 'url'
        return None, None

    @staticmethod
    def _origin_allowed(origin, env):
        host = urlparse(origin).netloc.lower()
        base = urlparse(env['ir.config_parameter'].get_param('web.base.url') or '').netloc.lower()
        if host and (host == base or host == request.httprequest.host.lower()):
            return True
        extra = env['ir.config_parameter'].get_param('zivoit_mcp.allowed_origins') or ''
        return host in {urlparse(o.strip()).netloc.lower() or o.strip().lower() for o in extra.split(',') if o.strip()}

    @staticmethod
    def _check_connector(connector, uid, key_source, ip, oauth_token=None):
        if oauth_token:
            if connector.state != 'active':
                return 'The AI connector "%s" is paused.' % connector.name
            if connector.expiry_date and connector.expiry_date < connector._today():
                return 'The AI connector "%s" expired on %s.' % (connector.name, connector.expiry_date)
            if not connector._is_user_allowed(oauth_token.user_id):
                return 'Your Odoo user is no longer allowed to use the AI connector "%s".' % connector.name
            if not connector._ip_allowed(ip):
                return 'Requests from %s are not allowed for this connector.' % ip
            return None
        if not connector:
            return ('No AI connector is configured for this Odoo user. An administrator must create one under '
                    'AI Connector > Connectors and set this user as its integration user.')
        if connector.state != 'active':
            return 'The AI connector "%s" is paused.' % connector.name
        if connector.expiry_date and connector.expiry_date < connector._today():
            return 'The AI connector "%s" expired on %s.' % (connector.name, connector.expiry_date)
        if key_source == 'url' and not connector.allow_url_key:
            return 'API keys in the URL are disabled for this connector; send an Authorization header instead.'
        if not connector._ip_allowed(ip):
            return 'Requests from %s are not allowed for this connector.' % ip
        if not connector.user_id.active:
            return 'The integration user is archived.'
        return None
