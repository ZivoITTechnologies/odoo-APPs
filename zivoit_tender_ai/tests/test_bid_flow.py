# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo.exceptions import UserError, ValidationError
from odoo.tests import Form, tagged

from .common import TenderCommon


@tagged('post_install', '-at_install', 'zivoit_tender')
class TestBidFlow(TenderCommon):

    def _boq(self, tender):
        tender.boq_line_ids = [
            (0, 0, {'display_type': 'line_section', 'name': 'Substructure'}),
            (0, 0, {'item_code': '2.1', 'name': 'Earthwork', 'uom': 'Cum', 'quantity': 1000,
                    'estimate_rate': 400, 'cost_rate': 300}),
            (0, 0, {'item_code': '5.4', 'name': 'RCC M30', 'uom': 'Cum', 'quantity': 100,
                    'estimate_rate': 10000, 'material_rate': 6000, 'labour_rate': 1500, 'equipment_rate': 500}),
        ]

    def test_full_lifecycle_won(self):
        self.env['ir.config_parameter'].sudo().set_param('zivoit_tender_ai.auto_lead_on_shortlist', 'True')
        tender = self._tender(user_id=self.manager.id, estimated_value=15e5 * 1.0)
        tender.action_review()
        self.assertEqual(tender.stage_type, 'review')
        tender.action_shortlist()
        self.assertEqual(tender.stage_type, 'shortlist')
        self.assertTrue(tender.shortlisted_date)
        self.assertTrue(tender.lead_id, 'Opportunity created on shortlist when enabled')
        self.assertEqual(tender.lead_id.tender_id, tender)
        self.assertEqual(tender.partner_id.name, 'South Central Railway')
        self.assertTrue(tender.activity_ids.filtered(lambda a: a.user_id == self.manager))
        self.assertTrue(tender.message_ids.filtered(lambda m: 'has been shortlisted' in (m.body or '')),
                        'Stage mail template posted on shortlist')
        # checklist from template, scored
        self.assertTrue(tender.checklist_line_ids)
        self.assertEqual(tender.go_decision, 'pending')
        tender.checklist_line_ids.write({'status': 'yes'})
        self.assertEqual((tender.go_score, tender.go_decision), (100, 'go'))
        tender.action_prepare_bid()
        self.assertEqual(tender.stage_type, 'prepare')
        with self.assertRaises(UserError):
            tender.action_submit_bid()  # no price yet
        self._boq(tender)
        # cost = 1000*300 + 100*8000 = 1,100,000 ; estimate = 400,000 + 1,000,000
        self.assertAlmostEqual(tender.boq_cost, 1100000)
        self.assertAlmostEqual(tender.boq_estimate_amount, 1400000)
        loaded = 1100000 * 1.10   # 8% overhead + 2% contingency
        self.assertAlmostEqual(tender.quoted_amount, loaded * 1.10, places=2)  # 10% margin
        self.assertAlmostEqual(tender.bid_amount, tender.quoted_amount)
        self.assertAlmostEqual(tender.expected_margin_amount, loaded * 0.10, places=2)
        self.assertAlmostEqual(tender.quote_vs_estimate_pct, (1331000 - 1500000) / 1500000 * 100, places=2)
        line = tender.boq_line_ids.filtered(lambda l: l.item_code == '5.4')
        self.assertAlmostEqual(line.quoted_rate, 8000 * 1.1 * 1.1, places=2)
        tender.final_quoted_amount = 1300000
        self.assertEqual(tender.bid_amount, 1300000)
        tender.action_submit_bid()
        self.assertEqual(tender.stage_type, 'submitted')
        self.assertEqual(tender.emd_status, 'paid')
        tender.action_evaluation()
        wizard = Form(self.env['zivoit.tender.result.wizard'].with_context(
            **tender.action_open_result_wizard()['context']))
        wizard.outcome = 'won'
        self.assertEqual(wizard.awarded_value, 1300000)
        action = wizard.save().action_confirm()
        self.assertEqual(tender.stage_type, 'won')
        self.assertEqual(tender.awarded_value, 1300000)
        self.assertEqual(tender.emd_status, 'adjusted')
        self.assertEqual(tender.lead_id.won_status, 'won')
        project = tender.project_id
        self.assertTrue(project)
        self.assertEqual(action['res_id'], project.id)
        self.assertEqual(project.tender_id, tender)
        self.assertEqual(project.contract_value, 1300000)
        self.assertEqual(len(project.task_ids), 2, 'One task per BOQ item (sections skipped)')
        self.assertTrue(all(tender.boq_line_ids.filtered(lambda l: not l.display_type).mapped('task_id')))

    def test_won_without_project(self):
        tender = self._tender()
        tender.stage_id = tender._stage('submitted')
        wizard = self.env['zivoit.tender.result.wizard'].create({'tender_id': tender.id, 'outcome': 'won',
                                                                 'create_project': False})
        wizard.action_confirm()
        self.assertEqual(tender.stage_type, 'won')
        self.assertFalse(tender.project_id)
        tender.action_create_project()
        self.assertTrue(tender.project_id)

    def test_lost_and_competitors(self):
        tender = self._tender(estimated_value=7.2e7, final_quoted_amount=6.98e7, emd_status='paid')
        tender.action_create_opportunity()
        tender.stage_id = tender._stage('evaluation')
        tender.competitor_ids = [
            (0, 0, {'name': 'A Infra', 'quoted_amount': 6.625e7}),
            (0, 0, {'name': 'B Constructions', 'quoted_amount': 6.81e7}),
            (0, 0, {'name': 'Us', 'quoted_amount': 6.98e7, 'is_us': True}),
            (0, 0, {'name': 'Disqualified', 'quoted_amount': 6.0e7, 'technically_qualified': False}),
        ]
        ranks = {c.name: c.rank for c in tender.competitor_ids}
        self.assertEqual(ranks, {'A Infra': 1, 'B Constructions': 2, 'Us': 3, 'Disqualified': 0})
        self.assertAlmostEqual(tender.competitor_ids.filtered('is_us').pct_vs_estimate, (6.98 - 7.2) / 7.2 * 100, places=2)
        wizard = self.env['zivoit.tender.result.wizard'].create({'tender_id': tender.id, 'outcome': 'lost'})
        with self.assertRaises(UserError):
            wizard.action_confirm()
        wizard.write({'lost_reason_id': self.env.ref('zivoit_tender_ai.lost_reason_price').id, 'our_rank': 3,
                      'l1_bidder': 'A Infra', 'l1_amount': 6.625e7})
        wizard.action_confirm()
        self.assertEqual(tender.stage_type, 'lost')
        self.assertEqual(tender.emd_status, 'refund_due')
        self.assertAlmostEqual(tender.gap_to_l1_pct, (6.98 - 6.625) / 6.625 * 100, places=2)
        self.assertFalse(tender.lead_id.active, 'Opportunity archived when lost')
        self.assertFalse(tender.is_open)
        tender.action_reopen()
        self.assertEqual(tender.stage_type, 'review')

    def test_mandatory_criterion_blocks_bid(self):
        tender = self._tender()
        tender.action_shortlist()
        mandatory = tender.checklist_line_ids.filtered('mandatory')[:1]
        tender.checklist_line_ids.write({'status': 'yes'})
        mandatory.status = 'no'
        self.assertTrue(tender.mandatory_failed)
        self.assertEqual(tender.go_decision, 'no_go')
        with self.assertRaises(UserError):
            tender.action_prepare_bid()
        mandatory.status = 'partial'
        self.assertIn(tender.go_decision, ('go', 'review'))
        tender.action_prepare_bid()

    def test_railway_checklist_template_selected_by_sector(self):
        tender = self._tender(sector_ids=[(6, 0, self.sector_rail.ids)])
        tender.action_shortlist()
        self.assertTrue(any('IREPS' in n for n in tender.checklist_line_ids.mapped('name')))
        n = len(tender.checklist_line_ids)
        tender.action_apply_checklist()
        self.assertEqual(len(tender.checklist_line_ids), n, 'Re-applying does not duplicate criteria')

    def test_drop_requires_reason(self):
        tender = self._tender()
        ctx = tender.action_drop()['context']
        self.assertEqual(ctx['default_outcome'], 'dropped')
        wizard = self.env['zivoit.tender.result.wizard'].create({
            'tender_id': tender.id, 'outcome': 'dropped',
            'lost_reason_id': self.env.ref('zivoit_tender_ai.lost_reason_eligibility').id})
        wizard.action_confirm()
        self.assertEqual(tender.stage_type, 'dropped')
        self.assertEqual(tender.lost_reason_id, self.env.ref('zivoit_tender_ai.lost_reason_eligibility'))

    def test_corrigendum_extends_deadline(self):
        tender = self._tender(user_id=self.manager.id, last_reminder_days=3)
        new_date = self.now + timedelta(days=30)
        self.env['zivoit.tender.corrigendum'].create({'tender_id': tender.id, 'name': 'Corr-1',
                                                      'description': 'Date extended', 'new_date_closing': new_date,
                                                      'new_estimated_value': 19e7})
        self.assertEqual(tender.date_closing, new_date.replace(microsecond=0))
        self.assertEqual(tender.estimated_value, 19e7)
        self.assertEqual(tender.last_reminder_days, 0)
        self.assertEqual(tender.corrigendum_count, 1)
        self.assertIn('Corrigendum Corr-1', tender.message_ids[0].body)

    def test_date_validation(self):
        with self.assertRaises(ValidationError):
            self._tender(date_published=self.now, date_closing=self.now - timedelta(days=1))

    def test_deadline_reminders(self):
        tender = self._tender(user_id=self.manager.id, date_closing=self.now + timedelta(days=2, hours=12))
        tender.action_shortlist()
        tender.activity_ids.unlink()
        self.Tender._cron_deadline_reminders()
        self.assertEqual(tender.last_reminder_days, 3)
        self.assertTrue(tender.activity_ids)
        self.assertIn('Deadline reminder', tender.message_ids[0].body)
        self.assertIn(self.manager.partner_id, tender.message_ids[0].partner_ids)
        count = len(tender.message_ids)
        self.Tender._cron_deadline_reminders()
        self.assertEqual(len(tender.message_ids), count, 'Same threshold is not reminded twice')
        # new tenders (not in the bid pipeline) are not reminded
        other = self._tender(date_closing=self.now + timedelta(days=1))
        self.Tender._cron_deadline_reminders()
        self.assertFalse(other.last_reminder_days)

    def test_expire_missed_tenders(self):
        missed = self._tender(date_closing=self.now + timedelta(days=1))
        prepared = self._tender(name='Other', date_closing=self.now + timedelta(days=1))
        prepared.stage_id = prepared._stage('prepare')
        self.env.cr.execute("UPDATE zivoit_tender SET date_closing = now() at time zone 'utc' - interval '1 hour' "
                            "WHERE id IN %s", [(missed.id, prepared.id)])
        (missed | prepared).invalidate_recordset(['date_closing'])
        self.Tender._cron_refresh_time_flags()
        self.assertEqual(missed.stage_type, 'expired')
        self.assertEqual(prepared.stage_type, 'prepare', 'Tenders in preparation are not auto-expired')
        self.assertEqual(prepared.deadline_bucket, 'closed')
        self.assertFalse(prepared.is_open)

    def test_emd_followup(self):
        tender = self._tender(user_id=self.manager.id, emd_status='refund_due')
        bg = self._tender(name='BG', user_id=self.manager.id, emd_status='paid',
                          emd_valid_until=self.now.date() + timedelta(days=5))
        self.Tender._cron_emd_followup()
        self.assertTrue(tender.activity_ids.filtered(lambda a: 'EMD refund' in a.summary))
        self.assertTrue(bg.activity_ids.filtered(lambda a: 'BG' in a.summary))
        self.Tender._cron_emd_followup()
        self.assertEqual(len(tender.activity_ids), 1, 'No duplicate follow-ups')

    def test_report_renders(self):
        tender = self._tender()
        tender.action_shortlist()
        tender.action_analyse()
        html, _fmt = self.env['ir.actions.report']._render_qweb_html(
            'zivoit_tender_ai.action_report_tender_brief', tender.ids)
        self.assertIn(b'Construction of ROB at Kazipet', html)
        self.assertIn(b'Bid / No-Bid', html)

    def test_kanban_stage_group_expand(self):
        groups = self.Tender.with_context(read_group_expand=True).formatted_read_group([], ['stage_id'], ['__count'])
        self.assertEqual(len(groups), self.env['zivoit.tender.stage'].search_count([]))
