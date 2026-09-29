# -*- coding: utf-8 -*-
from datetime import date, timedelta

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase


class TestEmploymentWindow(TransactionCase):
    """A sheet never holds a day before the employee's joining date."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        group = cls.env['resource.calendar.group'].create({
            'name': 'Window Test Group'})
        for day in ['0', '1', '2', '3', '6']:  # Sun-Thu
            cls.env['resource.calendar.group.line'].create({
                'name': f'Work {day}', 'calendar_group_id': group.id,
                'dayofweek': day, 'day_period': 'full_day',
                'hour_from': 8.0, 'hour_to': 16.0,
            })
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Window Test Calendar', 'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, group.id)],
        })
        cls.manager_employee = cls.env['hr.employee'].create({
            'name': 'Window Test Manager'})
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Window Test Employee',
            'resource_calendar_id': cls.calendar.id,
            'parent_id': cls.manager_employee.id,
        })

    def _create_sheet(self, employee=None):
        return self.env['ksw.attendance.sheet'].create({
            'employee_id': (employee or self.employee).id,
            'month': '1', 'year': 2026,
        })

    def _new_joiner(self, joining, name='New Joiner'):
        return self.env['hr.employee'].create({
            'name': name,
            'resource_calendar_id': self.calendar.id,
            'parent_id': self.manager_employee.id,
            'contract_date_start': joining,
        })

    def _auto_atts(self, employee):
        return self.env['hr.attendance'].search([
            ('employee_id', '=', employee.id),
            ('x_is_auto_generated', '=', True),
        ])

    # The case that was reported: joined 18th, sheet gave the whole month.
    def test_sheet_starts_on_joining_date(self):
        emp = self._new_joiner(date(2026, 1, 18))
        sheet = self._create_sheet(employee=emp)
        dates = sheet.line_ids.mapped('date')
        self.assertEqual(min(dates), date(2026, 1, 18))
        self.assertEqual(max(dates), date(2026, 1, 31))
        self.assertEqual(sheet.total_days, 14)
        self.assertEqual(sheet.total_attended, 14)
        self.assertFalse(self._auto_atts(emp).filtered(
            lambda a: a.check_in.date() < date(2026, 1, 17)))
        self.assertFalse(sheet._employment_window_blockers())

    def test_joining_before_month_gives_full_month(self):
        emp = self._new_joiner(date(2025, 6, 1))
        self.assertEqual(len(self._create_sheet(employee=emp).line_ids), 31)

    def test_no_contract_date_keeps_full_month(self):
        self.assertEqual(len(self._create_sheet().line_ids), 31)

    def test_earliest_version_is_the_joining_date(self):
        emp = self._new_joiner(date(2025, 3, 1))
        emp.sudo().version_id.contract_date_end = date(2026, 1, 19)
        emp.sudo().create_version({
            'date_version': date(2026, 1, 20),
            'contract_date_start': date(2026, 1, 20),
        })
        self.assertEqual(len(self._create_sheet(employee=emp).line_ids), 31)

    def test_sheet_for_month_before_joining_refused(self):
        emp = self._new_joiner(date(2026, 2, 3))
        with self.assertRaises(UserError):
            self._create_sheet(employee=emp)

    def test_line_before_joining_refused(self):
        emp = self._new_joiner(date(2026, 1, 18))
        sheet = self._create_sheet(employee=emp)
        with self.assertRaises(ValidationError):
            self.env['ksw.attendance.sheet.line'].create({
                'sheet_id': sheet.id, 'date': date(2026, 1, 5),
            })

    def test_line_outside_month_refused(self):
        sheet = self._create_sheet()
        with self.assertRaises(ValidationError):
            self.env['ksw.attendance.sheet.line'].create({
                'sheet_id': sheet.id, 'date': date(2026, 2, 1),
            })

    def test_regenerate_respects_joining_date(self):
        emp = self._new_joiner(date(2026, 1, 18))
        sheet = self._create_sheet(employee=emp)
        sheet.action_generate_lines()
        self.assertEqual(len(sheet.line_ids), 14)

    # Joining date edited after the sheet exists
    def test_later_joining_date_trims_draft_sheet(self):
        emp = self._new_joiner(date(2026, 1, 5))
        sheet = self._create_sheet(employee=emp)
        kept = sheet.line_ids.filtered(
            lambda l: l.date == date(2026, 1, 20))  # a Tuesday
        kept.write({'is_attended': False})

        emp.sudo().version_id.write({'contract_date_start': date(2026, 1, 18)})

        self.assertEqual(min(sheet.line_ids.mapped('date')), date(2026, 1, 18))
        self.assertEqual(len(sheet.line_ids), 14)
        self.assertFalse(kept.is_attended, 'manual marks must survive')
        self.assertFalse(self._auto_atts(emp).filtered(
            lambda a: a.check_in.date() < date(2026, 1, 17)))
        self.assertFalse(sheet._employment_window_blockers())

    def test_earlier_joining_date_extends_draft_sheet(self):
        emp = self._new_joiner(date(2026, 1, 18))
        sheet = self._create_sheet(employee=emp)
        emp.sudo().version_id.write({'contract_date_start': date(2026, 1, 10)})
        self.assertEqual(min(sheet.line_ids.mapped('date')), date(2026, 1, 10))
        self.assertEqual(len(sheet.line_ids), 22)

    def test_confirmed_sheet_not_touched(self):
        emp = self._new_joiner(date(2026, 1, 5))
        sheet = self._create_sheet(employee=emp)
        sheet.sudo()._do_confirm()
        emp.sudo().version_id.write({'contract_date_start': date(2026, 1, 18)})
        self.assertEqual(min(sheet.line_ids.mapped('date')), date(2026, 1, 5))

    def test_stale_sheet_blocks_confirmation(self):
        """Backstop: a sheet that escaped alignment cannot be released."""
        emp = self._new_joiner(date(2026, 1, 5))
        sheet = self._create_sheet(employee=emp)
        # Move the joining date behind the ORM's back, so no hook fires.
        self.env.cr.execute(
            "UPDATE hr_version SET contract_date_start = %s WHERE id = %s",
            (date(2026, 1, 18), emp.version_id.id))
        emp.version_id.invalidate_recordset(['contract_date_start'])

        blockers = sheet._confirmation_blockers()
        self.assertTrue(any('before the joining date' in b for b in blockers))
        with self.assertRaises(UserError):
            sheet.action_supervisor_confirm()

        sheet._align_to_employment()
        self.assertFalse(sheet._confirmation_blockers())

    # Automatic creation routes skip an employee who has not joined yet
    def test_cron_skips_future_joiner(self):
        today = fields.Date.context_today(self.env['hr.employee'])
        next_month = (today.replace(day=28) + timedelta(days=5)).replace(day=1)
        emp = self._new_joiner(next_month)
        emp.write({'x_is_attendance_sheet': True})   # must not raise
        self.env['ksw.attendance.sheet']._cron_generate_sheets(commit=False)
        self.assertFalse(self.env['ksw.attendance.sheet'].search([
            ('employee_id', '=', emp.id)]))
