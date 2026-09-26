"""The leave lock on an attendance sheet line refuses CHANGES, not no-ops.

Reported case: an approved Annual Vacation covers part of a month, so the
sheet lines inside the leave are absent and locked (``x_leave_id``). The
supervisor then presses "Mark All Absent" to settle the rest of the month —
and the whole action aborted with

    Cannot modify attendance for days locked by an approved leave (...)

even though it was writing ``is_attended = False`` over days that were
already absent. There was no way left to mark the remaining days absent.
"""
from datetime import date

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestSheetLockMarkAllAbsent(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.calendar_group = cls.env['resource.calendar.group'].create({
            'name': 'Lock Test Group',
        })
        for day in ['0', '1', '2', '3', '6']:  # Mon-Thu + Sun
            cls.env['resource.calendar.group.line'].create({
                'name': f'Work Day {day}',
                'calendar_group_id': cls.calendar_group.id,
                'dayofweek': day,
                'day_period': 'full_day',
                'hour_from': 8.0,
                'hour_to': 16.5,
            })
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Lock Test Calendar',
            'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, cls.calendar_group.id)],
        })

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Lock Test Employee',
            'resource_calendar_id': cls.calendar.id,
            'x_is_attendance_sheet': True,
        })

        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'Lock Test Annual',
            'requires_allocation': False,
            'is_annual_leave': True,
        })

    def _locked_sheet(self):
        """A March 2027 sheet whose 1st-15th workdays are locked by a leave."""
        sheet = self.env['ksw.attendance.sheet'].sudo().create({
            'employee_id': self.employee.id,
            'month': '3',
            'year': 2027,
        })
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': date(2027, 3, 1),
            'request_date_to': date(2027, 3, 15),
        })
        locked = sheet.line_ids.filtered(
            lambda l: l.date <= date(2027, 3, 15))
        # Same two writes _lock_attendance_sheet_lines performs.
        locked.with_context(ksw_system_write=True).write(
            {'is_attended': False})
        locked.write({'x_leave_id': leave.id})
        return sheet, leave, locked

    def test_mark_all_absent_works_with_locked_days(self):
        sheet, _leave, locked = self._locked_sheet()

        sheet.action_mark_all_absent()  # must not raise

        self.assertTrue(
            all(not l.is_attended for l in sheet.line_ids.filtered('is_workday')),
            'Every workday of the month must end up absent')
        self.assertTrue(
            all(l.x_leave_id for l in locked),
            'The leave must keep ownership of the days it locked')

    def test_mark_all_present_still_refused_on_locked_days(self):
        sheet, _leave, _locked = self._locked_sheet()

        with self.assertRaises(UserError):
            sheet.action_mark_all_present()

    def test_single_locked_day_cannot_be_set_attended(self):
        _sheet, _leave, locked = self._locked_sheet()

        with self.assertRaises(UserError):
            locked[0].write({'is_attended': True})
