# -*- coding: utf-8 -*-
"""An approved late/early excuse must survive "delete + re-download".

Reported for ALAA MOHAMMAD A ALZAHER (KSWCO employee 9940, leave 4839):
attendance was deleted for July 2026 and re-downloaded via the "Download
Specific Attendance" wizard. The approved excuse for 5 July stopped applying
and the late deduction came back, even though the leave was still validated.

Root cause: `hr.leave.attendance.line.attendance_id` used `ondelete='cascade'`.
Deleting the attendance record destroyed the line that recorded the HR-approved
`accepted_minutes`, and the m2m junction row (`hr_leave_attendance_rel`, always
ON DELETE CASCADE) went with it. The re-downloaded punch gets a new id and
nothing re-attached the leave to it, so `_compute_net_minutes` saw zero
accepted minutes and the deduction reappeared.

Fix: `attendance_id` is now `ondelete='set null'`, so the line survives as an
orphan with its `date` / `hour_from` / `hour_to` / `accepted_minutes` intact,
and `hr.attendance._relink_attendance_issue_lines()` (called from create())
re-attaches it to the next punch that lands on the same employee + date.
"""
from datetime import datetime as dt

from .test_night_shift_leave import NightShiftLeaveCommon


class TestAttendanceIssueRelinkAfterRedownload(NightShiftLeaveCommon):

    def test_excuse_survives_delete_and_redownload(self):
        attendance = self._attendance(
            self.day_employee,
            dt.combine(self.shift_day, dt.min.time()).replace(hour=5, minute=30),
            dt.combine(self.shift_day, dt.min.time()).replace(hour=13, minute=45),
            late=30.0,
        )
        leave = self._excuse(self.day_employee, attendance)
        self.assertEqual(leave.state, 'validate')
        line = leave.x_attendance_line_ids
        self.assertEqual(len(line), 1)
        self.assertEqual(line.accepted_minutes, 30.0)
        self.assertEqual(attendance.x_net_late_minutes, 0.0,
                          'the approved excuse should fully cancel the late deduction')

        # Simulate "Clear Attendance" / delete for a targeted re-download.
        attendance.unlink()

        # The line survives as an orphan — its data is not lost.
        self.assertTrue(line.exists())
        self.assertFalse(line.attendance_id)
        self.assertEqual(line.accepted_minutes, 30.0)
        self.assertEqual(line.date, self.shift_day)

        # Re-download recreates the punch under a brand-new id.
        redownloaded = self._attendance(
            self.day_employee,
            dt.combine(self.shift_day, dt.min.time()).replace(hour=5, minute=30),
            dt.combine(self.shift_day, dt.min.time()).replace(hour=13, minute=45),
            late=30.0,
        )
        self.assertNotEqual(redownloaded.id, attendance.id)

        self.assertEqual(line.attendance_id, redownloaded)
        self.assertIn(redownloaded, leave.x_attendance_ids)
        self.assertEqual(
            redownloaded.x_net_late_minutes, 0.0,
            'the excuse must still cancel the deduction after redownload')

    def test_relink_ignores_leaves_not_yet_validated(self):
        """An orphaned line whose leave was refused/reset must not be relinked."""
        attendance = self._attendance(
            self.day_employee,
            dt.combine(self.shift_day, dt.min.time()).replace(hour=5, minute=30),
            dt.combine(self.shift_day, dt.min.time()).replace(hour=13, minute=45),
            late=30.0,
        )
        leave = self._excuse(self.day_employee, attendance)
        line = leave.x_attendance_line_ids
        attendance.unlink()
        leave.sudo().with_context(leave_skip_state_check=True).write({'state': 'refuse'})

        redownloaded = self._attendance(
            self.day_employee,
            dt.combine(self.shift_day, dt.min.time()).replace(hour=5, minute=30),
            dt.combine(self.shift_day, dt.min.time()).replace(hour=13, minute=45),
            late=30.0,
        )

        self.assertFalse(line.attendance_id)
        self.assertEqual(redownloaded.x_net_late_minutes, 30.0)
