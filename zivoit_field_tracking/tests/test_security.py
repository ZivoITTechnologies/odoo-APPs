# -*- coding: utf-8 -*-
from odoo.exceptions import AccessError
from odoo.tests import tagged

from .common import ZivoTrackCommon


@tagged('post_install', '-at_install', 'zivoit_field_tracking')
class TestSecurity(ZivoTrackCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        P = cls.env['zivo.track.point']
        P._ingest(cls.dev1, [P._normalize_pwa({'uid': 'own1', 'lat': 17.44, 'lon': 78.44})])
        P._ingest(cls.dev2, [P._normalize_pwa({'uid': 'own2', 'lat': 17.45, 'lon': 78.45})])

    def test_salesperson_sees_only_own_data(self):
        P = self.env['zivo.track.point'].with_user(self.sales1)
        self.assertEqual(set(P.search([]).mapped('client_uid')), {'own1'})
        S = self.env['zivo.track.session'].with_user(self.sales1)
        self.assertEqual(S.search([]).user_id, self.sales1)
        D = self.env['zivo.track.device'].with_user(self.sales1)
        self.assertEqual(D.search([]), self.dev1)
        board = P.get_live_board()
        self.assertEqual([u['user_id'] for u in board['users']], [self.sales1.id])
        self.assertFalse(board['is_manager'])

    def test_salesperson_cannot_read_colleague_route(self):
        other_session = self.env['zivo.track.point'].search([('client_uid', '=', 'own2')]).session_id
        with self.assertRaises(AccessError):
            other_session.with_user(self.sales1).get_route_data()

    def test_salesperson_cannot_forge_points(self):
        with self.assertRaises(AccessError):
            self.env['zivo.track.point'].with_user(self.sales1).create({
                'device_id': self.dev1.id, 'user_id': self.sales1.id, 'company_id': self.sales1.company_id.id,
                'latitude': 1, 'longitude': 1, 'fix_time': '2026-01-01 00:00:00', 'client_uid': 'forged'})
        point = self.env['zivo.track.point'].search([('client_uid', '=', 'own1')])
        with self.assertRaises(AccessError):
            point.with_user(self.sales1).write({'latitude': 1.0})

    def test_manager_sees_everyone(self):
        P = self.env['zivo.track.point'].with_user(self.manager)
        self.assertEqual(set(P.search([]).mapped('client_uid')) & {'own1', 'own2'}, {'own1', 'own2'})
        board = P.get_live_board()
        ids = {u['user_id'] for u in board['users']}
        self.assertTrue({self.sales1.id, self.sales2.id} <= ids)
        self.assertTrue(board['is_manager'])

    def test_outsider_has_no_access(self):
        with self.assertRaises(AccessError):
            self.env['zivo.track.point'].with_user(self.outsider).search([])
        self.assertFalse(self.outsider.zivo_tracking_enabled)
        self.assertTrue(self.sales1.zivo_tracking_enabled)
        self.assertTrue(self.manager.zivo_tracking_enabled)  # implied group

    def test_tracking_enabled_search(self):
        Users = self.env['res.users']
        enabled = Users.search([('zivo_tracking_enabled', '=', True)])
        self.assertIn(self.sales1, enabled)
        self.assertNotIn(self.outsider, enabled)
        self.assertIn(self.outsider, Users.search([('zivo_tracking_enabled', '=', False)]))
