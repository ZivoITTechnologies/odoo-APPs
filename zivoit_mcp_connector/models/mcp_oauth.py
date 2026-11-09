# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
"""Built-in OAuth 2.1 authorization server ("Sign in with Odoo").

Lets Claude (claude.ai, Claude Desktop, Claude Code), ChatGPT, Cursor and any MCP
client that implements the MCP authorization specification connect to /mcp with
the person's own Odoo login: protected-resource metadata (RFC 9728),
authorization-server metadata (RFC 8414), dynamic client registration
(RFC 7591), authorization code + PKCE S256, refresh-token rotation and
revocation (RFC 7009). Only hashes of codes and tokens are stored.
"""
import base64
import hashlib
import hmac
import secrets
from datetime import timedelta
from urllib.parse import urlparse

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

ACCESS_PREFIX = 'zmcp_at_'
REFRESH_PREFIX = 'zmcp_rt_'
CODE_TTL_MINUTES = 10
FORBIDDEN_SCHEMES = {'javascript', 'data', 'file', 'vbscript', 'about', 'blob'}


def token_hash(value):
    return hashlib.sha256((value or '').encode()).hexdigest()


def pkce_s256(verifier):
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


def validate_redirect_uri(uri):
    """Redirect URIs must be https, loopback http, or a native app scheme (cursor://, vscode://)."""
    if not uri or len(uri) > 2000 or any(c in uri for c in ' \n\r\t'):
        return False
    parsed = urlparse(uri)
    scheme = (parsed.scheme or '').lower()
    if not scheme or scheme in FORBIDDEN_SCHEMES or parsed.fragment:
        return False
    if scheme == 'https':
        return bool(parsed.netloc)
    if scheme == 'http':
        return (parsed.hostname or '') in ('localhost', '127.0.0.1', '::1', '[::1]')
    return '.' in scheme or scheme.isalnum()  # private-use URI scheme of a native app


class ZivoMcpOauthClient(models.Model):
    _name = 'zivo.mcp.oauth.client'
    _description = 'AI Connector OAuth Client'
    _order = 'id desc'

    name = fields.Char(string="Application", required=True)
    client_id = fields.Char(string="Client ID", required=True, readonly=True, copy=False, index=True,
                            default=lambda self: 'zmcp_' + secrets.token_urlsafe(18))
    client_secret_hash = fields.Char(copy=False, groups='base.group_system')
    is_public = fields.Boolean(string="Public Client (PKCE only)", default=True,
                               help="Public clients have no secret and must use PKCE (Claude, ChatGPT, Cursor).")
    redirect_uris = fields.Text(string="Redirect URIs", required=True,
                                help="One per line, e.g. https://claude.ai/api/mcp/auth_callback")
    registration = fields.Selection([('dynamic', 'Dynamic registration'), ('manual', 'Created by an administrator')],
                                    default='manual', required=True, readonly=True)
    connector_id = fields.Many2one('zivo.mcp.connector', string="Default Connector", ondelete='set null',
                                   domain="[('auth_mode', '=', 'oauth')]")
    active = fields.Boolean(default=True)
    token_ids = fields.One2many('zivo.mcp.oauth.token', 'client_id', string="Sessions")
    session_count = fields.Integer(compute='_compute_session_count')
    redirect_hosts = fields.Char(compute='_compute_redirect_hosts', string="Redirect Hosts")

    _client_id_uniq = models.Constraint('unique(client_id)', 'The client ID must be unique.')

    def _compute_session_count(self):
        data = dict(self.env['zivo.mcp.oauth.token']._read_group(
            [('client_id', 'in', self.ids), ('active_session', '=', True)], ['client_id'], ['__count']))
        for rec in self:
            rec.session_count = data.get(rec, 0)

    @api.depends('redirect_uris')
    def _compute_redirect_hosts(self):
        for rec in self:
            hosts = []
            for uri in rec._redirect_uri_list():
                parsed = urlparse(uri)
                hosts.append(parsed.netloc or ('%s://' % parsed.scheme))
            rec.redirect_hosts = ', '.join(dict.fromkeys(hosts))

    def _redirect_uri_list(self):
        self.ensure_one()
        return [u.strip() for u in (self.redirect_uris or '').splitlines() if u.strip()]

    @api.constrains('redirect_uris')
    def _check_redirect_uris(self):
        for rec in self:
            uris = rec._redirect_uri_list()
            if not uris:
                raise ValidationError(_("At least one redirect URI is required."))
            for uri in uris:
                if not validate_redirect_uri(uri):
                    raise ValidationError(_("%s is not an allowed redirect URI (use https, http://localhost "
                                            "or an app scheme).", uri))

    def _check_secret(self, secret):
        self.ensure_one()
        stored = self.sudo().client_secret_hash
        return bool(stored and secret and hmac.compare_digest(stored, token_hash(secret)))

    def action_generate_secret(self):
        self.ensure_one()
        secret = secrets.token_urlsafe(32)
        self.sudo().write({'client_secret_hash': token_hash(secret), 'is_public': False})
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('Client secret (copy it now, it is not stored)'), 'message': secret,
            'type': 'warning', 'sticky': True}}

    def action_revoke_sessions(self):
        self.env['zivo.mcp.oauth.token'].sudo().search([('client_id', 'in', self.ids)]).action_revoke()
        return True

    def action_view_sessions(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('zivoit_mcp_connector.action_zivo_mcp_oauth_token')
        action['domain'] = [('client_id', '=', self.id)]
        return action

    @api.model
    def _mcp_register(self, metadata):
        """RFC 7591 dynamic client registration. Returns (client, secret or None)."""
        uris = metadata.get('redirect_uris') or []
        if isinstance(uris, str):
            uris = [uris]
        if not uris or not all(isinstance(u, str) and validate_redirect_uri(u) for u in uris):
            raise ValueError('invalid_redirect_uri')
        method = metadata.get('token_endpoint_auth_method') or 'none'
        if method not in ('none', 'client_secret_post', 'client_secret_basic'):
            raise ValueError('invalid_client_metadata')
        grants = metadata.get('grant_types') or ['authorization_code', 'refresh_token']
        if 'authorization_code' not in grants:
            raise ValueError('invalid_client_metadata')
        name = (metadata.get('client_name') or urlparse(uris[0]).netloc or 'MCP client')[:120]
        client = self.sudo().create({
            'name': name,
            'redirect_uris': '\n'.join(uris),
            'registration': 'dynamic',
            'is_public': method == 'none',
        })
        secret = None
        if method != 'none':
            secret = secrets.token_urlsafe(32)
            client.client_secret_hash = token_hash(secret)
        return client, secret


class ZivoMcpOauthCode(models.Model):
    _name = 'zivo.mcp.oauth.code'
    _description = 'AI Connector OAuth Authorization Code'

    code_hash = fields.Char(required=True, index=True)
    client_id = fields.Many2one('zivo.mcp.oauth.client', required=True, ondelete='cascade')
    user_id = fields.Many2one('res.users', required=True, ondelete='cascade')
    connector_id = fields.Many2one('zivo.mcp.connector', required=True, ondelete='cascade')
    redirect_uri = fields.Char(required=True)
    code_challenge = fields.Char(required=True)
    scope = fields.Char()
    resource = fields.Char()
    expires_at = fields.Datetime(required=True)
    used = fields.Boolean()

    @api.model
    def _issue(self, client, user, connector, redirect_uri, code_challenge, scope, resource):
        code = secrets.token_urlsafe(32)
        self.sudo().create({
            'code_hash': token_hash(code), 'client_id': client.id, 'user_id': user.id,
            'connector_id': connector.id, 'redirect_uri': redirect_uri, 'code_challenge': code_challenge,
            'scope': scope, 'resource': resource,
            'expires_at': fields.Datetime.now() + timedelta(minutes=CODE_TTL_MINUTES),
        })
        return code

    @api.autovacuum
    def _gc_codes(self):
        self.sudo().search([('expires_at', '<', fields.Datetime.now() - timedelta(hours=1))]).unlink()


class ZivoMcpOauthToken(models.Model):
    _name = 'zivo.mcp.oauth.token'
    _description = 'AI Connector OAuth Session'
    _order = 'id desc'
    _rec_name = 'client_id'

    client_id = fields.Many2one('zivo.mcp.oauth.client', string="Application", required=True, ondelete='cascade', index=True)
    user_id = fields.Many2one('res.users', string="Odoo User", required=True, ondelete='cascade', index=True)
    connector_id = fields.Many2one('zivo.mcp.connector', string="Connector", required=True, ondelete='cascade')
    access_hash = fields.Char(index=True, copy=False)
    refresh_hash = fields.Char(index=True, copy=False)
    access_expires = fields.Datetime(string="Access Token Expires")
    refresh_expires = fields.Datetime(string="Session Expires")
    scope = fields.Char()
    revoked = fields.Boolean(index=True)
    last_used = fields.Datetime()
    last_ip = fields.Char(string="Last IP")
    active_session = fields.Boolean(compute='_compute_active_session', search='_search_active_session',
                                    string="Active")

    @api.depends('revoked', 'refresh_expires')
    def _compute_active_session(self):
        now = fields.Datetime.now()
        for rec in self:
            rec.active_session = not rec.revoked and bool(rec.refresh_expires and rec.refresh_expires > now)

    def _search_active_session(self, operator, value):
        if operator not in ('=', '!=') or not isinstance(value, bool):
            raise NotImplementedError()
        positive = [('revoked', '=', False), ('refresh_expires', '>', fields.Datetime.now())]
        if (operator == '=') == value:
            return positive
        return ['|', ('revoked', '=', True), ('refresh_expires', '<=', fields.Datetime.now())]

    @api.model
    def _ttl(self):
        ICP = self.env['ir.config_parameter'].sudo()
        access = int(ICP.get_param('zivoit_mcp.oauth_access_ttl_minutes') or 60)
        refresh = int(ICP.get_param('zivoit_mcp.oauth_refresh_ttl_days') or 30)
        return max(5, access), max(1, refresh)

    @api.model
    def _issue(self, client, user, connector, scope, previous=None):
        access_minutes, refresh_days = self._ttl()
        access = ACCESS_PREFIX + secrets.token_urlsafe(32)
        refresh = REFRESH_PREFIX + secrets.token_urlsafe(40)
        now = fields.Datetime.now()
        vals = {
            'access_hash': token_hash(access), 'refresh_hash': token_hash(refresh),
            'access_expires': now + timedelta(minutes=access_minutes),
            'refresh_expires': now + timedelta(days=refresh_days),
            'scope': scope, 'revoked': False,
        }
        if previous:
            previous.sudo().write(vals)  # rotation: the old refresh token stops working
            token = previous
        else:
            vals.update({'client_id': client.id, 'user_id': user.id, 'connector_id': connector.id})
            token = self.sudo().create(vals)
        return token, access, refresh, access_minutes * 60

    @api.model
    def _from_access_token(self, raw):
        if not raw or not raw.startswith(ACCESS_PREFIX):
            return self.browse()
        token = self.sudo().search([('access_hash', '=', token_hash(raw))], limit=1)
        if not token or token.revoked or not token.access_expires or token.access_expires < fields.Datetime.now():
            return self.browse()
        if not token.client_id.active or not token.user_id.active:
            return self.browse()
        return token

    @api.model
    def _from_refresh_token(self, raw):
        if not raw or not raw.startswith(REFRESH_PREFIX):
            return self.browse()
        token = self.sudo().search([('refresh_hash', '=', token_hash(raw))], limit=1)
        if not token or token.revoked or not token.refresh_expires or token.refresh_expires < fields.Datetime.now():
            return self.browse()
        return token

    def _touch(self, ip):
        """Record usage at most every 5 minutes to avoid write contention on parallel calls."""
        self.ensure_one()
        self.env.cr.execute(
            "UPDATE zivo_mcp_oauth_token SET last_used = now() at time zone 'UTC', last_ip = %s "
            "WHERE id = %s AND (last_used IS NULL OR last_used < (now() at time zone 'UTC') - interval '5 minutes')",
            (ip, self.id))

    def action_revoke(self):
        self.sudo().write({'revoked': True, 'access_hash': False, 'refresh_hash': False})
        return True

    @api.autovacuum
    def _gc_tokens(self):
        limit = fields.Datetime.now() - timedelta(days=30)
        self.sudo().search(['|', '&', ('revoked', '=', True), ('write_date', '<', limit),
                            ('refresh_expires', '<', limit)]).unlink()
