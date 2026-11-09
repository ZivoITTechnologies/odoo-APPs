# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlparse

from odoo.tests import HttpCase, new_test_user, tagged

REDIRECT = 'https://claude.ai/api/mcp/auth_callback'


def pkce_pair():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return verifier, challenge


@tagged('post_install', '-at_install', 'zivo_mcp')
class TestOauthFlow(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connector = cls.env.ref('zivoit_mcp_connector.connector_default_oauth')
        cls.member = new_test_user(cls.env, login='ai_member', password='ai_member_pw1',
                                   groups='base.group_user,zivoit_mcp_connector.group_zivo_mcp_user')
        cls.outsider = new_test_user(cls.env, login='ai_outsider', password='ai_outsider_pw1',
                                     groups='base.group_user')

    # ------------------------------------------------------------------ helpers
    def _register(self, **extra):
        body = {'client_name': 'Claude', 'redirect_uris': [REDIRECT], 'token_endpoint_auth_method': 'none',
                'grant_types': ['authorization_code', 'refresh_token']}
        body.update(extra)
        return self.url_open('/oauth/register', data=json.dumps(body), headers={'Content-Type': 'application/json'})

    def _authorize_url(self, client_id, challenge, **extra):
        params = {'response_type': 'code', 'client_id': client_id, 'redirect_uri': REDIRECT, 'state': 'xyz',
                  'code_challenge': challenge, 'code_challenge_method': 'S256', 'scope': 'mcp',
                  'resource': self.base_url() + '/mcp'}
        params.update(extra)
        from urllib.parse import urlencode
        return '/oauth/authorize?' + urlencode(params)

    def _consent(self, client_id, challenge, decision='allow', **extra):
        page = self.url_open(self._authorize_url(client_id, challenge, **extra))
        self.assertEqual(page.status_code, 200, page.text[:500])
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', page.text))
        fields.update({'csrf_token': csrf, 'decision': decision})
        import html
        fields = {k: html.unescape(v) for k, v in fields.items()}
        res = self.url_open('/oauth/authorize', data=fields, allow_redirects=False)
        self.assertIn(res.status_code, (302, 303), res.text[:500])
        location = res.headers['Location']
        self.assertTrue(location.startswith(REDIRECT), location)
        return parse_qs(urlparse(location).query)

    def _token(self, **data):
        return self.url_open('/oauth/token', data=data)

    def _mcp(self, token, method, params=None):
        return self.url_open('/mcp', data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}),
                             headers={'Content-Type': 'application/json', 'Authorization': 'Bearer %s' % token})

    def _full_flow(self, login='ai_member', password='ai_member_pw1'):
        client_id = self._register().json()['client_id']
        self.authenticate(login, password)
        verifier, challenge = pkce_pair()
        query = self._consent(client_id, challenge)
        res = self._token(grant_type='authorization_code', code=query['code'][0], redirect_uri=REDIRECT,
                          client_id=client_id, code_verifier=verifier)
        self.assertEqual(res.status_code, 200, res.text)
        return client_id, res.json()

    # ------------------------------------------------------------------ tests
    def test_discovery(self):
        prm = self.url_open('/.well-known/oauth-protected-resource').json()
        self.assertTrue(prm['resource'].endswith('/mcp'))
        issuer = prm['authorization_servers'][0]
        self.assertEqual(self.url_open('/.well-known/oauth-protected-resource/mcp').json(), prm)
        asm = self.url_open('/.well-known/oauth-authorization-server').json()
        self.assertEqual(asm['issuer'], issuer)
        self.assertEqual(asm['code_challenge_methods_supported'], ['S256'])
        self.assertTrue(asm['registration_endpoint'].endswith('/oauth/register'))
        self.assertTrue(asm['token_endpoint'].endswith('/oauth/token'))
        res = self.url_open('/mcp', data='{}', headers={'Content-Type': 'application/json'})
        self.assertEqual(res.status_code, 401)
        self.assertIn('resource_metadata="%s/.well-known/oauth-protected-resource"' % issuer,
                      res.headers['WWW-Authenticate'])

    def test_registration_validation(self):
        res = self._register()
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.json()['client_id'].startswith('zmcp_'))
        self.assertNotIn('client_secret', res.json())
        self.assertEqual(self._register(redirect_uris=['javascript:alert(1)']).status_code, 400)
        self.assertEqual(self._register(redirect_uris=['http://evil.example.com/cb']).status_code, 400)
        self.assertEqual(self._register(redirect_uris=['http://127.0.0.1:33418/callback']).status_code, 201)
        self.assertEqual(self._register(redirect_uris=['cursor://anysphere.cursor-retrieval/oauth/callback']).status_code, 201)
        confidential = self._register(token_endpoint_auth_method='client_secret_post').json()
        self.assertTrue(confidential['client_secret'])
        self.env['ir.config_parameter'].sudo().set_param('zivoit_mcp.oauth_dcr_disabled', 'True')
        self.assertEqual(self._register().status_code, 403)

    def test_login_required(self):
        client_id = self._register().json()['client_id']
        _verifier, challenge = pkce_pair()
        res = self.url_open(self._authorize_url(client_id, challenge), allow_redirects=False)
        self.assertIn(res.status_code, (302, 303))
        self.assertIn('/web/login', res.headers['Location'])

    def test_full_flow_tools_refresh_revoke(self):
        client_id, tokens = self._full_flow()
        self.assertEqual(tokens['token_type'], 'Bearer')
        access, refresh = tokens['access_token'], tokens['refresh_token']
        # MCP calls run as the signed-in user
        self.assertEqual(self._mcp(access, 'initialize', {'protocolVersion': '2025-06-18'}).status_code, 200)
        names = [t['name'] for t in self._mcp(access, 'tools/list').json()['result']['tools']]
        self.assertIn('ai_tool_search', names)
        ctx = self._mcp(access, 'tools/call', {'name': 'ai_tool_mcp_retrieve_initial_context', 'arguments': {}}).json()
        self.assertEqual(ctx['result']['structuredContent']['user']['login'], 'ai_member')
        res = self._mcp(access, 'tools/call', {'name': 'ai_tool_search', 'arguments': {
            'model_name': 'res.partner', 'domain': '[]', 'limit': 2}}).json()['result']
        self.assertFalse(res['isError'], res)
        token = self.env['zivo.mcp.oauth.token'].search([('user_id', '=', self.member.id)])
        self.assertEqual(len(token), 1)
        self.assertTrue(token.last_used)
        log = self.env['zivo.mcp.log'].search([('user_id', '=', self.member.id), ('tool_name', '=', 'ai_tool_search')])
        self.assertEqual(log.connector_id, self.connector)
        # refresh rotates both tokens
        res = self._token(grant_type='refresh_token', refresh_token=refresh, client_id=client_id)
        self.assertEqual(res.status_code, 200, res.text)
        new = res.json()
        self.assertNotEqual(new['access_token'], access)
        self.assertEqual(self._mcp(access, 'tools/list').status_code, 401)
        self.assertEqual(self._mcp(new['access_token'], 'tools/list').status_code, 200)
        self.assertEqual(self._token(grant_type='refresh_token', refresh_token=refresh, client_id=client_id).json()['error'],
                         'invalid_grant')
        # revoke
        self.assertEqual(self.url_open('/oauth/revoke', data={'token': new['refresh_token'], 'client_id': client_id}).status_code, 200)
        self.assertEqual(self._mcp(new['access_token'], 'tools/list').status_code, 401)

    def test_pkce_and_code_replay(self):
        client_id = self._register().json()['client_id']
        self.authenticate('ai_member', 'ai_member_pw1')
        verifier, challenge = pkce_pair()
        code = self._consent(client_id, challenge)['code'][0]
        bad = self._token(grant_type='authorization_code', code=code, redirect_uri=REDIRECT, client_id=client_id,
                          code_verifier=pkce_pair()[0])
        self.assertEqual(bad.json()['error'], 'invalid_grant')
        # the code is burnt after a failed attempt
        again = self._token(grant_type='authorization_code', code=code, redirect_uri=REDIRECT, client_id=client_id,
                            code_verifier=verifier)
        self.assertEqual(again.json()['error'], 'invalid_grant')
        self.assertEqual(self._token(grant_type='password', client_id=client_id).json()['error'], 'unsupported_grant_type')
        self.assertEqual(self._token(grant_type='authorization_code', client_id='nope').status_code, 401)

    def test_pkce_required_and_deny(self):
        client_id = self._register().json()['client_id']
        self.authenticate('ai_member', 'ai_member_pw1')
        res = self.url_open(self._authorize_url(client_id, 'x', code_challenge_method='plain'), allow_redirects=False)
        self.assertIn('error=invalid_request', res.headers['Location'])
        _v, challenge = pkce_pair()
        query = self._consent(client_id, challenge, decision='deny')
        self.assertEqual(query['error'], ['access_denied'])
        self.assertEqual(query['state'], ['xyz'])
        # unregistered redirect URI is never redirected to
        res = self.url_open(self._authorize_url(client_id, challenge, redirect_uri='https://evil.example.com/cb'),
                            allow_redirects=False)
        self.assertEqual(res.status_code, 400)

    def test_user_without_right(self):
        client_id = self._register().json()['client_id']
        self.authenticate('ai_outsider', 'ai_outsider_pw1')
        _v, challenge = pkce_pair()
        res = self.url_open(self._authorize_url(client_id, challenge), allow_redirects=False)
        self.assertEqual(res.status_code, 400)
        self.assertIn('not allowed to connect AI assistants', res.text)

    def test_confidential_client(self):
        reg = self._register(token_endpoint_auth_method='client_secret_basic').json()
        self.authenticate('ai_member', 'ai_member_pw1')
        verifier, challenge = pkce_pair()
        code = self._consent(reg['client_id'], challenge)['code'][0]
        no_secret = self._token(grant_type='authorization_code', code=code, redirect_uri=REDIRECT,
                                client_id=reg['client_id'], code_verifier=verifier)
        self.assertEqual(no_secret.status_code, 401)
        code = self._consent(reg['client_id'], challenge)['code'][0]
        basic = base64.b64encode(('%s:%s' % (reg['client_id'], reg['client_secret'])).encode()).decode()
        res = self.url_open('/oauth/token', data={'grant_type': 'authorization_code', 'code': code,
                                                  'redirect_uri': REDIRECT, 'code_verifier': verifier},
                            headers={'Authorization': 'Basic %s' % basic})
        self.assertEqual(res.status_code, 200, res.text)

    def test_paused_connector_and_removed_right(self):
        _client_id, tokens = self._full_flow()
        self.connector.action_pause()
        self.assertEqual(self._mcp(tokens['access_token'], 'tools/list').status_code, 403)
        self.connector.action_activate()
        self.assertEqual(self._mcp(tokens['access_token'], 'tools/list').status_code, 200)
        self.member.group_ids = [(3, self.env.ref('zivoit_mcp_connector.group_zivo_mcp_user').id)]
        self.assertEqual(self._mcp(tokens['access_token'], 'tools/list').status_code, 403)

    def test_session_management(self):
        _client_id, tokens = self._full_flow()
        token = self.env['zivo.mcp.oauth.token'].search([('user_id', '=', self.member.id)])
        self.assertTrue(token.active_session)
        self.assertEqual(self.connector.oauth_session_count, 1)
        # a user sees only their own sessions
        own = self.env['zivo.mcp.oauth.token'].with_user(self.member).search([])
        self.assertEqual(own, token)
        token.with_user(self.member).action_revoke()
        self.assertFalse(token.active_session)
        self.assertEqual(self._mcp(tokens['access_token'], 'tools/list').status_code, 401)
