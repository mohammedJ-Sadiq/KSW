# -*- coding: utf-8 -*-
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user


class TestScheduleAccess(TransactionCase):
    """Work schedules drive every late/early deduction: read for all,
    change only for HR Officers and System Administrators."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.employee_user = new_test_user(
            cls.env, login='sched_plain', groups='base.group_user')
        cls.hr_user = new_test_user(
            cls.env, login='sched_hr', groups='base.group_user,hr.group_hr_user')
        cls.admin_user = new_test_user(
            cls.env, login='sched_admin', groups='base.group_user,base.group_system')
        cls.group = cls.env['resource.calendar.group'].create({'name': 'Acc Test'})
        cls.line = cls.env['resource.calendar.group.line'].create({
            'name': 'Mon', 'calendar_group_id': cls.group.id, 'dayofweek': '0',
            'hour_from': 8.0, 'hour_to': 16.0,
        })

    def test_employee_can_read(self):
        self.assertEqual(self.line.with_user(self.employee_user).hour_from, 8.0)

    def test_employee_cannot_edit_or_delete(self):
        with self.assertRaises(AccessError):
            self.line.with_user(self.employee_user).write({'hour_from': 11.0})
        with self.assertRaises(AccessError):
            self.line.with_user(self.employee_user).unlink()
        with self.assertRaises(AccessError):
            self.group.with_user(self.employee_user).write({'name': 'x'})
        with self.assertRaises(AccessError):
            self.env['resource.calendar.group.line'].with_user(
                self.employee_user).create({
                    'name': 'x', 'calendar_group_id': self.group.id})

    def test_hr_officer_and_admin_can_edit(self):
        self.line.with_user(self.hr_user).write({'hour_from': 9.0})
        self.assertEqual(self.line.hour_from, 9.0)
        self.line.with_user(self.admin_user).write({'hour_from': 10.0})
        self.assertEqual(self.line.hour_from, 10.0)
