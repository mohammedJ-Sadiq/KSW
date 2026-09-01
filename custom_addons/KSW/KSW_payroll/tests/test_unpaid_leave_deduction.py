# -*- coding: utf-8 -*-
"""An unpaid month must be deducted, not paid.

KSWCO payslip 18099 (FAISAL KUNDEYIL MOHAMEDKUTTY, August 2026) came out with
WORK100 = 24 days and ATT_ABS = 7, because unpaid leave 4838 (2026-07-23 →
2026-08-24) had been linked to every absence in the period and the link set
`x_is_covered` — read by the payroll as "excused and paid".  The employee was
paid in full for the days he was explicitly not to be paid for.

`hr.leave._excuses_absence()` (KSW_attendance_leave, narrowed in
KSW_unpaid_leave) now separates the two questions the link used to answer at
once: *why* was he away (still recorded) and *is he paid* for it (no).

Setup mirrors the other payroll tests:
    wage=6000  travel=500  mobile=0  other=0  →  deductible base 6500
    August has 31 days → daily rate = round(6500 / 31) = 210
"""
from datetime import datetime as dt, date

from odoo.tests.common import TransactionCase


class TestUnpaidLeaveDeduction(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Unpaid Deduction Test Calendar',
            'tz': 'Asia/Riyadh',
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Unpaid Deduction Test Employee',
            'resource_calendar_id': cls.calendar.id,
            'tz': 'Asia/Riyadh',
        })
        cls.version = cls.employee.current_version_id
        cls.version.write({
            'name': 'Unpaid Deduction Test Version',
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'resource_calendar_id': cls.calendar.id,
            'wage': 6000.0,
            'da': 0.0,
            'travel_allowance': 500.0,
            'mobile_allowance': 0.0,
            'other_allowance': 0.0,
            'hra': 1500.0,
            'struct_id': cls.env.ref('om_hr_payroll.structure_base').id,
        })
        cls.employee._compute_current_version_id()

        cls.unpaid_type = cls.env['hr.leave.type'].create({
            'name': 'Test Unpaid Leave (payroll)',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
            'is_unpaid_leave': True,
        })
        cls.paid_type = cls.env['hr.leave.type'].create({
            'name': 'Test Sick Leave (payroll)',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
        })

        # The window under test: 4 consecutive days inside August.
        cls.day_from = date(2026, 8, 3)
        cls.day_to = date(2026, 8, 6)
        cls.days = 4
        cls.daily_rate = round(6500.0 / 31.0)  # 210

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _absences(self):
        records = self.env['hr.attendance']
        for day in range(self.day_from.day, self.day_to.day + 1):
            moment = dt(2026, 8, day, 0, 0)
            records |= self.env['hr.attendance'].create({
                'employee_id': self.employee.id,
                'check_in': moment,
                'check_out': moment,
                'x_is_absent': True,
            })
        return records

    def _validated_leave(self, leave_type, absences):
        leave = self.env['hr.leave'].with_context(
            tracking_disable=True, mail_create_nosubscribe=True,
        ).create({
            'employee_id': self.employee.id,
            'holiday_status_id': leave_type.id,
            'request_date_from': self.day_from,
            'request_date_to': self.day_to,
        })
        leave.write({'x_attendance_ids': [(4, a.id) for a in absences]})
        leave.with_context(leave_skip_state_check=True).write(
            {'state': 'validate'})
        absences.invalidate_recordset()
        return leave

    def _worked_days(self):
        return {
            w['code']: w
            for w in self.env['hr.payslip'].get_worked_day_lines(
                self.version, self.day_from, self.day_to)
        }

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------
    def test_unpaid_days_are_absent_and_deducted(self):
        absences = self._absences()
        self._validated_leave(self.unpaid_type, absences)

        self.assertEqual(
            sum(absences.mapped('x_deduction_amount')),
            self.days * self.daily_rate,
            'Every unpaid day carries a full daily-rate deduction.',
        )

        wd = self._worked_days()
        self.assertEqual(wd['WORK100']['number_of_days'], 0)
        self.assertEqual(wd['ATT_ABS']['number_of_days'], self.days)
        self.assertEqual(wd['ATT_ABS']['amount'],
                         self.days * self.daily_rate)
        self.assertEqual(wd['ATT_DED']['amount'],
                         self.days * self.daily_rate)

    def test_paid_leave_days_stay_worked(self):
        """The control: an ordinary leave still pays the day."""
        absences = self._absences()
        self._validated_leave(self.paid_type, absences)

        self.assertEqual(sum(absences.mapped('x_deduction_amount')), 0.0)

        wd = self._worked_days()
        self.assertEqual(wd['WORK100']['number_of_days'], self.days)
        self.assertNotIn('ATT_ABS', wd)
        self.assertNotIn('ATT_DED', wd)
