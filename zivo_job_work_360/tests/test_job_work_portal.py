# -*- coding: utf-8 -*-
import re

from odoo.tests import HttpCase, new_test_user, tagged

from .common import JobWorkCommon


@tagged('post_install', '-at_install', 'job_work')
class TestJobWorkPortal(HttpCase, JobWorkCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.job_type.quality_required = False
        cls.portal_user = new_test_user(cls.env, login='abc_portal', password='abc_portal_pwd',
                                        groups='base.group_portal', name='ABC Portal')
        cls.portal_user.partner_id.parent_id = cls.vendor
        cls.other_portal = new_test_user(cls.env, login='xyz_portal', password='xyz_portal_pwd',
                                         groups='base.group_portal', name='XYZ Portal')
        cls.other_portal.partner_id.parent_id = cls.vendor2

    def _csrf(self, url):
        res = self.url_open(url)
        self.assertEqual(res.status_code, 200)
        match = re.search(r'name="csrf_token" value="([^"]+)"', res.text)
        self.assertTrue(match, "csrf token present in form")
        return res, match.group(1)

    def test_01_portal_flow_and_isolation(self):
        job = self._create_job(qty=10.0, material_qty=10.0)
        job.action_plan()
        other_job = self._create_job(qty=5.0, material_qty=5.0, partner=self.vendor2)
        other_job.action_plan()
        draft_job = self._create_job(qty=5.0, material_qty=5.0)

        # isolation at ORM level
        Job = self.env['zivo.job.work'].with_user(self.portal_user)
        visible = Job.search([])
        self.assertIn(job, visible)
        self.assertNotIn(other_job, visible)
        self.assertNotIn(draft_job, visible, "draft jobs are not exposed to job workers")

        self.authenticate('abc_portal', 'abc_portal_pwd')
        res = self.url_open('/my/job-works?filterby=all')
        self.assertEqual(res.status_code, 200)
        self.assertIn(job.name, res.text)
        self.assertNotIn(other_job.name, res.text)
        self.assertEqual(self.url_open('/my/job-works/%s' % other_job.id).status_code, 404)
        self.assertEqual(self.url_open('/my/job-works/%s' % draft_job.id).status_code, 404)

        url = '/my/job-works/%s' % job.id
        res, token = self._csrf(url)
        self.assertIn('Accept Job', res.text)
        res = self.url_open(url + '/action', data={'csrf_token': token, 'action': 'accept'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(job.vendor_state, 'accepted')
        self.assertTrue(job.vendor_accept_date)

        # material issued, vendor confirms receipt and reports
        job.action_issue_material()
        job.picking_ids.button_validate()
        self.assertEqual(job.state, 'issued')
        res, token = self._csrf(url)
        self.url_open(url + '/action', data={'csrf_token': token, 'action': 'receipt'})
        self.assertEqual(job.state, 'at_vendor')
        self.url_open(url + '/progress', data={'csrf_token': token, 'qty_completed': '8', 'qty_scrap': '0.5',
                                               'note': 'Batch 1'})
        self.assertEqual(job.vendor_reported_qty, 8.0)
        self.assertEqual(job.state, 'processing', "reporting progress starts processing")
        res = self.url_open(url + '/progress', data={'csrf_token': token, 'qty_completed': '-3'})
        self.assertIn('cannot be negative', res.text)
        self.assertEqual(len(job.progress_ids), 1)

        self.url_open(url + '/request', data={'csrf_token': token, 'material_id': str(job.material_ids.id),
                                              'qty': '2', 'reason': 'Setup loss'})
        self.assertEqual(job.request_ids.state, 'to_approve')
        self.assertEqual(job.request_ids.source, 'portal')
        # cannot request material of another job
        self.url_open(url + '/request', data={'csrf_token': token, 'material_id': str(other_job.material_ids.id),
                                              'qty': '2', 'reason': 'x'})
        self.assertEqual(len(job.request_ids), 1)

        res = self.url_open(url + '/upload', data={'csrf_token': token, 'doc_type': 'inspection'},
                            files={'attachment': ('cert.pdf', b'%PDF-1.4 test', 'application/pdf')})
        self.assertEqual(res.status_code, 200)
        att = self.env['ir.attachment'].search([('res_model', '=', 'zivo.job.work'), ('res_id', '=', job.id),
                                                ('name', '=', 'cert.pdf')])
        self.assertTrue(att)
        self.url_open(url + '/upload', data={'csrf_token': token},
                      files={'attachment': ('virus.exe', b'MZ', 'application/octet-stream')})
        self.assertFalse(self.env['ir.attachment'].search([('name', '=', 'virus.exe')]))

        self.url_open(url + '/action', data={'csrf_token': token, 'action': 'complete'})
        self.assertEqual(job.vendor_state, 'completed')

        # the other vendor cannot act on this job
        self.authenticate('xyz_portal', 'xyz_portal_pwd')
        res = self.url_open('/my/job-works?filterby=all')
        self.assertNotIn(job.name, res.text)
        job.vendor_state = 'new'
        res = self.url_open(url + '/action', data={'csrf_token': token, 'action': 'accept'})
        self.assertIn(res.status_code, (400, 403, 404))
        self.assertEqual(job.vendor_state, 'new', "another job worker cannot act on the job")

    def test_02_token_access_and_pdf(self):
        job = self._create_job(qty=10.0, material_qty=10.0)
        job.action_plan()
        job._portal_ensure_token()
        self.assertEqual(self.url_open('/my/job-works/%s' % job.id).status_code, 404)
        res = self.url_open(job.get_portal_url())
        self.assertEqual(res.status_code, 200)
        self.assertIn(job.name, res.text)
        res = self.url_open('/my/job-works/%s?access_token=wrong' % job.id)
        self.assertEqual(res.status_code, 404)
        res = self.url_open(job.get_portal_url(report_type='html'))
        self.assertEqual(res.status_code, 200)
        self.assertIn(job.name, res.text)

    def test_03_portal_home_counter(self):
        job = self._create_job(qty=10.0, material_qty=10.0)
        job.action_plan()
        self.authenticate('abc_portal', 'abc_portal_pwd')
        res = self.make_jsonrpc_request('/my/counters', {'counters': ['jw_count']})
        self.assertEqual(res['jw_count'], 1)
