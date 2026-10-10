# -*- coding: utf-8 -*-
"""Confirming a payslip never changes what it pays.

This shop exports the bank file and pays before pressing Confirm (or the
batch's Mark As Done), so a computed payslip's figures are what the
employee was paid. Confirming used to recompute them: batch 250 (August
2026) and the next month both moved NETs after the money had left, e.g.
an Other Allowance raised after the batch was generated paid 4,200 on a
payslip the bank paid at 3,200.
"""
from datetime import date

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestConfirmKeepsFigures(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Confirm Keeps Figures Calendar', 'tz': 'Asia/Riyadh'})
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Confirm Keeps Figures Employee',
            'resource_calendar_id': cls.calendar.id,
        })
        cls.version = cls.employee.current_version_id
        cls.version.write({
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'resource_calendar_id': cls.calendar.id,
            'wage': 2000.0, 'hra': 600.0, 'other_allowance': 600.0,
            'travel_allowance': 0.0, 'mobile_allowance': 0.0, 'da': 0.0,
            'meal_allowance': 0.0, 'medical_allowance': 0.0,
            'struct_id': cls.env.ref('om_hr_payroll.structure_base').id,
        })
        cls.employee._compute_current_version_id()

    def _computed_slip(self):
        slip = self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'name': 'Confirm keeps figures',
            'date_from': date(2026, 7, 1),
            'date_to': date(2026, 7, 31),
            'struct_id': self.env.ref('om_hr_payroll.structure_base').id,
            'version_id': self.version.id,
        })
        slip.compute_sheet()
        return slip

    def _net(self, slip):
        return slip._ksw_net_amount()

    def _change_after_export(self, slip):
        """Something that lands after the bank file went out: a 100 SAR
        deduction a recompute would pick up (KSW_DEDUCTIONS rule)."""
        self.env['hr.payslip.input'].create({
            'name': 'Arrived after export', 'payslip_id': slip.id,
            'code': 'KSW_DED_9999', 'amount': 100.0,
            'version_id': self.version.id,
        })

    def test_confirm_keeps_the_paid_net(self):
        slip = self._computed_slip()
        paid = self._net(slip)
        self.assertTrue(paid, 'fixture must produce a NET')
        self._change_after_export(slip)
        control = slip.copy()   # carries the new input
        control.compute_sheet()
        self.assertEqual(self._net(control), paid - 100.0,
                         'a recompute WOULD change the NET')
        control.unlink()

        slip.action_payslip_done()
        self.assertEqual(slip.state, 'done')
        self.assertEqual(self._net(slip), paid)

    def test_batch_mark_as_done_keeps_the_paid_net(self):
        slip = self._computed_slip()
        run = self.env['hr.payslip.run'].create({
            'name': 'Confirm keeps figures batch',
            'date_start': date(2026, 7, 1), 'date_end': date(2026, 7, 31),
        })
        slip.payslip_run_id = run
        paid = self._net(slip)
        self._change_after_export(slip)
        run.action_open_done_wizard()
        self.assertEqual(slip.state, 'done')
        self.assertEqual(run.state, 'done')
        self.assertEqual(self._net(slip), paid)

    def test_uncomputed_slip_is_still_computed(self):
        slip = self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'name': 'Never computed',
            'date_from': date(2026, 7, 1),
            'date_to': date(2026, 7, 31),
            'struct_id': self.env.ref('om_hr_payroll.structure_base').id,
            'version_id': self.version.id,
        })
        slip.action_payslip_done()
        self.assertEqual(slip.state, 'done')
        self.assertTrue(slip.line_ids)

    def test_a_net_that_moves_on_confirm_is_refused(self):
        slip = self._computed_slip()
        paid = self._net(slip)
        Payslip = type(self.env['hr.payslip'])
        original = Payslip._stamp_paid_bank_account

        def moving_stamp(slips):
            original(slips)
            slips.line_ids.filtered(lambda l: l.code == 'NET').write(
                {'amount': paid + 500.0})
        self.patch(Payslip, '_stamp_paid_bank_account', moving_stamp)
        with self.assertRaises(UserError):
            slip.action_payslip_done()
        self.assertEqual(slip.state, 'draft')
        self.assertEqual(self._net(slip), paid)
