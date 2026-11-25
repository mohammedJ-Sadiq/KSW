# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError

class TestFieldAccess(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        
        # Create a contract type
        cls.contract_type = cls.env['hr.contract.type'].create({'name': 'Permanent'})
        
        # Create Employee
        cls.emp = cls.env['hr.employee'].create({
            'name': 'Test Employee',
            'type_id': cls.contract_type.id
        })
        
        # Create User with Employee group
        cls.user_employee = cls.env['res.users'].create({
            'name': 'User Employee',
            'login': 'user_employee',
            'email': 'emp@example.com',
            'group_ids': [
                (6, 0, [cls.env.ref('base.group_user').id, 
                         cls.env.ref('KSW_base_security.group_hr_employee_subordinate').id])
            ],
        })
        cls.emp.write({'user_id': cls.user_employee.id})

    def test_leave_fields_access(self):
        """Test that an employee can read leave-related fields on their own record."""
        fields_to_check = [
            'current_leave_id', 
            'current_leave_state', 
            'leave_date_from', 
            'allocation_count', 
            'allocations_count'
        ]
        data = self.emp.with_user(self.user_employee).read(fields_to_check)
        self.assertTrue(data)
        for field in fields_to_check:
            self.assertIn(field, data[0], f"Field {field} should be accessible")

    def test_homeworking_fields_access(self):
        """Test that an employee can read homeworking-related fields on their own record."""
        fields_to_check = ['exceptional_location_id']
        data = self.emp.with_user(self.user_employee).read(fields_to_check)
        self.assertTrue(data)
        self.assertIn('exceptional_location_id', data[0])

    def test_version_fields_access(self):
        """Test that an employee can read version-related fields on their own record."""
        fields_to_check = ['version_id', 'version_ids', 'versions_count', 'date_start']
        data = self.emp.with_user(self.user_employee).read(fields_to_check)
        self.assertTrue(data)
        for field in fields_to_check:
            self.assertIn(field, data[0], f"Field {field} should be accessible")

    def test_kanban_public_birthday_readable(self):
        """The stock Employees kanban reads birthday_public_display_string, whose
        compute reads the HR-only birthday_public_display flag. A supervisor
        reads hr.employee itself (not the public model), so the kanban and a
        phone Search More were refused (KSWCO 2026-11-25, workshop Driver)."""
        supervisor = self.env['res.users'].create({
            'name': 'User Supervisor', 'login': 'user_supervisor_bday',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id,
                                  self.env.ref('KSW_base_security.group_hr_employee_supervisor').id])],
        })
        self.env['hr.employee'].create({'name': 'Supervisor', 'user_id': supervisor.id})
        # A birthday is needed: without one the compute never reads the flag.
        self.emp.write({'parent_id': supervisor.employee_id.id, 'birthday': '1990-05-01'})
        self.assertTrue(self.env['hr.employee'].with_user(supervisor).browse().has_access('read'))
        self.env.invalidate_all()
        res = self.env['hr.employee'].with_user(supervisor).web_search_read(
            [('id', '=', self.emp.id)], {'birthday_public_display_string': {}})
        self.assertEqual(res['records'][0]['birthday_public_display_string'], 'hidden')
