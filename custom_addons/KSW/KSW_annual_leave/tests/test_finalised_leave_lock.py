"""Tests: a finalised time off request can only be reversed by the Settings
Administrator.

Mirrors the payslip rule (``hr.payslip._check_payroll_manager``, KSWCO
SLIP/11307): once a request is approved, nobody walks it backwards except
the system administrator. "Finalised" has two shapes here —

  * ``state == 'validate'`` for the ordinary leave types, and
  * a KSW multi-step chain past **GM final approval**, which sits in
    ``state == 'confirm'`` until HR confirms the signed form.

Every route out of a finalised request is covered: Refuse, Back to Approval,
Reset to Draft, the Cancel wizard, a direct ``write({'state': ...})`` over
RPC, and delete.
"""
from datetime import date, timedelta

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestFinalisedLeaveLock(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, groups=()):
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@finlock.test',
                'group_ids': [(6, 0, [cls.env.ref('base.group_user').id]
                               + [cls.env.ref(g).id for g in groups])],
            })

        cls.user_dm = _mkuser('Lock DM', 'finlock_dm')
        cls.env['hr.employee'].create({
            'name': 'Lock DM Emp', 'user_id': cls.user_dm.id})

        # A normal Time Off Officer + HR Approver: the role that could
        # previously refuse an approved request.
        cls.user_hr = _mkuser('Lock HR', 'finlock_hr', (
            'KSW_annual_leave.group_annual_leave_hr',
            'KSW_annual_leave.group_leave_officer',
        ))
        # Odoo's own Time Off Administrator — deliberately NOT enough.
        cls.user_holidays_admin = _mkuser(
            'Lock Holidays Admin', 'finlock_hadmin',
            ('hr_holidays.group_hr_holidays_manager',))
        # The only role that may reverse a finalised request.
        cls.user_admin = _mkuser('Lock Sys Admin', 'finlock_admin', (
            'base.group_system',
            'KSW_annual_leave.group_leave_officer',
            'hr_holidays.group_hr_holidays_manager',
        ))

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Lock Requesting Employee',
            'user_id': _mkuser('Lock Emp', 'finlock_emp').id,
            'leave_manager_id': cls.user_dm.id,
        })
        cls.user_emp = cls.employee.user_id

        cls.annual_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave Finalised-Lock Test',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })
        cls.plain_type = cls.env['hr.leave.type'].create({
            'name': 'Plain Leave Finalised-Lock Test',
            'requires_allocation': False,
            'leave_validation_type': 'hr',
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def setUp(self):
        super().setUp()
        # Leaves for the same employee may not overlap; hand each request its
        # own slice of the calendar.
        self._slot = 0

    def _leave(self, leave_type=None, days=3, start=None):
        if start is None:
            self._slot += 1
            start = date.today() + timedelta(days=10 * self._slot)
        return self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': (leave_type or self.plain_type).id,
            'request_date_from': start,
            'request_date_to': start + timedelta(days=days),
        })

    def _validated(self, leave_type=None):
        """An ordinary leave that reached state 'validate'."""
        leave = self._leave(leave_type)
        leave.sudo().write({'state': 'validate'})
        return leave

    def _gm_final_approved(self):
        """A multi-step request past GM final approval (state stays 'confirm')."""
        leave = self._leave(self.annual_type)
        leave.sudo().write({
            'x_annual_approval_state': 'pending_employee_signature'})
        self.assertEqual(leave.state, 'confirm')
        return leave

    # ==================================================================
    # _is_finalised
    # ==================================================================

    def test_is_finalised_recognises_both_finish_lines(self):
        self.assertFalse(self._leave()._is_finalised())
        self.assertFalse(self._leave(self.annual_type)._is_finalised())
        self.assertTrue(self._validated()._is_finalised())
        self.assertTrue(self._gm_final_approved()._is_finalised())

    def test_mid_chain_request_is_not_finalised(self):
        """Everything up to and including GM final *pending* stays reversible."""
        for step in ('pending_dm', 'pending_hr', 'pending_gm_initial',
                     'pending_acc', 'pending_gm_final'):
            leave = self._leave(self.annual_type)
            leave.sudo().write({'x_annual_approval_state': step})
            self.assertFalse(
                leave._is_finalised(),
                '%s must remain refusable by the approvers' % step)

    # ==================================================================
    # Refuse
    # ==================================================================

    def test_officer_cannot_refuse_validated_leave(self):
        leave = self._validated()
        with self.assertRaises(UserError):
            leave.with_user(self.user_hr).action_refuse()
        self.assertEqual(leave.state, 'validate')

    def test_hr_cannot_refuse_after_gm_final_approval(self):
        leave = self._gm_final_approved()
        with self.assertRaises(UserError):
            leave.with_user(self.user_hr).action_refuse()
        self.assertEqual(
            leave.x_annual_approval_state, 'pending_employee_signature')

    def test_admin_can_refuse_validated_leave(self):
        leave = self._validated()
        leave.with_user(self.user_admin).action_refuse()
        self.assertEqual(leave.state, 'refuse')

    def test_admin_can_refuse_after_gm_final_approval(self):
        leave = self._gm_final_approved()
        leave.with_user(self.user_admin).action_refuse()
        self.assertEqual(leave.state, 'refuse')

    def test_sudo_is_exempt(self):
        """Internal flows (crons, payslip cancellation) keep working."""
        leave = self._validated()
        leave.sudo().action_refuse()
        self.assertEqual(leave.state, 'refuse')

    def test_pending_request_is_still_refusable(self):
        """No regression: the guard only bites once the request is finalised."""
        leave = self._leave(self.annual_type)
        leave.sudo().write({'x_annual_approval_state': 'pending_hr'})
        leave.with_user(self.user_hr).action_refuse()
        self.assertEqual(leave.state, 'refuse')

    # ==================================================================
    # Back to Approval / Reset to Draft / direct write
    # ==================================================================

    def test_officer_cannot_send_validated_leave_back_to_approval(self):
        leave = self._validated()
        with self.assertRaises(UserError):
            leave.with_user(self.user_hr)._move_validate_leave_to_confirm()
        self.assertEqual(leave.state, 'validate')

    def test_officer_cannot_reset_finalised_request_to_draft(self):
        leave = self._gm_final_approved()
        with self.assertRaises(UserError):
            leave.with_user(self.user_hr).action_draft()
        self.assertEqual(
            leave.x_annual_approval_state, 'pending_employee_signature')

    def test_direct_state_write_is_blocked(self):
        """The RPC route around the buttons is closed too."""
        leave = self._validated()
        with self.assertRaises(UserError):
            leave.with_user(self.user_hr).write({'state': 'refuse'})
        self.assertEqual(leave.state, 'validate')

    def test_direct_state_write_blocked_after_gm_final(self):
        leave = self._gm_final_approved()
        with self.assertRaises(UserError):
            leave.with_user(self.user_hr).write({'state': 'confirm'})

    # ==================================================================
    # Cancel wizard
    # ==================================================================

    def test_employee_cannot_cancel_approved_own_leave(self):
        leave = self._validated()
        self.assertFalse(leave.with_user(self.user_emp).can_cancel)
        with self.assertRaises(UserError):
            leave.with_user(self.user_emp)._action_user_cancel('changed my mind')
        self.assertEqual(leave.state, 'validate')

    def test_employee_cannot_cancel_once_a_step_is_signed(self):
        leave = self._leave(self.annual_type)
        leave.sudo().write({'x_annual_approval_state': 'pending_hr'})
        with self.assertRaises(UserError):
            leave.with_user(self.user_emp)._action_user_cancel('changed my mind')

    # ==================================================================
    # Delete
    # ==================================================================

    def test_holidays_administrator_cannot_delete_finalised_request(self):
        """Odoo's Time Off Administrator is not the Settings Administrator."""
        leave = self._gm_final_approved()
        with self.assertRaises(UserError):
            leave.with_user(self.user_holidays_admin).unlink()
        self.assertTrue(leave.exists())

    def test_officer_cannot_delete_validated_leave(self):
        leave = self._validated()
        with self.assertRaises(UserError):
            leave.with_user(self.user_hr).unlink()
        self.assertTrue(leave.exists())

    def test_admin_can_delete_finalised_request(self):
        leave = self._gm_final_approved()
        leave.with_user(self.user_admin).unlink()
        self.assertFalse(leave.exists())
