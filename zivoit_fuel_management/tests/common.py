# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class PumpCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.ref_ = lambda self_or_cls, x: env.ref('zivoit_fuel_management.%s' % x)
        r = lambda x: env.ref('zivoit_fuel_management.%s' % x)
        cls.today = fields.Date.context_today(env['res.partner'])
        cls.d0 = cls.today - timedelta(days=10)
        cls.hsd, cls.ms = r('product_hsd'), r('product_ms')
        env['pump.rate'].create([{'product_id': cls.hsd.id, 'date': cls.d0, 'rate': 95.0},
                                 {'product_id': cls.ms.id, 'date': cls.d0, 'rate': 105.0}])
        cls.tank = env['pump.tank'].create({'name': 'T-HSD', 'product_id': cls.hsd.id, 'capacity': 20000,
                                            'opening_stock': 10000, 'opening_date': cls.d0})
        cls.tank.action_post_opening()
        cls.tank_ms = env['pump.tank'].create({'name': 'T-MS', 'product_id': cls.ms.id, 'capacity': 15000,
                                               'opening_stock': 8000, 'opening_date': cls.d0})
        cls.tank_ms.action_post_opening()
        cls.n1 = env['pump.nozzle'].create({'name': 'N1', 'tank_id': cls.tank.id, 'opening_meter': 1000.0})
        cls.n2 = env['pump.nozzle'].create({'name': 'N2', 'tank_id': cls.tank_ms.id, 'opening_meter': 500.0})
        cls.shift1, cls.shift2 = r('shift_1'), r('shift_2')
        cls.customer = env['res.partner'].create({
            'name': 'Test Haulage Co', 'is_company': True, 'is_pump_customer': True,
            'pump_credit_limit': 50000.0, 'email': 'haulage@example.com'})
        cls.vehicle = env['pump.vehicle'].create({'name': 'TS01AB1234', 'partner_id': cls.customer.id,
                                                  'product_id': cls.hsd.id, 'tank_capacity': 600})
        cls.other = env['res.partner'].create({'name': 'Other Fleet', 'is_company': True, 'is_pump_customer': True,
                                               'pump_credit_limit': 100000.0})

    def _slip(self, qty, partner=None, date=None, shift=None, confirm=True):
        s = self.env['pump.credit.sale'].create({
            'partner_id': (partner or self.customer).id, 'product_id': self.hsd.id, 'qty': qty,
            'date': date or self.d0, 'shift_id': (shift or self.shift1).id,
            'vehicle_id': self.vehicle.id if not partner else False})
        if confirm:
            s.action_confirm()
        return s

    def _acc(self, x):
        return self.env.ref('zivoit_fuel_management.%s' % x)

    def _balance(self, acc):
        self.env['pump.account'].invalidate_model(['balance'])
        return round(acc.balance, 2)
