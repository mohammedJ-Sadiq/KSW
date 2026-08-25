"""Each department's handover is approved by its own General Manager.

`ksw.pay.run.action_approve` used to be one atomic act over every submitted
department: approve them all, build the register, lock the month. That only
made sense while one GM answered for the whole company. It now approves the
caller's own departments; the month finalises by itself once nothing is
left waiting, and a deliberate `action_close_month` handles the case where
a department's GM never signs.

The last two tests are the ones that matter most: a department whose GM
never approved must NOT be paid when the month is closed, and the register
built at finalisation must reflect that.
"""
from odoo.exceptions import AccessError, UserError

from .test_submission import SubmissionCommon


class TestCommissionDepartmentGm(SubmissionCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # SubmissionCommon puts one GM over both departments. Split them:
        # A keeps its own GM, B gets another, and a third person is the
        # company's GM — the only one who may close or reopen the month.
        cls.gm_a = cls.gm
        cls.user_gm_b = cls._plain_user('deptgm_b')
        cls.emp_gm_b = cls.env['hr.employee'].sudo().create({
            'name': 'Dept GM B Emp', 'user_id': cls.user_gm_b.id})
        cls.dept_b.sudo().write({'x_gm_id': cls.emp_gm_b.id})

        cls.user_company_gm = cls._plain_user('deptgm_company')
        cls.emp_company_gm = cls.env['hr.employee'].sudo().create({
            'name': 'Company GM Emp', 'user_id': cls.user_company_gm.id})
        cls.env.company.sudo().x_default_gm_id = cls.emp_company_gm.id

    @classmethod
    def _plain_user(cls, login):
        """No commission group at all — being named on the department is
        meant to be the whole qualification."""
        return cls.env['res.users'].sudo().create({
            'name': login, 'login': login,
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
        })

    # ------------------------------------------------------------------
    def _submit(self, supervisor, department, employee):
        batch = self._batch(supervisor, department)
        self._entry(batch, employee, user=supervisor)
        batch.submission_id.sudo().action_submit()
        return batch.submission_id

    # ------------------------------------------------------------------
    # Resolution and capability
    # ------------------------------------------------------------------
    def test_the_submission_names_its_own_gm(self):
        sub = self._submit(self.sup_a, self.dept_a, self.emp_a)
        self.assertEqual(sub.gm_id, self.gm_a)

    def test_naming_a_gm_grants_the_commission_gm_group(self):
        self.assertTrue(
            self.user_gm_b.has_group('KSW_commissions.group_commission_gm'))

    def test_the_gm_group_no_longer_grants_officer_reach(self):
        self.assertFalse(
            self.user_gm_b.has_group(
                'KSW_commissions.group_commission_officer'),
            'the Officer implication is what made every GM see everything')

    # ------------------------------------------------------------------
    # Authority
    # ------------------------------------------------------------------
    def test_a_gm_cannot_approve_another_departments_handover(self):
        sub = self._submit(self.sup_a, self.dept_a, self.emp_a)
        with self.assertRaises(UserError) as caught:
            sub.with_user(self.user_gm_b).action_approve()
        self.assertIn(self.gm_a.name, str(caught.exception))
        self.assertEqual(sub.state, 'submitted')

    def test_a_gm_cannot_return_another_departments_handover(self):
        sub = self._submit(self.sup_a, self.dept_a, self.emp_a)
        sub.sudo().write({'return_reason': 'fix it'})
        with self.assertRaises(UserError):
            sub.with_user(self.user_gm_b).action_return()
        self.assertEqual(sub.state, 'submitted')

    def test_a_gm_approves_his_own_departments_handover(self):
        sub = self._submit(self.sup_a, self.dept_a, self.emp_a)
        sub.with_user(self.gm_a).sudo().action_approve()
        self.assertEqual(sub.state, 'approved')

    def test_a_gm_cannot_see_another_departments_handover(self):
        sub = self._submit(self.sup_a, self.dept_a, self.emp_a)
        visible = self.env['ksw.pay.submission'].with_user(
            self.user_gm_b).search([('id', '=', sub.id)])
        self.assertFalse(visible)
        with self.assertRaises(AccessError):
            sub.with_user(self.user_gm_b).read(['total_amount'])

    # ------------------------------------------------------------------
    # The month
    # ------------------------------------------------------------------
    def test_the_run_offers_a_gm_only_his_own_departments(self):
        sub_a = self._submit(self.sup_a, self.dept_a, self.emp_a)
        sub_b = self._submit(self.sup_b, self.dept_b, self.emp_b)
        run = sub_a.run_id
        self.assertEqual(
            run.with_user(self.gm_a).sudo().x_gm_submission_ids, sub_a)
        self.assertEqual(
            run.with_user(self.user_gm_b).sudo().x_gm_submission_ids, sub_b)

    def test_approving_one_department_leaves_the_month_open(self):
        sub_a = self._submit(self.sup_a, self.dept_a, self.emp_a)
        self._submit(self.sup_b, self.dept_b, self.emp_b)
        run = sub_a.run_id
        run.with_user(self.gm_a).sudo().action_approve()
        self.assertEqual(sub_a.state, 'approved')
        self.assertNotEqual(
            run.state, 'approved',
            'the month must wait for the other department\'s GM')

    def test_the_month_finalises_once_no_department_is_left_waiting(self):
        sub_a = self._submit(self.sup_a, self.dept_a, self.emp_a)
        sub_b = self._submit(self.sup_b, self.dept_b, self.emp_b)
        run = sub_a.run_id
        run.with_user(self.gm_a).sudo().action_approve()
        run.with_user(self.user_gm_b).sudo().action_approve()
        self.assertEqual(sub_b.state, 'approved')
        self.assertEqual(run.state, 'approved')

    def test_a_gm_with_nothing_waiting_is_told_who_is_holding_it_up(self):
        self._submit(self.sup_a, self.dept_a, self.emp_a)
        sub_b = self._submit(self.sup_b, self.dept_b, self.emp_b)
        run = sub_b.run_id
        run.with_user(self.gm_a).sudo().action_approve()
        with self.assertRaises(UserError) as caught:
            run.with_user(self.gm_a).sudo().action_approve()
        self.assertIn(sub_b.display_name, str(caught.exception))

    # ------------------------------------------------------------------
    # Closing
    # ------------------------------------------------------------------
    def test_a_department_gm_cannot_close_the_month(self):
        sub_a = self._submit(self.sup_a, self.dept_a, self.emp_a)
        self._submit(self.sup_b, self.dept_b, self.emp_b)
        run = sub_a.run_id
        run.with_user(self.gm_a).sudo().action_approve()
        # No sudo() on the call under test: _is_month_closer() exempts
        # env.su, so a sudo'd call would pass whatever the guard said.
        with self.assertRaises(UserError):
            run.with_user(self.gm_a).action_close_month()

    def test_the_company_gm_can_close_the_month(self):
        sub_a = self._submit(self.sup_a, self.dept_a, self.emp_a)
        self._submit(self.sup_b, self.dept_b, self.emp_b)
        run = sub_a.run_id
        run.with_user(self.gm_a).sudo().action_approve()
        run.with_user(self.user_company_gm).sudo().action_close_month()
        self.assertEqual(run.state, 'approved')

    def test_closing_does_not_pay_a_department_its_gm_never_approved(self):
        """The whole point of splitting the approval.

        Department B handed over but its GM never signed. Closing the month
        must leave B's people out of the register entirely — paying them
        would mean the close silently approved on B's GM's behalf.
        """
        sub_a = self._submit(self.sup_a, self.dept_a, self.emp_a)
        sub_b = self._submit(self.sup_b, self.dept_b, self.emp_b)
        run = sub_a.run_id
        run.with_user(self.gm_a).sudo().action_approve()
        run.with_user(self.user_company_gm).sudo().action_close_month()

        self.assertEqual(sub_b.state, 'submitted', 'B was never approved')
        paid = run.sudo().line_ids.mapped('employee_id')
        self.assertIn(self.emp_a, paid)
        self.assertNotIn(self.emp_b, paid,
                         'B\'s GM never approved, so B must not be paid')
