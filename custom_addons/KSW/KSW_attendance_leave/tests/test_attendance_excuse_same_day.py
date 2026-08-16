# -*- coding: utf-8 -*-
"""Same-day attendance excuses must not surface a phantom Early Leave issue.

Reported on ticket 5015: an employee checked in late and immediately filed an
excuse for the same, still-open day. Because the day hadn't finished, the
attendance record's `x_early_leave_minutes` was computed against a
placeholder check-out (effectively `scheduled_end - check_in`), so
`_generate_attendance_lines` produced a bogus Early Leave line alongside the
real Late line -- from check-in time all the way to the scheduled shift end.

Fix: only generate the Early Leave line once the attendance record's date is
strictly before today. The Late line (and everything else) is unaffected,
and the employee can still file the Early Leave excuse later once the day
has actually ended.
"""
from datetime import datetime as dt, timedelta

from odoo import fields

from .test_night_shift_leave import NightShiftLeaveCommon


class TestAttendanceExcuseSameDay(NightShiftLeaveCommon):

    def test_early_leave_suppressed_for_todays_open_day(self):
        today = fields.Date.context_today(self.env['hr.leave'])
        attendance = self._attendance(
            self.day_employee,
            dt.combine(today, dt.min.time()).replace(hour=8, minute=30),
            dt.combine(today, dt.min.time()).replace(hour=8, minute=30),
            late=30.0, early=485.0,
        )
        leave = self._excuse(self.day_employee, attendance)

        self.assertEqual(len(leave.x_attendance_line_ids), 1)
        self.assertEqual(leave.x_attendance_line_ids.issue_type, 'late')

    def test_early_leave_available_once_day_has_passed(self):
        yesterday = fields.Date.context_today(self.env['hr.leave']) - timedelta(days=1)
        attendance = self._attendance(
            self.day_employee,
            dt.combine(yesterday, dt.min.time()).replace(hour=8, minute=30),
            dt.combine(yesterday, dt.min.time()).replace(hour=16, minute=0),
            late=30.0, early=30.0,
        )
        leave = self._excuse(self.day_employee, attendance)

        issue_types = set(leave.x_attendance_line_ids.mapped('issue_type'))
        self.assertEqual(issue_types, {'late', 'early_leave'})

    def test_late_only_today_is_unaffected(self):
        today = fields.Date.context_today(self.env['hr.leave'])
        attendance = self._attendance(
            self.day_employee,
            dt.combine(today, dt.min.time()).replace(hour=8, minute=30),
            dt.combine(today, dt.min.time()).replace(hour=8, minute=30),
            late=30.0, early=0.0,
        )
        leave = self._excuse(self.day_employee, attendance)

        self.assertEqual(len(leave.x_attendance_line_ids), 1)
        self.assertEqual(leave.x_attendance_line_ids.issue_type, 'late')
