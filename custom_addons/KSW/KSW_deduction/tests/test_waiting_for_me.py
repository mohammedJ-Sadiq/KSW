# -*- coding: utf-8 -*-
"""Tests for the "Waiting For Me" flag/filter and the per-step approver
notifications on the loan approval chain."""
from .common import DeductionCommon


class TestWaitingForMe(DeductionCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def _mk(login, group_xmlid, employee=None):
            user = Users.create({
                'name': login,
                'login': login,
                'email': f'{login}@kswwfm.test',
                'group_ids': [(6, 0, [cls.env.ref(group_xmlid).id])],
            })
            if employee is not None:
                employee.write({'user_id': user.id})
            return user

        cls.user_employee = _mk('kswwfm_emp',
                                'KSW_deduction.group_deduction_user',
                                employee=cls.employee)
        cls.user_dm = _mk('kswwfm_dm',
                          'KSW_deduction.group_deduction_user',
                          employee=cls.manager_emp)
        cls.user_officer = _mk('kswwfm_officer',
                               'KSW_deduction.group_deduction_officer')
        cls.user_hr = _mk('kswwfm_hr', 'KSW_deduction.group_loan_hr')
        cls.user_acc = _mk('kswwfm_acc', 'KSW_deduction.group_loan_acc')
        # The GM step follows the employee's department, not the GM group, so
        # this GM needs an employee record and a department to be GM of —
        # holding group_loan_gm authorises nothing on its own. See
        # tests/test_department_gm.py for the per-department contract.
        cls.gm_emp = cls.env['hr.employee'].create({'name': 'KSWWFM GM'})
        cls.user_gm = _mk('kswwfm_gm', 'KSW_deduction.group_loan_gm',
                          employee=cls.gm_emp)
        (cls.dept_a | cls.dept_b).sudo().write({'x_gm_id': cls.gm_emp.id})
        cls.user_disb = _mk('kswwfm_disb',
                            'KSW_deduction.group_loan_disbursement')
        cls.mt_comment = cls.env.ref('mail.mt_comment')

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_loan(self, employee=None):
        return self._make_deduction(
            ded_type=self.type_loan, employee=employee, amount=1200.0,
            installments=4, start_month=self.next_month)

    def _flag_for(self, loan, user):
        return loan.with_user(user).sudo().x_is_pending_my_action

    def _new_messages(self, loan, before_ids):
        return loan.message_ids.filtered(lambda m: m.id not in before_ids)

    def assertNotified(self, loan, before_ids, partner):
        """Exactly-one new mt_comment message carrying `partner`."""
        new = self._new_messages(loan, before_ids)
        notif = new.filtered(
            lambda m: m.subtype_id == self.mt_comment
            and partner in m.partner_ids)
        self.assertEqual(len(notif), 1,
                         'expected one notification to %s' % partner.name)

    # ------------------------------------------------------------------
    # Flag / compute
    # ------------------------------------------------------------------

    def test_flag_pending_dm(self):
        loan = self._make_loan()
        loan.action_submit()
        self.assertEqual(loan.approval_state, 'pending_dm')
        self.assertTrue(self._flag_for(loan, self.user_dm))
        for user in (self.user_hr, self.user_acc, self.user_gm,
                     self.user_disb, self.user_employee):
            self.assertFalse(self._flag_for(loan, user))

    def test_flag_dm_fallback_officer(self):
        # employee_b has no manager: officers act as DM fallback
        loan_b = self._make_loan(employee=self.employee_b)
        loan_b.action_submit()
        self.assertTrue(self._flag_for(loan_b, self.user_officer))
        self.assertFalse(self._flag_for(loan_b, self.user_dm))
        # when a manager user exists, officers are NOT flagged
        loan_a = self._make_loan()
        loan_a.action_submit()
        self.assertFalse(self._flag_for(loan_a, self.user_officer))

    def test_flag_progression(self):
        loan = self._make_loan()
        loan.action_submit()

        def check(state, owner, previous):
            self.assertEqual(loan.approval_state, state)
            self.assertTrue(self._flag_for(loan, owner))
            self.assertFalse(self._flag_for(loan, previous))

        loan.action_dm_approve()
        check('pending_hr', self.user_hr, self.user_dm)
        loan.x_hr_no_penalties_confirmed = True
        loan.action_hr_approve()
        check('pending_acc', self.user_acc, self.user_hr)
        loan.x_acc_budget_confirmed = True
        loan.action_acc_approve()
        check('pending_gm', self.user_gm, self.user_acc)
        loan.action_gm_approve()
        check('pending_disbursement', self.user_disb, self.user_gm)
        loan.with_user(self.user_disb).sudo().action_disbursement_confirm()
        self.assertEqual(loan.approval_state, 'approved')
        for user in (self.user_dm, self.user_hr, self.user_acc,
                     self.user_gm, self.user_disb):
            self.assertFalse(self._flag_for(loan, user))

    def test_flag_false_after_refuse(self):
        loan = self._make_loan()
        loan.action_submit()
        loan.with_user(self.user_dm).sudo()._do_refuse('No budget',
                                                       'pending_dm')
        self.assertEqual(loan.approval_state, 'refused')
        for user in (self.user_dm, self.user_hr, self.user_officer):
            self.assertFalse(self._flag_for(loan, user))

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def test_search_positive_and_negative(self):
        loan = self._make_loan()
        loan.action_submit()
        Ded = self.env['ksw.deduction']

        found = Ded.with_user(self.user_dm).sudo().search(
            [('x_is_pending_my_action', '=', True)])
        self.assertIn(loan, found)
        self.assertNotIn(loan, Ded.with_user(self.user_hr).sudo().search(
            [('x_is_pending_my_action', '=', True)]))
        self.assertNotIn(loan, Ded.with_user(self.user_dm).sudo().search(
            [('x_is_pending_my_action', '!=', True)]))

        loan.action_dm_approve()
        self.assertIn(loan, Ded.with_user(self.user_hr).sudo().search(
            [('x_is_pending_my_action', '=', True)]))
        self.assertNotIn(loan, Ded.with_user(self.user_dm).sudo().search(
            [('x_is_pending_my_action', '=', True)]))

    def test_search_dm_fallback_officer(self):
        loan_b = self._make_loan(employee=self.employee_b)
        loan_b.action_submit()
        Ded = self.env['ksw.deduction']
        self.assertIn(loan_b, Ded.with_user(self.user_officer).sudo().search(
            [('x_is_pending_my_action', '=', True)]))
        self.assertNotIn(loan_b, Ded.with_user(self.user_dm).sudo().search(
            [('x_is_pending_my_action', '=', True)]))

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def test_notify_each_step(self):
        loan = self._make_loan()

        before = loan.message_ids.ids
        loan.action_submit()
        self.assertNotified(loan, before, self.user_dm.partner_id)

        before = loan.message_ids.ids
        loan.with_user(self.user_dm).sudo().action_dm_approve()
        self.assertNotified(loan, before, self.user_hr.partner_id)

        loan.x_hr_no_penalties_confirmed = True
        before = loan.message_ids.ids
        loan.with_user(self.user_hr).sudo().action_hr_approve()
        self.assertNotified(loan, before, self.user_acc.partner_id)

        loan.x_acc_budget_confirmed = True
        before = loan.message_ids.ids
        loan.with_user(self.user_acc).sudo().action_acc_approve()
        self.assertNotified(loan, before, self.user_gm.partner_id)

        before = loan.message_ids.ids
        loan.with_user(self.user_gm).sudo().action_gm_approve()
        self.assertNotified(loan, before, self.user_disb.partner_id)

    def test_notify_dm_fallback_officer(self):
        loan_b = self._make_loan(employee=self.employee_b)
        before = loan_b.message_ids.ids
        loan_b.action_submit()
        self.assertNotified(loan_b, before, self.user_officer.partner_id)

    def test_notify_employee_on_final_and_refuse(self):
        loan = self._walk_loan_to_pending_gm(self._make_loan())
        loan.action_gm_approve()
        before = loan.message_ids.ids
        loan.with_user(self.user_disb).sudo().action_disbursement_confirm()
        self.assertNotified(loan, before, self.user_employee.partner_id)

        refused = self._make_loan(employee=self.employee_b)
        refused.action_submit()
        # employee_b has no linked user: refusal must not crash
        refused.with_user(self.user_officer).sudo()._do_refuse(
            'Rejected', 'pending_dm')
        self.assertEqual(refused.approval_state, 'refused')

    def test_notify_employee_on_refuse_with_user(self):
        loan = self._make_loan()
        loan.action_submit()
        before = loan.message_ids.ids
        loan.with_user(self.user_dm).sudo()._do_refuse('Over budget',
                                                       'pending_dm')
        self.assertNotified(loan, before, self.user_employee.partner_id)

    def test_non_loan_never_pending(self):
        ded = self._make_deduction(ded_type=self.type_advance)
        before = ded.message_ids.ids
        ded.action_submit()
        self.assertEqual(ded.state, 'active')
        for user in (self.user_dm, self.user_hr, self.user_officer):
            self.assertFalse(self._flag_for(ded, user))
        new = self._new_messages(ded, before)
        self.assertFalse(new.filtered(lambda m: m.partner_ids))
