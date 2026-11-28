# -*- coding: utf-8 -*-
"""Demo data generator: realistic field days around Hyderabad, with expenses and claims."""
import base64
import io
import logging
import random
from datetime import datetime, time, timedelta

import pytz

from odoo import api, fields, models

from . import geo

_logger = logging.getLogger(__name__)

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
    ('Warangal Agro Agencies', 'Warangal', 17.9689, 79.5941),
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
        self._generate_tracking()
        try:
            self._generate_expenses()
        except Exception:  # noqa: BLE001 - demo data must never block the installation
            _logger.exception('ZivoIT Field Tracking: expense demo data could not be generated')
        return True

    @api.model
    def _generate_tracking(self):
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


# ----------------------------------------------------------------------------
# expense demo: one week of finished field days per salesperson
# ----------------------------------------------------------------------------
PAST_DAYS = {
    'ravi': {'vehicle': 'two_wheeler', 'route': 'ravi'},
    'sneha': {'vehicle': 'four_wheeler', 'route': 'sneha'},
    'arjun': {'vehicle': 'two_wheeler', 'route': 'arjun'},
    'priya': {'vehicle': 'public', 'route': 'priya'},
}
OUTSTATION = [(17.3950, 78.5590), (17.5560, 78.9960), (17.7280, 79.2400), (17.9689, 79.5941),
              (17.7280, 79.2400), (17.3950, 78.5590)]  # Hyderabad - Warangal - back
BILLS = {
    'food': [('Hotel Shadab - lunch', 'Lunch with customer', 340), ('Chai Point', 'Tea & snacks', 90),
             ('Paradise Biryani', 'Lunch', 420)],
    'toll': [('GMR Parking', 'Parking', 40), ('ORR Toll Plaza', 'Toll', 120)],
    'conveyance': [('Rapido Auto', 'Auto to customer', 180), ('Uber', 'Cab - Begumpet to Kondapur', 260)],
    'fuel': [('Indian Oil - HP Petrol', 'Petrol', 500)],
    'lodging': [('Hotel Haritha Kakatiya', 'Hotel - Warangal', 2400)],
    'courier': [('DTDC Express', 'Sample courier', 150)],
}


def _receipt_png(shop, item, amount, when, rnd):
    """Small bill-like picture (no external file needed)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:  # pragma: no cover
        return False
    width, height = 420, 600
    img = Image.new('RGB', (width, height), (250, 248, 242))
    draw = ImageDraw.Draw(img)

    def font(size):
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # old Pillow
            return ImageFont.load_default()
    draw.rectangle([10, 10, width - 10, height - 10], outline=(210, 205, 195), width=2)
    draw.text((width // 2, 50), shop.upper(), fill=(30, 30, 30), font=font(24), anchor='mm')
    draw.text((width // 2, 85), 'Hyderabad - GSTIN 36ABCDE%04dF1Z5' % rnd.randint(1000, 9999),
              fill=(90, 90, 90), font=font(14), anchor='mm')
    draw.line([30, 110, width - 30, 110], fill=(120, 120, 120), width=1)
    draw.text((30, 125), 'Bill No: %s' % rnd.randint(10000, 99999), fill=(60, 60, 60), font=font(16))
    draw.text((30, 150), when.strftime('Date: %d-%m-%Y  %H:%M'), fill=(60, 60, 60), font=font(16))
    draw.line([30, 185, width - 30, 185], fill=(120, 120, 120), width=1)
    y = 200
    base = amount / 1.05
    draw.text((30, y), item, fill=(30, 30, 30), font=font(18))
    draw.text((width - 30, y), '%.2f' % base, fill=(30, 30, 30), font=font(18), anchor='ra')
    y += 40
    draw.text((30, y), 'GST 5%', fill=(80, 80, 80), font=font(16))
    draw.text((width - 30, y), '%.2f' % (amount - base), fill=(80, 80, 80), font=font(16), anchor='ra')
    y += 50
    draw.line([30, y, width - 30, y], fill=(30, 30, 30), width=2)
    draw.text((30, y + 15), 'TOTAL', fill=(10, 10, 10), font=font(26))
    draw.text((width - 30, y + 15), 'Rs %.2f' % amount, fill=(10, 10, 10), font=font(26), anchor='ra')
    draw.text((width // 2, height - 60), 'Thank you! Visit again', fill=(90, 90, 90), font=font(16), anchor='mm')
    # slight paper texture
    for _i in range(500):
        x, yy = rnd.randint(12, width - 12), rnd.randint(12, height - 12)
        draw.point((x, yy), fill=(235, 232, 225))
    img = img.rotate(rnd.uniform(-2.5, 2.5), expand=False, fillcolor=(250, 248, 242))
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=80)
    return base64.b64encode(buf.getvalue())


class ZivoTrackDemoExpenses(models.AbstractModel):
    _inherit = 'zivo.track.demo'

    @api.model
    def _past_day(self, user, device, day, waypoints, rnd, partners, vehicle, tz, start_hour=9.5, hours=7.5,
                  mock=False):
        Point = self.env['zivo.track.point'].sudo()
        Session = self.env['zivo.track.session'].sudo()
        jitter = [(lat + rnd.uniform(-0.004, 0.004), lon + rnd.uniform(-0.004, 0.004)) for lat, lon in waypoints]
        path = self._interpolate(jitter, 450, rnd)
        start_local = tz.localize(datetime.combine(day, time(0, 0))) + timedelta(hours=start_hour)
        start = start_local.astimezone(pytz.utc).replace(tzinfo=None)
        step = hours * 60.0 / max(len(path) - 1, 1)
        session = Session.search([('user_id', '=', user.id), ('date', '=', day)], limit=1) or Session.create({
            'user_id': user.id, 'date': day, 'company_id': user.company_id.id, 'vehicle_type': vehicle})
        vals = []
        for i, (lat, lon) in enumerate(path):
            when = start + timedelta(minutes=step * i)
            offline = 0.4 < i / max(len(path), 1) < 0.5
            vals.append({
                'device_id': device.id, 'user_id': user.id, 'company_id': user.company_id.id,
                'latitude': lat, 'longitude': lon, 'accuracy': rnd.choice([5, 8, 10, 12]),
                'speed_kmh': rnd.uniform(15, 45), 'battery': max(15, 95 - i * 0.2), 'fix_time': when,
                'received_time': when + timedelta(minutes=35 if offline else 0.1),
                'delay_minutes': 35 if offline else 0.1, 'captured_offline': offline,
                'is_mock': mock and i % 9 == 0,
                'client_uid': 'demo-past-%s-%s-%s' % (user.id, day, i), 'session_id': session.id,
            })
        Point.create(vals)
        visits = 0
        for wp in waypoints[1:-1]:
            partner = partners.get((round(wp[0], 4), round(wp[1], 4)))
            if not partner:
                continue
            idx = min(range(len(path)), key=lambda k: geo.haversine_m(path[k][0], path[k][1], *wp))
            when = start + timedelta(minutes=step * idx)
            self.env['zivo.track.visit'].sudo().create({
                'user_id': user.id, 'partner_id': partner.id, 'device_id': device.id,
                'company_id': user.company_id.id, 'session_id': session.id, 'check_in_time': when,
                'check_out_time': when + timedelta(minutes=rnd.randint(12, 30)), 'state': 'done',
                'check_in_latitude': path[idx][0], 'check_in_longitude': path[idx][1], 'check_in_accuracy': 8,
                'outcome': rnd.choice(['order', 'follow_up', 'collection', 'demo']),
                'client_uid': 'demo-past-visit-%s-%s-%s' % (user.id, day, idx),
            })._update_geofence()
            visits += 1
        session.write({'state': 'closed', 'start_time': start, 'end_time': start + timedelta(hours=hours)})
        session._recompute_stats()
        return session, path, start

    @api.model
    def _generate_expenses(self):
        users = {key: self.env.ref('zivoit_field_tracking.demo_user_%s' % key, raise_if_not_found=False)
                 for key in PAST_DAYS}
        users = {k: u for k, u in users.items() if u}
        if not users or self.env['zivo.expense.claim'].sudo().search_count(
                [('user_id', 'in', [u.id for u in users.values()])]):
            return True
        rnd = random.Random(42)
        admin = self.env.ref('base.user_admin')
        Employee = self.env['hr.employee'].sudo()
        manager_emp = admin.employee_id
        for key, user in users.items():
            if not user.employee_id:
                Employee.create({
                    'name': user.name, 'user_id': user.id, 'company_id': user.company_id.id,
                    'job_title': 'Field Sales Executive', 'work_email': user.email,
                    'parent_id': manager_emp.id or False, 'expense_manager_id': admin.id,
                })
            user.sudo().zivo_vehicle_type = PAST_DAYS[key]['vehicle']
            self.env['zivo.track.session'].sudo().search([
                ('user_id', '=', user.id), ('claim_id', '=', False)]).write({'vehicle_type': PAST_DAYS[key]['vehicle']})
        partners = {}
        for name, _city, lat, lon in CUSTOMERS:
            partner = self.env['res.partner'].search([('name', '=', name)], limit=1)
            if partner:
                partners[(round(lat, 4), round(lon, 4))] = partner
        Line = self.env['zivo.expense.line'].sudo().with_context(zivo_internal=True)
        cats = {k: self.env.ref('zivoit_field_tracking.expense_category_%s' % k) for k in BILLS}
        today = fields.Date.context_today(self)
        days = []
        d = today - timedelta(days=1)
        while len(days) < 7:
            if d.weekday() != 6:
                days.append(d)
            d -= timedelta(days=1)
        days.reverse()
        for key, user in users.items():
            tz = pytz.timezone(user.tz or 'Asia/Kolkata')
            device = self.env['zivo.track.device'].sudo().search([('user_id', '=', user.id)], limit=1) or \
                self.env['zivo.track.device'].sudo().create({'user_id': user.id, 'name': '%s - Phone' % user.name})
            spec = PAST_DAYS[key]
            for n, day in enumerate(days):
                waypoints = ROUTES[spec['route']]['waypoints']
                if n % 2:
                    waypoints = list(reversed(waypoints))
                hours = rnd.choice([7.0, 7.5, 8.0, 8.5]) if n != 3 else 4.0  # one half day
                if key == 'sneha' and n == 2:
                    waypoints, hours = OUTSTATION, 10.0
                session, path, start = self._past_day(
                    user, device, day, waypoints, rnd, partners, spec['vehicle'], tz, hours=hours,
                    mock=(key == 'arjun' and n == 4))
                bills = []
                if key == 'sneha' and n == 2:
                    bills = [('lodging', 0, True), ('food', 2, True), ('toll', 1, True)]
                elif key == 'priya':
                    bills = [('conveyance', n % 2, True), ('food', 1, n != 5)]
                else:
                    bills = [('food', n % 3, True)]
                    if n in (1, 5):
                        bills.append(('toll', n % 2, True))
                    if key == 'ravi' and n == 4:
                        bills.append(('fuel', 0, True))       # fuel on a mileage day -> flagged
                    if key == 'ravi' and n == 6:
                        bills.append(('courier', 0, False))   # missing bill photo -> flagged
                    if key == 'arjun' and n == 2:
                        bills.append(('conveyance', 1, True))
                for b, (cat_key, idx, with_photo) in enumerate(bills):
                    shop, item, amount = BILLS[cat_key][idx % len(BILLS[cat_key])]
                    pt = path[min(len(path) - 1, int(len(path) * (0.3 + 0.2 * b)))]
                    lat, lon = pt
                    if key == 'arjun' and n == 2 and cat_key == 'conveyance':
                        lat, lon = lat + 0.06, lon + 0.05  # recorded far from the route -> flagged
                    when = start + timedelta(hours=1.5 + 2 * b)
                    Line.create({
                        'name': item, 'user_id': user.id, 'company_id': user.company_id.id, 'date': day,
                        'session_id': session.id, 'category_id': cats[cat_key].id, 'quantity': 1,
                        'unit_amount': amount, 'source': 'phone', 'latitude': lat, 'longitude': lon,
                        'gps_accuracy': 9, 'captured_offline': b == 1,
                        'client_uid': 'demo-bill-%s-%s-%s' % (user.id, day, b),
                        'receipt': _receipt_png(shop, item, amount, when + timedelta(minutes=330), rnd)
                        if with_photo else False,
                    })
        # claims in every state of the workflow
        Claim = self.env['zivo.expense.claim'].sudo()
        claims = {}
        for key, user in users.items():
            claim = Claim._create_for(user, days[0], days[-1])
            claims[key] = claim
        if 'ravi' in claims:
            travel = claims['ravi'].line_ids.filtered(lambda l: l.line_type == 'travel')[:1]
            if travel:  # claimed more km than tracked -> flagged
                travel.with_context(zivo_internal=True).write({'quantity': round(travel.tracked_km * 1.35, 1)})
                travel._refresh_warnings()
            claims['ravi'].note = 'Extra km: detour to the Ameerpet distributor (road closed).'
            claims['ravi'].action_submit()
        if 'sneha' in claims:
            claims['sneha'].action_submit()
            claims['sneha'].with_user(admin).sudo().action_approve()
            if claims['sneha'].state == 'approved' and claims['sneha'].employee_id:
                claims['sneha'].action_post_expenses()
        if 'arjun' in claims:
            claims['arjun'].action_submit()
            claims['arjun']._do_reject('Fake GPS app detected on one day and the auto bill was recorded '
                                       '6 km away from your route. Please explain and resubmit.')
        return True
