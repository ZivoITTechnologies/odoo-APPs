# -*- coding: utf-8 -*-
from datetime import timedelta
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import Form, tagged

from .common import TenderCommon


@tagged('post_install', '-at_install', 'zivoit_tender')
class TestTenderAgent(TenderCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        T = cls._tender
        cls.t_rob = T(name='Construction of ROB in lieu of LC 45 at Kazipet', organisation='South Central Railway',
                      department='Secunderabad Division', estimated_value=18.5e7)
        cls.t_doubling = T(name='Doubling of track Kazipet - Vijayawada', organisation='South Central Railway',
                           location='Vijayawada', estimated_value=42e7, date_closing=cls.now + timedelta(days=4))
        cls.t_sr = T(name='Construction of FOB at Chennai Egmore', organisation='Southern Railway', location='Chennai',
                     estimated_value=4.2e7)
        cls.t_ts_road = T(name='Widening of Hyderabad - Karimnagar road to four lane', organisation='R&B Department',
                          location='Medchal, Telangana', estimated_value=64e7)
        cls.t_ts_small = T(name='CC road in Siddipet village', organisation='Panchayat Raj Engineering, Telangana',
                           estimated_value=40e5, date_closing=cls.now + timedelta(days=20))
        cls.t_ts_maint = T(name='Annual maintenance of R&B roads in Warangal', organisation='R&B Department Telangana',
                           estimated_value=3e7)
        cls.t_closed = T(name='Construction of RUB near Bhongir', organisation='South Central Railway',
                         date_published=cls.now - timedelta(days=30), date_closing=cls.now - timedelta(days=2))

    def test_profile_from_natural_language(self):
        with Form(self.Profile) as form:
            form.name = 'SCR agent'
            form.nl_query = 'open tenders in railways in construction in SCR'
            self.assertEqual(form.zone_ids[:], self.scr)
            self.assertEqual(form.sector_ids[:], self.sector_rail)
        profile = form.save()
        profile.live_search = False
        profile.notify_mode = 'none'
        matches = self.Tender.search(profile._build_domain())
        self.assertEqual(set(matches.ids), {self.t_rob.id, self.t_doubling.id})
        self.assertNotIn(self.t_sr, matches, 'Southern Railway is not SCR')
        self.assertNotIn(self.t_closed, matches, 'Closed tenders are excluded')

    def test_profile_telangana(self):
        profile = self.Profile.create({'name': 'TS', 'nl_query': 'construction tenders in Telengana', 'live_search': False,
                                       'notify_mode': 'none'})
        profile.action_parse_query()
        self.assertEqual(profile.state_ids, self.ts)
        profile.action_run()
        self.assertTrue({self.t_rob, self.t_ts_road, self.t_ts_small, self.t_ts_maint} <= set(profile.tender_ids))
        self.assertNotIn(self.t_sr, profile.tender_ids)

    def test_value_deadline_exclusion_filters(self):
        profile = self.Profile.create({'name': 'TS roads', 'live_search': False, 'notify_mode': 'none',
                                       'nl_query': 'road tenders in Telangana above 1 crore excluding maintenance'})
        profile.action_parse_query()
        self.assertEqual(profile.min_value, 1e7)
        self.assertEqual(profile.exclude_keywords, 'maintenance')
        profile.action_run()
        self.assertIn(self.t_ts_road, profile.tender_ids)
        self.assertNotIn(self.t_ts_small, profile.tender_ids, 'Below the minimum value')
        self.assertNotIn(self.t_ts_maint, profile.tender_ids, 'Excluded keyword')
        # closing within 5 days
        p2 = self.Profile.create({'name': 'urgent', 'live_search': False, 'notify_mode': 'none', 'deadline_days': 5})
        self.assertEqual(self.Tender.search(p2._build_domain()), self.t_doubling)
        # at least 10 days to prepare
        p3 = self.Profile.create({'name': 'time', 'live_search': False, 'notify_mode': 'none', 'min_days_left': 10,
                                  'sector_ids': [(6, 0, self.sector_rail.ids)]})
        self.assertNotIn(self.t_doubling, self.Tender.search(p3._build_domain()))
        # max EMD / completion
        p4 = self.Profile.create({'name': 'emd', 'live_search': False, 'notify_mode': 'none', 'max_emd': 5e5})
        self.assertFalse(self.Tender.search(p4._build_domain()) & self.t_rob)
        # keywords search across fields
        p5 = self.Profile.create({'name': 'kw', 'live_search': False, 'notify_mode': 'none', 'keywords': 'ROB, doubling'})
        self.assertEqual(set(self.Tender.search(p5._build_domain()).ids), {self.t_rob.id, self.t_doubling.id})

    def test_run_scores_notifies_and_auto_shortlists(self):
        profile = self.Profile.create({
            'name': 'SCR', 'live_search': False, 'notify_mode': 'both', 'zone_ids': [(6, 0, self.scr.ids)],
            'sector_ids': [(6, 0, self.sector_rail.ids)], 'keywords': 'rob', 'auto_shortlist_score': 70,
            'auto_assign_user_id': self.executive.id, 'auto_tag_ids': [(6, 0, self.env.ref('zivoit_tender_ai.tag_hot').ids)],
            'notify_user_ids': [(6, 0, [self.manager.id])],
        })
        action = profile.action_run()
        self.assertEqual(action['res_model'], 'zivoit.tender')
        self.assertEqual(profile.last_new_count, 1)
        self.assertEqual(self.t_rob.profile_ids, profile)
        self.assertGreaterEqual(self.t_rob.relevance_score, 70)
        self.assertIn('Keywords: rob', self.t_rob.match_reason)
        self.assertEqual(self.t_rob.stage_type, 'shortlist', 'Auto-shortlisted at score >= 70')
        self.assertTrue(self.t_rob.checklist_line_ids, 'Default checklist applied on shortlist')
        self.assertEqual(self.t_rob.user_id, self.executive)
        self.assertIn(self.env.ref('zivoit_tender_ai.tag_hot'), self.t_rob.tag_ids)
        self.assertIn(self.manager.partner_id, self.t_rob.message_partner_ids)
        alert = profile.message_ids.filtered(lambda m: '1 new tender' in (m.body or ''))
        self.assertTrue(alert)
        self.assertIn(self.manager.partner_id, alert.partner_ids)
        # running again does not re-alert
        count = len(profile.message_ids)
        profile.action_run()
        self.assertEqual(profile.last_new_count, 0)
        self.assertEqual(len(profile.message_ids), count)

    def test_live_search_uses_sources(self):
        self.Source.search([]).write({'active': False})
        source = self.Source.create({'name': 'feed', 'source_type': 'json_api', 'url': 'https://api.x/t?q={query}'})
        profile = self.Profile.create({'name': 'live', 'nl_query': 'bridge tenders in SCR', 'notify_mode': 'none'})
        profile.action_parse_query()
        rows = [{'title': 'Construction of major bridge No. 112 between Kazipet and Warangal',
                 'organisation': 'South Central Railway', 'closing_date': '2030-12-01'}]
        with patch.object(type(source), '_fetch_json_api', return_value=rows) as fetch:
            profile.action_run()
        self.assertEqual(fetch.call_args.args[0], 'bridge tenders in SCR')
        self.assertTrue(profile.tender_ids.filtered(lambda t: 'major bridge' in t.name))
        self.assertEqual(profile.log_ids[:1].source_id, source)

    def test_digest_email(self):
        profile = self.Profile.create({'name': 'Digest', 'live_search': False, 'notify_mode': 'digest',
                                       'keywords': 'doubling', 'notify_user_ids': [(6, 0, [self.manager.id])]})
        profile.action_run()
        self.assertFalse(profile.message_ids.filtered(lambda m: 'new tender' in (m.body or '')),
                         'Digest-only agents do not send instant alerts')
        digest = profile._get_digest_tenders()
        self.assertEqual(digest['new'], self.t_doubling)
        self.assertEqual(digest['closing'], self.env['zivoit.tender'], 'Closes in 4 days, not within 3')
        self.env['mail.mail'].search([]).unlink()
        self.Profile._cron_send_digests()
        mail = self.env['mail.mail'].search([('email_to', '=', 'mgr@example.com')])
        self.assertEqual(len(mail), 1)
        self.assertIn('Doubling of track', mail.body_html)
        self.assertIn('Tender digest - Digest - 1 new', mail.subject)

    def test_cron_runs_due_agents_only(self):
        due = self.Profile.create({'name': 'due', 'live_search': False, 'notify_mode': 'none', 'frequency': 'daily',
                                   'keywords': 'widening'})
        manual = self.Profile.create({'name': 'manual', 'live_search': False, 'notify_mode': 'none',
                                      'frequency': 'manual', 'keywords': 'widening'})
        recent = self.Profile.create({'name': 'recent', 'live_search': False, 'notify_mode': 'none',
                                      'frequency': 'weekly', 'keywords': 'widening', 'last_run': self.now})
        self.Profile._cron_run_agents()
        self.assertEqual(due.tender_ids, self.t_ts_road)
        self.assertFalse(manual.tender_ids)
        self.assertFalse(recent.tender_ids)
        self.assertTrue(due.next_run > self.now)

    def test_agent_search_api(self):
        result = self.Tender.agent_search('open tenders in railways in construction in SCR')
        ids = [r['id'] for r in result['results']]
        self.assertEqual(set(ids), {self.t_rob.id, self.t_doubling.id})
        self.assertEqual(result['filters']['zones'], ['SCR'])
        self.assertEqual(result['filters']['sector_labels'], ['Railways (Civil & Track)'])
        self.assertTrue(all(r['score'] > 0 for r in result['results']))
        self.assertFalse(result['live']['ran'])
        result = self.Tender.agent_search('road tenders in Telangana above 10 crore')
        self.assertEqual([r['id'] for r in result['results']], [self.t_ts_road.id])
        self.assertEqual(result['results'][0]['value'], '64.00 Cr')

    def test_agent_wizard(self):
        wizard = self.env['zivoit.tender.agent.search'].create({'query': 'tenders in SCR', 'live': False})
        wizard.action_search()
        self.assertEqual(wizard.state, 'done')
        self.assertEqual(set(wizard.tender_ids.ids), {self.t_rob.id, self.t_doubling.id})
        self.assertIn('Zones: SCR', wizard.parsed)
        self.assertIn('Sectors: Railways (Civil & Track)', wizard.parsed)
        action = wizard.action_open_results()
        self.assertEqual(action['domain'], [('id', 'in', wizard.tender_ids.ids)])
        action = wizard.action_save_agent()
        profile = self.Profile.browse(action['res_id'])
        self.assertEqual(profile.zone_ids, self.scr)
        self.assertEqual(profile.tender_ids, wizard.tender_ids)
        wizard.action_shortlist_selected()
        self.assertEqual(set(wizard.tender_ids.mapped('stage_type')), {'shortlist'})

    def test_dashboard_data(self):
        data = self.Tender.get_dashboard_data()
        self.assertEqual(data['kpi']['open'], 6)
        self.assertEqual(data['kpi']['closing_week'], 1)
        self.assertTrue(data['by_sector'])
        self.assertTrue(any(row['name'] == 'Telangana' for row in data['by_state']))
        self.assertEqual(data['closing'][0]['id'], self.t_doubling.id)
        self.assertFalse(data['ai_enabled'])

    def test_agent_security(self):
        private = self.Profile.with_user(self.manager).create({'name': 'Private', 'shared': False, 'live_search': False})
        shared = self.Profile.with_user(self.manager).create({'name': 'Shared', 'shared': True, 'live_search': False})
        visible = self.Profile.with_user(self.executive).search([('id', 'in', (private | shared).ids)])
        self.assertEqual(visible, shared)
        with self.assertRaises(AccessError):
            shared.with_user(self.executive).write({'name': 'hijack'})
        own = self.Profile.with_user(self.executive).create({'name': 'Mine', 'live_search': False})
        own.with_user(self.executive).write({'keywords': 'rob'})
        # executives cannot touch sources / settings-level config
        with self.assertRaises(AccessError):
            self.Source.with_user(self.executive).create({'name': 'x', 'source_type': 'rss', 'url': 'https://x'})
        with self.assertRaises(AccessError):
            self.t_rob.with_user(self.executive).unlink()
