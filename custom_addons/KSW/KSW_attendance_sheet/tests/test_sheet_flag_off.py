# -*- coding: utf-8 -*-
from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, new_test_user


class TestSheetFlagOff(TransactionCase):
    """Turning "Uses Attendance Sheet" off removes this month's sheet."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.hr_user = new_test_user(
            cls.env, login='sheet_flag_off_hr', groups='hr.group_hr_user')
        calendar = cls.env['resource.calendar'].create({
            'name': 'Flag Off Schedule'})
        manager = cls.env['hr.employee'].create({'name': 'Flag Off Manager'})
        # Superuser: exempt from the prerequisite guard (struct_id etc.).
        cls.emp = cls.env['hr.employee'].create({
            'name': 'Flag Off Employee', 'parent_id': manager.id,
            'main_calendar_id': calendar.id,
        })
        cls.emp.write({'x_is_attendance_sheet': True})
        cls.Sheet = cls.env['ksw.attendance.sheet']
        cls.today = fields.Date.context_today(cls.Sheet)

    def _current(self):
        return self.Sheet.search([
            ('employee_id', '=', self.emp.id),
            ('month', '=', str(self.today.month)),
            ('year', '=', self.today.year)])

    def _auto_atts(self):
        return self.env['hr.attendance'].search([
            ('employee_id', '=', self.emp.id),
            ('x_is_auto_generated', '=', True)])

    def _turn_off(self):
        self.emp.with_user(self.hr_user).write(
            {'x_is_attendance_sheet': False})

    def test_flag_off_removes_current_sheet_and_attendance(self):
        sheet = self._current()
        self.assertTrue(sheet)
        self.assertTrue(self._auto_atts(), 'fixture: sheet made no attendance')
        self._turn_off()
        self.assertFalse(sheet.exists())
        self.assertFalse(self._auto_atts())

    def test_past_month_sheet_kept(self):
        past = self.today - relativedelta(months=1)
        old = self.Sheet.create({
            'employee_id': self.emp.id, 'month': str(past.month),
            'year': past.year})
        self._turn_off()
        self.assertTrue(old.exists())
        self.assertFalse(self._current())

    def test_confirmed_current_sheet_refuses_flag_off(self):
        self._current().write({'state': 'confirmed'})
        with self.assertRaises(ValidationError):
            self._turn_off()
        self.assertTrue(self.emp.x_is_attendance_sheet)

    def test_superuser_keeps_confirmed_sheet(self):
        sheet = self._current()
        sheet.write({'state': 'confirmed'})
        self.emp.write({'x_is_attendance_sheet': False})
        self.assertTrue(sheet.exists())

    def test_deleting_a_sheet_removes_its_attendance(self):
        self.assertTrue(self._auto_atts(), 'fixture: sheet made no attendance')
        self._current().unlink()
        self.assertFalse(self._auto_atts())
