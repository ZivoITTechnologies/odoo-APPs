# -*- coding: utf-8 -*-
"""Demo data generator: realistic field days around Hyderabad."""
import random
from datetime import timedelta

from odoo import api, fields, models

from . import geo

ROUTES = {
    'ravi': { 'batt': 92,  # live, moving now
        'waypoints': [(17.4375, 78.4483), (17.4260, 78.4510), (17.4156, 78.4347), (17.4325, 78.4073),
                      (17.4483, 78.3915), (17.4474, 78.3762)],
        'start_h': 5.5, 'end_ago_min': 1, 'offline': [(0.35, 0.50)], 'visits': [1, 3, 4],
    },
    'sneha': { 'batt': 78,  # delayed (signal weak)
        'waypoints': [(17.4700, 78.6010), (17.4400, 78.5800), (17.4058, 78.5591), (17.3688, 78.5247),
                      (17.3457, 78.5522)],
        'start_h': 6.0, 'end_ago_min': 18, 'offline': [(0.55, 0.70)], 'visits': [1, 2, 3],
    },
    'arjun': { 'batt': 71,  # no signal for 2h - backlog still on the phone
        'waypoints': [(17.4849, 78.4138), (17.4930, 78.3900), (17.4968, 78.3614), (17.5120, 78.3520)],
        'start_h': 4.5, 'end_ago_min': 125, 'offline': [(0.20, 0.45)], 'visits': [1, 2],
    },
    'priya': { 'batt': 88,  # day ended
        'waypoints': [(17.4399, 78.4983), (17.4447, 78.4664), (17.4239, 78.4738), (17.4062, 78.4691)],
        'start_h': 7.5, 'end_ago_min': 90, 'offline': [], 'visits': [1, 2], 'ended': True,
    },
}

CUSTOMERS = [
    ('Sri Venkateswara Traders', 'Ameerpet', 17.4260, 78.4510),
    ('Banjara Electronics', 'Banjara Hills', 17.4325, 78.4073),
    ('Madhapur Medicals', 'Madhapur', 17.4483, 78.3915),
    ('HITEC Office Supplies', 'HITEC City', 17.4474, 78.3762),
    ('Uppal Hardware Mart', 'Uppal', 17.4058, 78.5591),
    ('Dilsukhnagar Super Bazaar', 'Dilsukhnagar', 17.3688, 78.5247),
    ('LB Nagar Agencies', 'LB Nagar', 17.3457, 78.5522),
    ('Kukatpally Kirana Stores', 'Kukatpally', 17.4930, 78.3900),
    ('Miyapur Distributors', 'Miyapur', 17.4968, 78.3614),
    ('Begumpet Foods', 'Begumpet', 17.4447, 78.4664),
    ('Secunderabad Paints', 'Secunderabad', 17.4239, 78.4738),
]


class ZivoTrackDemo(models.AbstractModel):
    _name = 'zivo.track.demo'
    _description = 'Field Tracking Demo Data'

    @api.model
    def _interpolate(self, waypoints, step_m, rnd=None):
        """Road-like path: each leg is broken into street segments with turns."""
        rnd = rnd or random.Random(7)
        corners = [waypoints[0]]
        for (a_lat, a_lon), (b_lat, b_lon) in zip(waypoints, waypoints[1:]):
            legs = rnd.randint(3, 5)
            for k in range(1, legs):
                t = k / legs
                # alternate "north-south" and "east-west" streets with small detours
                lat = a_lat + (b_lat - a_lat) * (t if k % 2 else t - 0.12 / legs)
                lon = a_lon + (b_lon - a_lon) * (t - 0.12 / legs if k % 2 else t)
                corners.append((lat + rnd.uniform(-0.0012, 0.0012), lon + rnd.uniform(-0.0012, 0.0012)))
            corners.append((b_lat, b_lon))
        pts = [corners[0]]
        for (a_lat, a_lon), (b_lat, b_lon) in zip(corners, corners[1:]):
            dist = geo.haversine_m(a_lat, a_lon, b_lat, b_lon)
            n = max(1, int(dist // step_m))
            for i in range(1, n + 1):
                t = i / n
                pts.append((a_lat + (b_lat - a_lat) * t, a_lon + (b_lon - a_lon) * t))
        return pts

    @api.model
    def _generate(self):
        rnd = random.Random(19)
        Partner = self.env['res.partner']
        partners = {}
        for name, city, lat, lon in CUSTOMERS:
            partner = Partner.search([('name', '=', name)], limit=1) or Partner.create({
                'name': name, 'is_company': True, 'city': city, 'country_id': self.env.ref('base.in').id,
                'partner_latitude': lat, 'partner_longitude': lon,
            })
            partners[(round(lat, 4), round(lon, 4))] = partner
        now = fields.Datetime.now().replace(microsecond=0)
        Point = self.env['zivo.track.point'].sudo()
        Session = self.env['zivo.track.session'].sudo()
        for key, spec in ROUTES.items():
            user = self.env.ref('zivoit_field_tracking.demo_user_%s' % key, raise_if_not_found=False)
            if not user or Point.search_count([('user_id', '=', user.id)]):
                continue
            device = self.env['zivo.track.device'].sudo().create({
                'name': '%s - Phone' % user.name, 'user_id': user.id,
                'source': 'traccar' if key == 'arjun' else 'pwa',
            })
            path = self._interpolate(spec['waypoints'], 180, rnd)
            end = now - timedelta(minutes=spec['end_ago_min'])
            total = len(path)
            # visits: dwell 15-25 min at selected waypoints
            visit_idx = {}
            for wp_index in spec['visits']:
                wp = spec['waypoints'][wp_index]
                nearest = min(range(total), key=lambda i: geo.haversine_m(path[i][0], path[i][1], *wp))
                visit_idx[nearest] = (wp, rnd.randint(15, 25))
            # timeline backwards from end
            duration_min = spec['start_h'] * 60
            base_step = (duration_min - sum(v[1] for v in visit_idx.values())) / max(total - 1, 1)
            times, t = [], end - timedelta(minutes=duration_min)
            for i in range(total):
                times.append(t)
                t += timedelta(minutes=base_step * rnd.uniform(0.8, 1.2))
                if i in visit_idx:
                    t += timedelta(minutes=visit_idx[i][1])
            shift = end - times[-1]
            times = [x + shift for x in times]
            vals = []
            for i, ((lat, lon), when) in enumerate(zip(path, times)):
                frac = i / max(total - 1, 1)
                offline_window = next((w for w in spec['offline'] if w[0] <= frac <= w[1]), None)
                if offline_window:
                    sync_at = times[min(total - 1, int(offline_window[1] * (total - 1)) + 1)]
                    received = max(when, sync_at)
                else:
                    received = when + timedelta(seconds=rnd.randint(2, 20))
                if key == 'arjun' and when > now - timedelta(minutes=130):
                    continue
                delay = (received - when).total_seconds() / 60.0
                vals.append({
                    'device_id': device.id, 'user_id': user.id, 'company_id': user.company_id.id,
                    'latitude': lat + rnd.uniform(-0.00008, 0.00008),
                    'longitude': lon + rnd.uniform(-0.00008, 0.00008),
                    'accuracy': rnd.choice([5, 6, 8, 10, 12, 15, 20]),
                    'speed_kmh': rnd.uniform(12, 35), 'heading': rnd.uniform(0, 359),
                    'battery': max(12, spec['batt'] - frac * 40), 'fix_time': when, 'received_time': received,
                    'delay_minutes': round(delay, 1), 'captured_offline': delay > 3,
                    'client_uid': 'demo-%s-%s' % (key, i),
                    'session_id': Session._get_for(user, when).id,
                })
                if i in visit_idx:
                    # stationary points during the visit
                    wp, dwell = visit_idx[i]
                    for k in range(1, dwell // 4):
                        w2 = when + timedelta(minutes=4 * k)
                        vals.append(dict(vals[-1], fix_time=w2, received_time=w2 + timedelta(seconds=5),
                                         delay_minutes=0.1, captured_offline=False, speed_kmh=0,
                                         client_uid='demo-%s-%s-%s' % (key, i, k),
                                         latitude=wp[0] + rnd.uniform(-0.00005, 0.00005),
                                         longitude=wp[1] + rnd.uniform(-0.00005, 0.00005)))
                    partner = partners.get((round(wp[0], 4), round(wp[1], 4)))
                    if partner:
                        far = key == 'sneha' and wp_index_is_last(spec, wp)
                        self.env['zivo.track.visit'].sudo().create({
                            'user_id': user.id, 'partner_id': partner.id, 'device_id': device.id,
                            'company_id': user.company_id.id,
                            'session_id': Session._get_for(user, when).id,
                            'check_in_time': when, 'check_out_time': when + timedelta(minutes=dwell),
                            'state': 'done',
                            'check_in_latitude': wp[0] + (0.004 if far else 0.0003),
                            'check_in_longitude': wp[1] + (0.003 if far else 0.0002),
                            'check_in_accuracy': 8,
                            'outcome': rnd.choice(['order', 'follow_up', 'collection', 'demo']),
                            'captured_offline': bool(offline_window),
                            'client_uid': 'demo-visit-%s-%s' % (key, i),
                        })._update_geofence()
            Point.create(vals)
            sessions = Session.search([('user_id', '=', user.id)])
            sessions.write({'start_time': times[0]})
            if spec.get('ended'):
                sessions.write({'state': 'closed', 'end_time': end})
            sessions._recompute_stats()
            last = Point.search([('user_id', '=', user.id)], order='fix_time desc', limit=1)
            device.write({'last_seen': last.received_time, 'last_fix_time': last.fix_time,
                          'last_latitude': last.latitude, 'last_longitude': last.longitude,
                          'last_battery': last.battery})
        return True


def wp_index_is_last(spec, wp):
    return spec['waypoints'][spec['visits'][-1]] == wp
