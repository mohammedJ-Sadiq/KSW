# -*- coding: utf-8 -*-
"""Tests for the GM/admin loan "Return to Approver" wizard."""
from odoo.exceptions import UserError
from .common import DeductionCommon


class TestLoanReturnApproverWizard(DeductionCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def _mk(login, group_xmlid, employee=None):
            user = Users.create({
                'name': login, 'login': login,
                'email': f'{login}@kswded.test',
                'group_ids': [(6, 0, [cls.env.ref(group_xmlid).id])],
            })
            if employee is not None:
                employee.write({'user_id': user.id})
            return user

        # DM authority is derived from employee.parent_id.user_id — attach
        # the DM user to the employee's existing manager record so
        # dm_approved_by gets a real stamp (mirrors test_deduction_security).
        cls.user_dm = _mk(
            'kswded_return_dm', 'KSW_deduction.group_deduction_user',
            employee=cls.manager_emp)

        cls.hr_emp = cls.env['hr.employee'].create({'name': 'KSWDED Return HR'})
        cls.user_hr = _mk('kswded_return_hr', 'KSW_deduction.group_loan_hr',
                           employee=cls.hr_emp)
        cls.acc_emp = cls.env['hr.employee'].create({'name': 'KSWDED Return Acc'})
        cls.user_acc = _mk('kswded_return_acc', 'KSW_deduction.group_loan_acc',
                            employee=cls.acc_emp)
        cls.gm_emp = cls.env['hr.employee'].create({'name': 'KSWDED Return GM'})
        cls.user_gm = _mk('kswded_return_gm', 'KSW_deduction.group_loan_gm',
                           employee=cls.gm_emp)
        # The GM step follows the employee's department, not the GM group:
        # holding group_loan_gm authorises nothing on its own. Both fixture
        # departments get this GM so the loans built here (dept A) and any
        # cross-department probe (dept B) both reach him.
        # See tests/test_department_gm.py for the per-department contract.
        (cls.dept_a | cls.dept_b).sudo().write({'x_gm_id': cls.gm_emp.id})

        cls.user_plain = _mk(
            'kswded_return_plain', 'KSW_deduction.group_deduction_user')
        # The module's "can override anything" tier — see ADMIN_OVERRIDE_GROUP
        # in ksw_loan_return_approver_wizard.py for why this is
        # group_deduction_manager rather than base.group_system.
        cls.admin = _mk(
            'kswded_return_manager', 'KSW_deduction.group_deduction_manager')

    def _loan_at_pending_gm(self, employee=None):
        """Walk a fresh loan through DM -> HR -> Acc using the specific
        role users (not the ambient test-runner identity), so every
        *_approved_by stamp is a real employee, matching production."""
        ded = self._make_deduction(ded_type=self.type_loan, amount=1200.0,
                                    installments=3, employee=employee)
        # Submitted with the ambient (superuser) test identity — action_submit
        # is not what this fixture is about; only the approval steps need a
        # real per-role actor so the *_approved_by stamps are meaningful.
        ded.action_submit()
        ded.with_user(self.user_dm).action_dm_approve()
        ded.with_user(self.user_hr).write({'x_hr_no_penalties_confirmed': True})
        ded.with_user(self.user_hr).action_hr_approve()
        ded.with_user(self.user_acc).write({'x_acc_budget_confirmed': True})
        ded.with_user(self.user_acc).action_acc_approve()
        self.assertEqual(ded.approval_state, 'pending_gm')
        return ded

    # ------------------------------------------------------------------
    # Button gate fields
    # ------------------------------------------------------------------
    def test_gm_gate_true_only_at_pending_gm(self):
        ded = self._loan_at_pending_gm()
        self.assertTrue(ded.with_user(self.user_gm).x_can_gm_return)
        # HR no longer has anything to return to their own step.
        self.assertFalse(ded.with_user(self.user_hr).x_can_gm_return)
        # Not the GM step yet — earlier in the chain. Different employee so
        # the "one personal loan at a time" rule doesn't collide with `ded`.
        ded2 = self._make_deduction(
            ded_type=self.type_loan, employee=self.employee_b)
        ded2.action_submit()
        self.assertFalse(ded2.with_user(self.user_gm).x_can_gm_return)

    def test_admin_gate_true_from_any_reached_step_except_first(self):
        # group_deduction_manager implies group_deduction_officer, which
        # qualifies for the derived DM-approval authority (x_can_dm_approve)
        # — no sudo() needed, this is real group-based access.
        ded = self._make_deduction(
            ded_type=self.type_loan, employee=self.employee_b)
        ded.action_submit()  # pending_dm — nothing earlier to return to
        self.assertFalse(ded.with_user(self.admin).x_can_admin_return)
        ded.with_user(self.admin).action_dm_approve()  # pending_hr
        self.assertTrue(ded.with_user(self.admin).x_can_admin_return)

    def test_plain_user_has_no_gate(self):
        ded = self._loan_at_pending_gm()
        self.assertFalse(ded.with_user(self.user_plain).x_can_gm_return)
        self.assertFalse(ded.with_user(self.user_plain).x_can_admin_return)

    # ------------------------------------------------------------------
    # Opener action authorisation
    # ------------------------------------------------------------------
    def test_open_wizard_denied_for_non_gm(self):
        ded = self._loan_at_pending_gm()
        with self.assertRaises(UserError):
            ded.with_user(self.user_hr).action_open_loan_return_wizard()

    def test_open_wizard_denied_for_gm_outside_own_step(self):
        ded = self._make_deduction(
            ded_type=self.type_loan, employee=self.employee_b)
        ded.action_submit()
        with self.assertRaises(UserError):
            ded.with_user(self.user_gm).action_open_loan_return_wizard()

    def test_open_wizard_ok_for_gm_at_own_step(self):
        ded = self._loan_at_pending_gm()
        action = ded.with_user(self.user_gm).action_open_loan_return_wizard()
        self.assertEqual(action['res_model'], 'ksw.loan.return.approver.wizard')
        self.assertEqual(action['context']['default_deduction_id'], ded.id)

    # ------------------------------------------------------------------
    # action_confirm: state transition, stamp clearing, notification
    # ------------------------------------------------------------------
    def test_gm_returns_to_hr_clears_hr_and_acc_stamps_keeps_dm(self):
        ded = self._loan_at_pending_gm()
        self.assertEqual(ded.hr_approved_by, self.hr_emp)
        self.assertEqual(ded.acc_approved_by, self.acc_emp)
        dm_approver = ded.dm_approved_by
        self.assertTrue(dm_approver)

        target = self.env['ksw.loan.return.step'].search(
            [('code', '=', 'pending_hr')], limit=1)
        wiz = self.env['ksw.loan.return.approver.wizard'].with_user(
            self.user_gm).create({
                'deduction_id': ded.id,
                'target_step_id': target.id,
                'reason': 'Please recheck the penalty note.',
            })
        wiz.action_confirm()

        self.assertEqual(ded.approval_state, 'pending_hr')
        self.assertEqual(ded.dm_approved_by, dm_approver)  # kept
        self.assertFalse(ded.hr_approved_by)
        self.assertFalse(ded.x_hr_no_penalties_confirmed)
        self.assertFalse(ded.acc_approved_by)
        self.assertFalse(ded.x_acc_budget_confirmed)

    def test_notification_reaches_target_approver(self):
        ded = self._loan_at_pending_gm()
        existing_ids = ded.message_ids.ids
        target = self.env['ksw.loan.return.step'].search(
            [('code', '=', 'pending_hr')], limit=1)
        wiz = self.env['ksw.loan.return.approver.wizard'].with_user(
            self.user_gm).create({
                'deduction_id': ded.id,
                'target_step_id': target.id,
                'reason': 'Recheck please.',
            })
        wiz.action_confirm()
        new_msgs = ded.message_ids.filtered(lambda m: m.id not in existing_ids)
        self.assertTrue(
            new_msgs.filtered(
                lambda m: self.user_hr.partner_id in m.partner_ids))

    def test_invalid_target_rejected_over_rpc(self):
        """The radio only offers reachable steps, but the server must
        re-validate: a GM cannot fabricate a target_step_id that skips
        ahead of what _allowed_targets actually permits."""
        ded = self._loan_at_pending_gm()
        gm_step = self.env['ksw.loan.return.step'].search(
            [('code', '=', 'pending_gm')], limit=1)
        wiz = self.env['ksw.loan.return.approver.wizard'].with_user(
            self.user_gm).create({
                'deduction_id': ded.id,
                'target_step_id': gm_step.id,
                'reason': 'x',
            })
        with self.assertRaises(UserError):
            wiz.action_confirm()

    def test_admin_can_return_gm_step_from_pending_disbursement(self):
        ded = self._loan_at_pending_gm()
        ded.with_user(self.user_gm).action_gm_approve()
        self.assertEqual(ded.approval_state, 'pending_disbursement')
        self.assertEqual(ded.gm_approved_by, self.gm_emp)

        gm_step = self.env['ksw.loan.return.step'].search(
            [('code', '=', 'pending_gm')], limit=1)
        wiz = self.env['ksw.loan.return.approver.wizard'].with_user(
            self.admin).create({
                'deduction_id': ded.id,
                'target_step_id': gm_step.id,
                'reason': 'GM needs to re-review before disbursement.',
            })
        wiz.action_confirm()
        self.assertEqual(ded.approval_state, 'pending_gm')
        self.assertFalse(ded.gm_approved_by)
