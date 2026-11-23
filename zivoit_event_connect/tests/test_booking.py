# -*- coding: utf-8 -*-
from datetime import datetime, time, timedelta

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from .common import ZivoCommon


@tagged('post_install', '-at_install', 'zivo_event_connect')
class TestEquipmentBooking(ZivoCommon, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._zivo_setup()
        cls.projector = cls.env['zivo.equipment'].create({
            'name': 'Projector', 'quantity': 2, 'tz': 'UTC', 'slot_duration': 1.0,
            'open_hour': 8.0, 'close_hour': 12.0, 'approver_ids': [(6, 0, cls.user_approver.ids)],
        })
        cls.laptop = cls.env['zivo.equipment'].create({
            'name': 'Laptop', 'quantity': 5, 'tz': 'UTC', 'requires_approval': False})
        cls.monday = cls._next_weekday(0)
        cls.saturday = cls._next_weekday(5)

    def _dt(self, day, hour):
        return datetime.combine(day, time(hour, 0))

    def _booking(self, equipment=None, start=9, stop=10, qty=1, user=None, day=None):
        user = user or self.user_requester
        return self.env['zivo.equipment.booking'].with_user(user).create({
            'equipment_id': (equipment or self.projector).id,
            'start': self._dt(day or self.monday, start),
            'stop': self._dt(day or self.monday, stop),
            'quantity': qty,
        })

    def test_slots(self):
        slots = self.projector._zivo_get_slots(self.monday)
        self.assertEqual([s['label'] for s in slots], ['08:00 - 09:00', '09:00 - 10:00', '10:00 - 11:00', '11:00 - 12:00'])
        self.assertTrue(all(s['bookable'] and s['available'] == 2 for s in slots))
        self.assertEqual(self.projector._zivo_get_slots(self.saturday), [], 'closed on Saturday')
        b = self._booking(qty=2)
        b.action_submit()
        slots = {s['label']: s for s in self.projector._zivo_get_slots(self.monday)}
        self.assertEqual(slots['09:00 - 10:00']['available'], 0)
        self.assertFalse(slots['09:00 - 10:00']['bookable'])
        self.assertEqual(slots['10:00 - 11:00']['available'], 2)

    def test_slots_timezone(self):
        self.projector.tz = 'Asia/Kolkata'
        slots = self.projector._zivo_get_slots(self.monday)
        # 08:00 IST == 02:30 UTC
        self.assertEqual(slots[0]['start'], self._dt(self.monday, 2) + timedelta(minutes=30))

    def test_peak_availability(self):
        # two bookings that do not overlap each other but both overlap 9-11
        b1 = self._booking(start=9, stop=10)
        b2 = self._booking(start=10, stop=11)
        (b1 | b2).action_submit()
        self.assertEqual(self.projector._zivo_available_qty(self._dt(self.monday, 9), self._dt(self.monday, 11)), 1)
        b3 = self._booking(start=9, stop=11)
        b3.action_submit()  # still 1 unit free over the interval
        b4 = self._booking(start=9, stop=10)
        with self.assertRaises(ValidationError):
            b4.action_submit()

    def test_draft_does_not_reserve(self):
        self._booking(qty=2)  # draft
        self.assertEqual(self.projector._zivo_available_qty(self._dt(self.monday, 9), self._dt(self.monday, 10)), 2)

    def test_quantity_and_dates(self):
        with self.assertRaises(ValidationError):
            self._booking(qty=3)
        with self.assertRaises(ValidationError):
            self._booking(start=10, stop=9)

    def test_window_checks(self):
        with self.assertRaises(ValidationError):
            self._booking(day=self.saturday).action_submit()
        with self.assertRaises(ValidationError):
            self._booking(start=7, stop=9).action_submit()
        self.projector.max_slots_per_booking = 2
        with self.assertRaises(ValidationError):
            self._booking(start=8, stop=11).action_submit()

    def test_approval_flow(self):
        b = self._booking()
        b.action_submit()
        self.assertEqual(b.state, 'submitted')
        activity = b.activity_ids
        self.assertEqual(activity.user_id, self.user_approver)
        self.assertFalse(b.with_user(self.user_requester).can_approve)
        with self.assertRaises(AccessError):
            b.with_user(self.user_requester).action_approve()
        b_appr = b.with_user(self.user_approver)
        self.assertTrue(b_appr.can_approve)
        b_appr.action_approve()
        self.assertEqual(b.state, 'approved')
        self.assertEqual(b.approver_id, self.user_approver)
        self.assertFalse(b.activity_ids)
        self.assertIn(self.user_requester.partner_id, b.message_partner_ids)
        b_appr.action_mark_returned()
        self.assertEqual(b.state, 'done')

    def test_reject_flow(self):
        b = self._booking()
        b.action_submit()
        wizard = self.env['zivo.booking.reject.wizard'].with_user(self.user_approver).create({
            'booking_ids': [(6, 0, b.ids)], 'reason': 'Needed for board meeting'})
        wizard.action_confirm()
        self.assertEqual(b.state, 'rejected')
        self.assertEqual(b.reject_reason, 'Needed for board meeting')
        self.assertFalse(b.activity_ids)
        b.with_user(self.user_requester).action_reset_draft()
        self.assertEqual(b.state, 'draft')
        with self.assertRaises(UserError):
            b.with_user(self.user_approver).action_approve()

    def test_auto_approval(self):
        b = self._booking(equipment=self.laptop, qty=3)
        b.action_submit()
        self.assertEqual(b.state, 'approved')

    def test_default_approvers_group(self):
        eq = self.env['zivo.equipment'].create({'name': 'Mic', 'quantity': 1, 'tz': 'UTC'})
        approvers = eq._zivo_get_approvers()
        self.assertIn(self.user_approver, approvers)
        self.assertNotIn(self.user_requester, approvers)
        eq.department_id = self.dept_it
        self.dept_it.manager_id = self.user_event
        eq.approver_ids = self.user_approver
        self.assertEqual(set(eq._zivo_get_approvers().ids), {self.user_event.id, self.user_approver.id})

    def test_record_rules(self):
        mine = self._booking()
        other = self._booking(user=self.user_approver, start=10, stop=11)
        visible = self.env['zivo.equipment.booking'].with_user(self.user_requester).search([])
        self.assertIn(mine, visible)
        self.assertNotIn(other, visible)
        visible = self.env['zivo.equipment.booking'].with_user(self.user_approver).search([])
        self.assertIn(mine, visible)

    def test_cancel_rights(self):
        b = self._booking()
        b.action_submit()
        with self.assertRaises(AccessError):
            b.with_user(self.user_event).action_cancel()
        b.with_user(self.user_requester).action_cancel()
        self.assertEqual(b.state, 'cancel')
        self.assertFalse(b.activity_ids)

    def test_sequence_and_audit(self):
        b = self._booking()
        self.assertTrue(b.name.startswith('EQB/'))
        b.action_submit()
        log = self.env['zivo.audit.log'].search([('model', '=', 'zivo.equipment.booking'), ('res_id', '=', b.id),
                                                 ('action', '=', 'write')])
        self.assertTrue(any('Waiting Approval' in (l.changes or '') for l in log))
