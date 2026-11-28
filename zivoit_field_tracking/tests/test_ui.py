# -*- coding: utf-8 -*-
from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install', 'zivoit_field_tracking')
class TestUi(HttpCase):

    def test_live_map_client_action_loads(self):
        """The manager live map opens in the backend without JS errors."""
        self.env['zivo.track.point']._ingest(
            self.env['zivo.track.device'].create({'user_id': self.env.ref('base.user_admin').id}),
            [self.env['zivo.track.point']._normalize_pwa({'uid': 'ui1', 'lat': 17.44, 'lon': 78.44})])
        self.start_tour('/odoo/action-zivoit_field_tracking.zivo_track_live_map_action',
                        'zivo_track_live_map_tour', login='admin')

    def test_settings_and_views_open(self):
        for xmlid in ('zivoit_field_tracking.zivo_track_session_action',
                      'zivoit_field_tracking.zivo_track_visit_action',
                      'zivoit_field_tracking.zivo_track_point_action',
                      'zivoit_field_tracking.zivo_track_device_action',
                      'zivoit_field_tracking.zivo_expense_claim_action',
                      'zivoit_field_tracking.zivo_expense_claim_approve_action',
                      'zivoit_field_tracking.zivo_expense_line_action',
                      'zivoit_field_tracking.zivo_expense_line_analysis_action',
                      'zivoit_field_tracking.zivo_expense_policy_action',
                      'zivoit_field_tracking.zivo_expense_category_action',
                      'zivoit_field_tracking.zivo_expense_claim_wizard_action'):
            action = self.env.ref(xmlid)
            for view_type in action.view_mode.split(','):
                self.env[action.res_model].get_views([(False, view_type)])

    def test_expense_claim_tour(self):
        """A salesperson opens the claim wizard, creates a claim from GPS and submits it."""
        from datetime import date
        from odoo.tests import new_test_user
        user = new_test_user(self.env, login='zv_tour_sales', password='zv_tour_sales_pw', name='Tour Sales',
                             tz='Asia/Kolkata', groups='zivoit_field_tracking.group_field_user')
        self.env['hr.employee'].create({'name': 'Tour Sales', 'user_id': user.id})
        device = self.env['zivo.track.device'].create({'user_id': user.id})
        Point = self.env['zivo.track.point']
        day = date.today()
        items = [Point._normalize_pwa({'uid': 'tour%s' % i, 'lat': 17.40 + i * 0.01, 'lon': 78.45,
                                       'ts': '%sT%02d:00:00Z' % (day, 1 + i)}) for i in range(8)]
        Point._ingest(device, items)
        self.env['zivo.track.session'].search([('user_id', '=', user.id)]).write({'state': 'closed'})
        self.start_tour('/odoo/action-zivoit_field_tracking.zivo_expense_claim_wizard_action',
                        'zivo_expense_claim_tour', login='zv_tour_sales')
        claim = self.env['zivo.expense.claim'].search([('user_id', '=', user.id)])
        self.assertEqual(claim.state, 'submitted')
        self.assertTrue(claim.line_ids.filtered(lambda l: l.line_type == 'travel'))
