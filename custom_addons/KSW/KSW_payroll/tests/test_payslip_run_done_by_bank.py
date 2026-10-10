# -*- coding: utf-8 -*-
"""Mark As Done one paying bank account at a time.

Only the payslips paid from the ticked accounts are confirmed; the batch
stays in draft until the last account is done.
"""
from datetime import date

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestPayslipRunDoneByBank(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        partner = cls.env.company.partner_id
        Bank = cls.env['res.partner.bank']
        cls.bank_a = Bank.create({'acc_number': 'RDB-WPS-A', 'partner_id': partner.id})
        cls.bank_b = Bank.create({'acc_number': 'RDB-CARD-B', 'partner_id': partner.id})
        cls.emp_a, cls.emp_b = cls.env['hr.employee'].create([
            {'name': 'Done By Bank A', 'x_salary_bank_account_id': cls.bank_a.id},
            {'name': 'Done By Bank B', 'x_salary_bank_account_id': cls.bank_b.id},
        ])

    def setUp(self):
        super().setUp()
        self.run = self.env['hr.payslip.run'].create({
            'name': 'Done By Bank Batch',
            'date_start': date(2026, 6, 1),
            'date_end': date(2026, 6, 30),
        })
        self.slips = self.env['hr.payslip'].create([{
            'employee_id': emp.id,
            'name': 'Done by bank slip',
            'date_from': date(2026, 6, 1),
            'date_to': date(2026, 6, 30),
            'payslip_run_id': self.run.id,
        } for emp in (self.emp_a, self.emp_b)])
        # Confirming is not what is under test here (it recomputes a sheet
        # this fixture has no contract for): record which slips it was
        # asked to confirm, and confirm them by state alone.
        self.confirmed = self.env['hr.payslip']
        test = self

        def fake_done(slips):
            test.confirmed |= slips
            slips.write({'state': 'done'})
            return True
        self.patch(type(self.env['hr.payslip']), 'action_payslip_done', fake_done)

    def _wizard(self):
        action = self.run.action_open_done_wizard()
        self.assertEqual(action['res_model'], 'ksw.payslip.run.done.wizard')
        return self.env['ksw.payslip.run.done.wizard'].browse(action['res_id'])

    def test_wizard_lists_each_account(self):
        wizard = self._wizard()
        self.assertEqual(set(wizard.line_ids.bank_account_id.ids),
                         {self.bank_a.id, self.bank_b.id})
        self.assertTrue(all(wizard.line_ids.mapped('selected')))

    def test_one_account_confirms_only_its_slips(self):
        wizard = self._wizard()
        for line in wizard.line_ids:
            line.selected = line.bank_account_id == self.bank_a
        wizard.action_confirm()
        slip_a = self.slips.filtered(lambda s: s.employee_id == self.emp_a)
        self.assertEqual(self.confirmed, slip_a)
        self.assertEqual(self.run.state, 'draft')

    def test_last_account_marks_the_batch_done(self):
        wizard = self._wizard()
        for line in wizard.line_ids:
            line.selected = line.bank_account_id == self.bank_a
        wizard.action_confirm()
        # One account left: the button confirms it straight away.
        self.run.action_open_done_wizard()
        self.assertEqual(self.confirmed, self.slips)
        self.assertEqual(self.run.state, 'done')

    def test_all_accounts_ticked_is_the_old_behaviour(self):
        self._wizard().action_confirm()
        self.assertEqual(self.confirmed, self.slips)
        self.assertEqual(self.run.state, 'done')

    def test_nothing_ticked_is_refused(self):
        wizard = self._wizard()
        wizard.line_ids.write({'selected': False})
        with self.assertRaises(UserError):
            wizard.action_confirm()
