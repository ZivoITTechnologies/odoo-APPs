# -*- coding: utf-8 -*-
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged

from .common import ZivoCommon


@tagged('post_install', '-at_install', 'zivo_event_connect')
class TestEventAccess(ZivoCommon, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._zivo_setup()
        cls.public_user = cls.env.ref('base.public_user')

    def _public_event(self, event, **ctx):
        return event.with_user(self.public_user).with_context(**ctx)

    def test_ip_range_matching(self):
        ev = self.ev_ip
        self.assertTrue(ev._zivo_ip_allowed('10.20.30.40'))
        self.assertTrue(ev._zivo_ip_allowed('192.168.1.200'))
        self.assertFalse(ev._zivo_ip_allowed('192.168.2.1'))
        self.assertFalse(ev._zivo_ip_allowed('8.8.8.8'))
        self.assertFalse(ev._zivo_ip_allowed('not-an-ip'))
        self.assertFalse(ev._zivo_ip_allowed(False))
        ev.zivo_ip_ranges = '2001:db8::/32\n203.0.113.7'
        self.assertTrue(ev._zivo_ip_allowed('2001:db8::1'))
        self.assertTrue(ev._zivo_ip_allowed('203.0.113.7'))
        self.assertFalse(ev._zivo_ip_allowed('203.0.113.8'))

    def test_ip_range_validation(self):
        with self.assertRaises(ValidationError):
            self.ev_ip.zivo_ip_ranges = '10.0.0.0/33'
        with self.assertRaises(ValidationError):
            self.env['event.event'].create({
                'name': 'No range', 'date_begin': self.ev_open.date_begin, 'date_end': self.ev_open.date_end,
                'zivo_access_mode': 'ip'})

    def test_public_visitor_ip_rule(self):
        self.assertFalse(self._public_event(self.ev_ip, zivo_remote_ip='8.8.8.8')._zivo_has_access())
        self.assertTrue(self._public_event(self.ev_ip, zivo_remote_ip='10.1.1.1')._zivo_has_access())

    def test_invitation_rule(self):
        token = self.invitation.token
        self.assertFalse(self._public_event(self.ev_invite, zivo_invite_tokens=[])._zivo_has_access())
        self.assertTrue(self._public_event(self.ev_invite, zivo_invite_tokens=[token])._zivo_has_access())
        self.assertFalse(self._public_event(self.ev_invite, zivo_invite_tokens=['bogus'])._zivo_has_access())
        self.invitation.action_revoke()
        self.assertFalse(self._public_event(self.ev_invite, zivo_invite_tokens=[token])._zivo_has_access())

    def test_invitation_logged_in_partner(self):
        portal = self.user_portal
        self.assertFalse(self.ev_invite.with_user(portal)._zivo_has_access())
        self.env['zivo.event.invitation'].create({'event_id': self.ev_invite.id, 'partner_id': portal.partner_id.id})
        self.assertTrue(self.ev_invite.with_user(portal)._zivo_has_access())

    def test_staff_can_preview(self):
        self.assertTrue(self.ev_invite.with_user(self.user_event)._zivo_has_access())
        self.assertTrue(self.ev_ip.with_user(self.user_event).with_context(zivo_remote_ip='8.8.8.8')._zivo_has_access())

    def test_website_visibility_search(self):
        Event = self.env['event.event'].with_user(self.public_user).with_context(
            zivo_remote_ip='8.8.8.8', zivo_invite_tokens=[])
        visible = Event.search([('is_visible_on_website', '=', True), ('name', 'like', 'ZEC ')])
        self.assertIn(self.ev_open, visible)
        self.assertNotIn(self.ev_ip, visible)
        self.assertNotIn(self.ev_invite, visible)
        Event = Event.with_context(zivo_remote_ip='10.0.0.5', zivo_invite_tokens=[self.invitation.token])
        visible = Event.search([('is_visible_on_website', '=', True), ('name', 'like', 'ZEC ')])
        self.assertIn(self.ev_ip, visible)
        self.assertIn(self.ev_invite, visible)
        # compute variant
        self.assertFalse(self.ev_ip.with_user(self.public_user).with_context(zivo_remote_ip='1.1.1.1').is_visible_on_website)

    def test_registration_marks_invitation(self):
        reg = self.env['event.registration'].create({
            'event_id': self.ev_invite.id, 'partner_id': self.partner_guest.id,
            'name': 'Guest Invitee', 'email': 'guest.invitee@example.com'})
        self.assertEqual(self.invitation.state, 'registered')
        self.assertEqual(self.invitation.registration_id, reg)

    def test_invite_wizard(self):
        p1 = self.env['res.partner'].create({'name': 'W1', 'email': 'w1@example.com'})
        self.dept_hr.member_ids = [(4, self.user_requester.id)]
        wizard = self.env['zivo.event.invite.wizard'].create({
            'event_id': self.ev_invite.id,
            'partner_ids': [(6, 0, [p1.id, self.partner_guest.id])],
            'department_ids': [(6, 0, self.dept_hr.ids)],
            'emails': 'extra.one@example.com\nnot-an-email\nW1@example.com',
        })
        wizard.action_invite()
        emails = set(self.ev_invite.zivo_invitation_ids.mapped('email'))
        self.assertEqual(emails, {'guest.invitee@example.com', 'w1@example.com', 'rita@example.com',
                                  'extra.one@example.com'})
        sent = self.ev_invite.zivo_invitation_ids.filtered(lambda i: i.email == 'w1@example.com')
        self.assertEqual(sent.state, 'sent')
        self.assertTrue(sent.invite_url.endswith('/event/invite/%s' % sent.token))

    def test_department_domain_includes_children(self):
        domain = self.env['event.event']._zivo_department_domain('hrx')
        events = self.env['event.event'].search(domain)
        self.assertIn(self.ev_payroll, events)
        self.assertNotIn(self.ev_open, events)
        by_id = self.env['event.event'].search(self.env['event.event']._zivo_department_domain(str(self.dept_it.id)))
        self.assertIn(self.ev_open, by_id)
        self.assertFalse(self.env['event.event'].search(self.env['event.event']._zivo_department_domain('nope')))

    def test_empty_access_mode_is_open(self):
        self.ev_open.zivo_access_mode = False
        self.assertTrue(self.ev_open.with_user(self.public_user)._zivo_has_access())
        visible = self.env['event.event'].with_user(self.public_user).search(
            [('is_visible_on_website', '=', True), ('name', '=', 'ZEC Open Day')])
        self.assertEqual(visible, self.ev_open)
        self.assertEqual(self.ev_open._zivo_api_payload()['access_mode'], 'open')
