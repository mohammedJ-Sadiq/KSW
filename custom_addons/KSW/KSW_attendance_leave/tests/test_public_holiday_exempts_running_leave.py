# -*- coding: utf-8 -*-
"""A public holiday must not reach into a leave that is already running.

Covers Odoo 19 Pitfalls #164: core's ``_reevaluate_leaves`` both crashes the
holiday's creation (unguarded bulk state write) and, when it does not crash,
restamps the duration of the very requests KSW exempts.
"""
from datetime import date, datetime

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPublicHolidayExemptsRunningLeave(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'PH Test Schedule',
            'tz': 'Asia/Riyadh',
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'PH Test Employee',
            'resource_calendar_id': cls.calendar.id,
        })
        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'PH Test Annual',
            'requires_allocation': True,
            'leave_validation_type': 'no_validation',
            'allocation_validation_type': 'no_validation',
        })

    def _create_running_leave(self):
        """An approved leave that no longer has an allocation behind it.

        Built in the only order core permits: allocate, take the leave, then
        withdraw the allocation.  ``requires_allocation`` cannot be flipped
        after the fact (``hr_leave_type.check_allocation_requirement_edit_
        validity``), and ``hr.leave.create()`` runs ``_check_validity()``
        unconditionally, so the request cannot be born over-drawn.

        The end state is what matters and it is ordinary here: a KSW annual
        vacation part-way through the year, whose balance accrues daily and
        has not caught up with the days already taken.
        """
        allocation = self.env['hr.leave.allocation'].sudo().create({
            'name': 'PH Test allocation',
            'holiday_status_id': self.leave_type.id,
            'employee_id': self.employee.id,
            'number_of_days': 30,
        })
        allocation.action_approve()
        leave = self.env['hr.leave'].sudo().with_context(
            leave_skip_state_check=True,
            leave_fast_create=True,
        ).create({
            'name': 'Running vacation',
            'employee_id': self.employee.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': date(2031, 5, 10),
            'request_date_to': date(2031, 5, 20),
            'date_from': datetime(2031, 5, 9, 21, 0, 0),
            'date_to': datetime(2031, 5, 20, 20, 59, 59),
            'state': 'validate',
        })
        # Withdraw the balance at SQL level, on purpose.  Every ORM route is
        # guarded -- action_archive() (no 'active' field on the model in Odoo
        # 19), action_refuse() and a plain write all end in "You cannot
        # reduce the duration below the duration of leaves already taken".
        # Which is the point: an over-drawn employee is not reachable through
        # stock flows, yet it is the normal condition in KSWCO, because the
        # KSW accrual cron writes the balance up day by day behind vacations
        # that were already approved in full.  Reproducing the state directly
        # is what the test is about; how it arose is not.
        # Deleted rather than zeroed: a DB check constraint
        # (hr_leave_allocation_duration_check) keeps number_of_days > 0.
        self.env.cr.execute(
            'DELETE FROM hr_leave_allocation WHERE id = %s', (allocation.id,))
        self.env['hr.leave.allocation'].invalidate_model()
        self.env['hr.leave.type'].invalidate_model()
        return leave

    def _create_holiday(self, calendar=None):
        vals = {
            'name': 'PH Test National Day',
            'date_from': datetime(2031, 5, 14, 21, 0, 0),
            'date_to': datetime(2031, 5, 15, 20, 59, 59),
        }
        if calendar is not None:
            vals['calendar_id'] = calendar.id
        return self.env['resource.calendar.leaves'].create(vals)

    def test_holiday_creates_over_a_running_leave(self):
        """Creating the holiday must succeed, not raise ValidationError."""
        self._create_running_leave()
        holiday = self._create_holiday()
        self.assertTrue(holiday.exists())
        self.assertFalse(
            holiday.calendar_id,
            'empty calendar_id is what makes it apply to all schedules')

    def test_holiday_creates_when_scoped_to_one_schedule(self):
        """Same, per-schedule: core's sweep ignores calendar_id entirely."""
        self._create_running_leave()
        holiday = self._create_holiday(calendar=self.calendar)
        self.assertTrue(holiday.exists())

    def test_running_leave_is_left_exactly_as_approved(self):
        """The exempted employee keeps his duration, his state and his days."""
        leave = self._create_running_leave()
        before_days = leave.number_of_days
        before_state = leave.state
        self.assertTrue(before_days, 'fixture must produce a non-zero duration')

        self._create_holiday()

        leave.invalidate_recordset()
        self.assertEqual(
            leave.number_of_days, before_days,
            'the holiday must not hand days back to someone already on leave')
        self.assertEqual(
            leave.state, before_state,
            'core refuses leaves it cannot revalidate; this one must survive')

    def test_holiday_with_no_one_on_leave_still_creates(self):
        """The exemption must not break the ordinary case."""
        holiday = self._create_holiday()
        self.assertTrue(holiday.exists())
