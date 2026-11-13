# -*- coding: utf-8 -*-
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import tagged

from .common import ZivoTrackCommon


@tagged('post_install', '-at_install', 'zivoit_field_tracking')
class TestIngest(ZivoTrackCommon):

    def test_offline_backlog_is_stored_with_original_gps_time(self):
        """Points recorded 2 hours ago without signal keep their GPS time and are flagged offline."""
        items = [self.pwa_item('a%s' % i, 17.4375 + i * 0.001, 78.4483, minutes_ago=120 - i * 2)
                 for i in range(10)]
        res = self.Point._ingest(self.dev1, items)
        self.assertEqual(len(res['accepted']), 10)
        pts = self.Point.search([('device_id', '=', self.dev1.id)], order='fix_time')
        self.assertEqual(len(pts), 10)
        self.assertTrue(all(pts.mapped('captured_offline')))
        self.assertGreater(min(pts.mapped('delay_minutes')), 100)
        self.assertLess(pts[0].fix_time, pts[-1].fix_time)
        self.assertEqual(pts[0].session_id, pts[-1].session_id)
        session = pts[0].session_id
        self.assertEqual(session.point_count, 10)
        self.assertEqual(session.offline_point_count, 10)
        self.assertAlmostEqual(session.distance_km, 1.0, delta=0.05)  # 9 x 111 m

    def test_live_point_is_not_offline(self):
        self.Point._ingest(self.dev1, [self.pwa_item('live1', 17.44, 78.44, minutes_ago=0)])
        point = self.Point.search([('client_uid', '=', 'live1')])
        self.assertFalse(point.captured_offline)
        self.assertEqual(point.speed_kmh, 18.0)  # 5 m/s

    def test_client_offline_flag_is_respected(self):
        self.Point._ingest(self.dev1, [self.pwa_item('flag1', 17.44, 78.44, offline=True)])
        self.assertTrue(self.Point.search([('client_uid', '=', 'flag1')]).captured_offline)

    def test_resend_is_idempotent(self):
        """A phone that never got the server reply re-sends the same batch: no duplicates."""
        items = [self.pwa_item('dup%s' % i, 17.44 + i * 0.001, 78.44, minutes_ago=30 - i) for i in range(5)]
        first = self.Point._ingest(self.dev1, items)
        second = self.Point._ingest(self.dev1, items + [items[0]])
        self.assertEqual(len(first['accepted']), 5)
        self.assertEqual(second['accepted'], [])
        self.assertEqual(len(second['duplicates']), 6)
        self.assertEqual(self.Point.search_count([('device_id', '=', self.dev1.id)]), 5)

    def test_same_uid_on_two_devices_is_allowed(self):
        self.Point._ingest(self.dev1, [self.pwa_item('same', 17.44, 78.44)])
        self.Point._ingest(self.dev2, [self.pwa_item('same', 17.45, 78.45)])
        self.assertEqual(self.Point.search_count([('client_uid', '=', 'same')]), 2)

    def test_out_of_order_upload_keeps_last_known_position(self):
        """An old offline batch arriving after a fresh point must not move the live marker back."""
        self.Point._ingest(self.dev1, [self.pwa_item('new', 17.50, 78.50, minutes_ago=1)])
        self.Point._ingest(self.dev1, [self.pwa_item('old', 17.40, 78.40, minutes_ago=90)])
        self.assertAlmostEqual(self.dev1.last_latitude, 17.50, places=4)
        board = {u['user_id']: u for u in self.Point.with_user(self.manager).get_live_board()['users']}
        self.assertAlmostEqual(board[self.sales1.id]['lat'], 17.50, places=4)

    def test_invalid_points_rejected_but_acknowledged(self):
        items = [self.pwa_item('bad0', 0, 0), self.pwa_item('bad1', 95, 10), self.pwa_item('ok', 17.4, 78.4)]
        res = self.Point._ingest(self.dev1, items)
        self.assertEqual(res['accepted'], ['ok'])
        self.assertEqual({r['uid'] for r in res['rejected']}, {'bad0', 'bad1'})

    def test_future_timestamp_clamped(self):
        future = fields.Datetime.now() + timedelta(days=2)
        item = self.Point._normalize_pwa({'uid': 'fut', 'lat': 17.4, 'lon': 78.4,
                                          'ts': future.strftime('%Y-%m-%dT%H:%M:%SZ')})
        self.Point._ingest(self.dev1, [item])
        point = self.Point.search([('client_uid', '=', 'fut')])
        self.assertLessEqual(point.fix_time, fields.Datetime.now() + timedelta(minutes=1))

    def test_low_accuracy_excluded_from_distance(self):
        items = [
            self.pwa_item('la1', 17.4400, 78.44, minutes_ago=20),
            self.pwa_item('la2', 17.4600, 78.44, minutes_ago=15, accuracy=900),  # bad fix ~2.2 km away
            self.pwa_item('la3', 17.4410, 78.44, minutes_ago=10),
        ]
        self.Point._ingest(self.dev1, items)
        bad = self.Point.search([('client_uid', '=', 'la2')])
        self.assertTrue(bad.low_accuracy)
        self.assertAlmostEqual(bad.session_id.distance_km, 0.111, delta=0.01)

    def test_mock_location_flags_device(self):
        self.Point._ingest(self.dev1, [self.pwa_item('mock', 17.4, 78.4, mock=True)])
        self.assertTrue(self.dev1.mock_detected)

    def test_session_split_by_local_day(self):
        """Day logs follow the salesperson's time zone (IST), not UTC."""
        now = fields.Datetime.now()
        Session = self.env['zivo.track.session']
        day = Session._local_date(self.sales1, now)
        # 23:50 IST yesterday and 00:10 IST today
        import pytz
        tz = pytz.timezone('Asia/Kolkata')
        from datetime import datetime, time
        late = tz.localize(datetime.combine(day - timedelta(days=1), time(23, 50))).astimezone(pytz.utc)
        early = tz.localize(datetime.combine(day, time(0, 10))).astimezone(pytz.utc)
        items = [self.Point._normalize_pwa({'uid': 'd1', 'lat': 17.4, 'lon': 78.4, 'ts': late.isoformat()}),
                 self.Point._normalize_pwa({'uid': 'd2', 'lat': 17.41, 'lon': 78.4, 'ts': early.isoformat()})]
        self.Point._ingest(self.dev1, items)
        sessions = self.Point.search([('client_uid', 'in', ['d1', 'd2'])]).mapped('session_id')
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sorted(sessions.mapped('date')), [day - timedelta(days=1), day])

    def test_disabled_user_cannot_authenticate(self):
        Device = self.env['zivo.track.device']
        self.assertEqual(Device._authenticate(self.dev1.token), self.dev1)
        self.sales1.group_ids = [(3, self.env.ref('zivoit_field_tracking.group_field_user').id)]
        self.assertFalse(Device._authenticate(self.dev1.token))
        self.assertFalse(Device._authenticate('zv-doesnotexist'))
        self.assertFalse(Device._authenticate(''))

    def test_archived_device_and_token_regeneration(self):
        Device = self.env['zivo.track.device']
        old = self.dev2.token
        self.dev2.action_regenerate_token()
        self.assertNotEqual(self.dev2.token, old)
        self.assertFalse(Device._authenticate(old))
        self.dev2.active = False
        self.assertFalse(Device._authenticate(self.dev2.token))

    def test_traccar_json_and_osmand_normalisation(self):
        j = self.Point._normalize_traccar_json({'device_id': 'x', 'location': {
            'timestamp': '2026-11-13T06:30:00.000Z',
            'coords': {'latitude': 17.4, 'longitude': 78.4, 'accuracy': 9, 'speed': 10, 'heading': 90,
                       'altitude': 540},
            'battery': {'level': 0.62, 'is_charging': True}, 'mock': True, 'is_moving': True}})
        self.assertEqual(j['speed_kmh'], 36.0)
        self.assertAlmostEqual(j['battery'], 62.0)
        self.assertTrue(j['charging'])
        self.assertTrue(j['mock'])
        q = self.Point._normalize_osmand_params({'id': 'x', 'lat': '17.4', 'lon': '78.4', 'speed': '10',
                                                  'batt': '55', 'timestamp': '1794551400'})
        self.assertAlmostEqual(q['speed_kmh'], 18.52)
        self.assertEqual(q['battery'], 55.0)
        loc = self.Point._normalize_osmand_params({'id': 'x', 'location': '17.5,78.5'})
        self.assertEqual((loc['lat'], loc['lon']), ('17.5', '78.5'))

    def test_live_board_statuses(self):
        P = self.Point
        P._ingest(self.dev1, [self.pwa_item('s1', 17.44, 78.44, minutes_ago=1)])
        P._ingest(self.dev2, [self.pwa_item('s2', 17.45, 78.45, minutes_ago=15)])
        board = {u['user_id']: u for u in P.with_user(self.manager).get_live_board()['users']}
        self.assertEqual(board[self.sales1.id]['status'], 'live')
        self.assertEqual(board[self.sales2.id]['status'], 'delayed')
        self.assertEqual(board[self.sales1.id]['trail'], [[17.44, 78.44]])
        # simulate time passing: 2 hours without any upload -> "No signal"
        later = fields.Datetime.now() + timedelta(hours=2)
        with patch.object(fields.Datetime, 'now', return_value=later):
            board = {u['user_id']: u for u in P.with_user(self.manager).get_live_board()['users']}
        self.assertEqual(board[self.sales1.id]['status'], 'nosignal')
        self.Point.search([('client_uid', '=', 's1')]).session_id.action_end_day()
        board = {u['user_id']: u for u in P.with_user(self.manager).get_live_board()['users']}
        self.assertEqual(board[self.sales1.id]['status'], 'ended')

    def test_visit_geofence_and_upsert(self):
        Visit = self.env['zivo.track.visit']
        now = fields.Datetime.now()
        ts = now.strftime('%Y-%m-%dT%H:%M:%SZ')
        near = {'uid': 'v1', 'partner_id': self.customer.id, 'check_in_ts': ts,
                'lat': 17.4376, 'lon': 78.4484, 'accuracy': 10}
        res = Visit._ingest(self.dev1, [near])
        self.assertEqual(res['accepted'], ['v1'])
        visit = Visit.search([('client_uid', '=', 'v1')])
        self.assertEqual(visit.state, 'in_progress')
        self.assertTrue(visit.within_geofence)
        self.assertEqual(visit.geofence_status, 'inside')
        # later the phone sends the check-out of the same visit (upsert, not a new record)
        out = dict(near, check_out_ts=(now + timedelta(minutes=20)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                   outcome='order', note='Ordered 20 cartons')
        Visit._ingest(self.dev1, [out])
        self.assertEqual(Visit.search_count([('client_uid', '=', 'v1')]), 1)
        self.assertEqual(visit.state, 'done')
        self.assertAlmostEqual(visit.duration_minutes, 20, delta=0.1)
        self.assertEqual(visit.outcome, 'order')
        # a check-in 2 km away is flagged
        far = {'uid': 'v2', 'partner_id': self.customer.id, 'check_in_ts': ts, 'lat': 17.4555, 'lon': 78.4483}
        Visit._ingest(self.dev1, [far])
        far_visit = Visit.search([('client_uid', '=', 'v2')])
        self.assertFalse(far_visit.within_geofence)
        self.assertEqual(far_visit.geofence_status, 'outside')
        self.assertGreater(far_visit.distance_from_customer_m, 1900)

    def test_visit_learns_customer_location(self):
        self.env['ir.config_parameter'].sudo().set_param('zivoit_field_tracking.learn_customer_location', 'True')
        shop = self.env['res.partner'].create({'name': 'New Shop', 'is_company': True})
        self.env['zivo.track.visit']._ingest(self.dev1, [{
            'uid': 'learn', 'partner_id': shop.id, 'lat': 17.401, 'lon': 78.402, 'accuracy': 12}])
        self.assertAlmostEqual(shop.partner_latitude, 17.401, places=5)

    def test_visit_unknown_customer_rejected(self):
        res = self.env['zivo.track.visit']._ingest(self.dev1, [{'uid': 'nx', 'partner_id': 99999999}])
        self.assertEqual(res['rejected'][0]['reason'], 'unknown_customer')

    def test_route_data_and_stops(self):
        items = [self.pwa_item('r%s' % i, 17.4400, 78.4400, minutes_ago=60 - i) for i in range(15)]
        items += [self.pwa_item('m%s' % i, 17.4400 + 0.003 * i, 78.4400, minutes_ago=44 - i) for i in range(1, 6)]
        self.Point._ingest(self.dev1, items)
        session = self.Point.search([('client_uid', '=', 'r0')]).session_id
        data = session.with_user(self.sales1).get_route_data()
        self.assertEqual(len(data['points']), 20)
        self.assertEqual(len(data['stops']), 1)
        self.assertEqual(session.stop_count, 1)

    def test_cron_purge_and_close(self):
        self.Point._ingest(self.dev1, [self.pwa_item('old1', 17.4, 78.4, minutes_ago=60 * 24 * 200)])
        self.Point._ingest(self.dev1, [self.pwa_item('new1', 17.4, 78.4, minutes_ago=5)])
        self.Point._cron_purge_old_points()
        self.assertFalse(self.Point.search([('client_uid', '=', 'old1')]))
        self.assertTrue(self.Point.search([('client_uid', '=', 'new1')]))
        old_session = self.env['zivo.track.session'].create({
            'user_id': self.sales1.id, 'date': fields.Date.today() - timedelta(days=3)})
        self.env['zivo.track.session']._cron_close_stale_days()
        self.assertEqual(old_session.state, 'closed')

    def test_batch_performance(self):
        """A day of offline points (1 000) syncs in one request."""
        items = [self.pwa_item('b%s' % i, 17.40 + i * 0.0002, 78.40, minutes_ago=600 - i * 0.5) for i in range(1000)]
        res = self.Point._ingest(self.dev1, items)
        self.assertEqual(len(res['accepted']), 1000)
        sessions = self.Point.search([('device_id', '=', self.dev1.id)]).mapped('session_id')
        self.assertEqual(sum(sessions.mapped('point_count')), 1000)
