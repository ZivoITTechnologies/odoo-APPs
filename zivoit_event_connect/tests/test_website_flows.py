# -*- coding: utf-8 -*-
import re
from datetime import timedelta

from odoo.tests import HttpCase, tagged

from .common import ZivoCommon


@tagged('post_install', '-at_install', 'zivo_event_connect')
class TestWebsiteFlows(ZivoCommon, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._zivo_setup()
        cls.equipment = cls.env['zivo.equipment'].create({
            'name': 'ZEC Projector', 'quantity': 2, 'tz': 'UTC', 'slot_duration': 1.0, 'open_hour': 8.0,
            'close_hour': 18.0, 'approver_ids': [(6, 0, cls.user_approver.ids)],
            'open_sat': True, 'open_sun': True})

    # ------------------------------------------------------------------ events
    def test_open_event_page(self):
        res = self.url_open('/event/%s/register' % self.ev_open.id)
        self.assertEqual(res.status_code, 200)

    def test_ip_event_blocked_from_localhost(self):
        res = self.url_open('/event/%s/register' % self.ev_ip.id)
        self.assertEqual(res.status_code, 403)
        self.assertIn('private', res.text.lower())
        self.ev_ip.zivo_ip_ranges = '127.0.0.0/8\n::1/128'
        res = self.url_open('/event/%s/register' % self.ev_ip.id)
        self.assertEqual(res.status_code, 200)

    def test_invitation_link_unlocks_event(self):
        url = '/event/%s/register' % self.ev_invite.id
        self.assertEqual(self.url_open(url).status_code, 403)
        listing = self.url_open('/event?search=ZEC')
        self.assertNotIn('ZEC Board Dinner', listing.text)
        res = self.url_open('/event/invite/%s' % self.invitation.token)
        self.assertEqual(res.status_code, 200)
        self.assertIn('ZEC Board Dinner', res.text)
        self.invitation.invalidate_recordset()
        self.assertEqual(self.invitation.state, 'opened')
        self.assertEqual(self.invitation.open_count, 1)
        # session remembers the invitation
        self.assertEqual(self.url_open(url).status_code, 200)
        self.assertIn('ZEC Board Dinner', self.url_open('/event?search=ZEC').text)
        self.assertEqual(self.url_open('/event/invite/doesnotexist').status_code, 404)

    def test_staff_preview(self):
        self.authenticate('zec_event_user', 'zec_event_user')
        self.assertEqual(self.url_open('/event/%s/register' % self.ev_invite.id).status_code, 200)

    def test_department_filter_listing(self):
        res = self.url_open('/event?department=hrx')
        self.assertIn('ZEC Payroll Clinic', res.text)
        self.assertNotIn('ZEC Open Day', res.text)

    # --------------------------------------------------------------- equipment
    def _csrf(self, html):
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    def test_portal_booking_flow(self):
        self.authenticate('zec_portal', 'zec_portal')
        res = self.url_open('/equipment')
        self.assertEqual(res.status_code, 200)
        self.assertIn('ZEC Projector', res.text)
        day = self._next_weekday(2)
        res = self.url_open('/equipment/%s?date=%s' % (self.equipment.id, day))
        self.assertEqual(res.status_code, 200)
        self.assertIn('10:00 - 11:00', res.text)
        csrf = self._csrf(res.text)
        data = [('csrf_token', csrf), ('date', str(day)), ('slot', '%s 10:00:00' % day), ('slot', '%s 11:00:00' % day),
                ('quantity', '2'), ('purpose', 'Client demo'), ('event_id', str(self.ev_open.id))]
        res = self.url_open('/equipment/%s/book' % self.equipment.id, data=data)
        self.assertEqual(res.status_code, 200)
        self.assertIn('/my/equipment-bookings/', res.url)
        booking = self.env['zivo.equipment.booking'].search([('equipment_id', '=', self.equipment.id)])
        self.assertEqual(len(booking), 1)
        self.assertEqual(booking.state, 'submitted')
        self.assertEqual(booking.user_id, self.user_portal)
        self.assertEqual(booking.quantity, 2)
        self.assertEqual(booking.duration, 2.0)
        self.assertEqual(booking.source, 'portal')
        self.assertEqual(booking.event_id, self.ev_open)
        self.assertIn('Waiting Approval', res.text)
        # slots now full
        res = self.url_open('/equipment/%s?date=%s' % (self.equipment.id, day))
        self.assertIn('Fully booked', res.text)
        # non-consecutive slots are refused
        data = [('csrf_token', csrf), ('date', str(day)), ('slot', '%s 13:00:00' % day), ('slot', '%s 15:00:00' % day)]
        res = self.url_open('/equipment/%s/book' % self.equipment.id, data=data)
        self.assertIn('consecutive', res.text)
        # booked slot no longer available
        data = [('csrf_token', csrf), ('date', str(day)), ('slot', '%s 10:00:00' % day)]
        res = self.url_open('/equipment/%s/book' % self.equipment.id, data=data)
        self.assertIn('no longer available', res.text)
        # my bookings & cancel
        res = self.url_open('/my/equipment-bookings')
        self.assertIn(booking.name, res.text)
        res = self.url_open('/my/equipment-bookings/%s/cancel' % booking.id, data={'csrf_token': csrf})
        self.assertEqual(res.status_code, 200)
        booking.invalidate_recordset()
        self.assertEqual(booking.state, 'cancel')

    def test_portal_cannot_see_others(self):
        other = self.env['zivo.equipment.booking'].create({
            'equipment_id': self.equipment.id, 'user_id': self.user_requester.id,
            'start': self.ev_open.date_begin, 'stop': self.ev_open.date_begin + timedelta(hours=1)})
        self.authenticate('zec_portal', 'zec_portal')
        self.assertEqual(self.url_open('/my/equipment-bookings/%s' % other.id).status_code, 404)

    def test_portal_home_counter(self):
        self.authenticate('zec_portal', 'zec_portal')
        res = self.url_open('/my/home')
        self.assertIn('/my/equipment-bookings', res.text)
