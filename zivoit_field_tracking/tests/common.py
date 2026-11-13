# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase, new_test_user


class ZivoTrackCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.sales1 = new_test_user(cls.env, login='zv_sales1', name='Field Sales One', tz='Asia/Kolkata',
                                   groups='zivoit_field_tracking.group_field_user')
        cls.sales2 = new_test_user(cls.env, login='zv_sales2', name='Field Sales Two', tz='Asia/Kolkata',
                                   groups='zivoit_field_tracking.group_field_user')
        cls.manager = new_test_user(cls.env, login='zv_manager', name='Field Manager', tz='Asia/Kolkata',
                                    groups='zivoit_field_tracking.group_field_manager')
        cls.outsider = new_test_user(cls.env, login='zv_outsider', name='No Tracking', groups='base.group_user')
        Device = cls.env['zivo.track.device']
        cls.dev1 = Device.create({'name': 'S1 phone', 'user_id': cls.sales1.id, 'source': 'pwa'})
        cls.dev2 = Device.create({'name': 'S2 phone', 'user_id': cls.sales2.id, 'source': 'traccar'})
        cls.customer = cls.env['res.partner'].create({
            'name': 'Ameerpet Traders', 'is_company': True,
            'partner_latitude': 17.4375, 'partner_longitude': 78.4483,
        })
        cls.Point = cls.env['zivo.track.point']

    def pwa_item(self, uid, lat, lon, minutes_ago=0, **extra):
        ts = fields.Datetime.now() - timedelta(minutes=minutes_ago)
        item = {'uid': uid, 'lat': lat, 'lon': lon, 'ts': ts.strftime('%Y-%m-%dT%H:%M:%SZ'), 'accuracy': 8, 'speed': 5}
        item.update(extra)
        return self.Point._normalize_pwa(item)
