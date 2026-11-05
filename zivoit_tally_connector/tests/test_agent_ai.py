# -*- coding: utf-8 -*-
"""Bridge-agent protocol and LLM-assisted features."""
import json
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import new_test_user, tagged

from ..lib import llm
from .common import TallyCommon


@tagged('post_install', '-at_install', 'zivoit_tally')
class TestTallyAgentAI(TallyCommon):

    def setUp(self):
        super().setUp()
        self.agent_user = new_test_user(self.env, 'tally_agent_bot',
                                        groups='zivoit_tally_connector.group_tally_agent')
        self.conn.write({'transport': 'agent'})
        self.conn.action_generate_agent_token()

    def _agent_round(self, token=None):
        """What zivoit_tally_agent.py does, executed against the simulator."""
        Conn = self.env['tally.connection'].with_user(self.agent_user)
        res = Conn.agent_poll(token or self.conn.agent_token, hostname='TALLY-PC',
                              version='test', tally_ok=True)
        results = [{'id': j['id'], 'ok': True, 'response': self.sim.handle(j['xml'])}
                   for j in res['jobs']]
        if results:
            Conn.agent_submit(token or self.conn.agent_token, results)
        return res

    def test_01_agent_heartbeat_and_security(self):
        with self.assertRaises(AccessError):
            self.env['tally.connection'].with_user(self.agent_user).agent_poll('bad-token')
        intruder = new_test_user(self.env, 'not_agent', groups='base.group_user')
        with self.assertRaises(AccessError):
            self.env['tally.connection'].with_user(intruder).agent_poll(self.conn.agent_token)
        res = self._agent_round()
        self.assertEqual(res['company'], 'Zivo Demo Pvt Ltd')
        self.assertTrue(self.conn.agent_online)
        self.assertEqual(self.conn.agent_hostname, 'TALLY-PC')

    def test_02_agent_push_masters_before_vouchers(self):
        inv = self.make_invoice()
        # first round: pulls + masters only (vouchers wait for masters)
        first = self._agent_round()
        purposes = {self.env['tally.request'].browse(j['id']).purpose for j in first['jobs']}
        self.assertNotIn('push_move', purposes)
        for _i in range(4):
            self._agent_round()
        self.assertEqual(inv.tally_state, 'synced', inv.tally_error)
        self.assertTrue([v for v in self.sim.company.vouchers.values() if v['number'] == inv.name])

    def test_03_agent_reports_tally_down_then_retries(self):
        inv = self.make_invoice()
        Conn = self.env['tally.connection'].with_user(self.agent_user)
        res = Conn.agent_poll(self.conn.agent_token, tally_ok=False)
        Conn.agent_submit(self.conn.agent_token, [
            {'id': j['id'], 'ok': False, 'error': 'TallyPrime is not listening'}
            for j in res['jobs']])
        self.assertTrue(self.requests(state='pending').filtered(lambda r: r.attempts == 1))
        self.requests().write({'next_attempt': '2000-01-01 00:00:00'})
        for _i in range(5):
            self._agent_round()
            self.requests(state='pending').write({'next_attempt': '2000-01-01 00:00:00'})
        self.assertEqual(inv.tally_state, 'synced', inv.tally_error)

    def test_04_stale_jobs_are_reclaimed(self):
        self.make_invoice()
        Conn = self.env['tally.connection'].with_user(self.agent_user)
        res = Conn.agent_poll(self.conn.agent_token)
        self.assertTrue(res['jobs'])
        self.env['tally.request'].browse([j['id'] for j in res['jobs']]).write(
            {'sent_at': '2000-01-01 00:00:00'})
        again = Conn.agent_poll(self.conn.agent_token)
        self.assertEqual({j['id'] for j in again['jobs']} & {j['id'] for j in res['jobs']},
                         {j['id'] for j in res['jobs']})

    def test_05_agent_package_builds(self):
        wiz = self.env['tally.agent.setup'].create({'connection_id': self.conn.id,
                                                    'api_user_id': self.agent_user.id})
        wiz.action_build()
        import base64
        import io
        import zipfile
        zf = zipfile.ZipFile(io.BytesIO(base64.b64decode(wiz.file)))
        self.assertIn('zivoit_tally_agent.py', zf.namelist())
        cfg = json.loads(zf.read('agent_config.json'))
        self.assertEqual(cfg['connection_token'], self.conn.agent_token)

    # ---------------------------------------------------------------- AI
    def test_06_llm_disambiguates_ambiguous_matches(self):
        self.conn.write({'transport': 'direct', 'ai_provider': 'anthropic',
                         'ai_api_key': 'test-key'})
        self.env['res.partner'].create({'name': 'Krishna Agencies Hyderabad', 'is_company': True})
        self.env['res.partner'].create({'name': 'Krishna Agency Warangal', 'is_company': True})
        self.sim.add_ledger('Krishna Agencies (Hyd)', 'Sundry Debtors')
        prompts = []

        def fake_complete(provider, key, model, prompt, **kw):
            prompts.append(prompt)
            payload = json.loads(prompt[prompt.index('['):])
            out = []
            for row in payload:
                idx = next((i for i, c in enumerate(row['candidates']) if 'Hyderabad' in c), None)
                out.append({'id': row['id'], 'choice': idx, 'confidence': 0.95,
                            'reason': 'Hyd = Hyderabad'})
            return json.dumps(out)
        with patch.object(llm, 'complete', fake_complete):
            self.conn.action_pull_now()
        m = self.env['tally.mapping']._find(self.conn, 'party', tally_name='Krishna Agencies (Hyd)')
        self.assertTrue(prompts, 'LLM must be consulted for the ambiguous middle band')
        self.assertEqual(m.partner_id.name, 'Krishna Agencies Hyderabad')
        self.assertEqual(m.state, 'confirmed')
        self.assertIn('AI:', m.match_reason)
        self.assertNotIn('test-key', ''.join(prompts))

    def test_07_ai_error_explanation_redacts_amounts(self):
        self.conn.write({'transport': 'direct', 'ai_provider': 'openai', 'ai_model': 'x',
                         'ai_api_key': 'k', 'auto_heal': False})
        seen = []

        def fake_complete(provider, key, model, prompt, **kw):
            seen.append(prompt)
            return 'Root cause: the voucher type is missing. Fix: create it in Tally.'
        self.sim.company.vchtypes.pop('Sales')
        with patch.object(llm, 'complete', fake_complete):
            inv = self.make_invoice()
            self.run_queue()
            self.assertEqual(inv.tally_state, 'error')
            req = self.requests('push_move', 'failed')
            self.assertEqual(req.error_code, 'missing_vchtype')
            self.assertIn('Voucher type', req.ai_hint)   # deterministic doctor answered first
            req.action_ai_explain()
        self.assertIn('Root cause', req.ai_hint)
        self.assertTrue(seen)
        self.assertNotIn('1180.00', seen[-1], 'amounts must be redacted when not allowed')

    def test_08_health_summary_without_llm(self):
        self.conn.transport = 'direct'
        self.make_invoice()
        self.run_queue()
        self.conn.action_ai_health()
        self.assertIn('successful', str(self.conn.ai_summary))
