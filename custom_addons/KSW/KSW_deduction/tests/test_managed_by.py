# -*- coding: utf-8 -*-
"""Tests for managed_by, x_can_edit_installments, action_mark_line_paid,
and the department-based record rule added in the managed_by feature."""
from odoo.exceptions import AccessError, UserError

from .common import DeductionCommon


class TestManagedBy(DeductionCommon):
    """managed_by field on ksw.deduction.type and its downstream effects."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def _mk(login, group_xmlids):
            return Users.create({
                'name': login,
                'login': login,
                'email': f'{login}@kswded.test',
                'group_ids': [(6, 0, [cls.env.ref(g).id for g in group_xmlids])],
            })

        cls.user_hr_officer = _mk(
            'kswmb_officer', ['KSW_deduction.group_deduction_officer'])
        cls.user_hr_approver = _mk(
            'kswmb_hr', ['KSW_deduction.group_loan_hr'])
        cls.user_acc = _mk(
            'kswmb_acc',
            ['KSW_deduction.group_installment_edit',
             'KSW_deduction.group_loan_acc'])
        cls.user_plain = _mk(
            'kswmb_plain', ['KSW_deduction.group_deduction_user'])

    # ------------------------------------------------------------------ #
    # managed_by defaults on built-in types                                #
    # ------------------------------------------------------------------ #

    def test_loan_type_managed_by_accounting(self):
        self.assertEqual(self.type_loan.managed_by, 'accounting')

    def test_advance_type_managed_by_hr(self):
        self.assertEqual(self.type_advance.managed_by, 'hr')

    def test_gov_penalty_managed_by_hr(self):
        self.assertEqual(self.type_gov_pen.managed_by, 'hr')

    def test_internal_penalty_managed_by_hr(self):
        self.assertEqual(self.type_internal_pen.managed_by, 'hr')

    def test_new_type_defaults_to_hr(self):
        t = self.env['ksw.deduction.type'].create({
            'name': 'CustomType', 'code': 'CUST77', 'category': 'company_paid',
        })
        self.assertEqual(t.managed_by, 'hr')

    def test_managed_by_propagates_to_deduction_via_related(self):
        ded_loan = self._make_deduction(self.type_loan)
        ded_pen = self._make_deduction(self.type_gov_pen)
        self.assertEqual(ded_loan.managed_by, 'accounting')
        self.assertEqual(ded_pen.managed_by, 'hr')

    # ------------------------------------------------------------------ #
    # x_can_edit_installments                                              #
    # ------------------------------------------------------------------ #

    def _active_penalty(self, amount=600.0, installments=3):
        ded = self._make_deduction(self.type_gov_pen, amount=amount,
                                   installments=installments)
        ded.action_submit()
        self.assertEqual(ded.state, 'active')
        return ded

    def _active_loan(self, amount=6000.0, installments=4):
        ded = self._make_deduction(self.type_loan, amount=amount,
                                   installments=installments)
        self._walk_loan_to_pending_gm(ded)
        ded.action_gm_approve()
        self.assertEqual(ded.state, 'active')
        return ded

    def test_x_can_edit_installments_false_for_plain_user(self):
        ded = self._active_penalty()
        self.assertFalse(ded.with_user(self.user_plain).x_can_edit_installments)

    def test_x_can_edit_installments_true_for_hr_officer_on_hr_type(self):
        ded = self._active_penalty()
        self.assertTrue(ded.with_user(self.user_hr_officer).x_can_edit_installments)

    def test_x_can_edit_installments_true_for_hr_approver_on_hr_type(self):
        ded = self._active_penalty()
        self.assertTrue(ded.with_user(self.user_hr_approver).x_can_edit_installments)

    def test_x_can_edit_installments_false_for_hr_officer_on_accounting_type(self):
        ded = self._active_loan()
        self.assertFalse(ded.with_user(self.user_hr_officer).x_can_edit_installments)

    def test_x_can_edit_installments_false_for_hr_approver_on_accounting_type(self):
        ded = self._active_loan()
        self.assertFalse(ded.with_user(self.user_hr_approver).x_can_edit_installments)

    def test_x_can_edit_installments_true_for_acc_on_hr_type(self):
        ded = self._active_penalty()
        self.assertTrue(ded.with_user(self.user_acc).x_can_edit_installments)

    def test_x_can_edit_installments_true_for_acc_on_accounting_type(self):
        ded = self._active_loan()
        self.assertTrue(ded.with_user(self.user_acc).x_can_edit_installments)

    def test_x_can_edit_installments_false_when_not_active(self):
        ded = self._make_deduction(self.type_gov_pen)   # draft
        self.assertFalse(ded.with_user(self.user_hr_officer).x_can_edit_installments)

    # ------------------------------------------------------------------ #
    # action_mark_line_paid — success                                       #
    # ------------------------------------------------------------------ #

    def test_mark_paid_by_hr_officer(self):
        ded = self._active_penalty(amount=300.0, installments=1)
        line = ded.line_ids
        line.with_user(self.user_hr_officer).action_mark_line_paid()
        self.assertEqual(line.state, 'paid')
        self.assertTrue(line.is_manual)
        self.assertEqual(line.manual_by, self.user_hr_officer)

    def test_mark_paid_by_hr_approver(self):
        ded = self._active_penalty(amount=500.0, installments=1)
        line = ded.line_ids
        line.with_user(self.user_hr_approver).action_mark_line_paid()
        self.assertEqual(line.state, 'paid')
        self.assertTrue(line.is_manual)

    def test_mark_paid_by_acc_on_hr_type(self):
        ded = self._active_penalty(amount=200.0, installments=1)
        ded.line_ids.with_user(self.user_acc).action_mark_line_paid()
        self.assertEqual(ded.line_ids.state, 'paid')

    def test_mark_paid_by_acc_on_accounting_type(self):
        ded = self._active_loan(amount=1500.0, installments=1)
        ded.line_ids.with_user(self.user_acc).action_mark_line_paid()
        self.assertEqual(ded.line_ids.state, 'paid')

    def test_mark_paid_sets_manual_date_to_today(self):
        from odoo import fields
        ded = self._active_penalty(amount=100.0, installments=1)
        line = ded.line_ids
        line.with_user(self.user_hr_officer).action_mark_line_paid()
        self.assertEqual(line.manual_date, fields.Date.context_today(line))

    def test_mark_paid_auto_completes_when_last_line(self):
        ded = self._active_penalty(amount=100.0, installments=1)
        ded.line_ids.with_user(self.user_hr_officer).action_mark_line_paid()
        self.assertEqual(ded.state, 'completed')

    def test_mark_paid_does_not_autocomplete_with_pending_siblings(self):
        ded = self._active_penalty(amount=300.0, installments=3)
        ded.line_ids[0].with_user(self.user_hr_officer).action_mark_line_paid()
        self.assertEqual(ded.state, 'active')
        self.assertEqual(ded.line_ids[0].state, 'paid')
        self.assertTrue(all(l.state == 'pending' for l in ded.line_ids[1:]))

    def test_mark_paid_posts_chatter(self):
        ded = self._active_penalty(amount=200.0, installments=1)
        before = len(ded.message_ids)
        ded.line_ids.with_user(self.user_hr_officer).action_mark_line_paid()
        self.assertGreater(len(ded.message_ids), before)
        bodies = ' '.join(str(m.body or '') for m in ded.message_ids)
        self.assertIn('Marked Paid', bodies)

    def test_mark_paid_note_appears_in_chatter(self):
        ded = self._active_penalty(amount=200.0, installments=1)
        ded.line_ids.write({'manual_note': 'Cash — receipt 99'})
        ded.line_ids.with_user(self.user_hr_officer).action_mark_line_paid()
        bodies = ' '.join(str(m.body or '') for m in ded.message_ids)
        self.assertIn('Cash — receipt 99', bodies)

    # ------------------------------------------------------------------ #
    # action_mark_line_paid — authorization failures                        #
    # ------------------------------------------------------------------ #

    def test_mark_paid_blocked_for_plain_user(self):
        ded = self._active_penalty()
        with self.assertRaises(UserError):
            ded.line_ids[0].with_user(self.user_plain).action_mark_line_paid()

    def test_mark_paid_blocked_for_hr_officer_on_accounting_type(self):
        ded = self._active_loan()
        with self.assertRaises(UserError):
            ded.line_ids[0].with_user(self.user_hr_officer).action_mark_line_paid()

    def test_mark_paid_blocked_for_hr_approver_on_accounting_type(self):
        ded = self._active_loan()
        with self.assertRaises(UserError):
            ded.line_ids[0].with_user(self.user_hr_approver).action_mark_line_paid()

    # ------------------------------------------------------------------ #
    # action_mark_line_paid — state guards                                  #
    # ------------------------------------------------------------------ #

    def test_mark_paid_blocked_when_already_paid(self):
        ded = self._active_penalty(amount=200.0, installments=1)
        line = ded.line_ids
        line.with_user(self.user_hr_officer).action_mark_line_paid()
        with self.assertRaises(UserError):
            line.with_user(self.user_hr_officer).action_mark_line_paid()

    def test_mark_paid_blocked_when_skipped(self):
        ded = self._active_penalty(amount=300.0, installments=3)
        # Skip one via sudo (so the total stays balanced via context flag)
        ded.line_ids[0].with_context(
            _skip_installment_total_check=True).write({'state': 'skipped'})
        with self.assertRaises(UserError):
            ded.line_ids[0].with_user(
                self.user_hr_officer).action_mark_line_paid()

    # ------------------------------------------------------------------ #
    # write() privilege guard on installment schedule edits                 #
    # ------------------------------------------------------------------ #

    def test_hr_officer_can_reschedule_hr_type_installment(self):
        """HR officers may change year/month on pending installments of
        HR-managed deductions (same privilege gate as mark-paid)."""
        ded = self._active_penalty(amount=300.0, installments=3)
        line = ded.line_ids[0]
        old_month = line.month
        new_month = old_month % 12 + 1
        line.with_user(self.user_hr_officer).write({'month': new_month})
        self.assertEqual(line.month, new_month)

    def test_hr_officer_cannot_reschedule_accounting_type_installment(self):
        """HR officers must NOT be allowed to edit installment schedules
        on accounting-managed (loan) deductions."""
        ded = self._active_loan()
        line = ded.line_ids[0]
        with self.assertRaises(UserError):
            line.with_user(self.user_hr_officer).write({'month': 1})

    # ------------------------------------------------------------------ #
    # Manual create() route — group gates                                   #
    # ------------------------------------------------------------------ #

    def _setup_for_manual_create(self, amount=100.0):
        """Return an active penalty with one line skipped (so adding a
        manual paid line of `amount` keeps the total consistent)."""
        ded = self._active_penalty(amount=amount * 2, installments=2)
        # Skip one line using the deferred-check context so the total guard
        # doesn't fire mid-setup (it will recheck when the manual line lands).
        ded.line_ids[0].with_context(
            _skip_installment_total_check=True).write({'state': 'skipped'})
        return ded

    def test_hr_officer_can_create_manual_line_on_hr_type(self):
        ded = self._setup_for_manual_create(amount=100.0)
        line = self.env['ksw.deduction.line'].with_user(
            self.user_hr_officer).create({
                'deduction_id': ded.id,
                'year': self.this_month.year,
                'month': self.this_month.month,
                'amount': 100.0,
            })
        self.assertTrue(line.is_manual)
        self.assertEqual(line.state, 'paid')
        self.assertEqual(line.manual_by, self.user_hr_officer)

    def test_hr_approver_can_create_manual_line_on_hr_type(self):
        ded = self._setup_for_manual_create(amount=100.0)
        line = self.env['ksw.deduction.line'].with_user(
            self.user_hr_approver).create({
                'deduction_id': ded.id,
                'year': self.this_month.year,
                'month': self.this_month.month,
                'amount': 100.0,
            })
        self.assertTrue(line.is_manual)

    def test_plain_user_cannot_create_manual_line(self):
        ded = self._active_penalty()
        # Our Python privilege guard raises UserError before ORM ACL check
        with self.assertRaises(UserError):
            self.env['ksw.deduction.line'].with_user(self.user_plain).create({
                'deduction_id': ded.id,
                'year': self.this_month.year,
                'month': self.this_month.month,
                'amount': 100.0,
            })

    def test_hr_cannot_create_manual_line_on_accounting_type(self):
        ded = self._active_loan()
        with self.assertRaises(UserError):
            self.env['ksw.deduction.line'].with_user(
                self.user_hr_officer).create({
                    'deduction_id': ded.id,
                    'year': self.this_month.year,
                    'month': self.this_month.month,
                    'amount': 100.0,
                })

    # ------------------------------------------------------------------ #
    # Record rule — HR approver sees HR-managed non-loan deductions         #
    # ------------------------------------------------------------------ #

    def test_hr_approver_sees_hr_managed_deduction(self):
        ded = self._active_penalty()
        visible = self.env['ksw.deduction'].with_user(
            self.user_hr_approver).search([('id', '=', ded.id)])
        self.assertIn(ded, visible,
                      "group_loan_hr must see HR-managed deductions via "
                      "ksw_deduction_rule_hr_managed record rule.")

    def test_hr_approver_does_not_see_accounting_managed_non_loan(self):
        acc_type = self.env['ksw.deduction.type'].create({
            'name': 'AccOnlyType', 'code': 'ACCONLY',
            'category': 'borrowed', 'managed_by': 'accounting',
        })
        ded = self._make_deduction(ded_type=acc_type)
        visible = self.env['ksw.deduction'].with_user(
            self.user_hr_approver).search([('id', '=', ded.id)])
        self.assertNotIn(
            ded, visible,
            "group_loan_hr must NOT see accounting-managed non-loan deductions.")

    # ------------------------------------------------------------------ #
    # action_record_payment button wires up correctly                       #
    # ------------------------------------------------------------------ #

    def test_action_record_payment_returns_wizard_action(self):
        ded = self._active_loan()
        action = ded.action_record_payment()
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'ksw.loan.payment.wizard')
        self.assertEqual(action['target'], 'new')
        self.assertEqual(action['context']['default_deduction_id'], ded.id)
