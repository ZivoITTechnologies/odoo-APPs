# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

from odoo.tests import tagged
from odoo.tests.common import BaseCase

from ..models import geo


@tagged('post_install', '-at_install', 'zivoit_field_tracking')
class TestGeo(BaseCase):

    def test_haversine_known_distance(self):
        # Ameerpet -> HITEC City (Hyderabad) is ~7.6 km in a straight line
        d = geo.haversine_m(17.4375, 78.4483, 17.4474, 78.3762)
        self.assertAlmostEqual(d / 1000, 7.7, delta=0.2)
        self.assertEqual(geo.haversine_m(17.0, 78.0, 17.0, 78.0), 0)

    def test_valid_coordinates(self):
        self.assertTrue(geo.valid_coordinates(17.4, 78.4))
        self.assertTrue(geo.valid_coordinates('17.4', '78.4'))
        self.assertFalse(geo.valid_coordinates(0, 0))
        self.assertFalse(geo.valid_coordinates(91, 10))
        self.assertFalse(geo.valid_coordinates(10, 181))
        self.assertFalse(geo.valid_coordinates('abc', 1))
        self.assertFalse(geo.valid_coordinates(None, 1))
        self.assertFalse(geo.valid_coordinates(float('nan'), 1))

    def test_parse_timestamp_formats(self):
        expected = datetime(2026, 11, 13, 6, 30, 0)
        self.assertEqual(geo.parse_timestamp(1794551400), expected)            # epoch seconds
        self.assertEqual(geo.parse_timestamp('1794551400'), expected)
        self.assertEqual(geo.parse_timestamp(1794551400000), expected)         # epoch ms
        self.assertEqual(geo.parse_timestamp('2026-11-13T06:30:00Z'), expected)
        self.assertEqual(geo.parse_timestamp('2026-11-13T12:00:00+05:30'), expected)
        self.assertEqual(geo.parse_timestamp('2026-11-13T06:30:00.123Z'), expected)
        self.assertEqual(geo.parse_timestamp('2026-11-13 06:30:00'), expected)
        self.assertIsNone(geo.parse_timestamp('not a date'))
        self.assertIsNone(geo.parse_timestamp(None))

    def test_route_distance_filters_spikes_and_bad_accuracy(self):
        t0 = datetime(2026, 11, 13, 4, 0, 0)
        pts = [
            (t0, 17.4375, 78.4483, 5),
            (t0 + timedelta(minutes=5), 17.4475, 78.4483, 5),        # ~1.11 km north
            (t0 + timedelta(minutes=5, seconds=10), 18.4475, 78.4483, 5),  # 111 km in 10 s: spike
            (t0 + timedelta(minutes=6), 17.9, 78.9, 900),            # very poor accuracy
            (t0 + timedelta(minutes=10), 17.4575, 78.4483, 5),       # another ~1.11 km
        ]
        dist = geo.route_distance_m(pts, max_accuracy_m=150, max_speed_kmh=250)
        self.assertAlmostEqual(dist / 1000, 2.22, delta=0.03)

    def test_detect_stops(self):
        t0 = datetime(2026, 11, 13, 4, 0, 0)
        pts = [(t0 + timedelta(minutes=i), 17.4375 + 0.00001 * i, 78.4483, 5) for i in range(15)]
        pts += [(t0 + timedelta(minutes=15 + i), 17.45 + 0.002 * i, 78.4483, 5) for i in range(5)]
        stops = geo.detect_stops(pts, radius_m=100, min_minutes=10)
        self.assertEqual(len(stops), 1)
        self.assertGreaterEqual(stops[0]['minutes'], 14)
