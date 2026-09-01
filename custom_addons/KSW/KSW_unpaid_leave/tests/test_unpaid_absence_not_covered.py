# -*- coding: utf-8 -*-
"""An unpaid leave explains an absence; it does not pay for it.

Regression for KSWCO leave 4838 (FAISAL KUNDEYIL MOHAMEDKUTTY, unpaid
2026-07-23 → 2026-08-24).  `_auto_link_absence_attendance()` links the
employee's absence records to any validated non-attendance-issue leave, and the
link used to set `x_is_covered` — which the payroll reads as "excused and
paid".  So every day of an unpaid month landed in WORK100 and ATTDED deducted
nothing: the employee was paid in full for leave granted precisely because he
was not to be paid.

The link itself is right — it is how the attendance view says *why* the day is
empty.  Only the payment half was wrong, so `hr.leave._excuses_absence()` now
takes unpaid leaves out of the paid set while the m2m stays populated.
"""
from datetime import datetime as dt, date

from odoo.tests.common import TransactionCase


class TestUnpaidAbsenceNotCovered(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Unpaid Coverage Test Calendar',
            'tz': 'Asia/Riyadh',
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Unpaid Coverage Test Employee',
            'resource_calendar_id': cls.calendar.id,
            'tz': 'Asia/Riyadh',
        })

        cls.unpaid_type = cls.env['hr.leave.type'].create({
            'name': 'Test Unpaid Leave',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
            'is_unpaid_leave': True,
        })
        cls.paid_type = cls.env['hr.leave.type'].create({
            'name': 'Test Sick Leave',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
        })
        # Same as unpaid_type but needs an approval, so a fresh request stays
        # in `confirm` instead of validating itself on create.
        cls.pending_unpaid_type = cls.env['hr.leave.type'].create({
            'name': 'Test Unpaid Leave (needs approval)',
            'requires_allocation': False,
            'leave_validation_type': 'manager',
            'request_unit': 'day',
            'is_unpaid_leave': True,
        })

        cls.date_from = date(2026, 8, 3)
        cls.date_to = date(2026, 8, 6)

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

    def _leave(self, leave_type):
        return self.env['hr.leave'].with_context(
            tracking_disable=True, mail_create_nosubscribe=True,
        ).create({
            'employee_id': self.employee.id,
            'holiday_status_id': leave_type.id,
            'request_date_from': self.date_from,
            'request_date_to': self.date_to,
        })

    def _validated(self, leave_type, absences):
        leave = self._leave(leave_type)
        leave.write({'x_attendance_ids': [(4, a.id) for a in absences]})
        leave.with_context(leave_skip_state_check=True).write(
            {'state': 'validate'})
        absences.invalidate_recordset()
        return leave

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------
    def test_unpaid_leave_does_not_cover_the_absence(self):
        absence = self._absence(self.date_from)
        self._validated(self.unpaid_type, absence)

        self.assertFalse(
            absence.x_is_covered,
            'An unpaid leave must never mark the day as covered — covered '
            'means paid, and the whole point of the leave is that it is not.',
        )
        self.assertTrue(
            absence.x_net_is_absent,
            'The day must stay absent so ATT_ABS counts it and ATTDED '
            'deducts for it.',
        )
        self.assertEqual(
            absence.x_net_worked_hours, 0.0,
            'No scheduled hours may be credited for an unpaid day.',
        )

    def test_the_link_is_kept_for_the_audit_trail(self):
        """Not covered is not the same as not linked."""
        absence = self._absence(self.date_from)
        leave = self._validated(self.unpaid_type, absence)

        self.assertIn(absence, leave.x_attendance_ids)
        self.assertIn(leave, absence.x_leave_ids)

    def test_auto_link_still_runs_for_unpaid_leaves(self):
        """The absences are attached on validation, just not excused."""
        leave = self._leave(self.unpaid_type)
        leave.with_context(leave_skip_state_check=True).write(
            {'state': 'validate'})
        absences = self.env['hr.attendance']
        for day in range(3, 7):
            absences |= self._absence(date(2026, 8, day))

        leave._auto_link_absence_attendance()
        absences.invalidate_recordset()

        self.assertEqual(len(leave.x_attendance_ids), 4)
        self.assertFalse(absences.filtered('x_is_covered'))
        self.assertEqual(len(absences.filtered('x_net_is_absent')), 4)

    def test_a_paid_leave_still_covers(self):
        """The control: nothing changes for ordinary (paid) leave types."""
        absence = self._absence(self.date_from)
        self._validated(self.paid_type, absence)

        self.assertTrue(absence.x_is_covered)
        self.assertFalse(absence.x_net_is_absent)

    def test_an_unapproved_unpaid_leave_covers_nothing_either(self):
        absence = self._absence(self.date_from)
        leave = self._leave(self.pending_unpaid_type)
        leave.write({'x_attendance_ids': [(4, absence.id)]})
        absence.invalidate_recordset()

        self.assertEqual(leave.state, 'confirm')
        self.assertFalse(absence.x_is_covered)
