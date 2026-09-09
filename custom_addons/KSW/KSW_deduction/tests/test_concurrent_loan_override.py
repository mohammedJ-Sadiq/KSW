"""A second personal loan is a warning to override, not a wall.

Until Sep 2026 `action_submit` refused outright when the employee already
had a live loan. The rule stays visible — the request now carries a banner
and a chatter note — but it is decided by people: HR, Accounting and the GM
each have to click "Accept Concurrent Loan" at their own step before their
approval will run, and the acceptance is stamped per step so the audit
trail names the role that took on the extra exposure.

Negative cases go through `with_user(...)` with no `sudo()` — the role
guard exempts `env.su`, so a sudo'd call would prove nothing (Odoo 19
Pitfalls #16). The acceptance guard itself is deliberately NOT exempt from
`env.su`: like `x_hr_no_penalties_confirmed`, it is a decision, not a
permission, so even a superuser call has to record it.
"""
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase


class TestConcurrentLoanOverride(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, groups=()):
            group_ids = [cls.env.ref('base.group_user').id]
            group_ids.extend(cls.env.ref(g).id for g in groups)
            return cls.env['res.users'].create({
                'name': name, 'login': login, 'email': f'{login}@conloan.test',
                'group_ids': [(6, 0, group_ids)],
            })

        cls.user_hr = _mkuser('Conloan HR', 'conloan_hr',
                              ('KSW_deduction.group_loan_hr',))
        cls.user_acc = _mkuser('Conloan Acc', 'conloan_acc',
                               ('KSW_deduction.group_loan_acc',))
        cls.user_gm = _mkuser('Conloan GM', 'conloan_gm')
        cls.user_gm_other = _mkuser('Conloan GM Other', 'conloan_gm_other')

        Employee = cls.env['hr.employee']
        cls.emp_gm = Employee.create(
            {'name': 'Conloan GM Emp', 'user_id': cls.user_gm.id})
        cls.emp_gm_other = Employee.create(
            {'name': 'Conloan GM Other Emp', 'user_id': cls.user_gm_other.id})

        Department = cls.env['hr.department']
        # Naming a department GM is what grants group_loan_gm.
        cls.dept = Department.create(
            {'name': 'Conloan Dept', 'x_gm_id': cls.emp_gm.id})
        cls.dept_other = Department.create(
            {'name': 'Conloan Other Dept', 'x_gm_id': cls.emp_gm_other.id})

        cls.manager = Employee.create({
            'name': 'Conloan Manager',
            'user_id': _mkuser('Conloan Mgr', 'conloan_mgr').id})
        cls.employee = Employee.create({
            'name': 'Conloan Employee', 'department_id': cls.dept.id,
            'parent_id': cls.manager.id})
        cls.employee_b = Employee.create({
            'name': 'Conloan Employee B', 'department_id': cls.dept.id,
            'parent_id': cls.manager.id})

        cls.loan_type = cls.env['ksw.deduction.type'].sudo().search(
            [('is_loan', '=', True)], limit=1)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _make_loan(self, employee=None, amount=1200.0):
        return self.env['ksw.deduction'].sudo().create({
            'employee_id': (employee or self.employee).id,
            'type_id': self.loan_type.id,
            'amount': amount,
            'installments': 12,
        })

    def _live_loan(self, employee=None):
        """A loan already in the chain — the thing a second one collides with."""
        loan = self._make_loan(employee)
        loan.sudo().write({'approval_state': 'pending_hr'})
        return loan

    def _at(self, loan, state):
        loan.sudo().write({'approval_state': state})
        return loan

    # ------------------------------------------------------------------
    # The block is gone
    # ------------------------------------------------------------------
    def test_second_loan_can_be_submitted(self):
        self._live_loan()
        second = self._make_loan()
        second.sudo().action_submit()
        self.assertEqual(second.approval_state, 'pending_dm')
        self.assertTrue(second.x_has_concurrent_loan)
        self.assertEqual(second.x_concurrent_loan_count, 1)

    def test_dm_step_needs_no_acceptance(self):
        """The warning is raised from HR upward, not at the DM step."""
        self._live_loan()
        second = self._at(self._make_loan(), 'pending_dm')
        self.assertFalse(
            second.with_user(self.user_hr).x_concurrent_ack_pending)

    def test_warning_names_the_other_loan(self):
        first = self._live_loan()
        second = self._make_loan()
        self.assertIn(first.name, second.x_concurrent_loan_summary)

    def test_another_employees_loan_is_not_concurrent(self):
        self._live_loan(self.employee_b)
        loan = self._make_loan(self.employee)
        self.assertFalse(loan.x_has_concurrent_loan)

    def test_cancelled_loan_is_not_concurrent(self):
        first = self._live_loan()
        first.sudo().write({'state': 'cancelled'})
        second = self._make_loan()
        self.assertFalse(second.x_has_concurrent_loan)

    # ------------------------------------------------------------------
    # Each step must accept before it can approve
    # ------------------------------------------------------------------
    def test_hr_cannot_approve_without_accepting(self):
        self._live_loan()
        second = self._at(self._make_loan(), 'pending_hr')
        second.sudo().write({'x_hr_no_penalties_confirmed': True})
        with self.assertRaises(ValidationError):
            second.with_user(self.user_hr).sudo().action_hr_approve()
        self.assertEqual(second.approval_state, 'pending_hr')

    def test_hr_can_approve_after_accepting(self):
        self._live_loan()
        second = self._at(self._make_loan(), 'pending_hr')
        second.sudo().write({'x_hr_no_penalties_confirmed': True})
        second.with_user(self.user_hr).action_bypass_concurrent_loan()
        self.assertTrue(second.x_concurrent_ack_hr)
        second.with_user(self.user_hr).sudo().action_hr_approve()
        self.assertEqual(second.approval_state, 'pending_acc')

    def test_accounting_must_accept_again(self):
        """HR's acceptance is HR's. Accounting makes its own call."""
        self._live_loan()
        second = self._at(self._make_loan(), 'pending_acc')
        second.sudo().write({
            'x_acc_budget_confirmed': True,
            'x_concurrent_ack_hr': True,
        })
        with self.assertRaises(ValidationError):
            second.with_user(self.user_acc).sudo().action_acc_approve()
        second.with_user(self.user_acc).action_bypass_concurrent_loan()
        second.with_user(self.user_acc).sudo().action_acc_approve()
        self.assertEqual(second.approval_state, 'pending_gm')

    def test_gm_must_accept_again(self):
        self._live_loan()
        second = self._at(self._make_loan(), 'pending_gm')
        second.sudo().write({
            'x_concurrent_ack_hr': True, 'x_concurrent_ack_acc': True})
        with self.assertRaises(ValidationError):
            second.with_user(self.user_gm).sudo().action_gm_approve()
        second.with_user(self.user_gm).action_bypass_concurrent_loan()
        second.with_user(self.user_gm).sudo().action_gm_approve()
        self.assertEqual(second.approval_state, 'pending_disbursement')

    # ------------------------------------------------------------------
    # Who may accept
    # ------------------------------------------------------------------
    def test_only_the_acting_role_may_accept(self):
        self._live_loan()
        second = self._at(self._make_loan(), 'pending_hr')
        with self.assertRaises(UserError):
            second.with_user(self.user_acc).action_bypass_concurrent_loan()
        self.assertFalse(second.x_concurrent_ack_hr)

    def test_another_departments_gm_may_not_accept(self):
        """Same predicate as the GM approve button — being *a* GM is not it."""
        self._live_loan()
        second = self._at(self._make_loan(), 'pending_gm')
        with self.assertRaises(UserError):
            second.with_user(
                self.user_gm_other).action_bypass_concurrent_loan()
        self.assertFalse(second.x_concurrent_ack_gm)

    def test_button_shows_only_to_the_acting_role(self):
        self._live_loan()
        second = self._at(self._make_loan(), 'pending_hr')
        self.assertTrue(
            second.with_user(self.user_hr).x_concurrent_ack_pending)
        self.assertFalse(
            second.with_user(self.user_acc).x_concurrent_ack_pending)
        second.with_user(self.user_hr).action_bypass_concurrent_loan()
        self.assertFalse(
            second.with_user(self.user_hr).x_concurrent_ack_pending,
            'once accepted the button has nothing left to do')

    def test_nothing_to_accept_without_a_concurrent_loan(self):
        loan = self._at(self._make_loan(), 'pending_hr')
        self.assertFalse(
            loan.with_user(self.user_hr).x_concurrent_ack_pending)
        with self.assertRaises(UserError):
            loan.with_user(self.user_hr).action_bypass_concurrent_loan()
        # ...and the approval goes through untouched.
        loan.sudo().write({'x_hr_no_penalties_confirmed': True})
        loan.with_user(self.user_hr).sudo().action_hr_approve()
        self.assertEqual(loan.approval_state, 'pending_acc')

    # ------------------------------------------------------------------
    # An acceptance does not survive its step being redone
    # ------------------------------------------------------------------
    def test_return_to_hr_clears_the_later_acceptances(self):
        self._live_loan()
        second = self._at(self._make_loan(), 'pending_gm')
        second.sudo().write({
            'x_concurrent_ack_hr': True,
            'x_concurrent_ack_acc': True,
            'x_concurrent_ack_gm': True,
        })
        step_hr = self.env['ksw.loan.return.step'].sudo().search(
            [('code', '=', 'pending_hr')], limit=1)
        wizard = self.env['ksw.loan.return.approver.wizard'].sudo().create({
            'deduction_id': second.id,
            'target_step_id': step_hr.id,
            'reason': 'Reconsider the exposure',
        })
        wizard.action_confirm()
        self.assertEqual(second.approval_state, 'pending_hr')
        self.assertFalse(second.x_concurrent_ack_hr)
        self.assertFalse(second.x_concurrent_ack_acc)
        self.assertFalse(second.x_concurrent_ack_gm)

    def test_reset_to_draft_clears_the_acceptances(self):
        self._live_loan()
        second = self._at(self._make_loan(), 'pending_gm')
        second.sudo().write({
            'x_concurrent_ack_hr': True,
            'x_concurrent_ack_acc': True,
            'x_concurrent_ack_gm': True,
        })
        second.sudo().action_reset_to_draft()
        self.assertFalse(second.x_concurrent_ack_hr)
        self.assertFalse(second.x_concurrent_ack_acc)
        self.assertFalse(second.x_concurrent_ack_gm)
