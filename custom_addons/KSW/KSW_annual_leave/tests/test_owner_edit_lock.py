# -*- coding: utf-8 -*-
"""The employee cannot rewrite a request approvers already signed.

Audit 2026-09-28: the own-leave write rule is `state not in (validate,
validate1)`, and the KSW chains stay in state 'confirm' until HR's Step 6,
so an employee could change dates after GM final approval, set the derived
day figures (paid days), and clear approver-filled amounts.
"""
from datetime import date, timedelta

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestOwnerEditLock(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        user = cls.env['res.users'].create({
            'name': 'Lock Emp', 'login': 'ownerlock_emp',
            'email': 'ownerlock_emp@x.test',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Owner Lock Employee', 'user_id': user.id})
        cls.user_emp = user
        cls.annual_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Owner Lock Test', 'requires_allocation': False,
            'leave_validation_type': 'annual_multi', 'is_annual_leave': True,
        })

    def _leave(self, step, offset=0):
        start = date.today() + timedelta(days=200 + offset)
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': self.annual_type.id,
            'request_date_from': start,
            'request_date_to': start + timedelta(days=3),
        })
        leave.sudo().write({'x_annual_approval_state': step})
        return leave

    def test_dates_editable_before_first_approval(self):
        leave = self._leave('pending_dm')
        new_end = leave.request_date_to + timedelta(days=1)
        leave.with_user(self.user_emp).write({'request_date_to': new_end})
        self.assertEqual(leave.request_date_to, new_end)

    def test_dates_frozen_after_any_approval(self):
        for i, step in enumerate(('pending_hr', 'pending_gm_initial',
                                  'pending_acc', 'pending_gm_final',
                                  'pending_employee_signature')):
            leave = self._leave(step, offset=10 * (i + 1))
            with self.assertRaises(UserError, msg=step):
                leave.with_user(self.user_emp).write({
                    'request_date_to': leave.request_date_to - timedelta(days=1)})

    def test_derived_figures_never_writable_by_owner(self):
        leave = self._leave('pending_dm', offset=80)
        with self.assertRaises(UserError):
            leave.with_user(self.user_emp).write({'number_of_days': 60.0})
        with self.assertRaises(UserError):
            leave.with_user(self.user_emp).write({'x_annual_portion_days': 60.0})

    def test_owner_cannot_clear_approver_figure(self):
        leave = self._leave('pending_gm_final', offset=90)
        leave.sudo().write({'x_penalty_amount': 250.0})
        with self.assertRaises(UserError):
            leave.with_user(self.user_emp).write({'x_penalty_amount': 0.0})
        self.assertEqual(leave.x_penalty_amount, 250.0)

    def test_create_ignores_owner_supplied_days(self):
        start = date.today() + timedelta(days=300)
        leave = self.env['hr.leave'].with_user(self.user_emp).create({
            'employee_id': self.employee.id,
            'holiday_status_id': self.annual_type.id,
            'request_date_from': start,
            'request_date_to': start + timedelta(days=3),
            'number_of_days': 60.0,
        })
        self.assertNotEqual(leave.sudo().number_of_days, 60.0)

    def test_unchanged_value_is_not_a_change(self):
        """A form resending the current value must not be refused."""
        leave = self._leave('pending_hr', offset=110)
        leave.with_user(self.user_emp).write({
            'request_date_to': leave.request_date_to})
