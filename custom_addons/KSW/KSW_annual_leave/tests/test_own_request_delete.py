"""Tests: an employee may delete their own time-off request until an
approver acts on it.

Odoo core lets an employee delete an own request only while it is in
confirm/validate1 AND its start date is still in the future
(`hr.leave._unlink_if_correct_states`). On the KSW multi-step chains a
request routinely sits in `confirm` past its own start date while it walks
the 6 approval steps, which stranded the employee with a request they could
neither approve nor remove.

What the override guarantees:

  - own request, untouched      -> deletable, whatever the start date
  - own request, a step signed  -> refused (UserError): the request stopped
                                   being the employee's the moment an
                                   approver acted on it (August 2026)
  - own request, fully approved -> refused (record rule, AccessError)
  - somebody else's request     -> refused (record rule, AccessError)
  - applies to plain leave types too, not just the annual chain
"""
from datetime import date, timedelta

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase


class TestOwnRequestDelete(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login):
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@owndel.test',
                'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
            })

        cls.user_dm = _mkuser('Del DM', 'owndel_dm')
        cls.env['hr.employee'].create({
            'name': 'Del DM Emp', 'user_id': cls.user_dm.id})

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Del Requesting Employee',
            'user_id': _mkuser('Del Emp', 'owndel_emp').id,
            'leave_manager_id': cls.user_dm.id,
        })
        cls.user_emp = cls.employee.user_id

        cls.other_employee = cls.env['hr.employee'].create({
            'name': 'Del Other Employee',
            'user_id': _mkuser('Del Other', 'owndel_other').id,
            'leave_manager_id': cls.user_dm.id,
        })
        cls.user_other = cls.other_employee.user_id

        cls.annual_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave Own-Delete Test',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })
        cls.plain_type = cls.env['hr.leave.type'].create({
            'name': 'Plain Leave Own-Delete Test',
            'requires_allocation': False,
            'leave_validation_type': 'hr',
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _leave(self, employee=None, leave_type=None, start=None, days=3):
        """A pending request (state 'confirm') for `employee`."""
        start = start or date.today() + timedelta(days=10)
        return self.env['hr.leave'].sudo().create({
            'employee_id': (employee or self.employee).id,
            'holiday_status_id': (leave_type or self.annual_type).id,
            'request_date_from': start,
            'request_date_to': start + timedelta(days=days),
        })

    # ==================================================================
    # Own pending request — deletable
    # ==================================================================

    def test_delete_own_future_request(self):
        """The plain Odoo case still works: own, pending, in the future."""
        leave = self._leave()
        leave.with_user(self.user_emp).unlink()
        self.assertFalse(leave.exists())

    def test_delete_own_request_that_already_started(self):
        """The KSW case: still pending, but its start date has passed."""
        leave = self._leave(start=date.today() - timedelta(days=5))
        self.assertEqual(leave.state, 'confirm')
        leave.with_user(self.user_emp).unlink()
        self.assertFalse(leave.exists())

    def test_delete_own_request_at_first_chain_step(self):
        """Deletable while the chain is still waiting on the first approver."""
        leave = self._leave(start=date.today() - timedelta(days=2))
        leave.sudo().write({'x_annual_approval_state': 'pending_dm'})
        leave.with_user(self.user_emp).unlink()
        self.assertFalse(leave.exists())

    def test_cannot_delete_own_request_once_a_step_is_signed(self):
        """The window closes as soon as an approver has acted."""
        steps = ('pending_hr', 'pending_gm_initial', 'pending_acc',
                 'pending_gm_final')
        for offset, step in enumerate(steps):
            # These leaves survive the test — keep them off each other's dates.
            leave = self._leave(
                start=date.today() + timedelta(days=100 + 10 * offset))
            leave.sudo().write({'x_annual_approval_state': step})
            with self.assertRaises(UserError):
                leave.with_user(self.user_emp).unlink()
            self.assertTrue(
                leave.exists(),
                'Employee must not delete an own request at %s' % step)

    def test_delete_own_plain_leave_type(self):
        """Not annual-chain specific — any leave type behaves the same."""
        leave = self._leave(leave_type=self.plain_type,
                            start=date.today() - timedelta(days=5))
        leave.with_user(self.user_emp).unlink()
        self.assertFalse(leave.exists())

    # ==================================================================
    # Refusals
    # ==================================================================

    def test_cannot_delete_own_approved_request(self):
        """Once fully approved the request is out of the employee's hands."""
        leave = self._leave()
        leave.sudo().write({
            'state': 'validate',
            'x_annual_approval_state': 'approved',
        })
        with self.assertRaises(AccessError):
            leave.with_user(self.user_emp).unlink()
        self.assertTrue(leave.exists())

    def test_cannot_delete_other_employees_request(self):
        """The relaxation is strictly own-record."""
        leave = self._leave(employee=self.other_employee,
                            start=date.today() - timedelta(days=5))
        with self.assertRaises(AccessError):
            leave.with_user(self.user_emp).unlink()
        self.assertTrue(leave.exists())

    def test_mixed_batch_still_checks_the_others(self):
        """A batch mixing own and foreign records must not let the foreign
        one through on the back of the own one."""
        own = self._leave(start=date.today() - timedelta(days=5))
        foreign = self._leave(employee=self.other_employee,
                              start=date.today() - timedelta(days=5))
        batch = (own | foreign).with_user(self.user_emp)
        with self.assertRaises(AccessError):
            batch.unlink()
        self.assertTrue(own.exists())
        self.assertTrue(foreign.exists())

    # ==================================================================
    # No regression for the KSW manager bypass
    # ==================================================================

    def test_ksw_officer_still_blocked_on_validated_leave(self):
        """The officer branch keeps its own state check."""
        officer = self.env['res.users'].create({
            'name': 'Del Officer', 'login': 'owndel_officer',
            'email': 'owndel_officer@owndel.test',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('KSW_annual_leave.group_leave_officer').id,
            ])],
        })
        leave = self._leave()
        leave.sudo().write({'state': 'validate'})
        with self.assertRaises(UserError):
            leave.with_user(officer).unlink()
        self.assertTrue(leave.exists())
