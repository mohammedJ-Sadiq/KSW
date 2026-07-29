# -*- coding: utf-8 -*-
"""Duration of ordinary leaves that merely *cover* absence records.

`_auto_link_absence_attendance()` fills `x_attendance_ids` on ordinary leave
types (business trip, sick, umrah…) so the absences show as covered.  That must
never turn the leave into an "attendance issue" leave whose duration is the
number of linked absence records — the duration has to stay calendar-derived.

Regression for KSWCO leave 4853: Business Trip 2026-07-22 → 2026-07-31 (8
working days on a Sat–Thu calendar) displayed "6 days" because only 6 absence
records had been synced from the biometric device at that point.
"""
from datetime import datetime as dt, date

from odoo.tests.common import TransactionCase


class TestRegularLeaveDuration(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # ── Sat–Thu calendar, 08:00-12:00 / 12:30-16:30 (8 h/day), Friday off ──
        work_days = ['0', '1', '2', '3', '5', '6']  # Mon-Thu, Sat, Sun

        cls.calendar_group = cls.env['resource.calendar.group'].create({
            'name': 'Test Sat-Thu Group',
        })
        for day in work_days:
            cls.env['resource.calendar.group.line'].create({
                'name': f'Work Day {day}',
                'calendar_group_id': cls.calendar_group.id,
                'dayofweek': day,
                'day_period': 'full_day',
                'hour_from': 8.0,
                'hour_to': 16.5,
            })
            cls.env['resource.calendar.group.line'].create({
                'name': f'Break Day {day}',
                'calendar_group_id': cls.calendar_group.id,
                'dayofweek': day,
                'day_period': 'break',
                'hour_from': 12.0,
                'hour_to': 12.5,
            })

        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Test Sat-Thu Calendar',
            'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, cls.calendar_group.id)],
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

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Test Trip Employee',
            'resource_calendar_id': cls.calendar.id,
            'tz': 'Asia/Riyadh',
        })

        # Ordinary day-unit leave type — NOT an attendance-issue type
        cls.trip_type = cls.env['hr.leave.type'].create({
            'name': 'Test Business Trip',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
        })
        # Hour-unit excuse type — duration must stay driven by accepted minutes
        cls.excuse_type = cls.env['hr.leave.type'].create({
            'name': 'Test Late Excuse',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'hour',
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _absence(self, day):
        """Biometric-style absence record: midnight UTC, no worked time."""
        moment = dt.combine(day, dt.min.time())
        return self.env['hr.attendance'].create({
            'employee_id': self.employee.id,
            'check_in': moment,
            'check_out': moment,
            'x_is_absent': True,
        })

    def _trip_leave(self):
        return self.env['hr.leave'].with_context(
            tracking_disable=True, mail_create_nosubscribe=True,
        ).create({
            'employee_id': self.employee.id,
            'holiday_status_id': self.trip_type.id,
            'request_date_from': date(2026, 7, 22),
            'request_date_to': date(2026, 7, 31),
        })

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------
    def test_duration_ignores_linked_absences(self):
        """Jul 22 → Jul 31 stays 8 working days however many absences are linked."""
        leave = self._trip_leave()
        self.assertEqual(leave.number_of_days, 8)

        # Only the days already synced from the device (Jul 24/31 are Fridays)
        absences = self.env['hr.attendance']
        for day in (22, 23, 25, 26, 27, 28):
            absences |= self._absence(date(2026, 7, day))
        leave.write({'x_attendance_ids': [(4, a.id) for a in absences]})
        leave.invalidate_recordset()

        self.assertEqual(
            leave.number_of_days, 8,
            'Linking covered absences must not shrink the duration to the '
            'absence count (was 6 before the fix).',
        )
        self.assertGreater(leave.number_of_hours, 0)

    def test_duration_survives_a_later_sync(self):
        """A late-arriving absence must not bump the duration either."""
        leave = self._trip_leave()
        first = self._absence(date(2026, 7, 22))
        leave.write({'x_attendance_ids': [(4, first.id)]})
        leave.invalidate_recordset()
        self.assertEqual(leave.number_of_days, 8)

        later = self._absence(date(2026, 7, 29))
        leave.write({'x_attendance_ids': [(4, later.id)]})
        leave.invalidate_recordset()
        self.assertEqual(leave.number_of_days, 8)

    def test_display_name_keeps_the_requested_range(self):
        """The label must not be rebuilt from the linked attendance dates."""
        leave = self._trip_leave()
        absence = self._absence(date(2026, 7, 22))
        leave.write({'x_attendance_ids': [(4, absence.id)]})
        leave.invalidate_recordset()
        self.assertNotIn('records', leave.display_name)

    def test_coverage_still_works(self):
        """The whole point of the auto-link — coverage — is unaffected."""
        leave = self._trip_leave()
        absence = self._absence(date(2026, 7, 22))
        leave.write({'x_attendance_ids': [(4, absence.id)]})
        leave.with_context(leave_skip_state_check=True).write({'state': 'validate'})
        absence.invalidate_recordset()
        self.assertTrue(absence.x_is_covered)

    def test_hour_excuse_still_uses_accepted_minutes(self):
        """Hour-unit excuse leaves keep the accepted-minutes duration."""
        moment = dt(2026, 7, 22, 5, 30)  # 08:30 Riyadh — 30 min late
        attendance = self.env['hr.attendance'].create({
            'employee_id': self.employee.id,
            'check_in': moment,
            'check_out': dt(2026, 7, 22, 13, 30),
            'x_late_minutes': 30.0,
        })
        leave = self.env['hr.leave'].with_context(
            tracking_disable=True, mail_create_nosubscribe=True,
        ).create({
            'employee_id': self.employee.id,
            'holiday_status_id': self.excuse_type.id,
            'request_date_from': date(2026, 7, 22),
            'request_date_to': date(2026, 7, 22),
            'x_attendance_ids': [(4, attendance.id)],
        })
        leave._generate_attendance_lines()
        leave.invalidate_recordset()
        self.assertAlmostEqual(leave.number_of_hours, 0.5, places=2)

    def test_group_line_fallback_counts_local_days(self):
        """No resource_calendar_attendance → group lines, counted on local dates.

        `date_from`/`date_to` are 21:00→20:59 UTC for a Riyadh day, so counting
        UTC dates turned a one-day leave into two.
        """
        calendar = self.calendar.copy({
            'name': 'Group-lines-only Calendar',
            'attendance_ids': [(5, 0, 0)],
        })
        employee = self.env['hr.employee'].create({
            'name': 'Group Lines Employee',
            'resource_calendar_id': calendar.id,
            'tz': 'Asia/Riyadh',
        })
        leave = self.env['hr.leave'].with_context(
            tracking_disable=True, mail_create_nosubscribe=True,
        ).create({
            'employee_id': employee.id,
            'holiday_status_id': self.trip_type.id,
            'request_date_from': date(2026, 7, 22),
            'request_date_to': date(2026, 7, 22),
        })
        self.assertEqual(leave.number_of_days, 1)

    def test_expired_schedule_falls_back_to_calendar_days(self):
        """A schedule whose lines expired must not yield a 0-day leave."""
        expired_group = self.env['resource.calendar.group'].create({
            'name': 'Expired Group',
        })
        for day in ('0', '1', '2', '3', '5', '6'):
            self.env['resource.calendar.group.line'].create({
                'name': f'Expired Day {day}',
                'calendar_group_id': expired_group.id,
                'dayofweek': day,
                'day_period': 'full_day',
                'hour_from': 8.0,
                'hour_to': 16.5,
                'end_date': date(2025, 2, 28),
            })
        calendar = self.env['resource.calendar'].create({
            'name': 'Expired Schedule Calendar',
            'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, expired_group.id)],
            'attendance_ids': [(5, 0, 0)],
        })
        employee = self.env['hr.employee'].create({
            'name': 'Expired Schedule Employee',
            'resource_calendar_id': calendar.id,
            'tz': 'Asia/Riyadh',
        })
        leave = self.env['hr.leave'].with_context(
            tracking_disable=True, mail_create_nosubscribe=True,
        ).create({
            'employee_id': employee.id,
            'holiday_status_id': self.trip_type.id,
            'request_date_from': date(2026, 7, 22),
            'request_date_to': date(2026, 7, 31),
        })
        self.assertEqual(leave.number_of_days, 10)  # calendar days, not 0

    def test_daily_work_hours_is_daily_not_weekly(self):
        """_get_daily_work_hours() with no date returns one day, not the week."""
        leave = self._trip_leave()
        self.assertAlmostEqual(
            leave._get_daily_work_hours(self.employee), 8.0, places=2,
            msg='Without a date the helper used to sum the whole week (48.5 h).',
        )
        self.assertAlmostEqual(
            leave._get_daily_work_hours(self.employee, date(2026, 7, 22)), 8.0,
            places=2,
        )
