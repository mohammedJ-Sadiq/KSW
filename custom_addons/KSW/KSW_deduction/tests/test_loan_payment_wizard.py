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
        # Step 5 (disbursement) is what actually activates the loan and
        # generates the installment schedule — see
        # `ksw.deduction.action_disbursement_confirm`.
        ded.action_disbursement_confirm()
        self.assertEqual(ded.state, 'active')
        return ded

    def _wizard(self, ded, payment_amount, note='', user=None, mode=None):
        Wizard = self.env['ksw.loan.payment.wizard']
        if user is not None:
            Wizard = Wizard.with_user(user)
        vals = {
            'deduction_id': ded.id,
            'payment_amount': payment_amount,
            'payment_date': self.this_month,
            'note': note,
        }
        if mode is not None:
            vals['application_mode'] = mode
        return Wizard.create(vals)

    def _redistribute_wizard(self, ded, payment_amount, note='', user=None):
        return self._wizard(ded, payment_amount, note=note, user=user,
                            mode='redistribute')

    def _due_order(self, ded, state=None):
        """Installments in chronological order, optionally filtered by state."""
        lines = ded.line_ids
        if state:
            lines = lines.filtered(lambda l: l.state == state)
        return lines.sorted(lambda l: (l.period_date, l.sequence, l.id))

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
        wiz = self._redistribute_wizard(ded, 4000.0, user=self.user_acc)
        wiz.action_confirm()
        manual = ded.line_ids.filtered(lambda l: l.is_manual)
        self.assertEqual(len(manual), 1,
                         "Exactly one manual paid line must be created.")
        self.assertAlmostEqual(manual.amount, 4000.0)
        self.assertEqual(manual.state, 'paid')

    def test_partial_payment_pending_lines_redistributed_equally(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._redistribute_wizard(ded, 4000.0, user=self.user_acc)
        wiz.action_confirm()
        pending = ded.line_ids.filtered(lambda l: l.state == 'pending')
        self.assertEqual(len(pending), 4)
        for line in pending:
            self.assertAlmostEqual(line.amount, 500.0)

    def test_partial_payment_total_constraint_preserved(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._redistribute_wizard(ded, 4000.0, user=self.user_acc)
        wiz.action_confirm()
        relevant = ded.line_ids.filtered(lambda l: l.state in ('pending', 'paid'))
        self.assertAlmostEqual(sum(relevant.mapped('amount')), 6000.0,
                               msg="paid + pending amounts must equal loan total.")

    def test_partial_payment_rounding_residue_total_preserved(self):
        # 7 / 3 = 2.33...: last installment absorbs the rounding residue
        ded = self._active_loan(amount=7.0, installments=3)
        wiz = self._redistribute_wizard(ded, 1.0, user=self.user_acc)
        wiz.action_confirm()
        relevant = ded.line_ids.filtered(lambda l: l.state in ('pending', 'paid'))
        self.assertAlmostEqual(sum(relevant.mapped('amount')), 7.0, places=2)

    def test_partial_payment_does_not_autocomplete_deduction(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._redistribute_wizard(ded, 4000.0, user=self.user_acc)
        wiz.action_confirm()
        self.assertEqual(ded.state, 'active')

    def test_partial_payment_posts_chatter_with_remaining_amount(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._redistribute_wizard(ded, 4000.0, note='Cash recv',
                                        user=self.user_acc)
        wiz.action_confirm()
        bodies = ' '.join(str(m.body or '') for m in ded.message_ids)
        self.assertIn('Partial Payment', bodies)
        self.assertIn('Cash recv', bodies)
        self.assertIn('2000.00', bodies)  # remaining balance

    def test_partial_payment_then_second_full_payment_completes(self):
        """Two wizard calls should work: first partial, then full remaining."""
        ded = self._active_loan(amount=6000.0, installments=4)
        # First: pay 4000
        wiz1 = self._redistribute_wizard(ded, 4000.0, user=self.user_acc)
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

    # ------------------------------------------------------------------ #
    # Sequential mode — settle the earliest installments first (default)   #
    # ------------------------------------------------------------------ #

    def test_sequential_is_the_default_mode(self):
        ded = self._active_loan(amount=4000.0, installments=4)
        wiz = self._wizard(ded, 700.0)
        self.assertEqual(wiz.application_mode, 'sequential')

    def test_sequential_summary_preview_for_mid_installment_payment(self):
        ded = self._active_loan(amount=4000.0, installments=4)
        wiz = self._wizard(ded, 1650.0)
        self.assertEqual(wiz.seq_settled_count, 1)
        self.assertAlmostEqual(wiz.seq_partial_paid, 650.0)
        self.assertAlmostEqual(wiz.seq_partial_remainder, 350.0)
        self.assertTrue(wiz.seq_partial_label)

    def test_sequential_summary_preview_on_exact_boundary(self):
        ded = self._active_loan(amount=4000.0, installments=4)
        wiz = self._wizard(ded, 2000.0)
        self.assertEqual(wiz.seq_settled_count, 2)
        self.assertFalse(wiz.seq_partial_label)
        self.assertAlmostEqual(wiz.seq_partial_remainder, 0.0)

    def test_sequential_partial_within_first_installment_splits_it(self):
        """700 against 4 x 1000: #1 paid 700 + new pending 300 same month."""
        ded = self._active_loan(amount=4000.0, installments=4)
        first = self._due_order(ded)[0]
        first_period = (first.year, first.month)
        wiz = self._wizard(ded, 700.0, user=self.user_acc)
        wiz.action_confirm()

        self.assertEqual(len(ded.line_ids), 5, "One remainder line is added.")
        paid = ded.line_ids.filtered(lambda l: l.state == 'paid')
        self.assertEqual(len(paid), 1)
        self.assertAlmostEqual(paid.amount, 700.0)
        self.assertTrue(paid.is_manual)
        self.assertEqual((paid.year, paid.month), first_period)

        remainder = ded.line_ids.filtered(
            lambda l: l.state == 'pending' and l.split_origin_id)
        self.assertEqual(len(remainder), 1)
        self.assertAlmostEqual(remainder.amount, 300.0)
        self.assertEqual((remainder.year, remainder.month), first_period,
                         "Remainder stays in the same month as its origin.")
        self.assertEqual(remainder.split_origin_id, paid)
        self.assertFalse(remainder.is_manual)

    def test_sequential_partial_leaves_later_installments_untouched(self):
        ded = self._active_loan(amount=4000.0, installments=4)
        later_before = [
            (l.year, l.month, l.amount) for l in self._due_order(ded)[1:]]
        wiz = self._wizard(ded, 700.0, user=self.user_acc)
        wiz.action_confirm()
        later_after = [
            (l.year, l.month, l.amount)
            for l in self._due_order(ded, state='pending')
            if not l.split_origin_id
        ]
        self.assertEqual(later_before, later_after,
                         "Later installments keep their amounts and dates.")

    def test_sequential_payment_spanning_two_installments(self):
        """1650 against 4 x 1000: #1 closed, #2 split 650 / 350."""
        ded = self._active_loan(amount=4000.0, installments=4)
        lines = self._due_order(ded)
        first_period = (lines[0].year, lines[0].month)
        second_period = (lines[1].year, lines[1].month)
        wiz = self._wizard(ded, 1650.0, user=self.user_acc)
        wiz.action_confirm()

        paid = self._due_order(ded, state='paid')
        self.assertEqual(len(paid), 2)
        self.assertAlmostEqual(paid[0].amount, 1000.0)
        self.assertEqual((paid[0].year, paid[0].month), first_period)
        self.assertAlmostEqual(paid[1].amount, 650.0)
        self.assertEqual((paid[1].year, paid[1].month), second_period)

        remainder = ded.line_ids.filtered(lambda l: l.split_origin_id)
        self.assertEqual(len(remainder), 1)
        self.assertAlmostEqual(remainder.amount, 350.0)
        self.assertEqual((remainder.year, remainder.month), second_period)

    def test_sequential_exact_boundary_creates_no_remainder_line(self):
        ded = self._active_loan(amount=4000.0, installments=4)
        wiz = self._wizard(ded, 2000.0, user=self.user_acc)
        wiz.action_confirm()
        self.assertEqual(len(ded.line_ids), 4, "No split, so no extra row.")
        self.assertEqual(
            len(ded.line_ids.filtered(lambda l: l.state == 'paid')), 2)
        self.assertFalse(ded.line_ids.filtered('split_origin_id'))

    def test_sequential_total_constraint_preserved(self):
        ded = self._active_loan(amount=4000.0, installments=4)
        for amount in (700.0, 1650.0):
            wiz = self._wizard(ded, amount, user=self.user_acc)
            wiz.action_confirm()
            relevant = ded.line_ids.filtered(
                lambda l: l.state in ('pending', 'paid'))
            self.assertAlmostEqual(
                sum(relevant.mapped('amount')), 4000.0, places=2,
                msg="paid + pending must still equal the loan total.")

    def test_sequential_does_not_autocomplete_deduction(self):
        ded = self._active_loan(amount=4000.0, installments=4)
        wiz = self._wizard(ded, 700.0, user=self.user_acc)
        wiz.action_confirm()
        self.assertEqual(ded.state, 'active')

    def test_sequential_then_full_payment_completes(self):
        ded = self._active_loan(amount=4000.0, installments=4)
        self._wizard(ded, 1650.0, user=self.user_acc).action_confirm()
        self._wizard(ded, 2350.0, user=self.user_acc).action_confirm()
        self.assertEqual(ded.state, 'completed')
        self.assertTrue(all(l.state == 'paid' for l in ded.line_ids))

    def test_sequential_rounding_residue_total_preserved(self):
        # 7 / 3 = 2.33 / 2.33 / 2.34 — pay across the first two lines.
        ded = self._active_loan(amount=7.0, installments=3)
        wiz = self._wizard(ded, 3.0, user=self.user_acc)
        wiz.action_confirm()
        relevant = ded.line_ids.filtered(
            lambda l: l.state in ('pending', 'paid'))
        self.assertAlmostEqual(sum(relevant.mapped('amount')), 7.0, places=2)

    def test_sequential_posts_chatter_with_settled_months_and_note(self):
        ded = self._active_loan(amount=4000.0, installments=4)
        lines = self._due_order(ded)
        wiz = self._wizard(ded, 1650.0, note='Cash SR-77', user=self.user_acc)
        wiz.action_confirm()
        bodies = ' '.join(str(m.body or '') for m in ded.message_ids)
        self.assertIn('Cash SR-77', bodies)
        self.assertIn('Fully settled', bodies)
        self.assertIn('Partially settled', bodies)
        self.assertIn(lines[0].display_name, bodies)
        self.assertIn(lines[1].display_name, bodies)
