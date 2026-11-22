# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install', 'zivoit_tender')
class TestTenderUi(HttpCase):

    def test_dashboard_agent_tour(self):
        self.env['ir.config_parameter'].sudo().set_param('zivoit_tender_ai.ai_provider', 'none')
        self.env['zivoit.tender'].create({
            'name': 'Construction of ROB tour test at Kazipet', 'organisation': 'South Central Railway',
            'estimated_value': 18.5e7, 'date_closing': fields.Datetime.now() + timedelta(days=10)})
        self.start_tour('/odoo/action-zivoit_tender_ai.action_zivoit_tender_dashboard', 'zivoit_tender_dashboard_tour',
                        login='admin')

    def test_any_industry_agent_tour(self):
        self.env['ir.config_parameter'].sudo().set_param('zivoit_tender_ai.ai_provider', 'none')
        Tender = self.env['zivoit.tender']
        Tender.create({'name': 'Implementation of ERP tour test for Kerala Co-operative Federation',
                       'organisation': 'Kerala State Co-operative Federation', 'location': 'Thiruvananthapuram',
                       'estimated_value': 3.4e7, 'date_closing': fields.Datetime.now() + timedelta(days=10)})
        Tender.create({'name': 'Implementation of ERP decoy for Tamil Nadu', 'organisation': 'TNCSC',
                       'location': 'Chennai', 'date_closing': fields.Datetime.now() + timedelta(days=10)})
        self.start_tour('/odoo/action-zivoit_tender_ai.action_zivoit_tender_dashboard', 'zivoit_tender_any_industry_tour',
                        login='admin')

    def test_views_load(self):
        """Every view of the module renders in the web client (list/kanban/form/search/pivot/graph/calendar)."""
        for model in ('zivoit.tender', 'zivoit.tender.profile', 'zivoit.tender.source', 'zivoit.tender.ra.bill',
                      'zivoit.tender.agent.search', 'zivoit.tender.result.wizard'):
            views = self.env['ir.ui.view'].search([('model', '=', model), ('type', '!=', 'qweb')])
            self.assertTrue(views, model)
            for view in views:
                self.env[model].get_views([(view.id, view.type)])
