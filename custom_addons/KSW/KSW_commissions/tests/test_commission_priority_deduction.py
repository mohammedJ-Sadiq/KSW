"""Tests for the automatic "Deduct From Commission First" priority.

The manual per-line ``ksw.deduction.line.x_awaiting_commission`` flag
becomes an automatic, per-employee default
(``hr.employee.x_deduct_commission_priority``, default True). Each month,
before anything reaches payroll, as much of the employee's pending
installment as the commission run can afford is settled there first;
whatever the commission can't cover is left as an ordinary unflagged
pending line, so KSW_deduction's own payroll injection / shortfall
carry-over (unchanged by this feature, and already covered by
KSW_deduction's own test suite) picks it up exactly as it would have
without this feature.
"""
from .test_submission import SubmissionCommon


class CommissionPriorityCommon(SubmissionCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.type_advance = cls.env.ref('KSW_deduction.type_advance')

    def _make_pending_installment(self, employee, amount):
        ded = self.env['ksw.deduction'].create({
            'employee_id': employee.id,
            'type_id': self.type_advance.id,
            'amount': amount,
            'installments': 1,
            'start_month': self.period,
            'reason': 'Commission priority test',
        })
        ded.action_submit()
        return ded

    def _commission_entry(self, employee, amount):
        """A single fixed-amount pay entry for `employee` this period.

        ``amount_override`` makes the earnings figure exact regardless of
        the component's normal quantity/rate math.
        """
        batch = self._batch(self.sup_a, employee.department_id,
                            component=self.meals)
        return self._entry(batch, employee, user=self.sup_a,
                           quantity=1.0, amount_override=amount)

    def _approve(self, batch):
        batch.submission_id.sudo().action_submit()
        run = batch.submission_id.run_id
        run.with_user(self.gm).action_approve()
        return run


class TestNoCommission(CommissionPriorityCommon):
    """Requirement 1 — nothing to offset, installment reaches payroll whole."""

    def test_employee_with_no_pay_entry_is_untouched(self):
        ded = self._make_pending_installment(self.emp_b, 1500.0)
        # A different employee's department submits, so the run has
        # something to approve — emp_b has no pay entry at all this period.
        batch = self._filled_batch(self.sup_a, self.dept_a, self.emp_a)
        self._approve(batch)

        self.assertEqual(len(ded.line_ids), 1)
        line = ded.line_ids
        self.assertEqual(line.state, 'pending')
        self.assertAlmostEqual(line.amount, 1500.0, places=2)
        self.assertFalse(line.x_awaiting_commission)
        self.assertFalse(
            self.env['ksw.pay.run.line'].search(
                [('employee_id', '=', self.emp_b.id)]),
            "no commission activity means no register line at all",
        )


class TestPartialCommissionOffset(CommissionPriorityCommon):
    """Requirement 2 — 1500 pending, 700 commission -> 700 + 800 split."""

    def test_commission_settles_what_it_can_rest_falls_to_payroll(self):
        ded = self._make_pending_installment(self.emp_a, 1500.0)
        entry = self._commission_entry(self.emp_a, 700.0)
        run = self._approve(entry.batch_id)

        run_line = run.line_ids.filtered(
            lambda l: l.employee_id == self.emp_a)
        self.assertAlmostEqual(run_line.earnings, 700.0, places=2)
        self.assertAlmostEqual(run_line.loan_offset, 700.0, places=2)
        self.assertAlmostEqual(run_line.net_payable, 0.0, places=2)

        self.assertEqual(len(ded.line_ids), 2)
        paid = ded.line_ids.filtered(lambda l: l.state == 'paid')
        pending = ded.line_ids.filtered(lambda l: l.state == 'pending')
        self.assertAlmostEqual(paid.amount, 700.0, places=2)
        self.assertEqual(paid.x_paid_via_pay_run_line_id, run_line)
        self.assertFalse(paid.x_awaiting_commission)
        self.assertAlmostEqual(pending.amount, 800.0, places=2)
        self.assertFalse(
            pending.x_awaiting_commission,
            "the uncovered remainder must fall straight into this month's "
            "payslip, not wait for a future commission run",
        )


class TestShortfallBothSides(CommissionPriorityCommon):
    """Requirement 3 — commission covers part, payroll picks up the rest,
    the true remainder still rolls forward exactly as it always has.

    Only the commission-side contract is asserted here (the unflagged
    2200 remainder): that KSW_deduction's own unmodified payroll injection
    then caps it to what NET affords and forwards the true shortfall is
    already covered by KSW_deduction's own payslip test suite.
    """

    def test_commission_partial_leaves_plain_pending_remainder(self):
        ded = self._make_pending_installment(self.emp_a, 2500.0)
        entry = self._commission_entry(self.emp_a, 300.0)
        run = self._approve(entry.batch_id)

        run_line = run.line_ids.filtered(
            lambda l: l.employee_id == self.emp_a)
        self.assertAlmostEqual(run_line.loan_offset, 300.0, places=2)

        paid = ded.line_ids.filtered(lambda l: l.state == 'paid')
        pending = ded.line_ids.filtered(lambda l: l.state == 'pending')
        self.assertAlmostEqual(paid.amount, 300.0, places=2)
        self.assertAlmostEqual(pending.amount, 2200.0, places=2)
        self.assertFalse(pending.x_awaiting_commission)


class TestToggleOff(CommissionPriorityCommon):
    """Requirement 4 — disabled means payroll-only, unchanged from before
    this feature existed."""

    def test_toggle_off_never_touches_commission(self):
        self.emp_a.sudo().write({'x_deduct_commission_priority': False})
        ded = self._make_pending_installment(self.emp_a, 1500.0)
        entry = self._commission_entry(self.emp_a, 700.0)
        run = self._approve(entry.batch_id)

        run_line = run.line_ids.filtered(
            lambda l: l.employee_id == self.emp_a)
        self.assertAlmostEqual(run_line.loan_offset, 0.0, places=2)
        self.assertAlmostEqual(run_line.net_payable, 700.0, places=2)

        self.assertEqual(len(ded.line_ids), 1)
        line = ded.line_ids
        self.assertEqual(line.state, 'pending')
        self.assertAlmostEqual(line.amount, 1500.0, places=2)
        self.assertFalse(line.x_awaiting_commission)


class TestManualParkCoexistsWithAutoFlag(CommissionPriorityCommon):
    """The pre-existing manual "slice" workflow must not be disturbed by
    the new automatic sweep running in the same period."""

    def test_manual_slice_keeps_waiting_when_auto_covers_the_rest(self):
        ded = self._make_pending_installment(self.emp_a, 200.0)
        auto_line = ded.line_ids
        # Accountant manually parks 150 of the 200 for a LATER commission
        # run, leaving 50 on the auto-generated line for the new automatic
        # sweep to pick up this run. Both writes have to land before the
        # total-matches-amount constraint is checked again, same as the
        # parent's own batched O2M writes do.
        auto_line.with_context(_skip_installment_total_check=True).write(
            {'amount': 50.0})
        manual = self.env['ksw.deduction.line'].sudo().with_context(
            _skip_installment_total_check=True).create({
                'deduction_id': ded.id, 'sequence': auto_line.sequence,
                'year': auto_line.year, 'month': auto_line.month,
                'amount': 150.0, 'x_awaiting_commission': True,
            })
        ded._validate_installments_total()
        entry = self._commission_entry(self.emp_a, 100.0)
        self._approve(entry.batch_id)

        # FIFO settles the lower-id line first: the 50 (auto-flagged this
        # run) is fully paid off and unflagged. The remaining 50 of
        # commission only partially covers the manual 150, whose leftover
        # 100 must STAY parked — it was never in our auto-flagged set.
        auto_line = auto_line.exists()
        self.assertEqual(auto_line.state, 'paid')
        self.assertFalse(auto_line.x_awaiting_commission)
        manual = manual.exists()
        self.assertEqual(manual.state, 'pending')
        self.assertAlmostEqual(manual.amount, 100.0, places=2)
        self.assertTrue(
            manual.x_awaiting_commission,
            "a manually-parked slice must keep waiting for a future "
            "commission run, not be swept back to payroll by the new "
            "automatic policy",
        )
