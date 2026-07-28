"""Tests for amending the vacation return date after it was confirmed.

The leave manager confirms the employee's return with a Return Date.  That
date drives payroll (worked days / vacation payslip), so a mistake has to be
correctable — but only by the same person who is allowed to confirm it:

  - Gate field: x_can_edit_return_date is True only for the leave manager,
                and only once the leave is on vacation / return confirmed
  - Auth:       writing x_return_date on a confirmed return raises UserError
                for anyone other than the leave manager
  - Chatter:    an amendment posts a "Return Date Amended" note
  - Before confirmation the date stays freely editable (no regression)
"""
from datetime import date, timedelta

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestReturnDateAmend(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, extra_groups=()):
            group_ids = [cls.env.ref('base.group_user').id]
            group_ids.extend(extra_groups)
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@retamend.test',
                'group_ids': [(6, 0, group_ids)],
            })

        cls.user_dm = _mkuser('Amend DM', 'amend_dm')
        cls.user_hr = _mkuser(
            'Amend HR', 'amend_hr',
            [cls.env.ref('KSW_annual_leave.group_annual_leave_hr').id])

        cls.emp_dm = cls.env['hr.employee'].create({
            'name': 'Amend DM Emp', 'user_id': cls.user_dm.id})
        cls.emp_hr = cls.env['hr.employee'].create({
            'name': 'Amend HR Emp', 'user_id': cls.user_hr.id})

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Amend Requesting Employee',
            'user_id': _mkuser('Amend Emp', 'amend_emp').id,
            'leave_manager_id': cls.user_dm.id,
        })
        cls.user_emp = cls.employee.user_id

        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave Return Amend Test',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })

        cls.RETURN = date(2028, 3, 10)
        cls.NEW_RETURN = date(2028, 3, 14)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _on_vacation_leave(self):
        """A validated leave sitting in the 'On Vacation' return state."""
        base = date(2028, 3, 1)
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': base,
            'request_date_to': base + timedelta(days=6),
        })
        leave.sudo().write({
            'state': 'validate',
            'x_annual_approval_state': 'approved',
            'x_return_state': 'on_vacation',
        })
        return leave

    def _confirmed_leave(self):
        leave = self._on_vacation_leave()
        leave.sudo().write({'x_return_date': self.RETURN})
        leave.with_user(self.user_dm).sudo().action_confirm_return_manager()
        return leave

    # ==================================================================
    # Gate field
    # ==================================================================

    def test_gate_true_for_manager_after_confirmation(self):
        leave = self._confirmed_leave()
        self.assertTrue(
            leave.with_user(self.user_dm).x_can_edit_return_date)

    def test_gate_false_for_hr(self):
        leave = self._confirmed_leave()
        self.assertFalse(
            leave.with_user(self.user_hr).x_can_edit_return_date)

    def test_gate_false_for_employee(self):
        leave = self._confirmed_leave()
        self.assertFalse(
            leave.with_user(self.user_emp).x_can_edit_return_date)

    def test_gate_false_when_return_not_applicable(self):
        """A leave that never went on vacation is not amendable."""
        leave = self._on_vacation_leave()
        leave.sudo().write({'x_return_state': 'not_applicable'})
        self.assertFalse(
            leave.with_user(self.user_dm).x_can_edit_return_date)

    # ==================================================================
    # Write guard
    # ==================================================================

    def test_manager_can_amend_confirmed_return_date(self):
        leave = self._confirmed_leave()
        leave.with_user(self.user_dm).sudo().write(
            {'x_return_date': self.NEW_RETURN})
        self.assertEqual(leave.x_return_date, self.NEW_RETURN)

    def test_hr_cannot_amend_confirmed_return_date(self):
        leave = self._confirmed_leave()
        with self.assertRaises(UserError):
            leave.with_user(self.user_hr).write(
                {'x_return_date': self.NEW_RETURN})

    def test_employee_cannot_amend_confirmed_return_date(self):
        leave = self._confirmed_leave()
        with self.assertRaises(UserError):
            leave.with_user(self.user_emp).write(
                {'x_return_date': self.NEW_RETURN})

    def test_return_state_survives_amendment(self):
        """Amending the date does not reopen the return workflow."""
        leave = self._confirmed_leave()
        leave.with_user(self.user_dm).sudo().write(
            {'x_return_date': self.NEW_RETURN})
        self.assertEqual(leave.x_return_state, 'hr_confirmed')

    def test_amendment_posts_chatter_note(self):
        leave = self._confirmed_leave()
        existing_ids = leave.message_ids.ids
        leave.with_user(self.user_dm).sudo().write(
            {'x_return_date': self.NEW_RETURN})
        new_msgs = leave.message_ids.filtered(
            lambda m: m.id not in existing_ids)
        self.assertTrue(new_msgs.filtered(
            lambda m: 'Return Date Amended' in (m.body or '')))

    def test_same_value_write_is_not_guarded(self):
        """Re-writing the identical date must not raise for anyone."""
        leave = self._confirmed_leave()
        leave.with_user(self.user_hr).write({'x_return_date': self.RETURN})
        self.assertEqual(leave.x_return_date, self.RETURN)

    def test_no_guard_before_confirmation(self):
        """While still 'On Vacation' the date stays freely editable."""
        leave = self._on_vacation_leave()
        leave.with_user(self.user_hr).write({'x_return_date': self.RETURN})
        self.assertEqual(leave.x_return_date, self.RETURN)
