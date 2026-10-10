# -*- coding: utf-8 -*-
"""Deleting a payslip batch can take its payslips with it.

A plain ``unlink()`` on ``hr.payslip.run`` deletes the batch and leaves its
payslips with an empty batch. The delete dialog now offers that as "Delete
Batch Only" and adds ``action_unlink_with_payslips`` for the batch and its
payslips together. Both are covered here; the dialog itself is JS.
"""
from datetime import date

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestPayslipRunDelete(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.employees = cls.env['hr.employee'].create([
            {'name': 'Run Delete Employee A'},
            {'name': 'Run Delete Employee B'},
        ])

    def _new_run(self):
        run = self.env['hr.payslip.run'].create({
            'name': 'Run Delete Batch',
            'date_start': date(2026, 6, 1),
            'date_end': date(2026, 6, 30),
        })
        self.env['hr.payslip'].create([{
            'employee_id': emp.id,
            'name': 'Run delete slip',
            'date_from': date(2026, 6, 1),
            'date_to': date(2026, 6, 30),
            'payslip_run_id': run.id,
        } for emp in self.employees])
        return run

    def test_delete_with_payslips_removes_both(self):
        run = self._new_run()
        slips = run.slip_ids
        self.assertEqual(len(slips), 2)
        run.action_unlink_with_payslips()
        self.assertFalse(run.exists())
        self.assertFalse(slips.exists())

    def test_plain_unlink_keeps_payslips_without_batch(self):
        run = self._new_run()
        slips = run.slip_ids
        run.unlink()
        self.assertFalse(run.exists())
        self.assertEqual(len(slips.exists()), 2)
        self.assertFalse(slips.payslip_run_id)

    def test_done_payslip_blocks_and_rolls_back(self):
        run = self._new_run()
        slips = run.slip_ids
        slips[0].write({'state': 'done'})
        with self.assertRaises(UserError):
            run.action_unlink_with_payslips()
        self.assertTrue(run.exists())
        self.assertEqual(len(slips.exists()), 2)

    def test_non_draft_batch_keeps_its_payslips(self):
        run = self._new_run()
        slips = run.slip_ids
        run.write({'state': 'close'})
        with self.assertRaises(UserError):
            run.action_unlink_with_payslips()
        self.assertTrue(run.exists())
        self.assertEqual(len(slips.exists()), 2)
