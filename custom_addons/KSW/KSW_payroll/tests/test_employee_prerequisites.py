# -*- coding: utf-8 -*-
from lxml import etree

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, new_test_user


class TestEmployeePrerequisites(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.hr_user = new_test_user(
            cls.env, login='payroll_prereq_hr', groups='hr.group_hr_user')
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Payroll Prereq Schedule'})
        cls.manager = cls.env['hr.employee'].create({'name': 'Prereq Mgr'})
        cls.structure = cls.env.ref('om_hr_payroll.structure_base')

    def _employee(self, **vals):
        return self.env['hr.employee'].with_user(self.hr_user).create(dict({
            'name': 'Payroll Prereq Emp',
            'parent_id': self.manager.id,
            'main_calendar_id': self.calendar.id,
        }, **vals))

    def test_sheet_refused_without_salary_structure(self):
        emp = self._employee()
        with self.assertRaises(ValidationError):
            emp.write({'x_is_attendance_sheet': True})

    def test_sheet_allowed_with_salary_structure(self):
        emp = self._employee(struct_id=self.structure.id)
        emp.write({'x_is_attendance_sheet': True})
        self.assertTrue(self.env['ksw.attendance.sheet'].search_count(
            [('employee_id', '=', emp.id)]))

    def test_new_employee_form_requires_the_main_fields(self):
        arch = self.env['hr.employee'].get_views(
            [(False, 'form')])['views']['form']['arch']
        doc = etree.fromstring(arch)
        for fname in ('department_id', 'parent_id', 'leave_manager_id',
                      'attendance_manager_id', 'main_calendar_id',
                      'struct_id'):
            nodes = doc.xpath('//field[@name="%s"][@required="not id"]'
                              % fname)
            self.assertTrue(nodes, '%s is not required on a new employee'
                            % fname)

    def test_salary_structure_given_at_create_is_kept(self):
        """om_hr_payroll dropped it: the related's version did not exist yet."""
        emp = self._employee(struct_id=self.structure.id)
        self.assertEqual(emp.sudo().struct_id, self.structure)
        self.assertEqual(emp.sudo().current_version_id.struct_id,
                         self.structure)

    def test_create_with_sheet_and_structure_opens_sheet(self):
        emp = self._employee(struct_id=self.structure.id,
                             x_is_attendance_sheet=True)
        self.assertTrue(self.env['ksw.attendance.sheet'].search_count(
            [('employee_id', '=', emp.id)]))

    # Contract start date: warn, never block
    def test_missing_contract_is_saved_with_a_warning(self):
        emp = self._employee()
        self.assertTrue(emp.exists(), 'saving must not be blocked')
        self.assertTrue(emp.x_contract_missing)
        notes = emp.sudo().message_ids.filtered(
            lambda m: 'No contract start date' in (m.body or ''))
        self.assertTrue(notes)

    def test_contract_given_no_warning(self):
        emp = self.env['hr.employee'].create({
            'name': 'Has Contract', 'contract_date_start': '2026-09-01'})
        self.assertFalse(emp.x_contract_missing)
        emp2 = self._employee()
        emp2.sudo().version_id.contract_date_start = '2026-09-01'
        self.assertFalse(emp2.x_contract_missing)

    def test_hr_officer_can_read_the_warning_flag(self):
        """contract_date_start is HR-Manager-only; the banner flag is not."""
        emp = self._employee()
        self.assertTrue(emp.with_user(self.hr_user).x_contract_missing)
        arch = self.env['hr.employee'].with_user(self.hr_user).get_views(
            [(False, 'form')])['views']['form']['arch']
        self.assertIn('x_contract_missing', arch)
