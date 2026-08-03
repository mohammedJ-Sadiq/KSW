# -*- coding: utf-8 -*-
"""Re-creating granted weekend days after attendance is cleared and re-downloaded.

Granted Friday/Saturday records exist only as Odoo-side `hr.attendance` rows
(`x_is_weekend=True`); the biometric device knows nothing about them.  So a
"Clear Attendance" + "Download All Attendance" cycle — the standard fix when a
sync goes wrong — restores every punch but silently drops every weekend grant.

`ksw.weekend.grant.wizard` re-creates them for a chosen range without paying
for the full historical absence scan that "Generate All Absences" runs.
"""
from datetime import datetime as dt, date, timedelta

from odoo.tests.common import TransactionCase


class TestWeekendGrantWizard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Sat-Thu calendar, 08:00-16:30 with a 30-min break — Friday ('4') off.
        work_days = ['0', '1', '2', '3', '5', '6']  # Mon-Thu, Sat, Sun

        cls.calendar_group = cls.env['resource.calendar.group'].create({
            'name': 'Weekend Grant Test Group',
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
            'name': 'Weekend Grant Test Calendar',
            'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, cls.calendar_group.id)],
        })

        cls.device = cls.env['biometric.device.details'].create({
            'name': 'Weekend Grant Test Device',
            'device_ip': '192.0.2.10',
            'port_number': 4370,
        })

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Weekend Grant Employee',
            'main_calendar_id': cls.calendar.id,
            'resource_calendar_id': cls.calendar.id,
            'tz': 'Asia/Riyadh',
            'biometric_user_id': '9001',
            'device_id': cls.device.id,
        })

        # Sat 2026-07-04 → Thu 2026-07-30: 4 Fridays (10, 17, 24) plus 03.
        cls.date_from = date(2026, 7, 4)
        cls.date_to = date(2026, 7, 30)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _punch(self, day):
        """A real worked day, as the device download would leave it."""
        return self.env['hr.attendance'].create({
            'employee_id': self.employee.id,
            'check_in': dt.combine(day, dt.min.time()) + timedelta(hours=5),
            'check_out': dt.combine(day, dt.min.time()) + timedelta(hours=13, minutes=30),
            'x_is_absent': False,
        })

    def _punch_all_workdays(self):
        day = self.date_from
        while day <= self.date_to:
            if day.weekday() != 4:  # every day except Friday
                self._punch(day)
            day += timedelta(days=1)

    def _wizard(self, **overrides):
        vals = {
            'device_id': self.device.id,
            'date_from': self.date_from,
            'date_to': self.date_to,
        }
        vals.update(overrides)
        return self.env['ksw.weekend.grant.wizard'].create(vals)

    def _granted(self):
        return self.env['hr.attendance'].search([
            ('employee_id', '=', self.employee.id),
            ('x_is_weekend', '=', True),
        ])

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------
    def test_check_reports_missing_grants_without_writing(self):
        """Dry run counts the missing Fridays and changes nothing."""
        self._punch_all_workdays()
        wizard = self._wizard()

        result = self.env['biometric.attendance.sync']._generate_weekend_records(
            wizard._target_employees(), self.date_from, self.date_to,
            dry_run=True)
        self.assertEqual(result['created'], 3, 'the three in-range Fridays')
        self.assertFalse(self._granted(), 'dry run must not create any record')

        wizard.action_check()
        self.assertFalse(self._granted(), 'dry run must not create any record')
        self.assertIn('3', wizard.result_message)

    def test_generate_recreates_the_lost_grants(self):
        """The re-download scenario: punches present, weekend grants gone."""
        self._punch_all_workdays()

        self._wizard().action_generate()

        granted = self._granted()
        self.assertEqual(len(granted), 3)
        self.assertEqual(
            sorted(a.check_in.date() for a in granted),
            [date(2026, 7, 10), date(2026, 7, 17), date(2026, 7, 24)])
        self.assertTrue(all(a.x_weekend_granted for a in granted))
        self.assertTrue(all(not a.x_is_absent for a in granted))
        self.assertTrue(all(a.worked_hours > 0 for a in granted))

    def test_generate_is_idempotent(self):
        """Running it twice must not duplicate grants."""
        self._punch_all_workdays()
        wizard = self._wizard()

        wizard.action_generate()
        self._wizard().action_generate()

        self.assertEqual(len(self._granted()), 3)

    def test_no_grant_when_both_adjacent_workdays_absent(self):
        """A Friday flanked by two absent days stays ungranted."""
        day = self.date_from
        while day <= self.date_to:
            # Skip Thu 09 Jul and Sat 11 Jul so the 10th has no attended neighbour.
            if day.weekday() != 4 and day not in (date(2026, 7, 9), date(2026, 7, 11)):
                self._punch(day)
            day += timedelta(days=1)

        self._wizard().action_generate()

        granted_dates = sorted(a.check_in.date() for a in self._granted())
        self.assertNotIn(date(2026, 7, 10), granted_dates)
        self.assertEqual(granted_dates, [date(2026, 7, 17), date(2026, 7, 24)])

    def test_scope_excludes_other_devices(self):
        """Only the selected device's employees are touched."""
        other_device = self.env['biometric.device.details'].create({
            'name': 'Other Device', 'device_ip': '192.0.2.11', 'port_number': 4370,
        })
        wizard = self._wizard(device_id=other_device.id)
        self.assertEqual(wizard.employee_count, 0)

    def test_include_unassigned_picks_up_device_less_employees(self):
        """Employees with a biometric ID but no device are opt-in only."""
        orphan = self.env['hr.employee'].create({
            'name': 'No Device Employee',
            'main_calendar_id': self.calendar.id,
            'resource_calendar_id': self.calendar.id,
            'tz': 'Asia/Riyadh',
            'biometric_user_id': '9002',
        })

        scoped = self._wizard()
        self.assertNotIn(orphan, scoped._target_employees())

        widened = self._wizard(include_unassigned=True)
        self.assertIn(orphan, widened._target_employees())
