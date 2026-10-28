"""A deduction taken from a commission month the vacation then pays.

KSWCO PE00260 / leave 5256 (Oct 2026): a 113 government penalty was taken
out of the approved, not-Paid August commission at activation. The vacation
then paid August in full, and confirming it unwound the register line, so
the 113 went back to payroll. The vacation papers showed the whole
commission and no deduction at all.

The installment follows the month: the vacation payslip that pays the month
carries the installment too, and confirming it collects it there.
"""
from datetime import date

from .test_commission_auto_recovery import AutoRecoveryCommon


class TestVacationTakesRecoveredDeduction(AutoRecoveryCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.version_a = cls.emp_a.current_version_id
        cls.version_a.sudo().write({
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'struct_id': cls.env.ref('om_hr_payroll.structure_base').id,
        })
        cls.unpaid_type = cls.env['hr.leave.type'].create({
            'name': 'Recovered Deduction Unpaid Leave',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
            'is_unpaid_leave': True,
        })

    def _vacation_payslip(self):
        leave = self.env['hr.leave'].with_context(
            tracking_disable=True, mail_create_nosubscribe=True,
        ).create({
            'employee_id': self.emp_a.id,
            'holiday_status_id': self.unpaid_type.id,
            'request_date_from': date(2029, 4, 1),
            'request_date_to': date(2029, 4, 20),
        })
        payslip = self.env['hr.payslip'].create({
            'employee_id': self.emp_a.id,
            'date_from': date(2029, 4, 1),
            'date_to': date(2029, 4, 30),
            'version_id': self.version_a.id,
            'struct_id': self.version_a.struct_id.id,
            'x_leave_id': leave.id,
        })
        self.env['hr.payslip.input'].create(
            self.env['hr.leave']._build_vacation_input_lines(
                leave, self.emp_a, payslip))
        payslip.compute_sheet()
        return leave, payslip

    def _held_installment(self):
        run = self._approve(self._commission_entry(self.emp_a, 1300.0).batch_id)
        ded = self._penalty(self.emp_a, 113.0)
        line = ded.line_ids
        self.assertEqual(line.state, 'paid')
        self.assertEqual(line.x_paid_via_pay_run_line_id.run_id, run)
        return run, line

    def test_vacation_payslip_shows_the_installment(self):
        _run, line = self._held_installment()
        leave, payslip = self._vacation_payslip()
        codes = payslip.input_line_ids.mapped('code')
        self.assertIn('KSW_DED_%d' % line.id, codes,
                      'the vacation pays the month, so it carries the '
                      'installment the month was paying')
        rows, _from_slip = leave._ksw_report_deduction_rows()
        self.assertEqual([r['amount'] for r in rows], [113.0])

    def test_confirming_collects_it_on_the_payslip(self):
        run, line = self._held_installment()
        _leave, payslip = self._vacation_payslip()
        payslip.write({'state': 'done'})

        self.assertEqual(line.state, 'paid')
        self.assertEqual(line.payslip_id, payslip)
        self.assertFalse(line.x_paid_via_pay_run_line_id)
        self.assertFalse(line.x_recover_from_run_id)
        self.assertFalse(self._register_line(run, self.emp_a),
                         'the vacation took the whole month off the register')

    def test_cancelling_gives_it_back_once(self):
        _run, line = self._held_installment()
        _leave, payslip = self._vacation_payslip()
        payslip.write({'state': 'done'})
        payslip.write({'state': 'cancel'})
        self.assertFalse(line.payslip_id)
        self.assertTrue(line.state == 'pending'
                        or line.x_paid_via_pay_run_line_id,
                        'still owed, by payroll or the restored month')

    def test_without_a_vacation_the_month_keeps_it(self):
        run, line = self._held_installment()
        self.assertAlmostEqual(self._register_line(run, self.emp_a).loan_offset,
                               113.0)
        self.assertEqual(line.x_paid_via_pay_run_line_id,
                         self._register_line(run, self.emp_a))
