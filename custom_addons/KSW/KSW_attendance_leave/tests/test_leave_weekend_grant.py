# -*- coding: utf-8 -*-
"""Approving a time-off request must re-open the weekend-grant decision.

A weekend day is granted when the workday immediately before or after it was
attended, and a *covered* absence (one attached to a validated leave) counts as
attended.  But that decision is taken once, by the nightly cron, the morning
after the weekend — days or weeks before HR actually approves the leave.  With
nothing re-running the pass, the Friday was skipped permanently: the employee
lost a paid rest day because his excuse was approved late.

These tests pin the re-check that runs on every approve / refuse / reset.
"""
from datetime import datetime as dt, date, timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class TestLeaveWeekendGrant(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Sat-Thu calendar, 08:00-16:30 with a 30-min break — Friday ('4') off.
        work_days = ['0', '1', '2', '3', '5', '6']  # Mon-Thu, Sat, Sun

        cls.calendar_group = cls.env['resource.calendar.group'].create({
            'name': 'Leave Weekend Grant Group',
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
            'name': 'Leave Weekend Grant Calendar',
            'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, cls.calendar_group.id)],
        })

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Leave Weekend Grant Employee',
            'main_calendar_id': cls.calendar.id,
            'resource_calendar_id': cls.calendar.id,
            'tz': 'Asia/Riyadh',
            'biometric_user_id': '9101',
        })

        # A regular (non attendance-issue) type, like "Business Trip".
        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'Weekend Grant Business Trip',
            'requires_allocation': False,
            'leave_validation_type': 'hr',
        })

        # Sat 2026-07-04 → Thu 2026-07-16.  Fri 2026-07-17 is the weekend day
        # under test; Thu 2026-07-16 is the day the leave covers.
        cls.date_from = date(2026, 7, 4)
        cls.absent_day = date(2026, 7, 16)   # Thursday
        cls.friday = date(2026, 7, 17)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _punch(self, day):
        return self.env['hr.attendance'].create({
            'employee_id': self.employee.id,
            'check_in': dt.combine(day, dt.min.time()) + timedelta(hours=5),
            'check_out': dt.combine(day, dt.min.time()) + timedelta(hours=13, minutes=30),
            'x_is_absent': False,
        })

    def _absence(self, day):
        """An absence row exactly as the biometric sync leaves it."""
        utc_midnight = dt.combine(day, dt.min.time())
        return self.env['hr.attendance'].create({
            'employee_id': self.employee.id,
            'check_in': utc_midnight,
            'check_out': utc_midnight,
            'worked_hours': 0.0,
            'x_is_absent': True,
        })

    def _seed_week(self):
        """Punch Sat 04 → Wed 15, then an uncovered absence on Thu 16.

        Nothing exists after the Friday yet — that is the state the nightly
        cron sees when it decides Friday 17.
        """
        day = self.date_from
        while day < self.absent_day:
            if day.weekday() != 4:  # skip Fridays
                self._punch(day)
            day += timedelta(days=1)
        self._absence(self.absent_day)

    def _run_cron_pass(self):
        """The one-day decision the nightly cron takes for the Friday."""
        return self.env['biometric.attendance.sync']._generate_weekend_records(
            self.employee, self.friday, self.friday, commit=False)

    def _create_leave(self, day_from, day_to=None):
        return self.env['hr.leave'].create({
            'name': 'Excuse',
            'employee_id': self.employee.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': day_from,
            'request_date_to': day_to or day_from,
        })

    def _grants(self):
        return self.env['hr.attendance'].search([
            ('employee_id', '=', self.employee.id),
            ('x_is_weekend', '=', True),
        ])

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------
    def test_cron_skips_the_friday_while_the_absence_is_uncovered(self):
        """Baseline: with Thursday absent and Saturday not yet synced, no grant."""
        self._seed_week()

        result = self._run_cron_pass()

        self.assertEqual(result['created'], 0)
        self.assertFalse(self._grants())

    def test_approving_the_leave_grants_the_adjacent_friday(self):
        """The reported bug: an approved excuse must earn the Friday back."""
        self._seed_week()
        self._run_cron_pass()
        self.assertFalse(self._grants(), 'precondition: Friday was skipped')

        leave = self._create_leave(self.absent_day)
        leave.sudo().action_approve()

        absence = self.env['hr.attendance'].search([
            ('employee_id', '=', self.employee.id),
            ('x_is_absent', '=', True),
        ])
        self.assertTrue(absence.x_is_covered,
                        'the leave must cover the Thursday absence')

        grants = self._grants()
        self.assertEqual(len(grants), 1, 'the Friday is granted on approval')
        self.assertEqual(grants.check_in.date(), self.friday)
        self.assertTrue(grants.x_weekend_granted)

    def test_approval_does_not_duplicate_an_existing_grant(self):
        """Re-approving over an already-granted Friday stays a no-op."""
        self._seed_week()
        leave = self._create_leave(self.absent_day)
        leave.sudo().action_approve()
        self.assertEqual(len(self._grants()), 1)

        leave._recheck_weekend_grants()

        self.assertEqual(len(self._grants()), 1, 'idempotent')

    def test_refusing_the_leave_revokes_the_grant(self):
        """Uncovering the Thursday takes the Friday away again."""
        self._seed_week()
        leave = self._create_leave(self.absent_day)
        leave.sudo().action_approve()
        self.assertEqual(len(self._grants()), 1)

        leave.sudo().action_refuse()

        self.assertFalse(self._grants(),
                         'a refused excuse no longer earns the weekend')

    def test_future_leave_grants_nothing_yet(self):
        """Weekends are only ever granted once the days have passed."""
        self._seed_week()
        today = fields.Date.context_today(self.env['hr.leave'])
        future = today + timedelta(days=30)

        leave = self._create_leave(future)
        leave.sudo().action_approve()

        self.assertFalse(self._grants().filtered(
            lambda a: a.check_in.date() > today))
