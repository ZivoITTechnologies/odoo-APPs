# -*- coding: utf-8 -*-
from odoo.tests import HttpCase, tagged

from .common import JobWorkCommon


@tagged('post_install', '-at_install', 'job_work')
class TestJobWorkUi(HttpCase, JobWorkCommon):

    def test_01_dashboard_and_job_form_tour(self):
        job = self._create_job(qty=10.0, material_qty=10.0)
        job.action_plan()
        self.start_tour('/odoo/action-zivo_job_work_360.action_job_work_dashboard', 'zivo_job_work_dashboard_tour',
                        login='admin')

    def test_02_create_job_tour(self):
        self.start_tour('/odoo/action-zivo_job_work_360.action_zivo_job_work', 'zivo_job_work_create_tour',
                        login='admin')
        job = self.env['zivo.job.work'].search([('client_ref', '=', 'TOUR-REF-1')])
        self.assertEqual(job.state, 'planned')
        self.assertEqual(job.material_ids.planned_qty, 25.0)
