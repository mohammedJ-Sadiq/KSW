# -*- coding: utf-8 -*-
"""Tests for the deduction critical fixes:
  1. Blocking a new personal loan while one is already in progress.
  2. Prioritised, capped, carry-forward payroll deduction collection.
"""
from datetime import date

from odoo.exceptions import UserError

from .common import DeductionCommon


class TestLoanBlock(DeductionCommon):
    def _activate_loan(self, amount=2000.0, installments=4):
        ded = self._make_deduction(self.type_loan, amount=amount,
                                   installments=installments)
        self._walk_loan_to_pending_gm(ded)
        ded.action_gm_approve()
        self.assertEqual(ded.state, 'active')
        return ded

    def test_block_when_active_loan_outstanding(self):
        self._activate_loan()
        second = self._make_deduction(self.type_loan, amount=500.0,
                                      installments=2)
        with self.assertRaises(UserError):
            second.action_submit()

    def test_block_when_loan_in_approval(self):
        first = self._make_deduction(self.type_loan, amount=1000.0,
                                     installments=2)
        first.action_submit()  # pending_dm
        second = self._make_deduction(self.type_loan, amount=500.0,
                                      installments=2)
        with self.assertRaises(UserError):
            second.action_submit()

    def test_allowed_after_first_fully_paid(self):
        first = self._activate_loan(amount=1000.0, installments=2)
        first.line_ids.write({'state': 'paid'})
        first.invalidate_recordset(['total_pending'])
        self.assertEqual(first.total_pending, 0.0)
        second = self._make_deduction(self.type_loan, amount=500.0,
                                      installments=2)
        # Should not raise.
        second.action_submit()
        self.assertEqual(second.approval_state, 'pending_dm')

    def test_non_loan_not_blocked(self):
        self._activate_loan()
        adv = self._make_deduction(self.type_advance, amount=300.0,
                                   installments=1)
        adv.action_submit()  # advances are not loans → no block
        self.assertEqual(adv.state, 'active')


class TestPayrollPriority(DeductionCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.calendar_group = cls.env['resource.calendar.group'].create({
            'name': 'KSWPRIO Sched'})
        for day in ['0', '1', '2', '3', '6']:
            cls.env['resource.calendar.group.line'].create({
                'name': f'd{day}',
                'calendar_group_id': cls.calendar_group.id,
                'dayofweek': day,
                'day_period': 'full_day',
                'hour_from': 8.0,
                'hour_to': 16.5,
            })
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'KSWPRIO Cal',
            'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, cls.calendar_group.id)],
        })
        cls.employee.write({'resource_calendar_id': cls.calendar.id})
        cls.struct = cls.env.ref('om_hr_payroll.structure_base')
        cls.version = cls.employee.current_version_id
        cls.version.write({
            'name': 'KSWPRIO Version',
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'resource_calendar_id': cls.calendar.id,
            'wage': 2500.0,
            'da': 0.0, 'travel_allowance': 0.0, 'meal_allowance': 0.0,
            'medical_allowance': 0.0, 'other_allowance': 0.0, 'hra': 0.0,
            'struct_id': cls.struct.id,
        })
        cls.employee._compute_current_version_id()
        cls.period_from = date(2026, 4, 1)
        cls.period_to = date(2026, 4, 30)
        # Fully-attended sheet → ATTDED = 0 → net == wage (2500). Total KSW
        # deductions in the test exceed this, so the priority capping is
        # exercised deterministically.
        cls.employee.write({'x_is_attendance_sheet': True})
        cls.env['ksw.attendance.sheet'].create({
            'employee_id': cls.employee.id, 'month': '4', 'year': 2026})

    def _make_payslip(self):
        return self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'name': 'Prio Slip',
            'date_from': self.period_from,
            'date_to': self.period_to,
            'struct_id': self.struct.id,
            'version_id': self.version.id,
        })

    def _ksw_inputs(self, slip):
        return slip.input_line_ids.filtered(
            lambda i: i.code and i.code.startswith('KSW_DED_'))

    def test_priority_cap_forward_and_reset(self):
        cur = self.env.company.currency_id
        # High-priority penalty (priority 10) and low-priority advance
        # (priority 30), both due in April, together exceeding salary.
        pen = self._make_deduction(self.type_gov_pen, amount=3000.0,
                                   installments=1,
                                   start_month=date(2026, 4, 1))
        pen.action_submit()
        adv = self._make_deduction(self.type_advance, amount=2000.0,
                                   installments=1,
                                   start_month=date(2026, 4, 1))
        adv.action_submit()

        slip = self._make_payslip()
        slip.compute_sheet()

        inputs = self._ksw_inputs(slip)
        self.assertEqual(len(inputs), 2)
        pen_line = pen.line_ids
        adv_line = adv.line_ids
        pen_inp = inputs.filtered(
            lambda i: i.code == f'KSW_DED_{pen_line.id}')
        adv_inp = inputs.filtered(
            lambda i: i.code == f'KSW_DED_{adv_line.id}')

        # Net must not go negative.
        net = slip.get_salary_line_total('NET')
        self.assertGreaterEqual(cur.round(net), 0.0)

        # Available before KSW = net + total collected. The penalty
        # (higher priority) is funded first; the advance absorbs the
        # shortfall.
        available = net + sum(inputs.mapped('amount'))
        self.assertGreater(available, 0.0)
        self.assertLess(available, 5000.0)  # precondition: capping happened
        self.assertEqual(cur.round(pen_inp.amount),
                         cur.round(min(3000.0, available)))
        self.assertEqual(cur.round(adv_inp.amount),
                         cur.round(max(available - 3000.0, 0.0)))

        collected_pen = pen_inp.amount
        collected_adv = adv_inp.amount

        slip.action_payslip_done()
        self.assertEqual(slip.state, 'done')

        # Penalty: partial → paid portion + forwarded remainder; total
        # still 3000.
        pen_paid = pen.line_ids.filtered(lambda l: l.state == 'paid')
        pen_pending = pen.line_ids.filtered(lambda l: l.state == 'pending')
        self.assertEqual(cur.round(sum(pen.line_ids.mapped('amount'))), 3000.0)
        self.assertEqual(cur.round(pen_paid.amount), cur.round(collected_pen))
        self.assertTrue(pen_pending)
        self.assertEqual(pen_pending.forwarded_from_payslip_id, slip)
        self.assertEqual(pen_pending.split_origin_id, pen_paid)

        # Advance: nothing collected → left fully pending, no split.
        self.assertEqual(cur.round(collected_adv), 0.0)
        self.assertEqual(adv.line_ids.state, 'pending')
        self.assertEqual(len(adv.line_ids), 1)

        # Reset the payslip → splits merge back, paid lines revert.
        slip.write({'state': 'draft'})
        self.assertEqual(len(pen.line_ids), 1)
        self.assertEqual(pen.line_ids.state, 'pending')
        self.assertEqual(cur.round(pen.line_ids.amount), 3000.0)
        self.assertFalse(pen.line_ids.payslip_id)
        self.assertEqual(adv.line_ids.state, 'pending')

    def test_forwarded_remainder_collected_next_month(self):
        # A pending line left over in April is picked up by the May payslip.
        pen = self._make_deduction(self.type_gov_pen, amount=3000.0,
                                   installments=1,
                                   start_month=date(2026, 4, 1))
        pen.action_submit()
        slip = self._make_payslip()
        slip.compute_sheet()
        slip.action_payslip_done()
        remainder = pen.line_ids.filtered(lambda l: l.state == 'pending')
        self.assertTrue(remainder)

        may = self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'name': 'May Slip',
            'date_from': date(2026, 5, 1),
            'date_to': date(2026, 5, 31),
            'struct_id': self.struct.id,
            'version_id': self.version.id,
        })
        may.compute_sheet()
        codes = [i.code for i in self._ksw_inputs(may)]
        self.assertIn(f'KSW_DED_{remainder.id}', codes)
