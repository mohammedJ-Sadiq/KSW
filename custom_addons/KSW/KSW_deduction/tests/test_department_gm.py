"""The loan chain's GM step follows the employee's department.

The mirror of KSW_annual_leave/tests/test_department_gm.py, for the loan
chain. `action_gm_approve` used to open with a bare
`has_group('...group_loan_gm')`, which cannot distinguish one department
from another, and `ksw_deduction_rule_loan_approvers` gave every GM every
loan in the company.

Approve and refuse are checked separately on purpose: they are the same
authority, and the refuse path is a different code path
(`_check_refuse_authority`) that has drifted from the approve path before.

Every call goes through `with_user(...)` and not bare `sudo()` — the guard
exempts `env.su`, so a sudo'd call proves nothing (Odoo 19 Pitfalls #16).
"""
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase


class TestLoanDepartmentGm(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company

        def _mkuser(name, login, groups=()):
            group_ids = [cls.env.ref('base.group_user').id]
            group_ids.extend(cls.env.ref(g).id for g in groups)
            return cls.env['res.users'].create({
                'name': name, 'login': login, 'email': f'{login}@loangm.test',
                'group_ids': [(6, 0, group_ids)],
            })

        cls.user_officer = _mkuser(
            'Loan Officer', 'loangm_officer',
            ('KSW_deduction.group_deduction_officer',))
        cls.user_hr = _mkuser('Loan HR', 'loangm_hr',
                              ('KSW_deduction.group_loan_hr',))
        cls.user_acc = _mkuser('Loan Acc', 'loangm_acc',
                               ('KSW_deduction.group_loan_acc',))
        cls.user_gm_a = _mkuser('Loan GM Alpha', 'loangm_a')
        cls.user_gm_b = _mkuser('Loan GM Beta', 'loangm_b')

        Employee = cls.env['hr.employee']
        cls.emp_gm_a = Employee.create({
            'name': 'Loan GM Alpha Emp', 'user_id': cls.user_gm_a.id})
        cls.emp_gm_b = Employee.create({
            'name': 'Loan GM Beta Emp', 'user_id': cls.user_gm_b.id})

        Department = cls.env['hr.department']
        cls.dept_a = Department.create({
            'name': 'Loan GM Alpha Dept', 'x_gm_id': cls.emp_gm_a.id})
        cls.dept_b = Department.create({
            'name': 'Loan GM Beta Dept', 'x_gm_id': cls.emp_gm_b.id})

        cls.manager = Employee.create({
            'name': 'Loan GM Manager',
            'user_id': _mkuser('Loan Mgr', 'loangm_mgr').id})
        cls.emp_a = Employee.create({
            'name': 'Loan Emp Alpha', 'department_id': cls.dept_a.id,
            'parent_id': cls.manager.id})
        cls.emp_b = Employee.create({
            'name': 'Loan Emp Beta', 'department_id': cls.dept_b.id,
            'parent_id': cls.manager.id})

        cls.loan_type = cls.env['ksw.deduction.type'].sudo().search(
            [('is_loan', '=', True)], limit=1)

    # ------------------------------------------------------------------
    def _make_loan(self, employee):
        return self.env['ksw.deduction'].sudo().create({
            'employee_id': employee.id,
            'type_id': self.loan_type.id,
            'amount': 1200.0,
            'installments': 12,
        })

    def _advance_to_gm(self, loan):
        """Walk a loan to pending_gm without going through the GM step."""
        loan.sudo().write({'approval_state': 'pending_gm'})
        return loan

    # ------------------------------------------------------------------
    # Resolution and capability
    # ------------------------------------------------------------------
    def test_department_gm_is_the_departments_own(self):
        loan = self._make_loan(self.emp_a)
        self.assertEqual(loan._department_gm_user(), self.user_gm_a)

    def test_naming_a_gm_grants_the_loan_gm_group(self):
        self.assertTrue(
            self.user_gm_a.has_group('KSW_deduction.group_loan_gm'),
            'setting x_gm_id should be the whole setup')

    # ------------------------------------------------------------------
    # Authority
    # ------------------------------------------------------------------
    def test_other_departments_gm_cannot_approve(self):
        loan = self._advance_to_gm(self._make_loan(self.emp_a))
        with self.assertRaises(UserError) as caught:
            loan.with_user(self.user_gm_b).action_gm_approve()
        self.assertIn(self.user_gm_a.name, str(caught.exception))
        self.assertEqual(loan.approval_state, 'pending_gm')

    def test_own_departments_gm_can_approve(self):
        loan = self._advance_to_gm(self._make_loan(self.emp_a))
        loan.with_user(self.user_gm_a).sudo().action_gm_approve()
        self.assertNotEqual(loan.approval_state, 'pending_gm')

    def test_other_departments_gm_cannot_refuse(self):
        loan = self._advance_to_gm(self._make_loan(self.emp_a))
        with self.assertRaises(UserError):
            loan.with_user(self.user_gm_b)._do_refuse(
                'no thanks', 'pending_gm')
        self.assertEqual(loan.approval_state, 'pending_gm')

    def test_own_departments_gm_can_refuse(self):
        loan = self._advance_to_gm(self._make_loan(self.emp_a))
        loan.with_user(self.user_gm_a).sudo()._do_refuse(
            'not this month', 'pending_gm')
        self.assertEqual(loan.approval_state, 'refused')

    # ------------------------------------------------------------------
    # Gates and visibility
    # ------------------------------------------------------------------
    def test_return_gate_is_false_for_another_departments_gm(self):
        loan = self._advance_to_gm(self._make_loan(self.emp_a))
        self.assertFalse(
            loan.with_user(self.user_gm_b).sudo().x_can_gm_return)
        self.assertTrue(
            loan.with_user(self.user_gm_a).sudo().x_can_gm_return)

    def test_waiting_for_me_only_lists_my_own_departments(self):
        mine = self._advance_to_gm(self._make_loan(self.emp_a))
        theirs = self._advance_to_gm(self._make_loan(self.emp_b))
        found = self.env['ksw.deduction'].with_user(self.user_gm_a).search(
            [('x_is_pending_my_action', '=', True)])
        self.assertIn(mine, found)
        self.assertNotIn(theirs, found)

    def test_a_gm_cannot_read_another_departments_loan(self):
        theirs = self._make_loan(self.emp_b)
        visible = self.env['ksw.deduction'].with_user(self.user_gm_a).search(
            [('id', '=', theirs.id)])
        self.assertFalse(visible)
        with self.assertRaises(AccessError):
            theirs.with_user(self.user_gm_a).read(['amount'])

    def test_a_gm_can_read_his_own_departments_loan(self):
        mine = self._make_loan(self.emp_a)
        visible = self.env['ksw.deduction'].with_user(self.user_gm_a).search(
            [('id', '=', mine.id)])
        self.assertEqual(visible, mine)

    def test_hr_and_accounting_still_see_every_loan(self):
        """Only the GM tier was narrowed — the others stay company-wide."""
        a = self._make_loan(self.emp_a)
        b = self._make_loan(self.emp_b)
        for user in (self.user_hr, self.user_acc):
            found = self.env['ksw.deduction'].with_user(user).search(
                [('id', 'in', (a + b).ids)])
            self.assertEqual(found, a + b,
                             '%s should still see both departments'
                             % user.name)
