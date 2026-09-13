# -*- coding: utf-8 -*-
"""Tests for ksw.deduction.reschedule.wizard.

The property that matters throughout: rescheduling changes the *plan*,
never the debt. Paid installments stay untouched and the sum of
pending + paid keeps matching `amount`, which is what
`_validate_installments_total` asserts on every parent write — so a
reschedule that silently altered a balance would raise rather than pass.
"""
from dateutil.relativedelta import relativedelta

from odoo.exceptions import AccessError, UserError, ValidationError

from .common import DeductionCommon


class TestRescheduleWizard(DeductionCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def _mk(login, group_xmlids):
            return Users.create({
                'name': login,
                'login': login,
                'email': f'{login}@kswresched.test',
                'group_ids': [(6, 0, [cls.env.ref(g).id for g in group_xmlids])],
            })

        # "Loan Modification: Full" — may reschedule any type.
        cls.user_acc = _mk(
            'kswresched_acc',
            ['KSW_deduction.group_installment_edit',
             'KSW_deduction.group_loan_acc'])
        # Accounting Data Entry — non-loan types only.
        cls.user_data_entry = _mk(
            'kswresched_de', ['KSW_deduction.group_acc_data_entry'])
        cls.user_plain = _mk(
            'kswresched_plain', ['KSW_deduction.group_deduction_user'])
        # The only role with perm_create on ksw.deduction.line — needed to
        # demonstrate what a hand-added row actually becomes.
        cls.user_manager = _mk(
            'kswresched_mgr',
            ['KSW_deduction.group_deduction_manager',
             'KSW_deduction.group_installment_edit'])

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _active_loan(self, amount=6000.0, installments=4):
        """Activate a loan, accepting the concurrent-loan warning.

        The second loan of an employee cannot be approved until each step
        has stamped its acknowledgement (`_check_concurrent_loan_ack`) —
        the policy is "accepted on record", not "blocked". Since the whole
        point of these tests is an employee carrying two loans at once,
        the fixture has to walk through that acceptance the way the real
        approvers do.
        """
        ded = self._make_deduction(self.type_loan, amount=amount,
                                   installments=installments)
        ded.action_submit()
        ded.action_dm_approve()
        self._accept_concurrent(ded)
        ded.x_hr_no_penalties_confirmed = True
        ded.action_hr_approve()
        self._accept_concurrent(ded)
        ded.x_acc_budget_confirmed = True
        ded.action_acc_approve()
        self._accept_concurrent(ded)
        ded.action_gm_approve()
        # A loan is only activated — and its schedule only generated — by
        # the disbursement confirmation.
        ded.action_disbursement_confirm()
        self.assertEqual(ded.state, 'active')
        return ded

    @staticmethod
    def _accept_concurrent(ded):
        if ded.x_has_concurrent_loan:
            ded.action_bypass_concurrent_loan()

    def _wizard(self, ded, user=None, siblings=None, **vals):
        Wizard = self.env['ksw.deduction.reschedule.wizard']
        if user is not None:
            Wizard = Wizard.with_user(user)
        base = {
            'deduction_id': ded.id,
            'start_month': self.this_month,
            'reason': 'Test reschedule',
            # `sibling_ids` defaults to every selectable sibling; tests
            # that do not opt in must clear it or they would silently
            # reschedule more than they assert on.
            'sibling_ids': [(6, 0, (siblings or self.env['ksw.deduction']).ids)],
        }
        base.update(vals)
        return Wizard.create(base)

    def _pending(self, ded):
        return ded.line_ids.filtered(lambda l: l.state == 'pending').sorted(
            lambda l: (l.year, l.month, l.sequence))

    # ------------------------------------------------------------------ #
    # The gap this wizard exists to close                                 #
    # ------------------------------------------------------------------ #

    def test_schedule_cannot_be_lengthened_by_hand(self):
        """Baseline: why the wizard is needed at all.

        Adding a row to the Installments tab produces a manual *paid*
        entry, and an auto line cannot be deleted. Both are deliberate
        (they are the manual-payment and cancel-flow guards) — together
        they make "more months" unreachable from the grid.
        """
        ded = self._active_loan(amount=6000.0, installments=4)
        line = self._pending(ded)[0]

        with self.assertRaises(UserError):
            line.with_user(self.user_acc).unlink()

        added = self.env['ksw.deduction.line'].with_user(self.user_manager).create({
            'deduction_id': ded.id,
            'year': self.this_month.year,
            'month': self.this_month.month,
            'amount': 0.0,
        })
        self.assertTrue(added.is_manual)
        self.assertEqual(added.state, 'paid')

    # ------------------------------------------------------------------ #
    # Lengthening a plan                                                  #
    # ------------------------------------------------------------------ #

    def test_extends_four_months_to_six(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        self.assertEqual(len(self._pending(ded)), 4)

        self._wizard(ded, user=self.user_acc, installments=6).action_confirm()

        pending = self._pending(ded)
        self.assertEqual(len(pending), 6)
        self.assertEqual(
            [l.amount for l in pending], [1000.0] * 6)
        # The debt itself is untouched.
        self.assertAlmostEqual(ded.amount, 6000.0)
        self.assertAlmostEqual(sum(pending.mapped('amount')), 6000.0)

    def test_shortens_plan(self):
        ded = self._active_loan(amount=6000.0, installments=6)
        self._wizard(ded, user=self.user_acc, installments=3).action_confirm()
        pending = self._pending(ded)
        self.assertEqual(len(pending), 3)
        self.assertEqual([l.amount for l in pending], [2000.0] * 3)

    def test_new_lines_are_pending_not_manual_paid(self):
        """`_ksw_auto_generating` must reach `ksw.deduction.line.create()`.

        Without it every rebuilt line would be stamped manual/paid — the
        loan would read as fully collected the moment it was rescheduled.
        """
        ded = self._active_loan(amount=6000.0, installments=4)
        self._wizard(ded, user=self.user_acc, installments=6).action_confirm()
        for line in self._pending(ded):
            self.assertFalse(line.is_manual)
            self.assertEqual(line.state, 'pending')
            self.assertFalse(line.payslip_id)

    def test_periods_run_consecutively_from_start_month(self):
        start = self.this_month + relativedelta(months=2)
        ded = self._active_loan(amount=6000.0, installments=4)
        self._wizard(ded, user=self.user_acc, installments=6,
                     start_month=start).action_confirm()
        expected = [
            (start + relativedelta(months=i)) for i in range(6)]
        self.assertEqual(
            [(l.year, l.month) for l in self._pending(ded)],
            [(d.year, d.month) for d in expected])

    def test_rounding_residue_lands_on_last_installment(self):
        ded = self._active_loan(amount=1000.0, installments=2)
        self._wizard(ded, user=self.user_acc, installments=3).action_confirm()
        pending = self._pending(ded)
        self.assertEqual([l.amount for l in pending][:2], [333.33, 333.33])
        self.assertAlmostEqual(pending[-1].amount, 333.34)
        self.assertAlmostEqual(sum(pending.mapped('amount')), 1000.0)

    # ------------------------------------------------------------------ #
    # Paid installments are never touched                                 #
    # ------------------------------------------------------------------ #

    def test_only_outstanding_is_respread(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        paid = self._pending(ded)[0]
        paid.sudo().write({'state': 'paid', 'is_manual': True})

        self._wizard(ded, user=self.user_acc, installments=6).action_confirm()

        # The paid line survives untouched...
        self.assertEqual(paid.state, 'paid')
        self.assertAlmostEqual(paid.amount, 1500.0)
        # ...and only the 4,500 left is re-spread.
        pending = self._pending(ded)
        self.assertEqual(len(pending), 6)
        self.assertEqual([l.amount for l in pending], [750.0] * 6)
        self.assertAlmostEqual(
            sum(ded.line_ids.filtered(
                lambda l: l.state in ('pending', 'paid')).mapped('amount')),
            6000.0)

    def test_parent_installment_count_includes_paid(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        self._pending(ded)[0].sudo().write({'state': 'paid', 'is_manual': True})
        self._wizard(ded, user=self.user_acc, installments=6).action_confirm()
        # 1 already paid + 6 new = 7, so the header pill stays honest.
        self.assertEqual(ded.installments, 7)

    def test_sequences_continue_after_paid_lines(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        paid = self._pending(ded)[0]
        paid.sudo().write({'state': 'paid', 'is_manual': True})
        self._wizard(ded, user=self.user_acc, installments=3).action_confirm()
        self.assertEqual(
            [l.sequence for l in self._pending(ded)],
            [paid.sequence + 1, paid.sequence + 2, paid.sequence + 3])

    # ------------------------------------------------------------------ #
    # Planning by monthly amount                                          #
    # ------------------------------------------------------------------ #

    def test_plan_by_monthly_derives_month_count(self):
        ded = self._active_loan(amount=9000.0, installments=4)
        wiz = self._wizard(ded, user=self.user_acc,
                           plan_by='monthly', target_monthly=1500.0)
        self.assertEqual(wiz.effective_installments, 6)
        self.assertAlmostEqual(wiz.monthly_total, 1500.0)
        wiz.action_confirm()
        self.assertEqual([l.amount for l in self._pending(ded)], [1500.0] * 6)

    def test_plan_by_monthly_rounds_up_and_last_month_is_smaller(self):
        """An indivisible balance gets an extra, smaller month — never a
        month above the agreed ceiling."""
        ded = self._active_loan(amount=1000.0, installments=2)
        wiz = self._wizard(ded, user=self.user_acc,
                           plan_by='monthly', target_monthly=300.0)
        self.assertEqual(wiz.effective_installments, 4)
        wiz.action_confirm()
        amounts = [l.amount for l in self._pending(ded)]
        self.assertEqual(len(amounts), 4)
        self.assertTrue(all(a <= 300.0 for a in amounts))
        self.assertAlmostEqual(sum(amounts), 1000.0)

    # ------------------------------------------------------------------ #
    # The two-loan case this was built for                                #
    # ------------------------------------------------------------------ #

    def test_two_loans_one_monthly_ceiling(self):
        """9,000 across two loans → 1,500/month for 6 months, combined.

        Each loan keeps its own record, reference and ledger; only the
        plans change. This is the alternative to cancelling both and
        re-issuing a merged one, which would write off debts that were
        never forgiven and record a disbursement that never happened.
        """
        loan_a = self._active_loan(amount=5400.0, installments=3)
        loan_b = self._active_loan(amount=3600.0, installments=2)

        wiz = self._wizard(loan_a, user=self.user_acc, siblings=loan_b,
                           plan_by='monthly', target_monthly=1500.0)
        self.assertAlmostEqual(wiz.total_outstanding, 9000.0)
        self.assertEqual(wiz.effective_installments, 6)
        wiz.action_confirm()

        a, b = self._pending(loan_a), self._pending(loan_b)
        self.assertEqual([l.amount for l in a], [900.0] * 6)
        self.assertEqual([l.amount for l in b], [600.0] * 6)
        # Both records still exist, still active, still their own debt.
        self.assertEqual(loan_a.state, 'active')
        self.assertEqual(loan_b.state, 'active')
        self.assertAlmostEqual(loan_a.amount, 5400.0)
        self.assertAlmostEqual(loan_b.amount, 3600.0)
        # And every month of the combined plan lands on the ceiling.
        for i in range(6):
            self.assertAlmostEqual(a[i].amount + b[i].amount, 1500.0)
            self.assertEqual((a[i].year, a[i].month), (b[i].year, b[i].month))

    def test_siblings_preselected_by_default(self):
        loan_a = self._active_loan(amount=5400.0, installments=3)
        loan_b = self._active_loan(amount=3600.0, installments=2)
        wiz = self.env['ksw.deduction.reschedule.wizard'].with_user(
            self.user_acc).create({
                'deduction_id': loan_a.id,
                'reason': 'x',
                'start_month': self.this_month,
            })
        self.assertIn(loan_b, wiz.sibling_ids)
        self.assertTrue(wiz.has_siblings)

    def test_sibling_picker_excludes_what_the_user_cannot_edit(self):
        """Never offer a picker wider than the caller's authority.

        Data Entry may not touch loans, so a loan must not appear in
        their sibling list — the confirm would only refuse it later.
        """
        advance = self._activate(self._make_deduction(
            self.type_advance, amount=2000.0, installments=2))
        loan = self._active_loan(amount=3000.0, installments=2)
        wiz = self.env['ksw.deduction.reschedule.wizard'].with_user(
            self.user_data_entry).create({
                'deduction_id': advance.id,
                'reason': 'x',
                'start_month': self.this_month,
            })
        self.assertNotIn(loan, wiz.available_sibling_ids)
        self.assertNotIn(loan, wiz.sibling_ids)

    # ------------------------------------------------------------------ #
    # Authority                                                           #
    # ------------------------------------------------------------------ #

    def test_data_entry_cannot_reschedule_a_loan(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        wiz = self._wizard(ded, user=self.user_data_entry, installments=6)
        with self.assertRaises(UserError):
            wiz.action_confirm()

    def test_data_entry_can_reschedule_an_advance(self):
        ded = self._activate(self._make_deduction(
            self.type_advance, amount=2000.0, installments=2))
        self._wizard(ded, user=self.user_data_entry,
                     installments=4).action_confirm()
        self.assertEqual(len(self._pending(ded)), 4)

    def test_plain_user_cannot_reschedule(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        # A plain user has no ACL on the wizard model at all, so it cannot
        # even be instantiated — the confirm guard is the second line of
        # defence, not the first.
        with self.assertRaises(AccessError):
            self._wizard(ded, user=self.user_plain,
                         installments=6).action_confirm()

    # ------------------------------------------------------------------ #
    # Input guards                                                        #
    # ------------------------------------------------------------------ #

    def test_zero_months_is_rejected(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        with self.assertRaises(ValidationError):
            self._wizard(ded, user=self.user_acc,
                         installments=0).action_confirm()

    def test_absurd_month_count_is_rejected(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        with self.assertRaises(ValidationError):
            self._wizard(ded, user=self.user_acc,
                         installments=200).action_confirm()

    def test_tiny_monthly_amount_is_rejected_as_too_long(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        with self.assertRaises(ValidationError):
            self._wizard(ded, user=self.user_acc, plan_by='monthly',
                         target_monthly=1.0).action_confirm()

    def test_fully_paid_deduction_has_nothing_to_reschedule(self):
        ded = self._active_loan(amount=6000.0, installments=4)
        ded.line_ids.sudo().write({'state': 'paid', 'is_manual': True})
        with self.assertRaises(UserError):
            self._wizard(ded, user=self.user_acc,
                         installments=6).action_confirm()

    def test_draft_deduction_cannot_be_rescheduled(self):
        ded = self._make_deduction(self.type_advance, amount=1000.0,
                                   installments=2)
        self.assertEqual(ded.state, 'draft')
        with self.assertRaises(UserError):
            self._wizard(ded, user=self.user_acc,
                         installments=4).action_confirm()

    def test_mixed_employees_are_rejected(self):
        mine = self._activate(self._make_deduction(
            self.type_advance, amount=1000.0, installments=2))
        theirs = self._activate(self._make_deduction(
            self.type_advance, employee=self.employee_b,
            amount=1000.0, installments=2))
        wiz = self._wizard(mine, user=self.user_acc, siblings=theirs,
                           installments=4)
        with self.assertRaises(UserError):
            wiz.action_confirm()

    # ------------------------------------------------------------------ #
    # Audit trail                                                         #
    # ------------------------------------------------------------------ #

    def test_chatter_records_the_change_on_every_deduction(self):
        loan_a = self._active_loan(amount=5400.0, installments=3)
        loan_b = self._active_loan(amount=3600.0, installments=2)
        before_a = loan_a.message_ids.ids
        before_b = loan_b.message_ids.ids

        self._wizard(loan_a, user=self.user_acc, siblings=loan_b,
                     plan_by='monthly', target_monthly=1500.0,
                     reason='Agreed 1,500/month').action_confirm()

        for ded, before, other in ((loan_a, before_a, loan_b),
                                   (loan_b, before_b, loan_a)):
            new = ded.message_ids.filtered(lambda m: m.id not in before)
            self.assertTrue(new, 'no chatter posted on %s' % ded.name)
            body = ''.join(new.mapped('body'))
            self.assertIn('Rescheduled', body)
            self.assertIn('Agreed 1,500/month', body)
            # Each note names the other deduction it was rescheduled with,
            # so neither ledger has to be read alone to understand it.
            self.assertIn(other.name, body)
