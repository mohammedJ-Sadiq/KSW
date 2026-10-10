"""Mark Paid one paying bank account at a time.

Each account is its own transfer. The wizard marks the register lines of
the ticked accounts paid; the run turns Paid with the last one. A paid
line is as final as a paid run: ``_months_paid_to`` reads either, so a
vacation never pays that month again, and the month cannot be reopened.
"""
from odoo.exceptions import UserError

from .test_vacation_commission_settlement import _SettlementCommon, JUL


class TestMarkPaidByBank(_SettlementCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        partner = cls.env.company.partner_id
        Bank = cls.env['res.partner.bank']
        cls.bank_a = Bank.create({'acc_number': 'MPB-WPS-A', 'partner_id': partner.id})
        cls.bank_b = Bank.create({'acc_number': 'MPB-CARD-B', 'partner_id': partner.id})
        cls.employee.sudo().x_salary_bank_account_id = cls.bank_a
        cls.colleague.sudo().x_salary_bank_account_id = cls.bank_b

    def _two_account_run(self):
        run = self._run(JUL)
        Line = self.env['ksw.pay.run.line'].sudo()
        Line.create({'run_id': run.id, 'employee_id': self.employee.id,
                     'earnings': 700.0})
        Line.create({'run_id': run.id, 'employee_id': self.colleague.id,
                     'earnings': 400.0})
        run.write({'state': 'approved'})
        return run

    def _wizard(self, run):
        action = run.action_open_mark_paid_wizard()
        self.assertEqual(action['res_model'], 'ksw.pay.run.paid.wizard')
        return self.env['ksw.pay.run.paid.wizard'].browse(action['res_id'])

    def _tick_only(self, wizard, bank):
        for line in wizard.line_ids:
            line.selected = line.bank_account_id == bank

    def _line(self, run, employee):
        return run.line_ids.filtered(lambda l: l.employee_id == employee)

    def test_wizard_lists_each_account(self):
        wizard = self._wizard(self._two_account_run())
        self.assertEqual(set(wizard.line_ids.bank_account_id.ids),
                         {self.bank_a.id, self.bank_b.id})
        self.assertTrue(all(wizard.line_ids.mapped('selected')))

    def test_one_account_paid_keeps_the_run_open(self):
        run = self._two_account_run()
        wizard = self._wizard(run)
        self._tick_only(wizard, self.bank_a)
        wizard.action_confirm()
        self.assertEqual(run.state, 'approved')
        self.assertTrue(self._line(run, self.employee).x_paid)
        self.assertFalse(self._line(run, self.colleague).x_paid)
        Run = self.env['ksw.pay.run']
        self.assertEqual(Run._months_paid_to(self.employee, {JUL}), {JUL})
        self.assertEqual(Run._months_paid_to(self.colleague, {JUL}), set())
        self.assertNotIn(self.jul, self._leave()._commission_entries_to_settle(),
                         'A vacation must not pay a month his account already paid.')

    def test_last_account_turns_the_run_paid(self):
        run = self._two_account_run()
        wizard = self._wizard(run)
        self._tick_only(wizard, self.bank_a)
        wizard.action_confirm()
        # Only one account left: Mark Paid marks it paid straight away.
        run.action_open_mark_paid_wizard()
        self.assertEqual(run.state, 'paid')
        self.assertTrue(all(run.line_ids.mapped('x_paid')))

    def test_partly_paid_month_cannot_be_reopened(self):
        run = self._two_account_run()
        wizard = self._wizard(run)
        self._tick_only(wizard, self.bank_b)
        wizard.action_confirm()
        self.assertFalse(run.x_can_reopen)
        with self.assertRaises(UserError):
            run.action_reopen()

    def test_nothing_ticked_is_refused(self):
        wizard = self._wizard(self._two_account_run())
        wizard.line_ids.write({'selected': False})
        with self.assertRaises(UserError):
            wizard.action_confirm()

    def test_paid_entry_cannot_be_reopened(self):
        run = self._two_account_run()
        wizard = self._wizard(run)
        self._tick_only(wizard, self.bank_a)
        wizard.action_confirm()
        self.assertIn(self.jul, self.jul._paid_entries())
