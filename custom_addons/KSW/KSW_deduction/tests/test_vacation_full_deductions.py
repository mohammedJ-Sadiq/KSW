# -*- coding: utf-8 -*-
"""Vacation / EOS payslips present the WHOLE deduction obligation.

An ordinary monthly payslip caps its KSW deduction inputs so the net never
goes negative. A vacation / EOS payslip must NOT: the accountant reviewing
the leave needs the employee's real position, so the full amount stays on
the document even when that drives the net negative — and only the part the
pay could actually absorb is ever settled as paid.
"""
from datetime import date

from .common import DeductionCommon


class TestVacationFullDeductions(DeductionCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.calendar_group = cls.env['resource.calendar.group'].create({
            'name': 'KSWVFD Sched'})
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
            'name': 'KSWVFD Cal',
            'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, cls.calendar_group.id)],
        })
        cls.employee.write({'resource_calendar_id': cls.calendar.id})
        cls.struct = cls.env.ref('om_hr_payroll.structure_base')
        cls.version = cls.employee.current_version_id
        cls.version.write({
            'name': 'KSWVFD Version',
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
        # Fully-attended sheet → ATTDED = 0 → net == wage (2500), so the
        # shortfall under test is caused by the deductions and nothing else.
        cls.employee.write({'x_is_attendance_sheet': True})
        cls.env['ksw.attendance.sheet'].create({
            'employee_id': cls.employee.id, 'month': '4', 'year': 2026})

        # A leave to hang the payslip off. Its dates sit AFTER the payslip
        # period on purpose: `KSW_payroll.compute_sheet` caps the attendance
        # window to the day before a leave that starts inside the period,
        # which would inject absent days and move the net for reasons that
        # have nothing to do with this test.
        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'KSWVFD Leave Type',
            'requires_allocation': False,
            'leave_validation_type': 'hr',
        })
        cls.leave = cls.env['hr.leave'].create({
            'name': 'KSWVFD Leave',
            'employee_id': cls.employee.id,
            'holiday_status_id': cls.leave_type.id,
            'request_date_from': date(2026, 6, 1),
            'request_date_to': date(2026, 6, 10),
        })

    def _make_payslip(self, leave=None, name='VFD Slip'):
        return self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'name': name,
            'date_from': self.period_from,
            'date_to': self.period_to,
            'struct_id': self.struct.id,
            'version_id': self.version.id,
            'x_leave_id': leave.id if leave else False,
        })

    def _ksw_inputs(self, slip):
        return slip.input_line_ids.filtered(
            lambda i: i.code and i.code.startswith('KSW_DED_'))

    def _unaffordable_deductions(self):
        """3000 penalty + 2000 advance against a 2500 salary."""
        pen = self._make_deduction(self.type_gov_pen, amount=3000.0,
                                   installments=1,
                                   start_month=date(2026, 4, 1))
        pen.action_submit()
        adv = self._make_deduction(self.type_advance, amount=2000.0,
                                   installments=1,
                                   start_month=date(2026, 4, 1))
        adv.action_submit()
        return pen, adv

    # ------------------------------------------------------------------
    # Presentation: the whole obligation stays on the document
    # ------------------------------------------------------------------

    def test_vacation_payslip_keeps_full_amounts_and_goes_negative(self):
        cur = self.env.company.currency_id
        pen, adv = self._unaffordable_deductions()

        slip = self._make_payslip(leave=self.leave)
        slip.compute_sheet()

        inputs = self._ksw_inputs(slip)
        self.assertEqual(len(inputs), 2)
        pen_inp = inputs.filtered(
            lambda i: i.code == f'KSW_DED_{pen.line_ids.id}')
        adv_inp = inputs.filtered(
            lambda i: i.code == f'KSW_DED_{adv.line_ids.id}')

        # Nothing capped — the full obligation is visible.
        self.assertEqual(cur.round(pen_inp.amount), 3000.0)
        self.assertEqual(cur.round(adv_inp.amount), 2000.0)

        # ...which is precisely why the net is allowed to go negative.
        net = slip.get_salary_line_total('NET')
        self.assertLess(cur.round(net), 0.0)

        # The KSW_DEDUCTIONS salary line carries the full 5000.
        ksw_line = slip.line_ids.filtered(lambda l: l.code == 'KSW_DEDUCTIONS')
        self.assertEqual(cur.round(ksw_line.total), -5000.0)

    def test_ordinary_payslip_still_caps(self):
        """The old behaviour is untouched off a leave-linked payslip."""
        cur = self.env.company.currency_id
        self._unaffordable_deductions()

        slip = self._make_payslip(leave=None, name='Ordinary Slip')
        slip.compute_sheet()

        net = slip.get_salary_line_total('NET')
        self.assertGreaterEqual(cur.round(net), 0.0)
        inputs = self._ksw_inputs(slip)
        self.assertLess(cur.round(sum(inputs.mapped('amount'))), 5000.0)
        # Capped, not carried: an ordinary payslip records no shortfall.
        self.assertEqual(cur.round(sum(
            i.x_ksw_uncollected for i in inputs)), 0.0)

    def test_uncollected_records_the_shortfall_in_priority_order(self):
        cur = self.env.company.currency_id
        pen, adv = self._unaffordable_deductions()

        slip = self._make_payslip(leave=self.leave)
        slip.compute_sheet()
        inputs = self._ksw_inputs(slip)
        pen_inp = inputs.filtered(
            lambda i: i.code == f'KSW_DED_{pen.line_ids.id}')
        adv_inp = inputs.filtered(
            lambda i: i.code == f'KSW_DED_{adv.line_ids.id}')

        # Available before KSW = net + everything presented.
        available = slip.get_salary_line_total('NET') + 5000.0
        self.assertGreater(available, 0.0)
        self.assertLess(available, 5000.0)

        # The penalty (payroll_priority 10) is funded first; the advance
        # (30) absorbs the shortfall.
        collected_pen = min(3000.0, available)
        collected_adv = max(available - 3000.0, 0.0)
        self.assertEqual(cur.round(pen_inp.x_ksw_uncollected),
                         cur.round(3000.0 - collected_pen))
        self.assertEqual(cur.round(adv_inp.x_ksw_uncollected),
                         cur.round(2000.0 - collected_adv))

    def test_no_shortfall_leaves_uncollected_at_zero(self):
        """An affordable vacation payslip records nothing carried."""
        cur = self.env.company.currency_id
        ded = self._make_deduction(self.type_advance, amount=300.0,
                                   installments=1,
                                   start_month=date(2026, 4, 1))
        ded.action_submit()

        slip = self._make_payslip(leave=self.leave)
        slip.compute_sheet()
        inputs = self._ksw_inputs(slip)
        self.assertEqual(cur.round(inputs.amount), 300.0)
        self.assertEqual(cur.round(inputs.x_ksw_uncollected), 0.0)
        self.assertGreaterEqual(
            cur.round(slip.get_salary_line_total('NET')), 0.0)

    # ------------------------------------------------------------------
    # Settlement: only what was consumed is marked done
    # ------------------------------------------------------------------

    def test_only_the_consumed_part_is_marked_paid(self):
        cur = self.env.company.currency_id
        pen, adv = self._unaffordable_deductions()

        slip = self._make_payslip(leave=self.leave)
        slip.compute_sheet()
        inputs = self._ksw_inputs(slip)
        pen_inp = inputs.filtered(
            lambda i: i.code == f'KSW_DED_{pen.line_ids.id}')
        adv_inp = inputs.filtered(
            lambda i: i.code == f'KSW_DED_{adv.line_ids.id}')
        collected_pen = pen_inp.amount - pen_inp.x_ksw_uncollected
        collected_adv = adv_inp.amount - adv_inp.x_ksw_uncollected

        slip.action_payslip_done()
        self.assertEqual(slip.state, 'done')

        # Penalty: partially consumed → paid portion + forwarded remainder,
        # and the schedule still totals the original 3000.
        pen_paid = pen.line_ids.filtered(lambda l: l.state == 'paid')
        pen_pending = pen.line_ids.filtered(lambda l: l.state == 'pending')
        self.assertEqual(cur.round(sum(pen.line_ids.mapped('amount'))), 3000.0)
        self.assertEqual(cur.round(pen_paid.amount), cur.round(collected_pen))
        self.assertTrue(pen_pending)
        self.assertEqual(cur.round(pen_pending.amount),
                         cur.round(3000.0 - collected_pen))
        self.assertEqual(pen_pending.forwarded_from_payslip_id, slip)

        # Advance: nothing consumed → untouched and still pending. This is
        # the assertion the whole feature exists for — the 2000 shown on the
        # payslip must NOT be read as collected.
        self.assertEqual(cur.round(collected_adv), 0.0)
        self.assertEqual(adv.line_ids.state, 'pending')
        self.assertEqual(len(adv.line_ids), 1)
        self.assertEqual(cur.round(adv.line_ids.amount), 2000.0)
        self.assertEqual(adv.state, 'active')

    def test_reset_merges_the_split_back(self):
        pen, adv = self._unaffordable_deductions()
        cur = self.env.company.currency_id
        slip = self._make_payslip(leave=self.leave)
        slip.compute_sheet()
        slip.action_payslip_done()

        slip.write({'state': 'draft'})
        self.assertEqual(len(pen.line_ids), 1)
        self.assertEqual(pen.line_ids.state, 'pending')
        self.assertEqual(cur.round(pen.line_ids.amount), 3000.0)
        self.assertFalse(pen.line_ids.payslip_id)
        self.assertEqual(adv.line_ids.state, 'pending')

    def test_remainder_is_collected_by_the_next_payslip(self):
        """Carried-forward is not written off — the next run picks it up."""
        pen, adv = self._unaffordable_deductions()
        slip = self._make_payslip(leave=self.leave)
        slip.compute_sheet()
        slip.action_payslip_done()

        pending = (pen.line_ids | adv.line_ids).filtered(
            lambda l: l.state == 'pending')
        self.assertTrue(pending)

        may = self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'name': 'VFD May Slip',
            'date_from': date(2026, 5, 1),
            'date_to': date(2026, 5, 31),
            'struct_id': self.struct.id,
            'version_id': self.version.id,
        })
        may.compute_sheet()
        codes = [i.code for i in self._ksw_inputs(may)]
        for line in pending:
            self.assertIn(f'KSW_DED_{line.id}', codes)

    # ------------------------------------------------------------------
    # The summary the accountant reads
    # ------------------------------------------------------------------

    def test_payslip_coverage_summary(self):
        cur = self.env.company.currency_id
        self._unaffordable_deductions()
        slip = self._make_payslip(leave=self.leave)
        slip.compute_sheet()

        self.assertEqual(cur.round(slip.x_ksw_ded_presented), 5000.0)
        self.assertEqual(
            cur.round(slip.x_ksw_ded_collected + slip.x_ksw_ded_carried),
            5000.0)
        self.assertGreater(cur.round(slip.x_ksw_ded_carried), 0.0)
        # Whole obligation of the employee, not just this payslip's slice.
        self.assertEqual(cur.round(slip.x_ksw_ded_outstanding), 5000.0)

    def test_leave_deduction_picture(self):
        cur = self.env.company.currency_id
        self._unaffordable_deductions()
        slip = self._make_payslip(leave=self.leave)
        slip.compute_sheet()

        leave = self.leave
        leave.invalidate_recordset()
        self.assertEqual(cur.round(leave.x_ksw_ded_outstanding), 5000.0)
        self.assertTrue(leave.x_ksw_ded_has_calc)
        self.assertEqual(cur.round(leave.x_ksw_ded_presented), 5000.0)
        self.assertTrue(leave.x_ksw_ded_shortfall)
        self.assertEqual(
            cur.round(leave.x_ksw_ded_collected + leave.x_ksw_ded_carried),
            5000.0)

    def test_leave_picture_without_a_calculation(self):
        """The outstanding total shows before any payslip exists."""
        cur = self.env.company.currency_id
        self._unaffordable_deductions()
        leave = self.leave
        leave.invalidate_recordset()
        self.assertFalse(leave.x_ksw_ded_has_calc)
        self.assertFalse(leave.x_ksw_ded_shortfall)
        self.assertEqual(cur.round(leave.x_ksw_ded_outstanding), 5000.0)
        self.assertEqual(cur.round(leave.x_ksw_ded_presented), 0.0)

    # ------------------------------------------------------------------
    # The whole balance, not just the installment due this month
    # ------------------------------------------------------------------

    def test_future_installments_are_pulled_forward(self):
        """KSWCO leave 5100: an EOS payslip presented 375 of a 1,500 loan
        because the other three installments were not due yet — and the
        employee is leaving, so they would never have been collected."""
        cur = self.env.company.currency_id
        ded = self._make_deduction(self.type_advance, amount=1500.0,
                                   installments=4,
                                   start_month=date(2026, 4, 1))
        ded.action_submit()
        self.assertEqual(len(ded.line_ids), 4)

        slip = self._make_payslip(leave=self.leave)   # April payslip
        slip.compute_sheet()
        inputs = self._ksw_inputs(slip)

        self.assertEqual(len(inputs), 4, 'the whole schedule is presented')
        self.assertEqual(cur.round(sum(inputs.mapped('amount'))), 1500.0)
        # The ones ahead of schedule say so.
        pulled = inputs.filtered(lambda i: 'pulled forward' in i.name)
        self.assertEqual(len(pulled), 3)

    def test_ordinary_payslip_still_only_takes_what_is_due(self):
        """The monthly run is untouched — the April installment only.

        Asserted on which lines are *presented*, not on the amount: an
        ordinary payslip in this fixture computes a fully-absent month, so
        the capping takes the amount to 0. That is the pre-existing quirk
        behind the stale failures in TestPayrollPriority — irrelevant here,
        since what is under test is the domain, not the arithmetic.
        """
        ded = self._make_deduction(self.type_advance, amount=1500.0,
                                   installments=4,
                                   start_month=date(2026, 4, 1))
        ded.action_submit()

        slip = self._make_payslip(leave=None, name='Ordinary Slip')
        slip.compute_sheet()
        inputs = self._ksw_inputs(slip)
        self.assertEqual(len(inputs), 1, 'only the installment due in April')
        april = ded.line_ids.filtered(
            lambda l: l.period_date == date(2026, 4, 1))
        self.assertEqual(inputs.code, f'KSW_DED_{april.id}')
        self.assertNotIn('pulled forward', inputs.name)

    def test_pulled_forward_installments_are_settled(self):
        """What the settlement can afford is paid — across the whole
        schedule, not just the installment that happened to be due."""
        cur = self.env.company.currency_id
        ded = self._make_deduction(self.type_advance, amount=1500.0,
                                   installments=4,
                                   start_month=date(2026, 4, 1))
        ded.action_submit()
        slip = self._make_payslip(leave=self.leave)
        slip.compute_sheet()

        inputs = self._ksw_inputs(slip)
        available = max(
            slip.get_salary_line_total('NET') + sum(inputs.mapped('amount')),
            0.0)
        slip._ksw_auto_confirm_leave_payslip()

        paid = sum(ded.line_ids.filtered(
            lambda l: l.state == 'paid').mapped('amount'))
        # Everything the pay could absorb, capped at the whole balance.
        self.assertEqual(cur.round(paid), cur.round(min(1500.0, available)))
        # The schedule still totals the loan either way.
        self.assertEqual(cur.round(sum(ded.line_ids.mapped('amount'))), 1500.0)
        if cur.compare_amounts(available, 1500.0) >= 0:
            self.assertEqual(ded.state, 'completed',
                             'an affordable settlement closes the loan')
        else:
            # More than the one installment that was due still got settled.
            self.assertGreater(cur.round(paid), 375.0)

    def test_manual_duplicate_is_flagged(self):
        """Accounting typed the balance in by hand to compensate; now that
        payroll takes it automatically, that is a double charge."""
        ded = self._make_deduction(self.type_advance, amount=1500.0,
                                   installments=4,
                                   start_month=date(2026, 4, 1))
        ded.action_submit()
        leave = self.leave
        leave.invalidate_recordset()
        self.assertFalse(leave.x_ksw_ded_manual_duplicate)

        leave.sudo().write({'x_remaining_loans': 1500.0})
        leave.invalidate_recordset()
        self.assertTrue(leave.x_ksw_ded_manual_duplicate)
        self.assertEqual(leave.x_ksw_ded_manual_total, 1500.0)

    # ------------------------------------------------------------------
    # Auto-confirm on generation, release on reversal
    # ------------------------------------------------------------------

    def test_auto_confirm_settles_and_cancel_releases(self):
        """A confirmed settlement consumes the installments; cancelling it
        puts them back — the whole point of confirming at generation."""
        cur = self.env.company.currency_id
        ded = self._make_deduction(self.type_advance, amount=300.0,
                                   installments=1,
                                   start_month=date(2026, 4, 1))
        ded.action_submit()

        slip = self._make_payslip(leave=self.leave)
        slip.compute_sheet()
        # Stand in for _create_vacation_payslip's definitive run.
        slip._ksw_auto_confirm_leave_payslip()

        self.assertEqual(slip.state, 'done')
        self.assertTrue(slip.x_vacation_auto_confirmed)
        self.assertEqual(ded.line_ids.state, 'paid')
        self.assertEqual(ded.line_ids.payslip_id, slip)

        # Reversal: cancelling releases what it collected.
        slip.sudo().write({'state': 'cancel'})
        self.assertEqual(ded.line_ids.state, 'pending')
        self.assertFalse(ded.line_ids.payslip_id)
        self.assertEqual(cur.round(ded.line_ids.amount), 300.0)

    def test_auto_confirm_settles_only_the_affordable_part(self):
        """The two halves together: full amounts shown, part paid."""
        cur = self.env.company.currency_id
        pen, adv = self._unaffordable_deductions()

        slip = self._make_payslip(leave=self.leave)
        slip.compute_sheet()
        slip._ksw_auto_confirm_leave_payslip()

        self.assertEqual(slip.state, 'done')
        # Still the whole obligation on the document...
        self.assertEqual(cur.round(slip.x_ksw_ded_presented), 5000.0)
        # ...but the advance was never consumed, so it is still pending.
        self.assertEqual(adv.line_ids.state, 'pending')
        self.assertEqual(cur.round(adv.line_ids.amount), 2000.0)
        self.assertTrue(pen.line_ids.filtered(lambda l: l.state == 'paid'))

    def test_auto_confirm_is_idempotent(self):
        slip = self._make_payslip(leave=self.leave)
        slip.compute_sheet()
        slip._ksw_auto_confirm_leave_payslip()
        slip._ksw_auto_confirm_leave_payslip()
        self.assertEqual(slip.state, 'done')

    def test_manual_confirm_is_not_flagged_auto(self):
        """The flag is what tells a hand-confirmed payslip apart, and a
        reversal refuses on that one."""
        slip = self._make_payslip(leave=self.leave)
        slip.compute_sheet()
        slip.sudo().action_payslip_done()
        self.assertEqual(slip.state, 'done')
        self.assertFalse(slip.x_vacation_auto_confirmed)

    def test_leave_drill_down_domains(self):
        pen, adv = self._unaffordable_deductions()
        action = self.leave.action_view_leave_outstanding_installments()
        lines = self.env['ksw.deduction.line'].search(action['domain'])
        self.assertEqual(lines, pen.line_ids | adv.line_ids)
