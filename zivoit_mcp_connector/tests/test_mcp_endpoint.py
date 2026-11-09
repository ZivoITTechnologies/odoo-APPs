# -*- coding: utf-8 -*-
# Part of ZivoIT Technologies. See LICENSE file for full copyright and licensing details.
import json

from odoo.tests import HttpCase, tagged

from .common import ZivoMcpSetupMixin, accounting_installed


@tagged('post_install', '-at_install', 'zivo_mcp')
class TestMcpEndpoint(ZivoMcpSetupMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_connector()

    def _post(self, payload, key='__default__', headers=None, url='/mcp'):
        hdrs = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
        if key == '__default__':
            key = self.key
        if key:
            hdrs['Authorization'] = 'Bearer %s' % key
        hdrs.update(headers or {})
        return self.url_open(url, data=json.dumps(payload), headers=hdrs)

    def _rpc(self, method, params=None, msg_id=1, **kw):
        res = self._post({'jsonrpc': '2.0', 'id': msg_id, 'method': method, 'params': params or {}}, **kw)
        return res

    def test_authentication(self):
        self.assertEqual(self._rpc('tools/list', key=None).status_code, 401)
        res = self._rpc('tools/list', key='0' * 40)
        self.assertEqual(res.status_code, 401)
        self.assertIn('Bearer', res.headers.get('WWW-Authenticate', ''))
        self.assertIn('resource_metadata=', self._rpc('tools/list', key=None).headers.get('WWW-Authenticate', ''))
        self.assertTrue(self.env['zivo.mcp.log'].search([('status', '=', 'auth_failed')]))

    def test_initialize_and_lifecycle(self):
        res = self._rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                       'clientInfo': {'name': 'test', 'version': '1'}})
        self.assertEqual(res.status_code, 200)
        result = res.json()['result']
        self.assertEqual(result['protocolVersion'], '2025-06-18')
        self.assertIn('tools', result['capabilities'])
        self.assertIn('Test Entity', result['instructions'])
        # unknown version -> latest supported
        res = self._rpc('initialize', {'protocolVersion': '1999-01-01'})
        self.assertEqual(res.json()['result']['protocolVersion'], '2025-11-25')
        # notification -> 202 without body
        res = self._post({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.assertEqual(res.status_code, 202)
        self.assertEqual(self._rpc('ping').json()['result'], {})
        self.assertEqual(self._rpc('does/not/exist').json()['error']['code'], -32601)

    def test_get_and_parse_errors(self):
        self.assertEqual(self.url_open('/mcp').status_code, 405)
        res = self.url_open('/mcp', data='not json', headers={'Authorization': 'Bearer %s' % self.key,
                                                              'Content-Type': 'application/json'})
        self.assertEqual(res.json()['error']['code'], -32700)

    def test_tools_list_and_call(self):
        tools = {t['name']: t for t in self._rpc('tools/list').json()['result']['tools']}
        self.assertIn('ai_tool_search', tools)
        self.assertEqual('zivo_profit_and_loss' in tools, accounting_installed())  # only when the app is installed
        self.assertNotIn('ai_tool_create_records', tools)  # read-only connector
        self.assertTrue(tools['ai_tool_search']['annotations']['readOnlyHint'])
        self.env['res.partner'].create({'name': 'MCP Endpoint Partner'})
        res = self._rpc('tools/call', {'name': 'ai_tool_search', 'arguments': {
            'model_name': 'res.partner', 'domain': '[["name", "=", "MCP Endpoint Partner"]]',
            'fields': ['name']}}).json()['result']
        self.assertFalse(res['isError'])
        self.assertEqual(res['structuredContent']['total_count'], 1)
        self.assertEqual(json.loads(res['content'][0]['text']), res['structuredContent'])
        # a tool error is reported to the assistant, not as a protocol error
        res = self._rpc('tools/call', {'name': 'ai_tool_search', 'arguments': {
            'model_name': 'res.users.apikeys', 'domain': '[]'}}).json()['result']
        self.assertTrue(res['isError'])
        # disabled / unknown tool is a protocol error
        err = self._rpc('tools/call', {'name': 'ai_tool_create_records', 'arguments': {}}).json()['error']
        self.assertEqual(err['code'], -32602)
        log = self.env['zivo.mcp.log'].search([('tool_name', '=', 'ai_tool_search')], limit=1, order='id asc')
        self.assertEqual(log.status, 'success')
        self.assertEqual(log.connector_id, self.connector)

    def test_write_tools_visible_in_read_write_mode(self):
        self.connector.access_mode = 'read_write'
        self.connector.tool_ids |= self.env.ref('zivoit_mcp_connector.tool_ai_tool_create_records')
        names = [t['name'] for t in self._rpc('tools/list').json()['result']['tools']]
        self.assertIn('ai_tool_create_records', names)

    def test_batch_and_prompts(self):
        res = self._post([
            {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'prompts/list'},
        ]).json()
        self.assertEqual(len(res), 2)
        prompts = res[1]['result']['prompts']
        self.assertIn('overdue_payables', [p['name'] for p in prompts])
        got = self._rpc('prompts/get', {'name': 'overdue_payables', 'arguments': {'days': '60'}}).json()['result']
        self.assertIn('min_days_overdue=60', got['messages'][0]['content']['text'])
        err = self._rpc('prompts/get', {'name': 'pl_comparison', 'arguments': {}}).json()['error']
        self.assertEqual(err['code'], -32602)

    def test_connector_controls(self):
        self.connector.action_pause()
        self.assertEqual(self._rpc('tools/list').status_code, 403)
        self.connector.action_activate()
        self.connector.allowed_ips = '10.99.0.0/16'
        self.assertEqual(self._rpc('tools/list').status_code, 403)
        self.connector.allowed_ips = False
        res = self._rpc('tools/list', headers={'Origin': 'https://evil.example.com'})
        self.assertEqual(res.status_code, 403)
        self.connector.rate_limit = 2
        statuses = [self._rpc('tools/list', msg_id=i).status_code for i in range(4)]
        self.assertIn(429, statuses)

    def test_url_key(self):
        self.assertEqual(self._rpc('tools/list', key=None, url='/zivoit/mcp/%s' % self.key).status_code, 403)
        self.connector.allow_url_key = True
        self.assertEqual(self._rpc('tools/list', key=None, url='/zivoit/mcp/%s' % self.key).status_code, 200)

    def test_admin_cannot_be_integration_user(self):
        from odoo.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.env['zivo.mcp.connector'].create({'name': 'Admin', 'auth_mode': 'api_key',
                                                   'user_id': self.env.ref('base.user_admin').id})

    def test_dashboard_and_test_connection(self):
        data = self.env['zivo.mcp.dashboard'].get_dashboard_data()
        self.assertIn('kpis', data)
        action = self.connector.action_test_connection()
        self.assertEqual(action['params']['type'], 'success', action['params']['message'])

    def test_crm_pipeline_tool(self):
        if 'crm.lead' not in self.env:
            self.skipTest('CRM is not installed')
        from odoo.addons.zivoit_mcp_connector.services import finance_tools
        self.integration_user.group_ids = [(4, self.env.ref('sales_team.group_sale_salesman_all_leads').id)]
        self.connector.restrict_models = False
        self.env['crm.lead'].create([
            {'name': 'Big deal', 'type': 'opportunity', 'expected_revenue': 50000, 'probability': 40},
            {'name': 'Small deal', 'type': 'opportunity', 'expected_revenue': 1000, 'probability': 90},
        ])
        res = finance_tools.crm_pipeline(self._ctx(), {'group_by': 'stage'})['entities'][0]
        self.assertGreaterEqual(res['pipeline_expected'], 51000)
        self.assertIn('Big deal', [d['opportunity'] for d in res['top_open_deals']])
        tools = [t['name'] for t in self._rpc('tools/list').json()['result']['tools']]
        self.assertIn('zivo_crm_pipeline', tools)
