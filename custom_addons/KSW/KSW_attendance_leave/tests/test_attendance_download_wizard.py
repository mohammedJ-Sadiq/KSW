# -*- coding: utf-8 -*-
"""Targeted re-download of one period for chosen employees.

The device-level buttons are all-or-nothing: "Download All Attendance" re-pulls
every employee for the whole `year_filter`, and "Download New Attendance Only"
just repeats what the 30-minute cron already does.  Neither can fix "March is
wrong for one employee".

`ksw.attendance.download.wizard` narrows the pull to a period and an employee
set, then rebuilds that period's absence rows and weekend grants — the two
things the device cannot give back because they only ever existed in Odoo.

The device connection is mocked; everything downstream of `get_attendance()`
is the real code path.
"""
from datetime import datetime as dt, date, timedelta
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class FakeLog:
    """Stand-in for a pyzk attendance log: naive device-local timestamp."""

    def __init__(self, user_id, timestamp):
        self.user_id = user_id
        self.timestamp = timestamp


class FakeConnection:
    def __init__(self, logs):
        self._logs = logs
        self.disconnected = False

    def get_attendance(self):
        return self._logs

    def disconnect(self):
        self.disconnected = True


class TestAttendanceDownloadWizard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Sat-Thu calendar, 08:00-16:30 with a 30-min break — Friday ('4') off.
        work_days = ['0', '1', '2', '3', '5', '6']  # Mon-Thu, Sat, Sun

        cls.calendar_group = cls.env['resource.calendar.group'].create({
            'name': 'Download Test Group',
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
            'name': 'Download Test Calendar',
            'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, cls.calendar_group.id)],
        })

        cls.device = cls.env['biometric.device.details'].create({
            'name': 'Download Test Device',
            'device_ip': '192.0.2.20',
            'port_number': 4370,
            'tz': 'Asia/Riyadh',
        })

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Download Employee A',
            'main_calendar_id': cls.calendar.id,
            'resource_calendar_id': cls.calendar.id,
            'tz': 'Asia/Riyadh',
            'biometric_user_id': '8001',
            'device_id': cls.device.id,
        })
        cls.colleague = cls.env['hr.employee'].create({
            'name': 'Download Employee B',
            'main_calendar_id': cls.calendar.id,
            'resource_calendar_id': cls.calendar.id,
            'tz': 'Asia/Riyadh',
            'biometric_user_id': '8002',
            'device_id': cls.device.id,
        })

        # Sat 2026-07-04 → Thu 2026-07-09, plus Fri 2026-07-10 off.
        cls.date_from = date(2026, 7, 4)
        cls.date_to = date(2026, 7, 9)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _logs_for(self, bio_id, days):
        """One 08:00 in / 16:30 out pair per day, in device-local time."""
        logs = []
        for day in days:
            logs.append(FakeLog(bio_id, dt.combine(day, dt.min.time()) + timedelta(hours=8)))
            logs.append(FakeLog(bio_id, dt.combine(day, dt.min.time()) + timedelta(hours=16, minutes=30)))
        return logs

    def _run(self, logs, **overrides):
        vals = {
            'device_id': self.device.id,
            'date_from': self.date_from,
            'date_to': self.date_to,
            'generate_absences': False,
            'generate_weekend_grants': False,
        }
        vals.update(overrides)
        wizard = self.env['ksw.attendance.download.wizard'].create(vals)
        conn = FakeConnection(logs)
        with patch.object(
            type(self.device), '_connect_device', return_value=conn,
        ):
            wizard.action_download()
        return wizard, conn

    def _attendances(self, employee):
        return self.env['hr.attendance'].search([
            ('employee_id', '=', employee.id),
        ], order='check_in')

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------
    def test_downloads_only_the_requested_period(self):
        """Punches outside the period are ignored, however many are on the device."""
        in_range = [self.date_from + timedelta(days=i) for i in range(3)]
        out_of_range = [date(2026, 5, 12), date(2026, 8, 20)]
        logs = (self._logs_for('8001', in_range)
                + self._logs_for('8001', out_of_range))

        self._run(logs, employee_ids=[(6, 0, self.employee.ids)])

        got = sorted(a.date for a in self._attendances(self.employee))
        self.assertEqual(got, in_range)

    def test_downloads_only_the_requested_employees(self):
        """A colleague's punches on the same device are left alone."""
        days = [self.date_from + timedelta(days=i) for i in range(3)]
        logs = self._logs_for('8001', days) + self._logs_for('8002', days)

        self._run(logs, employee_ids=[(6, 0, self.employee.ids)])

        self.assertEqual(len(self._attendances(self.employee)), 3)
        self.assertFalse(self._attendances(self.colleague))

    def test_last_download_time_is_untouched(self):
        """Pulling an old month must not make the incremental cron skip ahead."""
        stamp = dt(2026, 7, 1, 6, 0, 0)
        self.device.last_download_time = stamp

        self._run(self._logs_for('8001', [self.date_from]),
                  employee_ids=[(6, 0, self.employee.ids)])

        self.assertEqual(self.device.last_download_time, stamp)

    def test_connection_is_always_released(self):
        self._, conn = self._run(
            self._logs_for('8001', [self.date_from]),
            employee_ids=[(6, 0, self.employee.ids)])
        self.assertTrue(conn.disconnected)

    def test_rerun_updates_instead_of_duplicating(self):
        """The repair case: downloading the same period twice is safe."""
        days = [self.date_from + timedelta(days=i) for i in range(3)]
        logs = self._logs_for('8001', days)

        self._run(logs, employee_ids=[(6, 0, self.employee.ids)])
        wizard, _conn = self._run(logs, employee_ids=[(6, 0, self.employee.ids)])

        self.assertEqual(len(self._attendances(self.employee)), 3)
        self.assertIn('0 day(s) created, 3 updated', wizard.result_message)

    def test_absences_and_weekend_grants_are_rebuilt(self):
        """The full repair: punches back, plus the rows only Odoo can produce."""
        # Sat 04 → Thu 09 attended except Mon 06; Fri 10 is the weekend.
        attended = [d for d in (self.date_from + timedelta(days=i) for i in range(6))
                    if d != date(2026, 7, 6)]
        wizard, _conn = self._run(
            self._logs_for('8001', attended),
            employee_ids=[(6, 0, self.employee.ids)],
            date_to=date(2026, 7, 10),
            generate_absences=True,
            generate_weekend_grants=True,
        )

        atts = self._attendances(self.employee)
        absent = atts.filtered('x_is_absent')
        granted = atts.filtered('x_is_weekend')

        self.assertEqual(absent.mapped('date'), [date(2026, 7, 6)])
        self.assertEqual(granted.mapped('date'), [date(2026, 7, 10)])
        self.assertIn('Absence records created: 1', wizard.result_message)
        self.assertIn('1 granted', wizard.result_message)

    def test_no_punches_explains_itself(self):
        wizard, _conn = self._run([], employee_ids=[(6, 0, self.employee.ids)])
        self.assertIn('No punches came back', wizard.result_message)

    def test_oversized_scope_is_refused_up_front(self):
        """Better to say "narrow it" than to die at 120s halfway through."""
        # 8 employees x 7 years > the 15000 employee-day budget.
        for i in range(6):
            self.env['hr.employee'].create({
                'name': f'Bulk Employee {i}',
                'main_calendar_id': self.calendar.id,
                'resource_calendar_id': self.calendar.id,
                'tz': 'Asia/Riyadh',
                'biometric_user_id': f'80{10 + i}',
                'device_id': self.device.id,
            })
        wizard = self.env['ksw.attendance.download.wizard'].create({
            'device_id': self.device.id,
            'date_from': date(2020, 1, 1),
            'date_to': date(2026, 12, 31),
        })
        self.assertEqual(wizard.employee_count, 8)

        with self.assertRaises(UserError) as ctx:
            wizard.action_download()
        self.assertIn('too large', str(ctx.exception))
