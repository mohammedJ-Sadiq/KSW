# -*- coding: utf-8 -*-
"""Custom Balance Consumption: the System Administrator picks how many days
an annual leave request charges to the balance, anywhere from the days
requested up to the whole remaining balance, and it is charged like Full
Balance Clearance."""
from datetime import date, timedelta

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase


class TestCustomConsumption(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user_admin = cls.env['res.users'].create({
            'name': 'Custom Consumption Admin',
            'login': 'test_custom_consumption_admin',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('base.group_system').id,
            ])],
        })
        cls.user_hr = cls.env['res.users'].create({
            'name': 'Custom Consumption HR',
            'login': 'test_custom_consumption_hr',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('hr_holidays.group_hr_holidays_user').id,
                cls.env.ref('KSW_annual_leave.group_annual_leave_hr').id,
                cls.env.ref('KSW_annual_leave.group_leave_officer').id,
            ])],
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Custom Consumption Employee',
        })
        cls.employee.current_version_id.write({
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'wage': 6000.0,
        })
        cls.employee._compute_current_version_id()
        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave Custom Consumption Test',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })
        cls.env['ksw.annual.leave'].sudo().create({
            'employee_id': cls.employee.id,
        })

    def _balance(self):
        return self.env['ksw.annual.leave'].sudo().search([
            ('employee_id', '=', self.employee.id)], limit=1).remaining_balance

    def _create_leave(self, days=10, date_from=date(2026, 8, 1)):
        return self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': date_from,
            'request_date_to': date_from + timedelta(days=days - 1),
        })

    def _set_custom(self, leave, days, user=None):
        leave.with_user(user or self.user_admin).write({
            'x_is_custom_consumption': True,
            'x_custom_consumption_days': days,
        })

    def test_admin_figure_is_the_charged_duration(self):
        balance = self._balance()
        self.assertGreater(balance, 15, 'Fixture needs a balance above 15 days.')
        leave = self._create_leave(days=10)
        self._set_custom(leave, 15)
        self.assertAlmostEqual(leave.number_of_days, 15, places=4)
        self.assertAlmostEqual(leave.x_actual_vacation_days, 10, places=4)
        self.assertEqual(leave._get_durations()[leave.id][0], 15)
        self.assertFalse(leave.x_is_full_clearance)
        self.assertEqual(leave.x_clearance_balance, 0)

    def test_both_ends_of_the_range_are_accepted(self):
        balance = self._balance()
        leave = self._create_leave(days=10)
        self._set_custom(leave, 10)
        self.assertAlmostEqual(leave.number_of_days, 10, places=4)
        self._set_custom(leave, balance)
        self.assertAlmostEqual(leave.number_of_days, balance, places=4)

    def test_below_requested_days_is_refused(self):
        leave = self._create_leave(days=10)
        with self.assertRaises(ValidationError):
            self._set_custom(leave, 9)

    def test_above_balance_is_refused(self):
        leave = self._create_leave(days=10)
        with self.assertRaises(ValidationError):
            self._set_custom(leave, self._balance() + 1)

    def test_only_system_admin_can_set_it(self):
        leave = self._create_leave(days=10)
        with self.assertRaises(UserError):
            self._set_custom(leave, 12, user=self.user_hr)
        self.assertFalse(leave.x_is_custom_consumption)

    def test_only_system_admin_can_create_with_it(self):
        with self.assertRaises(UserError):
            self.env['hr.leave'].with_user(self.user_hr).create({
                'employee_id': self.employee.id,
                'holiday_status_id': self.leave_type.id,
                'request_date_from': date(2026, 9, 1),
                'request_date_to': date(2026, 9, 5),
                'x_is_custom_consumption': True,
                'x_custom_consumption_days': 7,
            })

    def test_cannot_combine_with_full_clearance(self):
        leave = self._create_leave(days=10)
        self._set_custom(leave, 12)
        with self.assertRaises(ValidationError):
            leave.sudo().write({'x_is_full_clearance': True})

    def test_cleared_when_request_exceeds_balance(self):
        leave = self._create_leave(days=10)
        self._set_custom(leave, 12)
        too_long = int(self._balance()) + 5
        leave.sudo().write({
            'request_date_to': leave.request_date_from + timedelta(days=too_long - 1),
        })
        self.assertFalse(leave.x_is_custom_consumption)
        self.assertAlmostEqual(leave.number_of_days, too_long, places=4)

    def test_reset_clears_it(self):
        leave = self._create_leave(days=10)
        self._set_custom(leave, 12)
        leave.sudo()._reset_annual_multi_fields()
        self.assertFalse(leave.x_is_custom_consumption)
        self.assertEqual(leave.x_custom_consumption_days, 0)
        self.assertAlmostEqual(leave.number_of_days, 10, places=4)
