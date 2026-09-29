# -*- coding: utf-8 -*-
"""Excuse lines follow their leave's access and freeze once decided.

Before: `hr.leave.attendance.line` was open to every internal user with no
record rule, so anyone could read anyone's lateness and raise anyone's
`accepted_minutes` — the figure that cancels the late/early deduction.
"""
from datetime import datetime as dt

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import new_test_user

from .test_night_shift_leave import NightShiftLeaveCommon


class TestAttendanceLineAccess(NightShiftLeaveCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.owner_user = new_test_user(cls.env, login='excuse_owner',
                                       groups='base.group_user')
        cls.day_employee.user_id = cls.owner_user
        cls.stranger = new_test_user(cls.env, login='excuse_stranger',
                                     groups='base.group_user')
        cls.env['hr.employee'].create({'name': 'Stranger',
                                       'user_id': cls.stranger.id})
        cls.officer = new_test_user(
            cls.env, login='excuse_officer',
            groups='base.group_user,hr_holidays.group_hr_holidays_manager')

    def _validated_excuse(self):
        att = self._attendance(
            self.day_employee,
            dt.combine(self.shift_day, dt.min.time()).replace(hour=5, minute=30),
            dt.combine(self.shift_day, dt.min.time()).replace(hour=13, minute=45),
            late=30.0,
        )
        leave = self._excuse(self.day_employee, att)
        self.assertEqual(leave.state, 'validate')
        return leave, leave.x_attendance_line_ids

    def _make_pending(self, leave):
        self.env.cr.execute(
            "UPDATE hr_leave SET state='confirm' WHERE id=%s", [leave.id])
        leave.invalidate_recordset(['state'])

    def test_stranger_cannot_read_or_write_someone_elses_line(self):
        leave, line = self._validated_excuse()
        self._make_pending(leave)
        with self.assertRaises(AccessError):
            line.with_user(self.stranger).read(['accepted_minutes'])
        with self.assertRaises(AccessError):
            line.with_user(self.stranger).write({'accepted_minutes': 0.0})
        with self.assertRaises(AccessError):
            line.with_user(self.stranger).unlink()

    def test_owner_can_adjust_own_pending_request(self):
        leave, line = self._validated_excuse()
        self._make_pending(leave)
        line.with_user(self.owner_user).write({'accepted_minutes': 10.0})
        self.assertEqual(line.accepted_minutes, 10.0)

    def test_nobody_changes_a_decided_excuse(self):
        leave, line = self._validated_excuse()
        with self.assertRaises(UserError):
            line.with_user(self.officer).write({'accepted_minutes': 0.0})
        with self.assertRaises(UserError):
            line.with_user(self.officer).unlink()
        with self.assertRaises(UserError):
            line.with_user(self.owner_user).write({'accepted_minutes': 0.0})
        self.assertEqual(line.accepted_minutes, 30.0)

    def test_stranger_search_finds_nothing(self):
        leave, line = self._validated_excuse()
        found = self.env['hr.leave.attendance.line'].with_user(
            self.stranger).search([('id', '=', line.id)])
        self.assertFalse(found)
        mine = self.env['hr.leave.attendance.line'].with_user(
            self.owner_user).search([('id', '=', line.id)])
        self.assertEqual(mine, line)

    def test_system_relink_still_allowed(self):
        leave, line = self._validated_excuse()
        line.sudo().write({'attendance_id': False})
        self.assertFalse(line.attendance_id)
