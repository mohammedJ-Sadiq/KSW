# -*- coding: utf-8 -*-
"""Tests for the two changes introduced together:

1. Saturday short-shift overtime reclassification (net-zero) for calendars
   flagged ``x_saturday_short_overtime`` (e.g. Standard 44 hours/week — Sat 3h,
   Abdullah Mutawa Special Shift — Sat 4h).  The (8h - actual Saturday hours)
   gap is deducted via ATT_DED and credited back 1:1 via SAT_OT → take-home
   unchanged, payslip shows an overtime line.

2. Daily-wage / deduction divisor = actual number of days in the month
   (not a fixed 30).

Setup uses the *actual* deductible-base formula the code implements:
    base = wage + travel_allowance + mobile_allowance + other_allowance
Wage is 6200 so the March (31-day) daily rate is exactly 6200/31 = 200.
"""
from datetime import datetime as dt, date

from odoo.tests.common import TransactionCase

from odoo.addons.KSW_payroll.models.hr_payslip import _days_in_month


class TestSaturdayShortOvertime(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Line = cls.env['resource.calendar.group.line']

        def _group(name, sat_from=None, sat_to=None):
            grp = cls.env['resource.calendar.group'].create({'name': name})
            for day in ['0', '1', '2', '3', '6']:          # Mon-Thu + Sun
                Line.create({
                    'name': f'W{day}', 'calendar_group_id': grp.id,
                    'dayofweek': day, 'day_period': 'full_day',
                    'hour_from': 8.0, 'hour_to': 16.5,
                })
                Line.create({
                    'name': f'B{day}', 'calendar_group_id': grp.id,
                    'dayofweek': day, 'day_period': 'break',
                    'hour_from': 12.0, 'hour_to': 13.0,
                })
            if sat_from is not None:
                Line.create({
                    'name': 'Sat', 'calendar_group_id': grp.id,
                    'dayofweek': '5', 'day_period': 'full_day',
                    'hour_from': sat_from, 'hour_to': sat_to,
                })
            return grp

        # Standard-44-like: Saturday 10:00-13:00 = 3h, flagged.
        cls.cal_44 = cls.env['resource.calendar'].create({
            'name': 'TEST Std 44', 'tz': 'Asia/Riyadh',
            'x_saturday_short_overtime': True,
            'calendar_group_ids': [(4, _group('g44', 10.0, 13.0).id)],
        })
        # Abdullah-like: Saturday 08:00-12:00 = 4h, flagged.
        cls.cal_abd = cls.env['resource.calendar'].create({
            'name': 'TEST Abd', 'tz': 'Asia/Riyadh',
            'x_saturday_short_overtime': True,
            'calendar_group_ids': [(4, _group('gabd', 8.0, 12.0).id)],
        })
        # 48h: Saturday full 8h, NOT flagged.
        cls.cal_48 = cls.env['resource.calendar'].create({
            'name': 'TEST 48', 'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, _group('g48', 8.0, 16.5).id)],
        })

        cls.WAGE = 6200.0            # base = 6200 → March daily rate = 200

    # ------------------------------------------------------------------
    def _employee(self, calendar, is_sheet=False):
        emp = self.env['hr.employee'].create({
            'name': 'SatOT Emp', 'resource_calendar_id': calendar.id,
            'x_is_attendance_sheet': is_sheet,
        })
        ver = emp.current_version_id
        ver.write({
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'resource_calendar_id': calendar.id,
            'wage': self.WAGE,
            'travel_allowance': 0.0, 'mobile_allowance': 0.0,
            'other_allowance': 0.0, 'hra': 0.0,
            'struct_id': self.env.ref('om_hr_payroll.structure_base').id,
        })
        emp._compute_current_version_id()
        return emp, ver

    def _cover_week(self, emp):
        """Clean attendance for every day 2026-03-01(Sun)..03-07(Sat) so
        there are no unpresented days.  Weekdays 08:00-16:30, Saturday
        10:00-13:00 (times in UTC = local - 3h for Riyadh... actually +3;
        local 08:00 = 05:00 UTC)."""
        for d in range(1, 8):
            day = date(2026, 3, d)
            if day.weekday() == 5:       # Saturday short shift 10:00-13:00 local
                ci, co = dt(2026, 3, d, 7, 0), dt(2026, 3, d, 10, 0)
            else:                        # 08:00-16:30 local
                ci, co = dt(2026, 3, d, 5, 0), dt(2026, 3, d, 13, 30)
            self.env['hr.attendance'].create({
                'employee_id': emp.id, 'check_in': ci, 'check_out': co,
            })

    def _wd(self, version):
        vals = self.env['hr.payslip'].get_worked_day_lines(
            version, date(2026, 3, 1), date(2026, 3, 7))
        return {w['code']: w for w in vals}

    # ==================================================================
    # Part B — days-in-month helper
    # ==================================================================
    def test_days_in_month_helper(self):
        self.assertEqual(_days_in_month(date(2026, 3, 15)), 31.0)   # March
        self.assertEqual(_days_in_month(date(2026, 4, 10)), 30.0)   # April
        self.assertEqual(_days_in_month(date(2024, 2, 1)), 29.0)    # leap Feb
        self.assertEqual(_days_in_month(date(2026, 2, 1)), 28.0)
        self.assertEqual(_days_in_month(False), 30.0)               # fallback

    def test_daily_wage_uses_days_in_month(self):
        emp, ver = self._employee(self.cal_48)
        ps_mar = self.env['hr.payslip'].create({
            'employee_id': emp.id, 'version_id': ver.id,
            'date_from': date(2026, 3, 1), 'date_to': date(2026, 3, 31),
            'struct_id': ver.struct_id.id,
        })
        ps_apr = self.env['hr.payslip'].create({
            'employee_id': emp.id, 'version_id': ver.id,
            'date_from': date(2026, 4, 1), 'date_to': date(2026, 4, 30),
            'struct_id': ver.struct_id.id,
        })
        self.assertAlmostEqual(ps_mar.x_daily_wage, self.WAGE / 31.0, places=2)
        self.assertAlmostEqual(ps_apr.x_daily_wage, self.WAGE / 30.0, places=2)

    # ==================================================================
    # Part A — scheduled Saturday hours helper
    # ==================================================================
    def test_scheduled_saturday_hours(self):
        P = self.env['hr.payslip']
        self.assertEqual(P._scheduled_saturday_hours(self.cal_44), 3.0)
        self.assertEqual(P._scheduled_saturday_hours(self.cal_abd), 4.0)
        self.assertEqual(P._scheduled_saturday_hours(self.cal_48), 8.5)

    # ==================================================================
    # Part A — net-zero reclassification
    # ==================================================================
    def test_std44_net_zero_five_hours(self):
        """Standard-44 (Sat 3h): one worked Saturday → 5h gap.
        daily_rate = 6200/31 = 200; hourly = 25; gap = 5 × 25 = 125.
        SAT_OT credit == the Saturday portion of ATT_DED → net zero."""
        emp, ver = self._employee(self.cal_44)
        self._cover_week(emp)
        wd = self._wd(ver)
        self.assertIn('SAT_OT', wd)
        self.assertIn('ATT_DED', wd)
        self.assertEqual(wd['SAT_OT']['amount'], 125)
        # No other deductions (all days clean, full coverage) → ATT_DED is
        # exactly the Saturday gap, i.e. equal to the SAT_OT credit → net zero.
        self.assertEqual(wd['ATT_DED']['amount'], wd['SAT_OT']['amount'])
        self.assertEqual(wd['SAT_OT']['number_of_hours'], 5.0)

    def test_abdullah_net_zero_four_hours(self):
        """Abdullah (Sat 4h): 4h gap → 4 × 25 = 100."""
        emp, ver = self._employee(self.cal_abd)
        self._cover_week(emp)
        wd = self._wd(ver)
        self.assertEqual(wd['SAT_OT']['amount'], 100)
        self.assertEqual(wd['ATT_DED']['amount'], wd['SAT_OT']['amount'])
        self.assertEqual(wd['SAT_OT']['number_of_hours'], 4.0)

    def test_48h_no_reclassification(self):
        """Full 8h Saturday, unflagged → no SAT_OT, no gap deduction."""
        emp, ver = self._employee(self.cal_48)
        self._cover_week(emp)
        wd = self._wd(ver)
        self.assertNotIn('SAT_OT', wd)
        self.assertNotIn('ATT_DED', wd)

    def test_sheet_employee_excluded(self):
        """Attendance-sheet employees are supervisor-marked, so the Saturday
        short-shift deduction/overtime must NOT apply even on a flagged
        calendar (Standard 44).  No SAT_OT, no gap ATT_DED.

        The sheet must be confirmed: payroll reads an unreleased month as
        zero attendance, which would produce an ATT_DED for the whole
        window and mask what this test is actually about.
        """
        emp, ver = self._employee(self.cal_44, is_sheet=True)
        self._cover_week(emp)          # would trigger SAT_OT if not excluded
        sheet = self.env['ksw.attendance.sheet'].sudo().create({
            'employee_id': emp.id, 'month': '3', 'year': 2026,
        })
        sheet.action_supervisor_confirm()

        wd = self._wd(ver)
        self.assertNotIn('SAT_OT', wd)
        self.assertNotIn('ATT_DED', wd)
