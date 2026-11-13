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
                      'zivoit_field_tracking.zivo_track_device_action'):
            action = self.env.ref(xmlid)
            for view_type in action.view_mode.split(','):
                self.env[action.res_model].get_views([(False, view_type)])
