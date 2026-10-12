"""A department manager who is also its GM approves his own handover.

KSWCO, Oct 2026: Taha manages Equipment Rental and is named its GM. Commission
Role is a single-select, so he held Supervisor only; the handover named him
(``gm_id``) but the Approve buttons are GM-only and nobody else may approve,
so his department could never be approved. The combined role fixes it, and
it must survive HR saving his user form with "Supervisor" picked.
"""
from .test_submission import SubmissionCommon

SUPERVISOR = 'KSW_commissions.group_commission_supervisor'
GM = 'KSW_commissions.group_commission_gm'
SUPERVISOR_GM = 'KSW_commissions.group_commission_supervisor_gm'


class TestSupervisorGmRole(SubmissionCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.dept_c = cls.env['hr.department'].sudo().create(
            {'name': 'Sub Dept C'})
        cls.sup_c = cls._supervisor('sub_sup_gm_c', cls.dept_c)
        cls.emp_c = cls._employee('Sub Emp C', cls.dept_c, 6000.0)
        # Name the department's own manager as its GM.
        cls.dept_c.sudo().write({'x_gm_id': cls.dept_c.manager_id.id})

    def test_naming_a_supervisor_gm_gives_the_combined_role(self):
        self.assertTrue(self.sup_c.has_group(SUPERVISOR_GM))
        self.assertTrue(self.sup_c.has_group(SUPERVISOR))
        self.assertTrue(self.sup_c.has_group(GM))

    def test_a_supervisor_not_named_gm_is_left_alone(self):
        self.assertFalse(self.sup_a.has_group(SUPERVISOR_GM))
        self.assertFalse(self.sup_a.has_group(GM))

    def test_saving_the_user_form_as_supervisor_keeps_the_gm_role(self):
        # What the Access Rights tab writes: one group per privilege.
        self.sup_c.sudo().write({'group_ids': [(6, 0, [
            self.env.ref('base.group_user').id,
            self.env.ref(SUPERVISOR).id,
        ])]})
        self.assertTrue(self.sup_c.has_group(GM))

    def test_his_handover_goes_to_him_and_he_approves_it(self):
        batch = self._filled_batch(self.sup_c, self.dept_c, self.emp_c)
        sub = batch.submission_id
        sub.with_user(self.sup_c).action_submit()
        self.assertEqual(sub.gm_id, self.sup_c)
        self.assertNotEqual(sub.gm_id, self.gm,
                            'must not fall back to the other GM')
        self.assertTrue(sub.with_user(self.sup_c).x_can_approve)
        # No sudo(): the guard exempts env.su (CLAUDE.md gotcha #16).
        sub.with_user(self.sup_c).action_approve()
        self.assertEqual(sub.state, 'approved')

    def test_the_other_gm_does_not_even_see_it(self):
        batch = self._filled_batch(self.sup_c, self.dept_c, self.emp_c)
        sub = batch.submission_id
        sub.with_user(self.sup_c).action_submit()
        self.assertFalse(self.env['ksw.pay.submission'].with_user(
            self.gm).search([('id', '=', sub.id)]))
