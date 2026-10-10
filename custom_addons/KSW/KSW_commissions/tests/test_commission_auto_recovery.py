"""Non-loan deductions are taken out of approved, unpaid commission first.

The floor case (KSWCO, Oct 2026): August commission approved on the 31st
and paid in October; a government fee charged in September left the
September payslip at zero. A non-loan deduction now takes its installment
out of any commission month that is approved and not yet marked Paid,
oldest first, the moment it is activated. What the commission cannot cover
stays a plain pending installment: the next payslip or the next approved
commission month collects it, whichever comes first. A personal loan is
never touched.

In BAS the commission is first credited in full to the accrual pool, and
each deduction is its own Dr accrual / Cr advance record: in the month
voucher if the debt existed on the month end, on its own voucher dated the
day it was charged otherwise.
"""
from dateutil.relativedelta import relativedelta

from odoo import fields

from .test_bas_journal_entry import BasJournalCommon


class AutoRecoveryCommon(BasJournalCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.type_gov_pen = cls.env.ref('KSW_deduction.type_gov_penalty')
        cls.type_loan = cls.env.ref('KSW_deduction.type_loan')
        cls.this_month = fields.Date.context_today(
            cls.env['ksw.deduction']).replace(day=1)
        # Only the automatic step is under test: the same-month sweep at
        # approval would take the installments first and hide it.
        (cls.emp_a | cls.emp_b).sudo().write(
            {'x_deduct_commission_priority': False})

    def _penalty(self, employee, amount):
        ded = self.env['ksw.deduction'].create({
            'employee_id': employee.id,
            'type_id': self.type_gov_pen.id,
            'amount': amount,
            'installments': 1,
            'start_month': self.this_month,
            'reason': 'Government fee',
        })
        ded.action_submit()
        self.assertEqual(ded.state, 'active')
        return ded

    def _register_line(self, run, employee):
        return run.line_ids.filtered(lambda l: l.employee_id == employee)


class TestTakenAtActivation(AutoRecoveryCommon):

    def test_01_whole_installment_from_an_approved_month(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        ded = self._penalty(self.emp_a, 1000.0)

        line = ded.line_ids
        self.assertEqual(line.state, 'paid')
        self.assertEqual(line.x_recover_from_run_id, run)
        # It keeps its own due month — no relabelling.
        self.assertEqual((line.year, line.month),
                         (self.this_month.year, self.this_month.month))
        reg = self._register_line(run, self.emp_a)
        self.assertEqual(line.x_paid_via_pay_run_line_id, reg)
        self.assertAlmostEqual(reg.loan_offset, 1000.0)
        self.assertAlmostEqual(reg.net_payable, 1456.0)
        self.assertEqual(line.x_settlement_date,
                         line._recovery_posting_date())

    def test_02_what_the_commission_cannot_cover_stays_on_payroll(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        ded = self._penalty(self.emp_a, 3376.0)

        paid = ded.line_ids.filtered(lambda l: l.state == 'paid')
        left = ded.line_ids.filtered(lambda l: l.state == 'pending')
        self.assertAlmostEqual(paid.amount, 2456.0)
        self.assertAlmostEqual(left.amount, 920.0)
        self.assertFalse(left.x_awaiting_commission,
                         'the payslip must still be able to collect it')
        self.assertFalse(left.x_recover_from_run_id)
        self.assertAlmostEqual(
            self._register_line(run, self.emp_a).net_payable, 0.0)

    def test_03_a_personal_loan_is_never_touched(self):
        self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        loan = self.env['ksw.deduction'].sudo().create({
            'employee_id': self.emp_a.id, 'type_id': self.type_loan.id,
            'amount': 1000.0, 'installments': 1,
            'start_month': self.this_month, 'reason': 'Loan'})
        loan._activate_and_generate_lines()
        self.assertEqual(loan.line_ids.state, 'pending')
        self.assertFalse(loan.line_ids.x_recover_from_run_id)

    def test_04_a_month_marked_paid_is_not_used(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        run.sudo().write({'state': 'paid'})
        ded = self._penalty(self.emp_a, 1000.0)
        self.assertEqual(ded.line_ids.state, 'pending')
        self.assertFalse(ded.line_ids.x_awaiting_commission)

    def test_05_an_open_month_is_not_used(self):
        entry = self._commission_entry(self.emp_a, 2456.0)
        entry.batch_id.submission_id.sudo().action_submit()
        ded = self._penalty(self.emp_a, 1000.0)
        self.assertEqual(ded.line_ids.state, 'pending')
        self.assertFalse(ded.line_ids.x_recover_from_run_id)

    def test_06_another_employees_commission_is_not_used(self):
        self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        ded = self._penalty(self.emp_b, 1000.0)
        self.assertEqual(ded.line_ids.state, 'pending')


class TestRemainderByTheNextCommission(AutoRecoveryCommon):

    def test_01_the_next_approved_month_collects_the_remainder(self):
        self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        ded = self._penalty(self.emp_a, 3376.0)
        left = ded.line_ids.filtered(lambda l: l.state == 'pending')
        self.assertAlmostEqual(left.amount, 920.0)

        next_period = (fields.Date.to_date(self.period)
                       + relativedelta(months=1))
        batch = self._batch(self.sup_a, self.dept_a, component=self.meals,
                            period=next_period)
        self._entry(batch, self.emp_a, user=self.sup_a,
                    quantity=1.0, amount_override=1500.0)
        run2 = self._approve(batch)

        self.assertEqual(left.state, 'paid')
        self.assertEqual(left.x_recover_from_run_id, run2)
        self.assertAlmostEqual(
            self._register_line(run2, self.emp_a).net_payable, 580.0)

    def test_02_reopening_the_month_keeps_the_recovery_for_reapproval(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        ded = self._penalty(self.emp_a, 1000.0)
        run.sudo().action_reopen()
        self.assertEqual(ded.line_ids.state, 'pending')
        self.assertEqual(ded.line_ids.x_recover_from_run_id, run)
        self.assertTrue(ded.line_ids.x_awaiting_commission)


class TestRecoveryJournal(AutoRecoveryCommon):

    def test_01_charged_after_the_month_end_is_its_own_voucher(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        ded = self._penalty(self.emp_a, 2306.0)
        month_end = run._bas_journal_date()
        charged = month_end + relativedelta(days=29)
        ded.sudo().x_charge_date = charged

        month_rows = run._bas_journal_rows()
        self.assertFalse(self._by_code(month_rows, '1205010385'),
                         'a later debt is never back-dated into the month')
        self.assertAlmostEqual(
            sum(r['credit'] for r in self._by_code(month_rows, '2107010001')),
            2456.0, msg='the pool carries his full right')

        by_date = run._bas_recovery_rows()
        self.assertEqual(list(by_date), [charged])
        rows = by_date[charged]
        self.assertEqual([r['code'] for r in rows],
                         ['2107010001', '1205010385'])
        self.assertAlmostEqual(rows[0]['debit'], 2306.0)
        self.assertAlmostEqual(rows[1]['credit'], 2306.0)
        self.assertIn(ded.name, rows[0]['ref'])
        self.assertEqual(len(run._bas_recovery_workbooks('26010001')), 1)

    def test_02_existing_on_the_month_end_is_a_record_in_the_month(self):
        run = self._approve(self._commission_entry(self.emp_a, 2456.0).batch_id)
        ded = self._penalty(self.emp_a, 1000.0)
        ded.sudo().x_charge_date = run._bas_journal_date()

        rows = run._bas_journal_rows()
        self.assertEqual([r['code'] for r in rows],
                         ['3201010006', '2107010001',
                          '2107010001', '1205010385'])
        self.assertAlmostEqual(rows[1]['credit'], 2456.0)
        self.assertAlmostEqual(rows[2]['debit'], 1000.0)
        self.assertFalse(run._bas_recovery_rows())
        self.env['ksw.bas.journal'].check_balanced(rows)
