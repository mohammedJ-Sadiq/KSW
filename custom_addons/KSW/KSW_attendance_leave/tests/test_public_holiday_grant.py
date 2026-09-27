# -*- coding: utf-8 -*-
"""A public holiday is granted like a weekend day.

Saving the holiday writes a full scheduled-day attendance row (tagged with
the holiday) for every biometric employee who is not on leave that day, in
place of the absence.  Attendance-sheet employees, employees on leave and
employees who actually came in are left alone.  Every change afterwards —
edit, delete, a leave approved or refused later — re-derives the day.
"""
from datetime import datetime as dt, date, timedelta

from odoo.tests.common import TransactionCase


class TestPublicHolidayGrant(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Sat-Thu, 08:00-16:30 with a 30-min break (8 h net); Friday off.
        cls.calendar_group = cls.env['resource.calendar.group'].create({
            'name': 'PH Grant Group',
        })
        for day in ['0', '1', '2', '3', '5', '6']:
            cls.env['resource.calendar.group.line'].create({
                'name': f'Work {day}', 'calendar_group_id': cls.calendar_group.id,
                'dayofweek': day, 'day_period': 'full_day',
                'hour_from': 8.0, 'hour_to': 16.5,
            })
            cls.env['resource.calendar.group.line'].create({
                'name': f'Break {day}', 'calendar_group_id': cls.calendar_group.id,
                'dayofweek': day, 'day_period': 'break',
                'hour_from': 12.0, 'hour_to': 12.5,
            })
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'PH Grant Calendar',
            'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, cls.calendar_group.id)],
        })

        def employee(name, bio):
            return cls.env['hr.employee'].create({
                'name': name,
                'main_calendar_id': cls.calendar.id,
                'resource_calendar_id': cls.calendar.id,
                'tz': 'Asia/Riyadh',
                'biometric_user_id': bio,
            })
        cls.employee = employee('PH Grant Employee', '9301')
        cls.colleague = employee('PH Grant Colleague', '9302')
        cls.no_device = employee('PH Grant Sheet Employee', False)

        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'PH Grant Business Trip',
            'requires_allocation': False,
            'leave_validation_type': 'hr',
        })

        cls.holiday_day = date(2026, 7, 15)   # Wednesday
        cls.friday = date(2026, 7, 17)
        cls.Sync = cls.env['biometric.attendance.sync']

    # -- helpers -------------------------------------------------------

    def _holiday(self, day, last=None, **extra):
        """Riyadh-local whole days, stored in UTC as the form stores them."""
        vals = {
            'name': 'PH Test Holiday',
            'date_from': dt.combine(day, dt.min.time()) - timedelta(hours=3),
            'date_to': dt.combine(last or day, dt.max.time()).replace(
                microsecond=0) - timedelta(hours=3),
        }
        vals.update(extra)
        return self.env['resource.calendar.leaves'].create(vals)

    def _absence(self, employee, day):
        midnight = dt.combine(day, dt.min.time())
        return self.env['hr.attendance'].create({
            'employee_id': employee.id, 'check_in': midnight,
            'check_out': midnight, 'worked_hours': 0.0, 'x_is_absent': True,
        })

    def _punch(self, employee, day):
        return self.env['hr.attendance'].create({
            'employee_id': employee.id,
            'check_in': dt.combine(day, dt.min.time()) + timedelta(hours=5),
            'check_out': dt.combine(day, dt.min.time()) + timedelta(hours=13),
        })

    def _rows(self, employee, day):
        return self.Sync._day_attendances(employee, day)

    def _leave(self, employee, day):
        leave = self.env['hr.leave'].create({
            'name': 'Trip', 'employee_id': employee.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': day, 'request_date_to': day,
        })
        leave.sudo().action_approve()
        return leave

    # -- granting ------------------------------------------------------

    def test_saving_the_holiday_replaces_the_absence(self):
        self._absence(self.employee, self.holiday_day)
        holiday = self._holiday(self.holiday_day)

        rows = self._rows(self.employee, self.holiday_day)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.x_public_holiday_id, holiday)
        self.assertFalse(rows.x_is_absent)
        self.assertFalse(rows.x_net_is_absent)
        self.assertAlmostEqual(rows.worked_hours, 8.0, places=2)
        self.assertAlmostEqual(rows.x_net_worked_hours, 8.0, places=2)

    def test_day_with_no_row_is_granted_too(self):
        """Unpresented days are deducted by payroll, so they need a row."""
        holiday = self._holiday(self.holiday_day)
        self.assertEqual(
            self._rows(self.colleague, self.holiday_day).x_public_holiday_id,
            holiday)

    def test_employee_on_leave_keeps_the_leave_day(self):
        self._absence(self.employee, self.holiday_day)
        self._leave(self.employee, self.holiday_day)

        self._holiday(self.holiday_day)

        rows = self._rows(self.employee, self.holiday_day)
        self.assertFalse(rows.x_public_holiday_id)
        self.assertTrue(rows.x_is_absent)
        self.assertTrue(rows.x_is_covered)
        # ...while everyone else still gets it.
        self.assertTrue(
            self._rows(self.colleague, self.holiday_day).x_public_holiday_id)

    def test_real_punch_is_left_alone(self):
        punch = self._punch(self.employee, self.holiday_day)
        self._holiday(self.holiday_day)
        self.assertEqual(self._rows(self.employee, self.holiday_day), punch)
        self.assertFalse(punch.x_public_holiday_id)

    def test_non_biometric_employee_gets_nothing(self):
        self._holiday(self.holiday_day)
        self.assertFalse(self._rows(self.no_device, self.holiday_day))

    def test_holiday_on_a_weekend_day_grants_nothing(self):
        self._holiday(self.friday)
        self.assertFalse(self._rows(self.employee, self.friday))

    def test_future_days_are_not_granted_yet(self):
        future = date.today() + timedelta(days=10)
        self._holiday(future)
        self.assertFalse(self._rows(self.employee, future))

    def test_absence_generator_grants_instead_of_absence(self):
        """Days that were still in the future when the holiday was saved."""
        holiday = self._holiday(self.holiday_day)
        self._rows(self.employee, self.holiday_day).unlink()

        created = self.Sync._check_absence_for_date(
            self.employee, self.holiday_day)

        self.assertFalse(created)
        self.assertEqual(
            self._rows(self.employee, self.holiday_day).x_public_holiday_id,
            holiday)

    def test_button_is_idempotent(self):
        holiday = self._holiday(self.holiday_day)
        result = holiday.action_grant_public_holiday()
        self.assertEqual(result['tag'], 'display_notification')
        self.assertEqual(
            len(self._rows(self.employee, self.holiday_day)), 1)
        again = self.Sync._sync_public_holidays(holiday)
        self.assertEqual((again['granted'], again['revoked']), (0, 0))

    # -- revoking ------------------------------------------------------

    def test_deleting_the_holiday_restores_the_absence(self):
        holiday = self._holiday(self.holiday_day)
        holiday.unlink()

        rows = self._rows(self.employee, self.holiday_day)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows.x_is_absent)
        self.assertFalse(rows.x_public_holiday_id)

    def test_moving_the_holiday_moves_the_grant(self):
        holiday = self._holiday(self.holiday_day)
        new_day = self.holiday_day - timedelta(days=1)
        holiday.write({
            'date_from': dt.combine(new_day, dt.min.time()) - timedelta(hours=3),
            'date_to': dt.combine(new_day, dt.max.time()).replace(
                microsecond=0) - timedelta(hours=3),
        })
        self.assertTrue(self._rows(self.employee, self.holiday_day).x_is_absent)
        self.assertEqual(
            self._rows(self.employee, new_day).x_public_holiday_id, holiday)

    def test_leave_approved_later_takes_the_holiday_back(self):
        self._holiday(self.holiday_day)
        leave = self._leave(self.employee, self.holiday_day)

        rows = self._rows(self.employee, self.holiday_day)
        self.assertFalse(rows.x_public_holiday_id)
        self.assertTrue(rows.x_is_absent)
        self.assertIn(leave, rows.x_leave_ids)

        leave.sudo().action_refuse()
        self.assertTrue(
            self._rows(self.employee, self.holiday_day).x_public_holiday_id)

    # -- telling it apart ----------------------------------------------

    def test_granted_day_says_public_holiday(self):
        holiday = self._holiday(self.holiday_day)
        row = self._rows(self.employee, self.holiday_day)
        self.assertIn('Public Holiday (PH Test Holiday)', row.display_name)
        self.assertIn(row, self.env['hr.attendance'].search(
            [('x_public_holiday_id', '!=', False),
             ('employee_id', '=', self.employee.id)]))
        self.assertEqual(holiday.x_granted_count,
                         len(holiday.x_granted_attendance_ids))
