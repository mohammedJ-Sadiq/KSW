"""Each General Manager has his own monthly pay run (Oct 2026).

KSWCO September 2026: Equipment Rental (GM Taha) landed on the same run as
Mohammed Amin's departments, so each GM opening the month saw the other's
company-level figures. User's rule: a separate run per GM, which that GM
finalises himself, and no GM sees another's.
"""
from odoo.exceptions import AccessError, UserError

from .test_submission import SubmissionCommon


class TestGmRuns(SubmissionCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # SubmissionCommon puts one GM over both departments. Give B its own.
        cls.gm_a = cls.gm
        cls.user_gm_b = cls.env['res.users'].sudo().create({
            'name': 'runs_gm_b', 'login': 'runs_gm_b',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        cls.emp_gm_b = cls.env['hr.employee'].sudo().create({
            'name': 'Runs GM B Emp', 'user_id': cls.user_gm_b.id})
        cls.dept_b.sudo().write({'x_gm_id': cls.emp_gm_b.id})

    def _submit(self, supervisor, department, employee):
        batch = self._filled_batch(supervisor, department, employee)
        batch.submission_id.sudo().action_submit()
        return batch.submission_id

    def _approve(self, sub, gm):
        sub.with_user(gm).action_approve()

    # ------------------------------------------------------------------
    def test_two_gms_get_two_runs_for_the_same_month(self):
        sub_a = self._submit(self.sup_a, self.dept_a, self.emp_a)
        sub_b = self._submit(self.sup_b, self.dept_b, self.emp_b)
        self.assertNotEqual(sub_a.run_id, sub_b.run_id)
        self.assertEqual(sub_a.run_id.period, sub_b.run_id.period)
        self.assertEqual(sub_a.run_id.gm_id, self.gm_a)
        self.assertEqual(sub_b.run_id.gm_id, self.user_gm_b)

    def test_a_gm_cannot_open_another_gms_run(self):
        sub_a = self._submit(self.sup_a, self.dept_a, self.emp_a)
        sub_b = self._submit(self.sup_b, self.dept_b, self.emp_b)
        Run = self.env['ksw.pay.run']
        self.assertEqual(
            Run.with_user(self.gm_a).search(
                [('period', '=', self.period)]), sub_a.run_id)
        self.assertEqual(
            Run.with_user(self.user_gm_b).search(
                [('period', '=', self.period)]), sub_b.run_id)
        with self.assertRaises(AccessError):
            sub_b.run_id.with_user(self.gm_a).read(['total_payable'])

    def test_each_gm_finalises_his_own_run(self):
        sub_a = self._submit(self.sup_a, self.dept_a, self.emp_a)
        self._approve(sub_a, self.gm_a)
        # No sudo(): _is_month_closer() exempts env.su.
        sub_a.run_id.with_user(self.gm_a).action_close_month()
        self.assertEqual(sub_a.run_id.state, 'approved')

    def test_a_gm_cannot_finalise_another_gms_run(self):
        sub_b = self._submit(self.sup_b, self.dept_b, self.emp_b)
        self._approve(sub_b, self.user_gm_b)
        run_b = sub_b.run_id
        # The guard reads the run's GM through sudo(), so it answers with a
        # refusal rather than an access error on a run he cannot see.
        with self.assertRaises(UserError):
            run_b.with_user(self.gm_a).action_close_month()
        self.assertNotIn(run_b.state, ('approved', 'paid'))

    def test_finalising_one_gms_run_does_not_lock_the_other(self):
        sub_a = self._submit(self.sup_a, self.dept_a, self.emp_a)
        self._approve(sub_a, self.gm_a)
        sub_a.run_id.with_user(self.gm_a).action_close_month()
        # B's supervisor can still record and submit in the same month.
        batch_b = self._filled_batch(self.sup_b, self.dept_b, self.emp_b)
        batch_b.submission_id.with_user(self.sup_b).action_submit()
        self.assertEqual(batch_b.submission_id.state, 'submitted')

    def test_changing_a_departments_gm_moves_its_open_submission(self):
        sub_b = self._submit(self.sup_b, self.dept_b, self.emp_b)
        old_run = sub_b.run_id
        self.dept_b.sudo().write({'x_gm_id': self.gm_employee.id})
        self.assertEqual(sub_b.run_id.gm_id, self.gm_a)
        self.assertNotEqual(sub_b.run_id, old_run)
        self.assertEqual(sub_b.batch_ids.run_id, sub_b.run_id)

    def test_an_approved_submission_stays_where_it_was_signed(self):
        sub_b = self._submit(self.sup_b, self.dept_b, self.emp_b)
        self._approve(sub_b, self.user_gm_b)
        run = sub_b.run_id
        self.dept_b.sudo().write({'x_gm_id': self.gm_employee.id})
        self.assertEqual(sub_b.run_id, run)
