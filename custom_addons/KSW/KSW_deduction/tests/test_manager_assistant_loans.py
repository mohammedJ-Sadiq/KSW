"""Tests: the Manager Assistant delegation on loan requests.

The deduction side is LOANS ONLY, deliberately. Non-loan deductions belong to
the accounting data-entry team and go draft -> active with no DM step at all,
so delegating them would produce records the manager could never approve. That
mirrors exactly what the manager themselves can raise today.

The sharpest risk on this model is `_compute_x_can_dm_approve`, which grants
DM approval unconditionally to `group_deduction_officer`. Handing an assistant
that group -- or letting `group_manager_assistant` imply it -- would give away
the exact step the delegation exists to protect. Pinned below.
"""
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase


class TestManagerAssistantLoans(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, groups=()):
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@asstloan.test',
                'group_ids': [(6, 0, [cls.env.ref('base.group_user').id]
                               + [cls.env.ref(g).id for g in groups])],
            })

        cls.ASSISTANT_GROUP = 'KSW_base_security.group_manager_assistant'

        cls.user_mgr = _mkuser('Loan Deleg Manager', 'asstloan_mgr')
        cls.emp_mgr = cls.env['hr.employee'].create({
            'name': 'Loan Deleg Manager Emp', 'user_id': cls.user_mgr.id})

        cls.user_other_mgr = _mkuser('Loan Deleg Other Mgr', 'asstloan_omgr')
        cls.emp_other_mgr = cls.env['hr.employee'].create({
            'name': 'Loan Deleg Other Mgr Emp',
            'user_id': cls.user_other_mgr.id})

        cls.emp_r1 = cls.env['hr.employee'].create({
            'name': 'Loan Deleg Report',
            'user_id': _mkuser('Loan Deleg R1', 'asstloan_r1').id,
            'parent_id': cls.emp_mgr.id,
        })
        cls.emp_other = cls.env['hr.employee'].create({
            'name': 'Loan Deleg Outsider',
            'user_id': _mkuser('Loan Deleg Out', 'asstloan_out').id,
            'parent_id': cls.emp_other_mgr.id,
        })

        cls.user_asst = _mkuser('Loan Deleg Assistant', 'asstloan_asst',
                                (cls.ASSISTANT_GROUP,))
        cls.user_mgr.sudo().write({'x_assistant_ids': [(4, cls.user_asst.id)]})

        cls.loan_type = cls.env['ksw.deduction.type'].sudo().search(
            [('is_loan', '=', True)], limit=1)
        cls.non_loan_type = cls.env['ksw.deduction.type'].sudo().search(
            [('managed_by', '=', 'acc_data_entry')], limit=1)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _loan_as(self, user, employee, ded_type=None):
        return self.env['ksw.deduction'].with_user(user).create({
            'employee_id': employee.id,
            'type_id': (ded_type or self.loan_type).id,
            'amount': 3000.0,
            'installments': 3,
            'reason': 'Assistant delegation test',
        })

    # ------------------------------------------------------------------
    # Scope
    # ------------------------------------------------------------------

    def test_create_loan_for_delegated_report(self):
        loan = self._loan_as(self.user_asst, self.emp_r1)
        self.assertTrue(loan.id)
        self.assertEqual(loan.sudo().employee_id, self.emp_r1)

    def test_create_loan_outside_delegation_denied(self):
        with self.assertRaises(UserError):
            self._loan_as(self.user_asst, self.emp_other)

    def test_non_loan_type_denied(self):
        """The Python ownership guard fires before the ORM (Pitfalls #12)."""
        if not self.non_loan_type:
            self.skipTest('no acc_data_entry deduction type installed')
        with self.assertRaises(UserError):
            self._loan_as(self.user_asst, self.emp_r1, self.non_loan_type)

    def test_read_scope_excludes_other_teams(self):
        mine = self._loan_as(self.user_asst, self.emp_r1)
        theirs = self.env['ksw.deduction'].sudo().create({
            'employee_id': self.emp_other.id,
            'type_id': self.loan_type.id,
            'amount': 1000.0, 'installments': 2,
        })
        visible = self.env['ksw.deduction'].with_user(self.user_asst).search([])
        self.assertIn(mine, visible)
        self.assertNotIn(theirs, visible)

    def test_employee_reassignment_blocked(self):
        loan = self._loan_as(self.user_asst, self.emp_r1)
        with self.assertRaises(UserError):
            loan.with_user(self.user_asst).write(
                {'employee_id': self.emp_other.id})

    # ------------------------------------------------------------------
    # Submit, then hands off
    # ------------------------------------------------------------------

    def test_submit_and_edit_window(self):
        loan = self._loan_as(self.user_asst, self.emp_r1)
        self.assertTrue(
            loan.with_user(self.user_asst).x_can_submit,
            "the Submit button must render for the delegated assistant")
        self.assertTrue(
            loan.with_user(self.user_asst).x_can_select_loan_type,
            "without this the type_id domain hides every Loan type")

        loan.with_user(self.user_asst).action_submit()
        self.assertEqual(loan.sudo().approval_state, 'pending_dm')

        # Still editable while the manager has not signed.
        loan.with_user(self.user_asst).write({'reason': 'Amended reason'})
        self.assertEqual(loan.sudo().reason, 'Amended reason')

        # The manager signs; the window closes.
        loan.with_user(self.user_mgr).action_dm_approve()
        self.assertEqual(loan.sudo().approval_state, 'pending_hr')
        with self.assertRaises(AccessError):
            loan.with_user(self.user_asst).write({'reason': 'Too late'})

    # ------------------------------------------------------------------
    # No authority, ever
    # ------------------------------------------------------------------

    def test_cannot_dm_approve(self):
        loan = self._loan_as(self.user_asst, self.emp_r1)
        loan.with_user(self.user_asst).action_submit()
        self.assertFalse(
            loan.with_user(self.user_asst).x_can_dm_approve,
            "an assistant is never a DM-approve authority source")
        with self.assertRaises(UserError):
            loan.with_user(self.user_asst).action_dm_approve()

    def test_cannot_delete(self):
        """UserError, not AccessError: ksw.deduction.unlink()'s own Loan
        Modification guard raises before the ORM reaches the record rule
        (Pitfalls #12). The assistant holds no Loan Modification level."""
        loan = self._loan_as(self.user_asst, self.emp_r1)
        with self.assertRaises(UserError):
            loan.with_user(self.user_asst).unlink()

    def test_assistant_group_does_not_imply_deduction_officer(self):
        """The one implication that would silently hand over DM approval."""
        group = self.env.ref(self.ASSISTANT_GROUP)
        officer = self.env.ref('KSW_deduction.group_deduction_officer')
        self.assertNotIn(officer, group.all_implied_ids)
