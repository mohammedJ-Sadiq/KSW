"""Whichever screen the last approval comes from, the month has to finalise.

A real dead end on KSWCO: the General Manager approved every department —
but on each department's own submission form, not down the run's Approve
button. `ksw.pay.submission.action_approve` only ever re-synced the run's
status, so:

* no department was left waiting, so the run's "Approve My Departments"
  button was gone;
* no department was unapproved either, so "Lock the Month Now" — which
  insisted on one — was gone as well;
* and the accountant's export is gated on a state the month could no longer
  reach.

Every batch approved, nothing left to press, and no way to hand the month to
accounting. The two repairs are one predicate called from both approval
routes, and a finalise button that only needs *something* approved.
"""
from odoo.exceptions import UserError

from .test_submission import SubmissionCommon


class TestRunFinalisation(SubmissionCommon):
    """One GM per department, plus the company GM who owns the month —
    the same shape as tests/test_department_gm.py, built here rather than
    inherited so this file's cases run once."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.gm_a = cls.gm
        cls.user_gm_b = cls._plain_user('runfin_gm_b')
        cls.dept_b.sudo().write({
            'x_gm_id': cls.env['hr.employee'].sudo().create({
                'name': 'Runfin GM B', 'user_id': cls.user_gm_b.id}).id})

        cls.user_company_gm = cls._plain_user('runfin_company_gm')
        cls.env.company.sudo().x_default_gm_id = cls.env['hr.employee'].sudo(
        ).create({'name': 'Runfin Company GM',
                  'user_id': cls.user_company_gm.id}).id

    @classmethod
    def _plain_user(cls, login):
        return cls.env['res.users'].sudo().create({
            'name': login, 'login': login,
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
        })

    def _submit(self, supervisor, department, employee):
        batch = self._filled_batch(supervisor, department, employee)
        batch.submission_id.sudo().action_submit()
        return batch.submission_id

    def _stuck_run(self):
        """Every department approved, the month never finalised.

        Reproduces the state the live run was found in, by approving each
        department the way the GM actually did it.
        """
        sub_a = self._submit(self.sup_a, self.dept_a, self.emp_a)
        sub_b = self._submit(self.sup_b, self.dept_b, self.emp_b)
        sub_a.with_user(self.gm_a).sudo().action_approve()
        return sub_a, sub_b, sub_a.run_id

    # ------------------------------------------------------------------
    # The repair itself
    # ------------------------------------------------------------------
    def test_approving_the_last_department_on_its_own_form_finalises(self):
        sub_a, sub_b, run = self._stuck_run()
        self.assertNotEqual(run.state, 'approved',
                            'B is still waiting on its own GM')

        sub_b.with_user(self.user_gm_b).sudo().action_approve()

        self.assertEqual(run.state, 'approved')
        self.assertEqual(
            run.sudo().line_ids.mapped('employee_id'),
            self.emp_a | self.emp_b)

    def test_the_register_is_settled_not_a_preview(self):
        sub_a, sub_b, run = self._stuck_run()
        sub_b.with_user(self.user_gm_b).sudo().action_approve()
        self.assertFalse(run.sudo().line_ids.filtered('x_preview_generated'),
                         'finalisation turns the preview into the settlement')

    def test_approving_a_department_early_leaves_the_month_alone(self):
        """One department signed, another still waiting — no lock."""
        sub_a, sub_b, run = self._stuck_run()
        self.assertNotIn(run.state, ('approved', 'paid'))
        self.assertEqual(sub_b.state, 'submitted')

    def test_finalising_twice_is_refused_not_repeated(self):
        sub_a, sub_b, run = self._stuck_run()
        sub_b.with_user(self.user_gm_b).sudo().action_approve()
        with self.assertRaises(UserError):
            run.with_user(self.user_company_gm).sudo().action_close_month()

    # ------------------------------------------------------------------
    # The button that was missing
    # ------------------------------------------------------------------
    def test_a_month_with_nothing_left_waiting_still_offers_the_button(self):
        """The belt to the braces above, for runs already stuck.

        An existing run reached 'all approved, never finalised' before the
        auto-finalisation existed. The button has to be there for it, which
        is why it no longer insists on an unapproved department.
        """
        sub_a, sub_b, run = self._stuck_run()
        # Approve B behind the workflow's back, exactly as the old code left
        # the live month: state moved, nothing finalised.
        sub_b.sudo().write({'state': 'approved'})
        sub_b.batch_ids.sudo().write({'state': 'approved'})
        run.invalidate_recordset()

        self.assertTrue(
            run.with_user(self.user_company_gm).sudo().x_can_close_month)
        run.with_user(self.user_company_gm).sudo().action_close_month()
        self.assertEqual(run.state, 'approved')

    def _system_admin(self, login):
        """A real administrator of this system: Settings, plus a Commission
        Role. base.group_system carries no ACL on ksw.pay.run — it is what
        grants the authority to finalise, not what lets him see the month
        (gotcha #38), which is why the test does not use it on its own."""
        return self.env['res.users'].sudo().create({
            'name': login, 'login': login,
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('base.group_system').id,
                self.env.ref('KSW_commissions.group_commission_officer').id,
            ])],
        })

    def test_the_system_administrator_can_finalise(self):
        """The way out when the company's GM is absent or was never set."""
        sub_a, sub_b, run = self._stuck_run()
        admin = self._system_admin('run_admin')
        # No sudo() on the guard under test — _is_month_closer exempts env.su.
        self.assertTrue(run.with_user(admin).x_can_close_month)
        run.with_user(admin).sudo().action_close_month()
        self.assertEqual(run.state, 'approved')

    def test_the_commission_administrator_alone_still_cannot(self):
        """Settings is the key, not the Commission Role: an Administrator
        who is not a system administrator is unchanged by this
        (tests/test_period_lock.py::test_10_only_the_gm_reopens)."""
        sub_a, sub_b, run = self._stuck_run()
        officer = self.env['res.users'].sudo().create({
            'name': 'run_officer', 'login': 'run_officer',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('KSW_commissions.group_commission_officer').id,
            ])],
        })
        self.assertFalse(run.with_user(officer).x_can_close_month)
        with self.assertRaises(UserError):
            run.with_user(officer).action_close_month()

    def test_a_department_gm_still_cannot_finalise_the_month(self):
        sub_a, sub_b, run = self._stuck_run()
        self.assertFalse(run.with_user(self.gm_a).x_can_close_month)
        with self.assertRaises(UserError):
            run.with_user(self.gm_a).action_close_month()

    def test_finalising_does_not_pay_a_department_never_approved(self):
        """Unchanged by the widened button: B handed over, its GM never
        signed, so B is left out even though the month is finalised."""
        sub_a, sub_b, run = self._stuck_run()
        run.with_user(self.user_company_gm).sudo().action_close_month()

        self.assertEqual(sub_b.state, 'submitted')
        paid = run.sudo().line_ids.mapped('employee_id')
        self.assertIn(self.emp_a, paid)
        self.assertNotIn(self.emp_b, paid)

    # ------------------------------------------------------------------
    # Handover to accounting
    # ------------------------------------------------------------------
    def test_the_accountant_is_told_the_month_is_ready(self):
        accountant = self.env['res.users'].sudo().create({
            'name': 'run_acc', 'login': 'run_acc',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref(
                    'KSW_commissions.group_commission_accountant').id,
            ])],
        })
        sub_a, sub_b, run = self._stuck_run()
        existing = run.sudo().message_ids.ids
        sub_b.with_user(self.user_gm_b).sudo().action_approve()

        new = run.sudo().message_ids.filtered(lambda m: m.id not in existing)
        self.assertTrue(
            new.filtered(lambda m: accountant.partner_id in m.partner_ids),
            'finalising the month is the handover to the bank export')

    def test_the_accountant_can_export_once_it_is_finalised(self):
        sub_a, sub_b, run = self._stuck_run()
        sub_b.with_user(self.user_gm_b).sudo().action_approve()
        accountant = self.env['res.users'].sudo().create({
            'name': 'run_acc2', 'login': 'run_acc2',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref(
                    'KSW_commissions.group_commission_accountant').id,
            ])],
        })
        self.assertTrue(run.with_user(accountant).sudo().x_can_export)
