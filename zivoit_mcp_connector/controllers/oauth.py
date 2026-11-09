# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
"""OAuth 2.1 endpoints implementing the MCP authorization specification.

Discovery: /.well-known/oauth-protected-resource[/mcp] (RFC 9728) and
/.well-known/oauth-authorization-server[/mcp] (RFC 8414).
Flow: /oauth/register (RFC 7591) -> /oauth/authorize (Odoo login + consent,
PKCE S256) -> /oauth/token (code and refresh grants) -> /oauth/revoke (RFC 7009).
"""
import base64
import hmac
import json
import logging
from urllib.parse import urlencode, urlparse, urlsplit, urlunsplit, parse_qsl

from odoo import fields, http
from odoo.http import request

from ..models.mcp_oauth import ACCESS_PREFIX, REFRESH_PREFIX, pkce_s256, token_hash
from ..services import utils

_logger = logging.getLogger(__name__)

SCOPES = ['mcp']
CORS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Authorization, Content-Type, MCP-Protocol-Version',
    'Access-Control-Max-Age': '86400',
}


def oauth_base_url():
    """Public address of this Odoo, used as OAuth issuer and MCP resource."""
    ICP = request.env['ir.config_parameter'].sudo()
    public = ICP.get_param('zivoit_mcp.public_url')
    if public:
        return public.rstrip('/')
    base = (ICP.get_param('web.base.url') or '').rstrip('/')
    host = request.httprequest.host.lower()
    if base and urlparse(base).netloc.lower() == host:
        return base
    scheme = request.httprequest.scheme
    if (request.httprequest.headers.get('X-Forwarded-Proto') or '').lower() == 'https':
        scheme = 'https'
    return '%s://%s' % (scheme, request.httprequest.host)


def resource_metadata_url():
    return oauth_base_url() + '/.well-known/oauth-protected-resource'


def _json(data, status=200, cors=True, extra=None):
    headers = [('Content-Type', 'application/json; charset=utf-8'), ('Cache-Control', 'no-store'), ('Pragma', 'no-cache')]
    if cors:
        headers += list(CORS.items())
    headers += list((extra or {}).items())
    return request.make_response(json.dumps(data), headers=headers, status=status)


def _oauth_error(error, description, status=400, extra=None):
    return _json({'error': error, 'error_description': description}, status, extra=extra)


def _with_query(url, params):
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True) + [(k, v) for k, v in params.items() if v is not None]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _params():
    """Form or JSON body parameters merged with the query string."""
    httpreq = request.httprequest
    params = dict(httpreq.args.items())
    if httpreq.method == 'POST':
        if (httpreq.mimetype or '').endswith('json'):
            try:
                body = json.loads(httpreq.get_data(as_text=True) or '{}')
                if isinstance(body, dict):
                    params.update({k: v for k, v in body.items() if v is not None})
            except ValueError:
                pass
        else:
            params.update(httpreq.form.items())
    return params


def _enabled():
    return not utils.as_bool(request.env['ir.config_parameter'].sudo().get_param('zivoit_mcp.oauth_disabled'), False)


class ZivoMcpOauthController(http.Controller):

    # ------------------------------------------------------------------ discovery
    @http.route(['/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp',
                 '/.well-known/oauth-protected-resource/zivoit/mcp'],
                type='http', auth='none', methods=['GET', 'OPTIONS'], csrf=False, save_session=False)
    def protected_resource_metadata(self, **kw):
        if request.httprequest.method == 'OPTIONS':
            return _json({}, 204)
        base = oauth_base_url()
        company = request.env['res.company'].sudo().search([], limit=1, order='id')
        return _json({
            'resource': base + '/mcp',
            'authorization_servers': [base],
            'scopes_supported': SCOPES,
            'bearer_methods_supported': ['header'],
            'resource_name': 'Odoo – %s' % company.name,
            'resource_documentation': 'https://www.zivoit.com',
        })

    @http.route(['/.well-known/oauth-authorization-server', '/.well-known/oauth-authorization-server/mcp',
                 '/.well-known/oauth-authorization-server/zivoit/mcp'],
                type='http', auth='none', methods=['GET', 'OPTIONS'], csrf=False, save_session=False)
    def authorization_server_metadata(self, **kw):
        if request.httprequest.method == 'OPTIONS':
            return _json({}, 204)
        if not _enabled():
            return _oauth_error('temporarily_unavailable', 'Sign in with Odoo is disabled on this database.', 404)
        base = oauth_base_url()
        dcr = not utils.as_bool(request.env['ir.config_parameter'].sudo().get_param('zivoit_mcp.oauth_dcr_disabled'), False)
        data = {
            'issuer': base,
            'authorization_endpoint': base + '/oauth/authorize',
            'token_endpoint': base + '/oauth/token',
            'revocation_endpoint': base + '/oauth/revoke',
            'response_types_supported': ['code'],
            'response_modes_supported': ['query'],
            'grant_types_supported': ['authorization_code', 'refresh_token'],
            'code_challenge_methods_supported': ['S256'],
            'token_endpoint_auth_methods_supported': ['none', 'client_secret_post', 'client_secret_basic'],
            'revocation_endpoint_auth_methods_supported': ['none', 'client_secret_post', 'client_secret_basic'],
            'scopes_supported': SCOPES,
            'authorization_response_iss_parameter_supported': True,
            'service_documentation': 'https://www.zivoit.com',
        }
        if dcr:
            data['registration_endpoint'] = base + '/oauth/register'
        return _json(data)

    # ------------------------------------------------------------------ dynamic client registration
    @http.route('/oauth/register', type='http', auth='none', methods=['POST', 'OPTIONS'], csrf=False,
                save_session=False, readonly=False)
    def register(self, **kw):
        if request.httprequest.method == 'OPTIONS':
            return _json({}, 204)
        ICP = request.env['ir.config_parameter'].sudo()
        if not _enabled() or utils.as_bool(ICP.get_param('zivoit_mcp.oauth_dcr_disabled'), False):
            return _oauth_error('access_denied', 'Dynamic client registration is disabled. Ask your Odoo '
                                'administrator for a client ID.', 403)
        try:
            metadata = json.loads(request.httprequest.get_data(as_text=True) or '{}')
        except ValueError:
            return _oauth_error('invalid_client_metadata', 'The body must be JSON.')
        if not isinstance(metadata, dict):
            return _oauth_error('invalid_client_metadata', 'The body must be a JSON object.')
        try:
            client, secret = request.env['zivo.mcp.oauth.client'].sudo()._mcp_register(metadata)
        except ValueError as exc:
            code = str(exc)
            return _oauth_error(code, 'Redirect URIs must use https, http://localhost or an app scheme.'
                                if code == 'invalid_redirect_uri' else 'Unsupported client metadata.')
        method = 'none' if client.is_public else (metadata.get('token_endpoint_auth_method') or 'client_secret_post')
        response = {
            'client_id': client.client_id,
            'client_id_issued_at': int(client.create_date.timestamp()),
            'client_name': client.name,
            'redirect_uris': client._redirect_uri_list(),
            'grant_types': ['authorization_code', 'refresh_token'],
            'response_types': ['code'],
            'token_endpoint_auth_method': method,
            'scope': ' '.join(SCOPES),
        }
        if secret:
            response.update(client_secret=secret, client_secret_expires_at=0)
        return _json(response, 201)

    # ------------------------------------------------------------------ authorization (login + consent)
    @http.route('/oauth/authorize', type='http', auth='user', methods=['GET', 'POST'],
                readonly=False)
    def authorize(self, **kw):
        params = _params()
        Client = request.env['zivo.mcp.oauth.client'].sudo()
        user = request.env.user
        if not _enabled():
            return self._error_page('Sign in with Odoo is disabled on this database.')
        client = Client.search([('client_id', '=', params.get('client_id') or '')], limit=1)
        if not client:
            return self._error_page('Unknown application (client_id). Remove the connector in your AI app and add it again.')
        uris = client._redirect_uri_list()
        redirect_uri = params.get('redirect_uri') or (uris[0] if len(uris) == 1 else '')
        if redirect_uri not in uris:
            return self._error_page('The redirect address of this application is not registered.')
        state = params.get('state')

        def deny(error, description):
            return request.redirect(_with_query(redirect_uri, {
                'error': error, 'error_description': description, 'state': state, 'iss': oauth_base_url()}), local=False)

        if params.get('response_type') != 'code':
            return deny('unsupported_response_type', 'Only response_type=code is supported.')
        if not params.get('code_challenge') or params.get('code_challenge_method', 'plain') != 'S256':
            return deny('invalid_request', 'PKCE with code_challenge_method=S256 is required.')
        requested = set((params.get('scope') or 'mcp').split())
        if requested - set(SCOPES) - {'openid', 'profile', 'email', 'offline_access'}:
            return deny('invalid_scope', 'Supported scope: mcp.')
        if user.share:
            return deny('access_denied', 'Only internal Odoo users can connect AI assistants.')
        connectors = request.env['zivo.mcp.connector'].sudo()._oauth_connectors_for(user)
        if not connectors:
            return self._error_page('Your Odoo user is not allowed to connect AI assistants. Ask your administrator '
                                    'to give you the "AI Connector / User" access right.', redirect_uri, state)

        if request.httprequest.method == 'POST':
            if params.get('decision') != 'allow':
                return deny('access_denied', 'The user denied the request.')
            try:
                connector = connectors.filtered(lambda c: c.id == int(params.get('connector_id') or 0))
            except ValueError:
                connector = None
            connector = connector or (client.connector_id & connectors) or connectors[:1]
            code = request.env['zivo.mcp.oauth.code'].sudo()._issue(
                client, user, connector, redirect_uri, params['code_challenge'], 'mcp', params.get('resource'))
            request.env['zivo.mcp.log'].sudo().create({
                'connector_id': connector.id, 'user_id': user.id, 'method': 'oauth/authorize', 'status': 'success',
                'message': 'Access granted to %s (%s)' % (client.name, urlparse(redirect_uri).netloc or redirect_uri),
                'ip_address': request.httprequest.remote_addr,
                'user_agent': (request.httprequest.headers.get('User-Agent') or '')[:250],
            })
            return request.redirect(_with_query(redirect_uri, {'code': code, 'state': state, 'iss': oauth_base_url()}),
                                    local=False)

        default = (client.connector_id & connectors) or connectors[:1]
        parsed = urlparse(redirect_uri)
        values = {
            'client': client,
            'client_host': parsed.netloc or (parsed.scheme + '://'),
            'connectors': connectors,
            'default_connector': default,
            'user': user,
            'params': {k: v for k, v in params.items() if k in (
                'client_id', 'redirect_uri', 'response_type', 'state', 'code_challenge', 'code_challenge_method',
                'scope', 'resource')},
            'company': user.company_id,
            'base_url': oauth_base_url(),
        }
        response = request.render('zivoit_mcp_connector.oauth_consent', values)
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "frame-ancestors 'none'"
        return response

    def _error_page(self, message, redirect_uri=None, state=None):
        return request.render('zivoit_mcp_connector.oauth_error', {'message': message, 'redirect_uri': redirect_uri},
                              status=400)

    # ------------------------------------------------------------------ token endpoint
    @http.route('/oauth/token', type='http', auth='none', methods=['POST', 'OPTIONS'], csrf=False,
                save_session=False, readonly=False)
    def token(self, **kw):
        if request.httprequest.method == 'OPTIONS':
            return _json({}, 204)
        if not _enabled():
            return _oauth_error('invalid_request', 'Sign in with Odoo is disabled on this database.')
        params = _params()
        client, error = self._authenticate_client(params)
        if error:
            return error
        grant = params.get('grant_type')
        Token = request.env['zivo.mcp.oauth.token'].sudo()
        if grant == 'authorization_code':
            Code = request.env['zivo.mcp.oauth.code'].sudo()
            code = Code.search([('code_hash', '=', token_hash(params.get('code') or ''))], limit=1)
            if not code or code.client_id != client:
                return _oauth_error('invalid_grant', 'Unknown authorization code.')
            if code.used:
                # replayed code: revoke what it produced (RFC 6749 §4.1.2)
                Token.search([('client_id', '=', client.id), ('user_id', '=', code.user_id.id),
                              ('create_date', '>=', code.create_date)]).action_revoke()
                return _oauth_error('invalid_grant', 'The authorization code was already used.')
            code.used = True
            if code.expires_at < fields.Datetime.now():
                return _oauth_error('invalid_grant', 'The authorization code has expired.')
            if params.get('redirect_uri') and params['redirect_uri'] != code.redirect_uri:
                return _oauth_error('invalid_grant', 'redirect_uri does not match the authorization request.')
            verifier = params.get('code_verifier') or ''
            if not (43 <= len(verifier) <= 128) or not hmac.compare_digest(pkce_s256(verifier), code.code_challenge):
                return _oauth_error('invalid_grant', 'PKCE verification failed.')
            if not code.connector_id._is_user_allowed(code.user_id):
                return _oauth_error('invalid_grant', 'This user may no longer use the connector.')
            token, access, refresh, expires = Token._issue(client, code.user_id, code.connector_id, code.scope or 'mcp')
        elif grant == 'refresh_token':
            token = Token._from_refresh_token(params.get('refresh_token'))
            if not token or token.client_id != client:
                return _oauth_error('invalid_grant', 'The refresh token is invalid or expired.')
            if not token.connector_id._is_user_allowed(token.user_id) or token.connector_id.state != 'active':
                token.action_revoke()
                return _oauth_error('invalid_grant', 'This user may no longer use the connector.')
            token, access, refresh, expires = Token._issue(client, token.user_id, token.connector_id,
                                                           token.scope or 'mcp', previous=token)
        else:
            return _oauth_error('unsupported_grant_type', 'Use authorization_code or refresh_token.')
        return _json({
            'access_token': access, 'token_type': 'Bearer', 'expires_in': expires,
            'refresh_token': refresh, 'scope': token.scope or 'mcp',
        })

    @http.route('/oauth/revoke', type='http', auth='none', methods=['POST', 'OPTIONS'], csrf=False,
                save_session=False, readonly=False)
    def revoke(self, **kw):
        if request.httprequest.method == 'OPTIONS':
            return _json({}, 204)
        params = _params()
        client, error = self._authenticate_client(params)
        if error:
            return error
        raw = params.get('token') or ''
        Token = request.env['zivo.mcp.oauth.token'].sudo()
        field = 'refresh_hash' if raw.startswith(REFRESH_PREFIX) else 'access_hash' if raw.startswith(ACCESS_PREFIX) else None
        if field:
            Token.search([(field, '=', token_hash(raw)), ('client_id', '=', client.id)]).action_revoke()
        return _json({})

    # ------------------------------------------------------------------
    @staticmethod
    def _authenticate_client(params):
        client_id, secret = params.get('client_id'), params.get('client_secret')
        auth = request.httprequest.headers.get('Authorization') or ''
        if auth.lower().startswith('basic '):
            try:
                decoded = base64.b64decode(auth[6:].strip()).decode()
                basic_id, _sep, basic_secret = decoded.partition(':')
                from urllib.parse import unquote
                client_id, secret = unquote(basic_id), unquote(basic_secret)
            except (ValueError, UnicodeDecodeError):
                return None, _oauth_error('invalid_client', 'Malformed Basic authentication.', 401)
        client = request.env['zivo.mcp.oauth.client'].sudo().search(
            [('client_id', '=', client_id or ''), ('active', '=', True)], limit=1)
        if not client:
            return None, _oauth_error('invalid_client', 'Unknown client.', 401)
        if not client.is_public and not client._check_secret(secret):
            return None, _oauth_error('invalid_client', 'Client authentication failed.', 401)
        return client, None
