# -*- coding: utf-8 -*-
import base64
import io
from datetime import date, datetime, time, timedelta

import pytz

from odoo.tests.common import TransactionCase, new_test_user


def jpeg_b64(color=(200, 30, 30)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (60, 80), color).save(buf, format='JPEG')
    return base64.b64encode(buf.getvalue())


class ZivoExpenseCommon(TransactionCase):
    """Two salespeople with employees, a team manager approver and a known policy."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True, mail_notrack=True))
        cls.company = cls.env.company
        cls.manager = new_test_user(cls.env, login='zx_manager', name='Field Manager X', tz='Asia/Kolkata',
                                    groups='zivoit_field_tracking.group_field_manager')
        cls.approver = new_test_user(cls.env, login='zx_approver', name='Team Lead X', tz='Asia/Kolkata',
                                     groups='zivoit_field_tracking.group_field_user')
        cls.sales = new_test_user(cls.env, login='zx_sales', name='Sales X', tz='Asia/Kolkata',
                                  groups='zivoit_field_tracking.group_field_user')
        cls.sales2 = new_test_user(cls.env, login='zx_sales2', name='Sales Y', tz='Asia/Kolkata',
                                   groups='zivoit_field_tracking.group_field_user')
        Employee = cls.env['hr.employee'].sudo()
        cls.emp_approver = Employee.create({'name': 'Team Lead X', 'user_id': cls.approver.id})
        cls.emp = Employee.create({'name': 'Sales X', 'user_id': cls.sales.id, 'parent_id': cls.emp_approver.id,
                                   'expense_manager_id': cls.approver.id})
        cls.emp2 = Employee.create({'name': 'Sales Y', 'user_id': cls.sales2.id})
        # deterministic policy for the tests (the default policy of the company is replaced)
        cls.env['zivo.expense.policy'].search([('company_id', '=', cls.company.id)]).write({'is_default': False})
        cls.policy = cls.env['zivo.expense.policy'].create({
            'name': 'Test Policy', 'is_default': True, 'company_id': cls.company.id,
            'da_full_amount': 400, 'da_full_min_hours': 6, 'da_half_amount': 200, 'da_half_min_hours': 3,
            'da_min_visits': 0, 'outstation_km': 100, 'da_outstation_amount': 900,
            'km_tolerance_pct': 10, 'location_check_km': 2,
            'rate_ids': [(0, 0, {'vehicle_type': 'two_wheeler', 'rate_per_km': 4.0, 'max_km_per_day': 50}),
                         (0, 0, {'vehicle_type': 'four_wheeler', 'rate_per_km': 10.0, 'max_km_per_day': 0}),
                         (0, 0, {'vehicle_type': 'public', 'rate_per_km': 0.0})],
        })
        cls.cat_food = cls.env.ref('zivoit_field_tracking.expense_category_food')
        cls.cat_fuel = cls.env.ref('zivoit_field_tracking.expense_category_fuel')
        cls.cat_toll = cls.env.ref('zivoit_field_tracking.expense_category_toll')
        cls.cat_ta = cls.env.ref('zivoit_field_tracking.expense_category_ta')
        cls.cat_da = cls.env.ref('zivoit_field_tracking.expense_category_da')
        cls.cat_food.write({'receipt_policy': 'above', 'receipt_threshold': 100, 'daily_limit': 500})
        cls.cat_fuel.write({'receipt_policy': 'always', 'conflicts_with_mileage': True})
        Device = cls.env['zivo.track.device']
        cls.dev = Device.create({'name': 'X phone', 'user_id': cls.sales.id, 'source': 'pwa'})
        cls.dev2 = Device.create({'name': 'Y phone', 'user_id': cls.sales2.id, 'source': 'pwa'})
        cls.day1 = date.today() - timedelta(days=3)
        cls.day2 = date.today() - timedelta(days=2)

    # ------------------------------------------------------------------ helpers
    @classmethod
    def track_day(cls, device, day, km, hours, start_hour=9.0, mock=False, close=True, lat0=17.40, lon0=78.45):
        """Ingest a straight north-bound route of ``km`` over ``hours`` on local ``day``."""
        tz = pytz.timezone('Asia/Kolkata')
        start = tz.localize(datetime.combine(day, time(0, 0)) + timedelta(hours=start_hour)) \
            .astimezone(pytz.utc).replace(tzinfo=None)
        steps = max(2, int(hours * 6))  # a point every 10 minutes
        Point = cls.env['zivo.track.point']
        deg = km / 111.195  # km per degree of latitude
        items = []
        for i in range(steps + 1):
            when = start + timedelta(hours=hours * i / steps)
            items.append(Point._normalize_pwa({
                'uid': 'td-%s-%s-%s' % (device.id, day, i), 'lat': lat0 + deg * i / steps, 'lon': lon0,
                'ts': when.strftime('%Y-%m-%dT%H:%M:%SZ'), 'accuracy': 8, 'mock': mock and i == 1,
            }))
        Point._ingest(device, items)
        session = cls.env['zivo.track.session'].search([('user_id', '=', device.user_id.id), ('date', '=', day)])
        if close:
            session.sudo().write({'state': 'closed'})
        return session

    def bill(self, user, day, category, amount, **vals):
        values = {'name': category.name, 'user_id': user.id, 'date': day, 'category_id': category.id,
                  'unit_amount': amount}
        values.update(vals)
        return self.env['zivo.expense.line'].with_user(user).create(values)
