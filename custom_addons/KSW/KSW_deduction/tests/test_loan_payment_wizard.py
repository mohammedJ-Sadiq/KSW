# -*- coding: utf-8 -*-
"""Tests for ksw.loan.payment.wizard — full and partial loan payments."""
from odoo.exceptions import AccessError, UserError, ValidationError

from .common import DeductionCommon


class TestLoanPaymentWizard(DeductionCommon):
    """ksw.loan.payment.wizard: computed summary, full payment,
    partial redistribution, validation guards, and auth checks."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def _mk(login, group_xmlids):
            return Users.create({
                'name': login,
                'login': login,
                'email': f'{login}@kswwiz.test',
                'group_ids': [(6, 0, [cls.env.ref(g).id for g in group_xmlids])],
            })

        cls.user_acc = _mk(
            'kswwiz_acc',
            ['KSW_deduction.group_installment_edit',
             'KSW_deduction.group_loan_acc'])
        cls.user_plain = _mk(
            'kswwiz_plain', ['KSW_deduction.group_deduction_user'])

    # ------------------------------------------------------------------ #
    # Helpers                                                               #
    # ------------------------------------------------------------------ #

    def _active_loan(self, amount=6000.0, installments=4):
        ded = self._make_deduction(self.type_loan, amount=amount,
                                   installments=installments)
        self._walk_loan_to_pending_gm(ded)
        ded.action_gm_approve()
        self.assertEqual(ded.state, 'active')
        return ded

    def _wizard(self, ded, payment_amount, note='', user=None):
        Wizard = self.env['ksw.loan.payment.wizard']
        if user is not None:
            Wizard = Wizard.with_user(user)
        return Wizard.create({
            'deduction_id': ded.id,
            'payment_amount': payment_amount,
            'payment_date': self.this_month,
            'note': note,
        })

    # ------------------------------------------------------------------ #
    # Computed summary fields (_compute_summary)                            #
    # ------------------------------------------------------------------ #

    def test_summary_total_outstanding_equals_deduction_amount(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._wizard(ded, 0)
        self.assertAlmostEqual(wiz.total_outstanding, 6000.0)
        self.assertEqual(wiz.pending_count, 4)

    def test_summary_is_full_payment_flag_true(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._wizard(ded, 6000.0)
        self.assertTrue(wiz.is_full_payment)

    def test_summary_is_full_payment_flag_false_for_partial(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._wizard(ded, 4000.0)
        self.assertFalse(wiz.is_full_payment)
        self.assertAlmostEqual(wiz.remaining_after, 2000.0)
        self.assertAlmostEqual(wiz.new_installment_amount, 500.0)

    # ------------------------------------------------------------------ #
    # Full payment                                                          #
    # ------------------------------------------------------------------ #

    def test_full_payment_marks_all_pending_lines_paid(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._wizard(ded, 6000.0, user=self.user_acc)
        wiz.action_confirm()
        self.assertTrue(all(l.state == 'paid' for l in ded.line_ids))

    def test_full_payment_sets_is_manual_on_all_lines(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._wizard(ded, 6000.0, user=self.user_acc)
        wiz.action_confirm()
        self.assertTrue(all(l.is_manual for l in ded.line_ids))

    def test_full_payment_creates_no_extra_line(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        count_before = len(ded.line_ids)
        wiz = self._wizard(ded, 6000.0, user=self.user_acc)
        wiz.action_confirm()
        self.assertEqual(len(ded.line_ids), count_before,
                         "Full payment must stamp existing lines; no new row.")

    def test_full_payment_auto_completes_deduction(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._wizard(ded, 6000.0, user=self.user_acc)
        wiz.action_confirm()
        self.assertEqual(ded.state, 'completed')

    def test_full_payment_posts_chatter_with_note(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        before = len(ded.message_ids)
        wiz = self._wizard(ded, 6000.0, note='Wire TXN-001', user=self.user_acc)
        wiz.action_confirm()
        self.assertGreater(len(ded.message_ids), before)
        bodies = ' '.join(str(m.body or '') for m in ded.message_ids)
        self.assertIn('Wire TXN-001', bodies)

    # ------------------------------------------------------------------ #
    # Partial payment with redistribution                                   #
    # ------------------------------------------------------------------ #

    def test_partial_payment_creates_one_manual_paid_line(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._wizard(ded, 4000.0, user=self.user_acc)
        wiz.action_confirm()
        manual = ded.line_ids.filtered(lambda l: l.is_manual)
        self.assertEqual(len(manual), 1,
                         "Exactly one manual paid line must be created.")
        self.assertAlmostEqual(manual.amount, 4000.0)
        self.assertEqual(manual.state, 'paid')

    def test_partial_payment_pending_lines_redistributed_equally(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._wizard(ded, 4000.0, user=self.user_acc)
        wiz.action_confirm()
        pending = ded.line_ids.filtered(lambda l: l.state == 'pending')
        self.assertEqual(len(pending), 4)
        for line in pending:
            self.assertAlmostEqual(line.amount, 500.0)

    def test_partial_payment_total_constraint_preserved(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._wizard(ded, 4000.0, user=self.user_acc)
        wiz.action_confirm()
        relevant = ded.line_ids.filtered(lambda l: l.state in ('pending', 'paid'))
        self.assertAlmostEqual(sum(relevant.mapped('amount')), 6000.0,
                               msg="paid + pending amounts must equal loan total.")

    def test_partial_payment_rounding_residue_total_preserved(self):
        # 7 / 3 = 2.33...: last installment absorbs the rounding residue
        ded = self._active_loan(amount=7.0, installments=3)
        wiz = self._wizard(ded, 1.0, user=self.user_acc)
        wiz.action_confirm()
        relevant = ded.line_ids.filtered(lambda l: l.state in ('pending', 'paid'))
        self.assertAlmostEqual(sum(relevant.mapped('amount')), 7.0, places=2)

    def test_partial_payment_does_not_autocomplete_deduction(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._wizard(ded, 4000.0, user=self.user_acc)
        wiz.action_confirm()
        self.assertEqual(ded.state, 'active')

    def test_partial_payment_posts_chatter_with_remaining_amount(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._wizard(ded, 4000.0, note='Cash recv', user=self.user_acc)
        wiz.action_confirm()
        bodies = ' '.join(str(m.body or '') for m in ded.message_ids)
        self.assertIn('Partial Payment', bodies)
        self.assertIn('Cash recv', bodies)
        self.assertIn('2000.00', bodies)  # remaining balance

    def test_partial_payment_then_second_full_payment_completes(self):
        """Two wizard calls should work: first partial, then full remaining."""
        ded = self._active_loan(amount=6000.0, installments=4)
        # First: pay 4000
        wiz1 = self._wizard(ded, 4000.0, user=self.user_acc)
        wiz1.action_confirm()
        # Now 4 pending lines of 500 each remain
        self.assertEqual(ded.state, 'active')
        # Second: pay remaining 2000 (full)
        wiz2 = self._wizard(ded, 2000.0, user=self.user_acc)
        wiz2.action_confirm()
        self.assertEqual(ded.state, 'completed')
        self.assertTrue(all(l.state == 'paid' for l in ded.line_ids))

    # ------------------------------------------------------------------ #
    # Validation guards                                                     #
    # ------------------------------------------------------------------ #

    def test_wizard_rejects_zero_amount(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._wizard(ded, 0.0, user=self.user_acc)
        with self.assertRaises(ValidationError):
            wiz.action_confirm()

    def test_wizard_rejects_negative_amount(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._wizard(ded, -100.0, user=self.user_acc)
        with self.assertRaises(ValidationError):
            wiz.action_confirm()

    def test_wizard_rejects_overpayment(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._wizard(ded, 9999.0, user=self.user_acc)
        with self.assertRaises(ValidationError):
            wiz.action_confirm()

    # ------------------------------------------------------------------ #
    # Auth checks                                                           #
    # ------------------------------------------------------------------ #

    def test_wizard_blocked_for_plain_user_at_acl_level(self):
        """Plain users lack group_installment_edit and cannot create the wizard."""
        ded = self._active_loan(amount=6000.0, installments=4)
        with self.assertRaises(AccessError):
            self._wizard(ded, 1000.0, user=self.user_plain)
