# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

from odoo import fields
from odoo.tests import new_test_user


class ZivoCommon:

    @classmethod
    def _zivo_setup(cls):
        env = cls.env
        cls.dept_hr = env['zivo.event.department'].create({'name': 'Human Resources', 'code': 'hrx'})
        cls.dept_hr_payroll = env['zivo.event.department'].create(
            {'name': 'Payroll', 'code': 'payrollx', 'parent_id': cls.dept_hr.id})
        cls.dept_it = env['zivo.event.department'].create({'name': 'IT', 'code': 'itx'})
        now = fields.Datetime.now()
        base = {
            'date_begin': now + timedelta(days=5),
            'date_end': now + timedelta(days=5, hours=6),
            'website_published': True,
        }
        Event = env['event.event']
        cls.ev_open = Event.create(dict(base, name='ZEC Open Day', zivo_department_ids=[(6, 0, cls.dept_it.ids)]))
        cls.ev_payroll = Event.create(dict(base, name='ZEC Payroll Clinic',
                                           zivo_department_ids=[(6, 0, cls.dept_hr_payroll.ids)]))
        cls.ev_ip = Event.create(dict(base, name='ZEC Intranet Workshop', zivo_access_mode='ip',
                                      zivo_ip_ranges='10.0.0.0/8\n192.168.1.0/24'))
        cls.ev_invite = Event.create(dict(base, name='ZEC Board Dinner', zivo_access_mode='invite'))
        cls.ev_unpublished = Event.create(dict(base, name='ZEC Draft Event', website_published=False))
        cls.ev_past = Event.create(dict(base, name='ZEC Past Event', date_begin=now - timedelta(days=10),
                                        date_end=now - timedelta(days=9)))
        cls.partner_guest = env['res.partner'].create({'name': 'Guest Invitee', 'email': 'guest.invitee@example.com'})
        cls.invitation = env['zivo.event.invitation'].create({
            'event_id': cls.ev_invite.id, 'partner_id': cls.partner_guest.id})
        cls.user_portal = new_test_user(env, login='zec_portal', groups='base.group_portal',
                                        name='Portal Paula', email='paula@example.com')
        cls.user_requester = new_test_user(env, login='zec_requester',
                                           groups='zivoit_event_connect.group_equipment_user',
                                           name='Requester Rita', email='rita@example.com')
        cls.user_approver = new_test_user(env, login='zec_approver',
                                          groups='zivoit_event_connect.group_equipment_approver',
                                          name='Approver Arjun', email='arjun@example.com')
        cls.user_event = new_test_user(env, login='zec_event_user', groups='event.group_event_user',
                                       name='Event Eva', email='eva@example.com')

    @staticmethod
    def _next_weekday(weekday=0):
        """Return the next date (>= 2 days from today) falling on ``weekday`` (0 = Monday)."""
        day = datetime.utcnow().date() + timedelta(days=2)
        while day.weekday() != weekday:
            day += timedelta(days=1)
        return day
