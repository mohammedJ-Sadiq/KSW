# -*- coding: utf-8 -*-
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, new_test_user


class TestSheetPrerequisites(TransactionCase):
    """The attendance sheet is refused until manager + main schedule exist."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.hr_user = new_test_user(
            cls.env, login='sheet_prereq_hr', groups='hr.group_hr_user')
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Prereq Main Schedule'})
        cls.manager = cls.env['hr.employee'].create({'name': 'Prereq Manager'})

    def _employee(self, **vals):
        return self.env['hr.employee'].with_user(self.hr_user).create(
            dict({'name': 'Prereq Employee'}, **vals))

    def _sheet_count(self, emp):
        return self.env['ksw.attendance.sheet'].search_count(
            [('employee_id', '=', emp.id)])

    def test_activation_refused_without_manager(self):
        emp = self._employee(main_calendar_id=self.calendar.id)
        with self.assertRaises(ValidationError):
            emp.write({'x_is_attendance_sheet': True})
        self.assertEqual(self._sheet_count(emp), 0)

    def test_activation_refused_without_main_schedule(self):
        emp = self._employee(parent_id=self.manager.id)
        with self.assertRaises(ValidationError):
            emp.write({'x_is_attendance_sheet': True})

    def test_create_with_sheet_refused_when_incomplete(self):
        with self.assertRaises(ValidationError):
            self._employee(x_is_attendance_sheet=True)

    def test_activation_allowed_and_sheet_opened(self):
        emp = self._employee(parent_id=self.manager.id,
                             main_calendar_id=self.calendar.id)
        emp.write({'x_is_attendance_sheet': True})
        self.assertEqual(self._sheet_count(emp), 1)

    def test_create_with_sheet_opens_sheet(self):
        emp = self._employee(parent_id=self.manager.id,
                             main_calendar_id=self.calendar.id,
                             x_is_attendance_sheet=True)
        self.assertEqual(self._sheet_count(emp), 1)

    def test_prerequisite_cannot_be_cleared_while_active(self):
        emp = self._employee(parent_id=self.manager.id,
                             main_calendar_id=self.calendar.id)
        emp.write({'x_is_attendance_sheet': True})
        with self.assertRaises(ValidationError):
            emp.write({'parent_id': False})

    def test_superuser_paths_exempt(self):
        emp = self.env['hr.employee'].create({'name': 'Imported'})
        emp.write({'x_is_attendance_sheet': True})   # no raise
        self.assertTrue(emp.x_is_attendance_sheet)
