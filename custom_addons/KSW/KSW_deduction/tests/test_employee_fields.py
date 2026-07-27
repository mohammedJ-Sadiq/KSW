# -*- coding: utf-8 -*-
"""Tests for hr.employee custom fields and action_view_deductions."""
from datetime import date
from dateutil.relativedelta import relativedelta
from .common import DeductionCommon
class TestEmployeeFields(DeductionCommon):
    def test_count_only_active_deductions(self):
        # 2 active + 1 cancelled
        d_active1 = self._make_deduction(amount=100.0, installments=1)
        d_active1.action_submit()
        d_active2 = self._make_deduction(amount=200.0, installments=2)
        d_active2.action_submit()
        d_cancel = self._make_deduction(amount=50.0, installments=1)
        d_cancel.action_submit()
        d_cancel.action_cancel()
        self.employee.invalidate_recordset(['x_deduction_count',
                                            'x_deduction_monthly_total',
                                            'x_deduction_currency_id'])
        self.assertEqual(self.employee.x_deduction_count, 2)
        self.assertEqual(self.employee.x_deduction_currency_id,
                         self.company.currency_id)
    def test_monthly_total_filters_current_month(self):
        # Current-month line counted
        d_curr = self._make_deduction(amount=100.0, installments=1,
                                      start_month=self.this_month)
        d_curr.action_submit()
        # Next-month line not counted
        d_next = self._make_deduction(amount=999.0, installments=1,
                                      start_month=self.next_month)
        d_next.action_submit()
        self.employee.invalidate_recordset(['x_deduction_monthly_total'])
        self.assertEqual(self.employee.x_deduction_monthly_total, 100.0)
    def test_monthly_total_includes_carried_forward_lines(self):
        """A line left pending from an earlier month is collected by this
        month's payroll (hr.payslip._ksw_pending_lines_domain uses
        period_date <= date_to), so it must count towards the monthly total.
        Reproduces the case where a payslip could only afford part of an
        installment and the remainder stayed dated to the previous month."""
        prev_month = self.this_month - relativedelta(months=1)
        d = self._make_deduction(amount=100.0, installments=1,
                                 start_month=self.this_month)
        d.action_submit()
        # Backdate the (still pending) line to last month, as a forwarded
        # remainder would be.
        d.line_ids.sudo().write({
            'year': prev_month.year, 'month': prev_month.month})
        self.employee.invalidate_recordset(['x_deduction_monthly_total'])
        self.assertEqual(self.employee.x_deduction_monthly_total, 100.0)

    def test_monthly_total_excludes_paid_and_skipped(self):
        d = self._make_deduction(amount=300.0, installments=3,
                                 start_month=self.this_month)
        d.action_submit()
        # Mark first (current month) line as paid → excluded
        curr_line = d.line_ids.filtered(
            lambda l: l.period_date == self.this_month)
        curr_line.write({'state': 'paid'})
        self.employee.invalidate_recordset(['x_deduction_monthly_total'])
        self.assertEqual(self.employee.x_deduction_monthly_total, 0.0)
    def test_outstanding_total_spans_all_months(self):
        # 4 x 100 starting this month -> 400 outstanding, 100 this month
        d_curr = self._make_deduction(amount=400.0, installments=4,
                                      start_month=self.this_month)
        d_curr.action_submit()
        # A deduction whose schedule only starts NEXT month contributes to
        # outstanding but not to the monthly figure — the gap this field fills.
        d_next = self._make_deduction(amount=200.0, installments=2,
                                      start_month=self.next_month)
        d_next.action_submit()
        self.employee.invalidate_recordset(['x_deduction_monthly_total',
                                            'x_deduction_outstanding_total'])
        self.assertEqual(self.employee.x_deduction_monthly_total, 100.0)
        self.assertEqual(self.employee.x_deduction_outstanding_total, 600.0)

    def test_outstanding_total_excludes_paid_and_non_active(self):
        d = self._make_deduction(amount=300.0, installments=3,
                                 start_month=self.this_month)
        d.action_submit()
        d.line_ids.filtered(
            lambda l: l.period_date == self.this_month).write({'state': 'paid'})
        # Draft (never submitted) deduction must not count
        self._make_deduction(amount=500.0, installments=5,
                             start_month=self.this_month)
        self.employee.invalidate_recordset(['x_deduction_outstanding_total'])
        # 300 total, one 100 line paid, draft deduction ignored
        self.assertEqual(self.employee.x_deduction_outstanding_total, 200.0)

    def test_deduction_mirrors_employee_totals(self):
        d = self._make_deduction(amount=400.0, installments=4,
                                 start_month=self.this_month)
        d.action_submit()
        self.employee.invalidate_recordset(['x_deduction_monthly_total',
                                            'x_deduction_outstanding_total'])
        d.invalidate_recordset(['x_emp_monthly_total',
                                'x_emp_outstanding_total'])
        self.assertEqual(d.x_emp_monthly_total, 100.0)
        self.assertEqual(d.x_emp_outstanding_total, 400.0)

    def test_month_drilldown_action_domain(self):
        d = self._make_deduction(amount=400.0, installments=4,
                                 start_month=self.this_month)
        d.action_submit()
        action = d.action_view_employee_month_installments()
        self.assertEqual(action['res_model'], 'ksw.deduction.line')
        month_end = self.this_month + relativedelta(months=1, days=-1)
        # No lower bound: carried-forward lines from earlier months are
        # collected this month and belong in the list.
        self.assertEqual(action['domain'], [
            ('employee_id', '=', self.employee.id),
            ('state', '=', 'pending'),
            ('deduction_id.state', '=', 'active'),
            ('period_date', '<=', month_end),
        ])
        self.assertEqual(action['context']['search_default_group_deduction'], 1)
        # The list must add up to the figure shown on the form
        lines = self.env['ksw.deduction.line'].search(action['domain'])
        self.assertEqual(sum(lines.mapped('amount')), d.x_emp_monthly_total)

    def test_outstanding_drilldown_has_no_period_clause(self):
        d = self._make_deduction(amount=400.0, installments=4,
                                 start_month=self.this_month)
        d.action_submit()
        action = d.action_view_employee_outstanding_installments()
        self.assertEqual(action['res_model'], 'ksw.deduction.line')
        self.assertEqual(action['domain'], [
            ('employee_id', '=', self.employee.id),
            ('state', '=', 'pending'),
            ('deduction_id.state', '=', 'active'),
        ])
        lines = self.env['ksw.deduction.line'].search(action['domain'])
        self.assertEqual(sum(lines.mapped('amount')),
                         d.x_emp_outstanding_total)

    def test_line_action_open_deduction(self):
        d = self._make_deduction(amount=400.0, installments=4,
                                 start_month=self.this_month)
        d.action_submit()
        line = d.line_ids[0]
        action = line.action_open_deduction()
        self.assertEqual(action['res_model'], 'ksw.deduction')
        self.assertEqual(action['res_id'], d.id)
        self.assertEqual(action['view_mode'], 'form')

    def test_action_view_deductions_returns_action(self):
        action = self.employee.action_view_deductions()
        self.assertEqual(action['res_model'], 'ksw.deduction')
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['domain'], [('employee_id', '=', self.employee.id)])
        self.assertEqual(action['context']['default_employee_id'], self.employee.id)
        self.assertIn('list', action['view_mode'])
        self.assertIn('form', action['view_mode'])
