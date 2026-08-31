# -*- coding: utf-8 -*-
"""Payroll only reads an attendance sheet the supervisor released.

An attendance-sheet employee has no punches — the month is a supervisor's
assertion. It used to flow into payroll whether or not anyone made that
assertion, so a supervisor who never opened the sheet still produced a full
month of paid attendance for their whole team. Now an unconfirmed (or
missing) sheet is read as zero attendance, which is what makes the
confirmation a gate rather than a formality.

Employee setup matches test_payslip_worked_days:
    wage=6000  travel=500  meal=300  medical=200  hra=1500
    Deductible base = 7000 (HRA excluded)
"""
from datetime import date

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestUnconfirmedSheetZero(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.calendar_group = cls.env['resource.calendar.group'].create({
            'name': 'Unconfirmed Sheet Group',
        })
        for day in ['0', '1', '2', '3', '6']:  # Sun-Thu
            cls.env['resource.calendar.group.line'].create({
                'name': f'Work {day}',
                'calendar_group_id': cls.calendar_group.id,
                'dayofweek': day,
                'day_period': 'full_day',
                'hour_from': 8.0,
                'hour_to': 16.5,
            })
            cls.env['resource.calendar.group.line'].create({
                'name': f'Break {day}',
                'calendar_group_id': cls.calendar_group.id,
                'dayofweek': day,
                'day_period': 'break',
                'hour_from': 12.0,
                'hour_to': 13.0,
            })
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Unconfirmed Sheet Calendar',
            'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, cls.calendar_group.id)],
        })

        cls.supervisor_user = cls.env['res.users'].create({
            'name': 'Zero Sheet Supervisor',
            'login': 'zero_sheet_supervisor',
            'email': 'zero_sup@zerosheet.test',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref(
                    'KSW_attendance_sheet.group_attendance_sheet_supervisor'
                ).id,
            ])],
        })
        cls.supervisor = cls.env['hr.employee'].create({
            'name': 'Zero Sheet Supervisor Employee',
            'user_id': cls.supervisor_user.id,
        })

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Zero Sheet Employee',
            'resource_calendar_id': cls.calendar.id,
            'parent_id': cls.supervisor.id,
            'x_is_attendance_sheet': True,
        })
        cls.version = cls.employee.current_version_id
        cls.version.write({
            'name': 'Zero Sheet Version 2026',
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'resource_calendar_id': cls.calendar.id,
            'wage': 6000.0,
            'da': 0.0,
            'travel_allowance': 500.0,
            'meal_allowance': 300.0,
            'medical_allowance': 200.0,
            'other_allowance': 0.0,
            'hra': 1500.0,
            'struct_id': cls.env.ref('om_hr_payroll.structure_base').id,
        })
        cls.employee._compute_current_version_id()

        cls.d_from = date(2026, 3, 1)
        cls.d_to = date(2026, 3, 31)
        cls.days_in_month = 31

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sheet(self):
        return self.env['ksw.attendance.sheet'].sudo().create({
            'employee_id': self.employee.id,
            'month': '3',
            'year': 2026,
        })

    def _worked_days(self):
        vals = self.env['hr.payslip'].get_worked_day_lines(
            self.version, self.d_from, self.d_to)
        return {w['code']: w for w in vals}

    # ------------------------------------------------------------------
    # The gate
    # ------------------------------------------------------------------

    def test_unconfirmed_sheet_pays_zero_attendance(self):
        self._sheet()   # created, never confirmed

        by_code = self._worked_days()

        self.assertEqual(by_code['WORK100']['number_of_days'], 0)
        self.assertEqual(by_code['WORK100']['number_of_hours'], 0)
        self.assertEqual(
            by_code['ATT_ABS']['number_of_days'], self.days_in_month,
            'Every calendar day of an unreleased month counts as absent.')

    def test_confirmed_sheet_pays_the_recorded_attendance(self):
        sheet = self._sheet()
        # Pick real workdays: an off day (the weekly rest day) is derived,
        # not editable, and would raise.
        absent = sheet.line_ids.filtered('is_workday').sorted('date')[:2]
        absent.write({'is_attended': False})
        sheet.action_supervisor_confirm()

        by_code = self._worked_days()

        self.assertEqual(by_code['ATT_ABS']['number_of_days'], 2)
        self.assertGreater(by_code['WORK100']['number_of_days'], 0)

    def test_missing_sheet_pays_zero_attendance(self):
        """No sheet at all is the same situation as an unreleased one."""
        by_code = self._worked_days()

        self.assertEqual(by_code['WORK100']['number_of_days'], 0)
        self.assertEqual(
            by_code['ATT_ABS']['number_of_days'], self.days_in_month)

    def test_unconfirmed_month_deducts_the_whole_period(self):
        """An unreleased month must cost exactly what a month explicitly
        marked fully absent costs.

        Asserted as an invariant against the confirmed path rather than
        against a hard-coded figure, so the test does not quietly encode a
        copy of the deductible-base formula it does not own.
        """
        self._sheet()
        unconfirmed = self._worked_days()

        # Same employee, same month, every workday marked absent by hand.
        sheet = self.env['ksw.attendance.sheet'].sudo().search([
            ('employee_id', '=', self.employee.id),
            ('month', '=', '3'), ('year', '=', 2026),
        ])
        sheet.line_ids.filtered('is_workday').write({'is_attended': False})
        sheet.action_supervisor_confirm()
        all_absent = self._worked_days()

        self.assertEqual(
            unconfirmed['ATT_ABS']['number_of_days'], self.days_in_month)
        self.assertEqual(
            all_absent['ATT_ABS']['number_of_days'], self.days_in_month,
            'Marking every workday absent forfeits the rest days too.')
        self.assertEqual(
            unconfirmed['ATT_DED']['amount'],
            all_absent['ATT_DED']['amount'])

    def test_reopening_a_sheet_takes_the_attendance_back(self):
        """A confirmation withdrawn after a late leave must stop counting."""
        sheet = self._sheet()
        sheet.action_supervisor_confirm()
        self.assertGreater(self._worked_days()['WORK100']['number_of_days'], 0)

        line = sheet.line_ids.filtered('is_workday').sorted('date')[0]
        line.with_context(ksw_system_write=True).write({'is_attended': False})

        self.assertEqual(sheet.state, 'draft')
        self.assertEqual(self._worked_days()['WORK100']['number_of_days'], 0)

    # ------------------------------------------------------------------
    # Payroll's own confirmation blocker
    # ------------------------------------------------------------------

    def test_unconfirmed_vacation_return_blocks_the_sheet(self):
        """The sheet and the payslip batch must refuse for the same reason.

        Otherwise a supervisor could assert a month of attendance for
        someone the system still believes is away.
        """
        sheet = self._sheet()
        leave_type = self.env['hr.leave.type'].sudo().create({
            'name': 'Zero Sheet Annual',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'is_annual_leave': True,
        })
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': leave_type.id,
            'request_date_from': date(2026, 3, 5),
            'request_date_to': date(2026, 3, 8),
        })
        leave.sudo().write({
            'state': 'validate',
            'x_return_state': 'on_vacation',
        })

        blockers = sheet._confirmation_blockers()
        self.assertTrue(
            any('On Vacation' in b for b in blockers),
            'The unresolved return must be reported: %s' % blockers)

        with self.assertRaises(UserError):
            sheet.action_supervisor_confirm()
        self.assertEqual(sheet.state, 'draft')

    def test_confirmed_return_does_not_block(self):
        sheet = self._sheet()
        leave_type = self.env['hr.leave.type'].sudo().create({
            'name': 'Zero Sheet Annual Confirmed',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'is_annual_leave': True,
        })
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': leave_type.id,
            'request_date_from': date(2026, 3, 5),
            'request_date_to': date(2026, 3, 8),
        })
        leave.sudo().write({
            'state': 'validate',
            'x_return_state': 'hr_confirmed',
            'x_return_date': date(2026, 3, 9),
        })
        # Validation already locked those days absent (x_leave_id), so the
        # sheet and the leave record already agree — nothing to correct.
        covered = sheet.line_ids.filtered(
            lambda l: date(2026, 3, 5) <= l.date <= date(2026, 3, 8))
        self.assertFalse(covered.filtered('is_attended'))

        self.assertFalse(sheet._confirmation_blockers())
        sheet.action_supervisor_confirm()
        self.assertEqual(sheet.state, 'confirmed')

    # ------------------------------------------------------------------
    # An unconfirmed return has no end date yet
    # ------------------------------------------------------------------

    def _open_vacation(self, date_from, date_to):
        leave_type = self.env['hr.leave.type'].sudo().create({
            'name': 'Zero Sheet Open Vacation',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'is_annual_leave': True,
        })
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': leave_type.id,
            'request_date_from': date_from,
            'request_date_to': date_to,
        })
        leave.sudo().write({
            'state': 'validate', 'x_return_state': 'on_vacation'})
        return leave

    def test_unconfirmed_return_covers_to_the_end_of_the_month(self):
        """The production shape: a vacation ending mid-month whose return
        nobody confirmed. Stopping on the requested end date would leave the
        rest of the month marked Attended — the sheet asserting a return
        that never happened, for an employee payroll refuses to pay at all.
        """
        sheet = self._sheet()
        leave = self._open_vacation(date(2026, 3, 5), date(2026, 3, 16))

        self.assertEqual(
            sheet._leave_coverage_end(leave, date(2026, 3, 31)),
            date(2026, 3, 31),
            'An open return must cover through the end of the period.')

        sheet.action_apply_approved_leave()

        # Mar 1-4 precede the vacation and were genuinely worked; from the
        # 5th onward nothing is known, so nothing is claimed as attended.
        self.assertEqual(sheet.total_absent, 27)
        self.assertEqual(sheet.total_attended, 4)

    def test_vacation_from_an_earlier_month_absents_the_whole_month(self):
        """KSWCO's actual shape — the one that prompted this.

        A vacation approved in June running to 16 August, return never
        confirmed. Every August day must be absent: the employee was away
        when the month began and nobody has recorded them coming back.
        Payroll refuses to produce a payslip for them at all, so a sheet
        claiming 15 attended days disagrees with what will actually be paid.
        """
        sheet = self._sheet()
        self._open_vacation(date(2026, 1, 20), date(2026, 3, 16))

        sheet.action_apply_approved_leave()

        self.assertEqual(sheet.total_attended, 0)
        self.assertEqual(sheet.total_absent, 31)

    def test_confirmed_return_stops_on_its_real_end_date(self):
        """Once the return is confirmed the override stops applying and the
        ordinary end date takes over."""
        sheet = self._sheet()
        leave = self._open_vacation(date(2026, 3, 5), date(2026, 3, 16))
        leave.sudo().write({
            'x_return_state': 'hr_confirmed',
            'x_return_date': date(2026, 3, 17),
        })

        self.assertEqual(
            sheet._leave_coverage_end(leave, date(2026, 3, 31)),
            date(2026, 3, 16))

        sheet.action_apply_approved_leave()

        self.assertGreater(
            sheet.total_attended, 0,
            'Days after a confirmed return are attended again.')

    def test_open_return_blocker_explains_the_open_end(self):
        sheet = self._sheet()
        self._open_vacation(date(2026, 3, 5), date(2026, 3, 16))

        blockers = '\n'.join(sheet._confirmation_blockers())

        self.assertIn('return was never confirmed', blockers)
        self.assertIn('no evidence the employee came back', blockers)
