"""Non-loan deductions: one installment by default, spreadable (Nov 2026).

The Oct 2026 lock is lifted on the seeded types. A payslip still takes
only the installments already due by its own month; later installments
wait for their month (a vacation / EOS settlement is the exception and
takes everything, see `_ksw_pending_lines_domain`).
"""
from datetime import timedelta

from odoo.exceptions import ValidationError

from .common import DeductionCommon


class TestNonLoanInstallments(DeductionCommon):

    def test_01_type_defaults_to_one(self):
        ded = self.env['ksw.deduction'].new({'type_id': self.type_gov_pen.id})
        ded._onchange_type_id()
        self.assertEqual(ded.installments, 1)

    def test_02_can_be_spread(self):
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=900.0, installments=3))
        self.assertEqual(ded.installments, 3)
        self.assertEqual(len(ded.line_ids), 3)

    def test_03_zero_or_negative_refused(self):
        for bad in (0, -2):
            with self.assertRaises(ValidationError):
                self._make_deduction(self.type_gov_pen, installments=bad)
        ded = self._make_deduction(self.type_gov_pen, installments=2)
        with self.assertRaises(ValidationError):
            ded.write({'installments': 0})

    def test_04_payslip_takes_only_due_installments(self):
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=900.0, installments=3))
        Payslip = self.env['hr.payslip']
        Line = self.env['ksw.deduction.line'].sudo()
        month_end = self.next_month - timedelta(days=1)
        due = Line.search(Payslip._ksw_pending_lines_domain(
            [self.employee.id], month_end)) & ded.line_ids
        self.assertEqual(len(due), 1)
        self.assertEqual(due.period_date, self.this_month)
        # A vacation / EOS settlement still takes the whole obligation.
        full = Line.search(Payslip._ksw_pending_lines_domain(
            [self.employee.id], month_end, full=True)) & ded.line_ids
        self.assertEqual(len(full), 3)
