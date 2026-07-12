# -*- coding: utf-8 -*-
"""Vacation / EOS payslip attendance-window cap tests.

Rule (gotcha for this project):
    Vacation and EOS payslips (hr.payslip with x_leave_id set) must ONLY
    read attendance UP TO the day before the leave starts (request_date_from
    - 1 day).  Days from the leave start through payslip.date_to are
    injected as absent so ATTDED deducts for them.  The VACATION_BAL / EOS
    inputs compensate for those vacation-period days.

    Without this cap, an employee who has attendance records for the full
    month (e.g. because the attendance sheet was generated before the leave
    was approved) would appear as WORK100 = 31 with no deduction — receiving
    a full regular salary on top of their vacation pay.

This test module covers:
  C1  Mid-month vacation: attendance capped to day before leave start.
  C2  Vacation from day 1: all calendar days are absent.
  C3  Full-month attendance present but still capped correctly.
  C4  Leave starts after payslip end: no cap, full month counted.
  C5  Last-day vacation: only one day absent.
  C6  Recompute after stale worked_days lines respects the cap.
  C7  Regular monthly payslip (no x_leave_id) is NOT affected.

Employee: wage=6000 SAR, travel=500 SAR, mobile=0, other=0 → base=6500.
Payslip month: July 2026 (31 calendar days).
"""
from datetime import date, datetime, time as dt_time

from odoo.tests.common import TransactionCase


JULY_2026 = date(2026, 7, 1)
JULY_2026_END = date(2026, 7, 31)
WAGE = 6000.0
TRAVEL = 500.0
BASE = WAGE + TRAVEL          # 6500
JULY_DAYS = 31
DAILY_RATE = BASE / JULY_DAYS  # ≈ 209.677


def _expected_deduction(n_days):
    """Expected ATT_DED / ATT_ABS monetary deduction for n absent days."""
    return round(DAILY_RATE * n_days)


class TestVacationAttendanceCap(TransactionCase):
    """Vacation and EOS payslips only count pre-leave attendance."""

    # ==================================================================
    # SETUP
    # ==================================================================

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # 7-day work schedule (all days workdays — keeps calendar math simple).
        cg = cls.env['resource.calendar.group'].create({
            'name': 'Vac Cap Test Group',
        })
        for day in ['0', '1', '2', '3', '4', '5', '6']:
            cls.env['resource.calendar.group.line'].create({
                'name': f'Work {day}',
                'calendar_group_id': cg.id,
                'dayofweek': day,
                'day_period': 'full_day',
                'hour_from': 8.0,
                'hour_to': 16.5,
            })
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Vac Cap Test Calendar',
            'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, cg.id)],
        })

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Vac Cap Test Employee',
            'resource_calendar_id': cls.calendar.id,
            'country_id': cls.env.ref('base.sa').id,
        })

        version = cls.employee.current_version_id
        version.write({
            'name': 'Vac Cap Version',
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'resource_calendar_id': cls.calendar.id,
            'wage': WAGE,
            'travel_allowance': TRAVEL,
            'meal_allowance': 0.0,
            'medical_allowance': 0.0,
            'mobile_allowance': 0.0,
            'other_allowance': 0.0,
            'hra': 1500.0,
            'struct_id': cls.env.ref('om_hr_payroll.structure_base').id,
        })
        cls.employee._compute_current_version_id()
        cls.version = cls.employee.current_version_id

        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave Vac Cap Test',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })

    # ==================================================================
    # HELPERS
    # ==================================================================

    def _att(self, day):
        """Create one hr.attendance record for the given date."""
        ci = datetime.combine(day, dt_time(8, 0))
        co = datetime.combine(day, dt_time(16, 30))
        return self.env['hr.attendance'].create({
            'employee_id': self.employee.id,
            'check_in': ci,
            'check_out': co,
        })

    def _att_range(self, d_from, d_to):
        """Create hr.attendance records for every day in [d_from, d_to]."""
        cur = d_from
        while cur <= d_to:
            self._att(cur)
            from datetime import timedelta
            cur += timedelta(days=1)

    def _leave(self, request_date_from, request_date_to=None):
        """Insert an annual leave directly via SQL to bypass approval chain."""
        if request_date_to is None:
            request_date_to = request_date_from
        cal_days = (request_date_to - request_date_from).days + 1
        d_from_utc = datetime.combine(request_date_from, dt_time(5, 0))
        d_to_utc = datetime.combine(request_date_to, dt_time(13, 30))
        self.env.cr.execute("""
            INSERT INTO hr_leave
                (employee_id, holiday_status_id, state,
                 request_date_from, request_date_to,
                 date_from, date_to,
                 number_of_days, number_of_hours,
                 x_return_state, x_annual_approval_state,
                 create_uid, write_uid, create_date, write_date)
            VALUES
                (%s, %s, 'confirm',
                 %s, %s, %s, %s,
                 %s, %s,
                 'not_applicable', 'pending_dm',
                 %s, %s, NOW(), NOW())
            RETURNING id
        """, (
            self.employee.id, self.leave_type.id,
            request_date_from, request_date_to, d_from_utc, d_to_utc,
            cal_days, cal_days * 8.5,
            self.env.uid, self.env.uid,
        ))
        leave_id = self.env.cr.fetchone()[0]
        self.env.invalidate_all()
        return self.env['hr.leave'].browse(leave_id)

    def _payslip(self, leave, date_from=JULY_2026, date_to=JULY_2026_END):
        """Create a vacation payslip with x_leave_id set."""
        return self.env['hr.payslip'].sudo().create({
            'employee_id': self.employee.id,
            'name': 'Vac Cap Test Payslip',
            'date_from': date_from,
            'date_to': date_to,
            'struct_id': self.env.ref('om_hr_payroll.structure_base').id,
            'version_id': self.version.id,
            'x_leave_id': leave.id,
        })

    def _monthly_payslip(self, date_from=JULY_2026, date_to=JULY_2026_END):
        """Create a regular monthly payslip without x_leave_id."""
        return self.env['hr.payslip'].sudo().create({
            'employee_id': self.employee.id,
            'name': 'Monthly Test Payslip',
            'date_from': date_from,
            'date_to': date_to,
            'struct_id': self.env.ref('om_hr_payroll.structure_base').id,
            'version_id': self.version.id,
        })

    def _wd(self, payslip, code):
        """Return the first worked-days line for code, or None."""
        return payslip.worked_days_line_ids.filtered(
            lambda w: w.code == code)[:1] or None

    def _wd_days(self, payslip, code):
        line = self._wd(payslip, code)
        return line.number_of_days if line else 0.0

    def _wd_amount(self, payslip, code):
        line = self._wd(payslip, code)
        return line.amount if line else 0.0

    # ==================================================================
    # C1 — Mid-month vacation: attendance window capped
    # ==================================================================

    def test_c1_mid_month_vacation_caps_attendance(self):
        """WORK100 = pre-leave days only; ATT_ABS = post-leave calendar days."""
        # Attend July 1-5 (5 days before leave on July 6)
        self._att_range(date(2026, 7, 1), date(2026, 7, 5))
        leave = self._leave(date(2026, 7, 6), date(2026, 7, 31))
        slip = self._payslip(leave)
        slip.compute_sheet()

        # 5 attended days before leave
        self.assertEqual(self._wd_days(slip, 'WORK100'), 5)
        # 26 absent days (July 6-31)
        self.assertEqual(self._wd_days(slip, 'ATT_ABS'), 26)
        self.assertEqual(self._wd_days(slip, 'ATT_DED'), 26)
        self.assertEqual(self._wd_amount(slip, 'ATT_DED'),
                         _expected_deduction(26))

    # ==================================================================
    # C2 — Vacation from day 1: all calendar days absent
    # ==================================================================

    def test_c2_vacation_from_day_one(self):
        """When the leave starts on day 1, all 31 July days are absent."""
        leave = self._leave(date(2026, 7, 1), date(2026, 7, 31))
        slip = self._payslip(leave)
        slip.compute_sheet()

        self.assertEqual(self._wd_days(slip, 'WORK100'), 0)
        self.assertEqual(self._wd_days(slip, 'ATT_ABS'), 31)
        self.assertEqual(self._wd_days(slip, 'ATT_DED'), 31)
        self.assertEqual(self._wd_amount(slip, 'ATT_DED'),
                         _expected_deduction(31))

    # ==================================================================
    # C3 — Full-month attendance still capped to pre-leave window
    # ==================================================================

    def test_c3_full_month_attendance_still_capped(self):
        """Even if all 31 July attendance records exist, only July 1-5 count."""
        self._att_range(date(2026, 7, 1), date(2026, 7, 31))
        leave = self._leave(date(2026, 7, 6), date(2026, 7, 31))
        slip = self._payslip(leave)
        slip.compute_sheet()

        # Attendance window capped at July 5 → only 5 days read
        self.assertEqual(self._wd_days(slip, 'WORK100'), 5)
        self.assertEqual(self._wd_days(slip, 'ATT_ABS'), 26)

    # ==================================================================
    # C4 — Leave starts after payslip end: no cap applied
    # ==================================================================

    def test_c4_leave_after_payslip_end_no_cap(self):
        """Leave starting in August does not cap the July payslip."""
        self._att_range(date(2026, 7, 1), date(2026, 7, 31))
        leave = self._leave(date(2026, 8, 1), date(2026, 8, 21))
        slip = self._payslip(leave)
        slip.compute_sheet()

        # Full July counts (leave is next month)
        self.assertEqual(self._wd_days(slip, 'WORK100'), 31)
        self.assertEqual(self._wd_days(slip, 'ATT_ABS'), 0)

    # ==================================================================
    # C5 — Last-day vacation: only one day absent
    # ==================================================================

    def test_c5_last_day_vacation_one_day_absent(self):
        """Vacation starting July 31 deducts exactly 1 day."""
        self._att_range(date(2026, 7, 1), date(2026, 7, 30))
        leave = self._leave(date(2026, 7, 31), date(2026, 8, 20))
        slip = self._payslip(leave)
        slip.compute_sheet()

        self.assertEqual(self._wd_days(slip, 'WORK100'), 30)
        self.assertEqual(self._wd_days(slip, 'ATT_ABS'), 1)
        self.assertEqual(self._wd_days(slip, 'ATT_DED'), 1)
        self.assertEqual(self._wd_amount(slip, 'ATT_DED'),
                         _expected_deduction(1))

    # ==================================================================
    # C6 — Recompute corrects previously stale worked-day lines
    # ==================================================================

    def test_c6_recompute_corrects_stale_lines(self):
        """A second compute_sheet() call after stale data yields correct values."""
        self._att_range(date(2026, 7, 1), date(2026, 7, 31))
        leave = self._leave(date(2026, 7, 6), date(2026, 7, 31))
        slip = self._payslip(leave)

        # Simulate stale worked_days (e.g. batch creation before cap existed)
        slip.sudo().worked_days_line_ids = [
            (0, 0, {
                'name': 'Stale Attended Days',
                'code': 'WORK100',
                'number_of_days': 31,
                'number_of_hours': 263.5,
                'sequence': 1,
                'version_id': self.version.id,
            })
        ]
        self.assertEqual(self._wd_days(slip, 'WORK100'), 31)  # stale

        # Recompute should clear and regenerate with correct window
        slip.compute_sheet()

        self.assertEqual(self._wd_days(slip, 'WORK100'), 5)
        self.assertEqual(self._wd_days(slip, 'ATT_ABS'), 26)

    # ==================================================================
    # C7 — Regular monthly payslip (no x_leave_id) is NOT affected
    # ==================================================================

    def test_c7_regular_payslip_no_cap(self):
        """A monthly payslip without x_leave_id reads the full month."""
        self._att_range(date(2026, 7, 1), date(2026, 7, 31))
        slip = self._monthly_payslip()
        slip.compute_sheet()

        self.assertEqual(self._wd_days(slip, 'WORK100'), 31)
        self.assertEqual(self._wd_days(slip, 'ATT_ABS'), 0)
