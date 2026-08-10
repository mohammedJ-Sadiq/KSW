# -*- coding: utf-8 -*-
"""Time off for employees on a shift that runs past midnight.

KSW work schedules store an overnight shift *wrapped*: the 21:00-05:00 Night
Shift Security group lines carry `hour_from=21, hour_to=5`.  Nothing unwrapped
that before handing the hours to `hr.leave`, which stores them as float times of
`request_date_from` / `request_date_to` and rejects anything outside 0..24.

Two failures came out of it on KSWCO for Hussain Qas-Rasah (employee 9909):

* an ordinary request got `date_from = D 21:00` and `date_to = D 05:00`, i.e. it
  ended before it began — "The start date must be before or equal to the end
  date";
* picking attendance 327019 (30 Jul 2026, a 4-second double punch that produced
  `x_early_leave_minutes = 485`) computed `hour_from = 5.0 - 485/60 = -3.08` and
  blew up the whole form with `ValueError: hour must be in 0..23`.
"""
from datetime import date, datetime as dt

from odoo.tests.common import TransactionCase


class NightShiftLeaveCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        work_days = ['0', '1', '2', '3', '5', '6']  # Mon-Thu, Sat, Sun

        # ── Night shift: 21:00 -> 05:00, stored wrapped on the group line ──
        cls.night_group = cls.env['resource.calendar.group'].create({
            'name': 'Test 21:00 - 05:00 Group',
        })
        for day in work_days:
            cls.env['resource.calendar.group.line'].create({
                'name': f'Night Day {day}',
                'calendar_group_id': cls.night_group.id,
                'dayofweek': day,
                'day_period': 'full_day',
                'hour_from': 21.0,
                'hour_to': 5.0,
            })

        # The resource.calendar attendances cannot wrap, so the real shift
        # calendars split them at midnight; only the group lines wrap.
        cls.night_calendar = cls.env['resource.calendar'].create({
            'name': 'Test Night Shift Calendar',
            'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, cls.night_group.id)],
            'attendance_ids': [(5, 0, 0)] + [
                (0, 0, {
                    'name': f'{day} {name}',
                    'dayofweek': day,
                    'day_period': 'morning' if name == 'after midnight' else 'afternoon',
                    'hour_from': hour_from,
                    'hour_to': hour_to,
                })
                for day in work_days
                for name, hour_from, hour_to in (
                    ('after midnight', 0.0, 5.0),
                    ('before midnight', 21.0, 24.0),
                )
            ],
        })

        # ── Ordinary day shift, for the "nothing else changed" regressions ──
        cls.day_group = cls.env['resource.calendar.group'].create({
            'name': 'Test 08:00 - 16:30 Group',
        })
        for day in work_days:
            cls.env['resource.calendar.group.line'].create({
                'name': f'Day {day}',
                'calendar_group_id': cls.day_group.id,
                'dayofweek': day,
                'day_period': 'full_day',
                'hour_from': 8.0,
                'hour_to': 16.5,
            })
        cls.day_calendar = cls.env['resource.calendar'].create({
            'name': 'Test Day Shift Calendar',
            'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, cls.day_group.id)],
            'attendance_ids': [(5, 0, 0)] + [
                (0, 0, {
                    'name': f'{day} {period}',
                    'dayofweek': day,
                    'day_period': period,
                    'hour_from': hour_from,
                    'hour_to': hour_to,
                })
                for day in work_days
                for period, hour_from, hour_to in (
                    ('morning', 8.0, 12.0),
                    ('afternoon', 12.5, 16.5),
                )
            ],
        })

        cls.night_employee = cls.env['hr.employee'].create({
            'name': 'Test Night Guard',
            'resource_calendar_id': cls.night_calendar.id,
            'tz': 'Asia/Riyadh',
        })
        cls.day_employee = cls.env['hr.employee'].create({
            'name': 'Test Day Clerk',
            'resource_calendar_id': cls.day_calendar.id,
            'tz': 'Asia/Riyadh',
        })

        cls.day_type = cls.env['hr.leave.type'].create({
            'name': 'Test Night Business Trip',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
        })
        cls.excuse_type = cls.env['hr.leave.type'].create({
            'name': 'Test Night Excuse',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'hour',
        })
        cls.half_day_type = cls.env['hr.leave.type'].create({
            'name': 'Test Night Half Day',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'half_day',
        })

        # Thursday 30 Jul 2026 — a working day on both calendars.
        cls.shift_day = date(2026, 7, 30)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _leave(self, employee, leave_type, date_from=None, date_to=None):
        return self.env['hr.leave'].with_context(
            tracking_disable=True, mail_create_nosubscribe=True,
        ).create({
            'employee_id': employee.id,
            'holiday_status_id': leave_type.id,
            'request_date_from': date_from or self.shift_day,
            'request_date_to': date_to or date_from or self.shift_day,
        })

    def _attendance(self, employee, check_in, check_out, late=0.0, early=0.0):
        return self.env['hr.attendance'].create({
            'employee_id': employee.id,
            'check_in': check_in,
            'check_out': check_out,
            'x_late_minutes': late,
            'x_early_leave_minutes': early,
        })

    def _excuse(self, employee, attendance):
        """Build the excuse leave the way the form does: pick the attendance,
        then let the onchange regenerate the hour lines."""
        leave = self._leave(employee, self.excuse_type)
        leave.x_attendance_ids = [(6, 0, attendance.ids)]
        leave._generate_attendance_lines()
        return leave


class TestNightShiftToUtc(NightShiftLeaveCommon):
    """`_to_utc` is the single choke point where an out-of-range hour reaches
    `float_to_time`; rolling the date there fixes every caller at once."""

    def test_hour_past_midnight_rolls_to_next_day(self):
        leave = self._leave(self.night_employee, self.day_type)
        # 29:00 of 30 Jul == 05:00 on 31 Jul Riyadh == 02:00 UTC.
        self.assertEqual(
            leave._to_utc(self.shift_day, 29.0, self.night_employee),
            dt(2026, 7, 31, 2, 0),
        )

    def test_negative_hour_rolls_to_previous_day(self):
        leave = self._leave(self.night_employee, self.day_type)
        # -3:00 of 30 Jul == 21:00 on 29 Jul Riyadh == 18:00 UTC.
        self.assertEqual(
            leave._to_utc(self.shift_day, -3.0, self.night_employee),
            dt(2026, 7, 29, 18, 0),
        )

    def test_hour_24_is_midnight_not_a_crash(self):
        leave = self._leave(self.night_employee, self.day_type)
        self.assertEqual(
            leave._to_utc(self.shift_day, 24.0, self.night_employee),
            dt(2026, 7, 30, 21, 0),  # 00:00 on 31 Jul Riyadh
        )

    def test_in_range_hour_is_untouched(self):
        leave = self._leave(self.day_employee, self.day_type)
        self.assertEqual(
            leave._to_utc(self.shift_day, 8.0, self.day_employee),
            dt(2026, 7, 30, 5, 0),
        )


class TestNightShiftOrdinaryLeave(NightShiftLeaveCommon):
    """A day-unit request on a wrapped schedule must not end before it starts."""

    def test_get_hour_from_to_unwraps_the_shift_end(self):
        leave = self._leave(self.night_employee, self.day_type)
        hour_from, hour_to = leave._get_hour_from_to(self.shift_day, self.shift_day)
        self.assertEqual(hour_from, 21.0)
        self.assertEqual(hour_to, 29.0, 'the shift ends at 05:00 the next morning')

    def test_single_day_request_spans_midnight(self):
        leave = self._leave(self.night_employee, self.day_type)
        self.assertEqual(leave.date_from, dt(2026, 7, 30, 18, 0))  # 21:00 local
        self.assertEqual(leave.date_to, dt(2026, 7, 31, 2, 0))     # 05:00 local
        self.assertGreater(leave.date_to, leave.date_from)

    def test_multi_day_request_ends_after_the_last_shift(self):
        leave = self._leave(
            self.night_employee, self.day_type,
            date_from=date(2026, 7, 28), date_to=date(2026, 7, 30),
        )
        self.assertEqual(leave.date_from, dt(2026, 7, 28, 18, 0))
        self.assertEqual(leave.date_to, dt(2026, 7, 31, 2, 0))

    def test_day_shift_request_is_unchanged(self):
        leave = self._leave(self.day_employee, self.day_type)
        self.assertEqual(leave.date_from, dt(2026, 7, 30, 5, 0))    # 08:00 local
        self.assertEqual(leave.date_to, dt(2026, 7, 30, 13, 30))    # 16:30 local

    def test_half_day_on_a_night_shift_still_ends_after_it_starts(self):
        """A half day is undefined on a shift that straddles midnight — the
        12:00 split point falls in the middle of the employee's night off.  It
        must at least stay a valid window rather than raising _check_date."""
        leave = self._leave(self.night_employee, self.half_day_type)
        self.assertGreater(leave.date_to, leave.date_from)

    def test_half_day_on_a_day_shift_is_unchanged(self):
        leave = self._leave(self.day_employee, self.half_day_type)
        self.assertEqual(leave.date_from, dt(2026, 7, 30, 5, 0))    # 08:00 local
        self.assertEqual(leave.date_to, dt(2026, 7, 30, 13, 30))    # 16:30 local


class TestNightShiftAttendanceExcuse(NightShiftLeaveCommon):
    """Late / early-leave excuses sliced off a wrapped shift."""

    def test_early_leave_longer_than_the_shift(self):
        """KSWCO attendance 327019: a 4-second double punch on the night shift
        produced 485 early-leave minutes, and `5.0 - 485/60` went negative."""
        attendance = self._attendance(
            self.night_employee,
            dt(2026, 7, 30, 17, 55, 24), dt(2026, 7, 30, 17, 55, 28),
            early=485.0,
        )
        leave = self._excuse(self.night_employee, attendance)

        line = leave.x_attendance_line_ids
        self.assertEqual(len(line), 1)
        self.assertEqual(line.issue_type, 'early_leave')
        # 29:00 - 8h05 = 20:55 on the shift day, running to 05:00 the next.
        self.assertAlmostEqual(line.hour_from, 20 + 55 / 60.0, places=3)
        self.assertAlmostEqual(line.hour_to, 5.0, places=3)
        self.assertAlmostEqual(line.duration_minutes, 485.0, places=1)
        self.assertAlmostEqual(line.accepted_minutes, 485.0, places=1)

        self.assertEqual(leave.date_from, dt(2026, 7, 30, 17, 55))
        self.assertEqual(leave.date_to, dt(2026, 7, 31, 2, 0))

    def test_early_leave_inside_the_small_hours(self):
        """A one-hour early leave on a 21:00-05:00 shift is entirely on the
        *next* calendar day — hour 28.0 of the shift day."""
        attendance = self._attendance(
            self.night_employee,
            dt(2026, 7, 30, 18, 0), dt(2026, 7, 31, 1, 0),
            early=60.0,
        )
        leave = self._excuse(self.night_employee, attendance)

        line = leave.x_attendance_line_ids
        self.assertAlmostEqual(line.hour_from, 4.0, places=3)
        self.assertAlmostEqual(line.hour_to, 5.0, places=3)
        self.assertAlmostEqual(line.duration_minutes, 60.0, places=1)
        self.assertEqual(leave.date_from, dt(2026, 7, 31, 1, 0))  # 04:00 local
        self.assertEqual(leave.date_to, dt(2026, 7, 31, 2, 0))    # 05:00 local

    def test_late_arrival_crossing_midnight(self):
        """Five hours late on a shift starting at 21:00 ends at 02:00."""
        attendance = self._attendance(
            self.night_employee,
            dt(2026, 7, 30, 23, 0), dt(2026, 7, 31, 2, 0),
            late=300.0,
        )
        leave = self._excuse(self.night_employee, attendance)

        line = leave.x_attendance_line_ids
        self.assertEqual(line.issue_type, 'late')
        self.assertAlmostEqual(line.hour_from, 21.0, places=3)
        self.assertAlmostEqual(line.hour_to, 2.0, places=3)
        self.assertAlmostEqual(line.duration_minutes, 300.0, places=1)
        self.assertEqual(leave.date_from, dt(2026, 7, 30, 18, 0))  # 21:00 local
        self.assertEqual(leave.date_to, dt(2026, 7, 30, 23, 0))    # 02:00 local

    def test_late_and_early_on_the_same_shift(self):
        attendance = self._attendance(
            self.night_employee,
            dt(2026, 7, 30, 18, 30), dt(2026, 7, 31, 1, 0),
            late=30.0, early=60.0,
        )
        leave = self._excuse(self.night_employee, attendance)

        lines = leave.x_attendance_line_ids
        self.assertEqual(len(lines), 2)
        self.assertEqual(
            sorted(lines.mapped('duration_minutes')), [30.0, 60.0],
        )
        # Window spans the first late minute to the last excused one: 21:00 -> 05:00.
        self.assertEqual(leave.date_from, dt(2026, 7, 30, 18, 0))
        self.assertEqual(leave.date_to, dt(2026, 7, 31, 2, 0))

    def test_duration_is_never_negative(self):
        """`hour_to - hour_from` on a wrapped line is minus 16 hours a day.  That
        went straight into `number_of_days = total_hours / avg_daily`, and the
        `number_of_days >= 0` constraint rejected the save with "If you want to
        change the number of days you should use the 'period' mode"."""
        attendance = self._attendance(
            self.night_employee,
            dt(2026, 7, 30, 18, 0), dt(2026, 7, 31, 1, 0),
            early=60.0,
        )
        leave = self._excuse(self.night_employee, attendance)

        self.assertAlmostEqual(leave.number_of_hours, 1.0, places=3)
        self.assertGreater(leave.number_of_days, 0.0)
        # One excused hour out of an eight-hour night shift.
        self.assertAlmostEqual(leave.number_of_days, 1 / 8.0, places=3)

    def test_daily_work_hours_of_a_wrapped_shift(self):
        leave = self._leave(self.night_employee, self.day_type)
        self.assertAlmostEqual(
            leave._get_daily_work_hours(self.night_employee, self.shift_day),
            8.0, places=3, msg='21:00 -> 05:00 is eight hours, not minus sixteen',
        )
        # Without a date the weekly average is used; still eight.
        self.assertAlmostEqual(
            leave._get_daily_work_hours(self.night_employee), 8.0, places=3,
        )

    def test_daily_work_hours_of_a_day_shift_is_unchanged(self):
        leave = self._leave(self.day_employee, self.day_type)
        self.assertAlmostEqual(
            leave._get_daily_work_hours(self.day_employee, self.shift_day),
            8.5, places=3,  # 08:00-16:30, no break line on this group
        )

    def test_day_shift_excuse_is_unchanged(self):
        attendance = self._attendance(
            self.day_employee,
            dt(2026, 7, 30, 5, 30), dt(2026, 7, 30, 13, 30),
            late=30.0,
        )
        leave = self._excuse(self.day_employee, attendance)

        line = leave.x_attendance_line_ids
        self.assertAlmostEqual(line.hour_from, 8.0, places=3)
        self.assertAlmostEqual(line.hour_to, 8.5, places=3)
        self.assertAlmostEqual(line.duration_minutes, 30.0, places=1)
        self.assertEqual(leave.date_from, dt(2026, 7, 30, 5, 0))
        self.assertEqual(leave.date_to, dt(2026, 7, 30, 5, 30))
