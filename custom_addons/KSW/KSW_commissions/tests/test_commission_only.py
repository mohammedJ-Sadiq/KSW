"""Settle a deduction from commission only — a GM's decision, on record.

KSWCO PE00067 (Nov 2026): to keep a 15,947 SAR deduction off payroll,
accounting had rescheduled it 15 months ahead and recorded manual payments
whenever a commission was paid; the approved September commission then took
nothing because the installment was not "due". Now the status is asked for
with a reason, decided by the department GM on the request (Home ▸ Waiting
for you), and every request stays as the log.
"""
from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.exceptions import UserError

from .test_commission_priority_deduction import CommissionPriorityCommon


class TestCommissionOnly(CommissionPriorityCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.co_acc = cls._user(
            'co_accountant', 'KSW_deduction.group_installment_edit')
        cls.data_entry = cls._user(
            'co_data_entry', 'KSW_deduction.group_acc_data_entry')
        cls.data_entry_2 = cls._user(
            'co_data_entry_2', 'KSW_deduction.group_acc_data_entry')
        cls.Request = cls.env['ksw.deduction.commission.only']
        cls.Payslip = cls.env['hr.payslip']

    def _advance(self, amount, by=None, employee=None):
        Ded = self.env['ksw.deduction']
        if by:
            Ded = Ded.with_user(by).sudo()   # create_uid = by
        ded = Ded.create({
            'employee_id': (employee or self.emp_a).id,
            'type_id': self.type_advance.id,
            'amount': amount,
            'installments': 1,
            'start_month': self.period,
            'reason': 'Commission-only test',
        })
        ded.sudo().action_submit()
        self.assertEqual(ded.state, 'active')
        return ded.sudo()

    def _ask(self, ded, user=None):
        return ded.with_user(user or self.co_acc)._request_commission_only(
            'He is paid by trips; salary cannot carry it.')

    def _on_payroll(self, ded, full=False):
        date_to = fields.Date.to_date(self.period) + relativedelta(
            months=1, days=-1)
        lines = self.env['ksw.deduction.line'].sudo().search(
            self.Payslip._ksw_pending_lines_domain(
                ded.employee_id.ids, date_to, full=full))
        return bool(lines & ded.line_ids)

    # ------------------------------------------------------------------
    def test_01_payroll_keeps_deducting_until_the_gm_approves(self):
        ded = self._advance(3000.0)
        req = self._ask(ded)
        self.assertEqual(req.state, 'pending_gm')
        self.assertEqual(req.gm_user_id, self.gm)
        self.assertAlmostEqual(req.outstanding_at_request, 3000.0)
        self.assertTrue(self._on_payroll(ded), 'a request alone moves no money')

        req.with_user(self.gm).action_gm_approve()
        self.assertTrue(ded.x_commission_only)
        self.assertEqual(ded.x_commission_only_request_id, req)
        self.assertEqual(req.decided_by, self.gm)
        self.assertFalse(self._on_payroll(ded))
        self.assertTrue(self._on_payroll(ded, full=True),
                        'a vacation / EOS settlement still collects it')

    def test_02_it_waits_for_the_gm_on_home_and_nowhere_else(self):
        ded = self._advance(1000.0)
        req = self._ask(ded)
        mine = [('x_is_pending_my_action', '=', True)]
        self.assertIn(req, self.Request.with_user(self.gm).search(mine))
        self.assertNotIn(req, self.Request.with_user(self.co_acc)
                         .search(mine))
        self.assertEqual(ded.x_commission_only_pending_id, req)
        self.assertFalse(ded.with_user(self.co_acc)
                         .x_can_request_commission_only,
                         'one request at a time')
        req.with_user(self.gm).action_gm_approve()
        self.assertNotIn(req, self.Request.with_user(self.gm).search(mine))

    def test_03_only_the_department_gm_decides(self):
        ded = self._advance(1000.0)
        req = self._ask(ded)
        with self.assertRaises(UserError):
            req.with_user(self.co_acc).action_gm_approve()
        with self.assertRaises(UserError):
            ded.with_user(self.co_acc).write({'x_commission_only': True})
        with self.assertRaises(UserError):
            ded.write({'x_commission_only': True})   # not even as superuser

    def test_04_the_asker_cannot_approve_his_own_request(self):
        self.gm.sudo().write({'group_ids': [(4, self.env.ref(
            'KSW_deduction.group_installment_edit').id)]})
        ded = self._advance(1000.0)
        req = self._ask(ded, user=self.gm)
        with self.assertRaises(UserError):
            req.with_user(self.gm).action_gm_approve()

    def test_05_data_entry_asks_only_on_what_he_created(self):
        ded = self._advance(1000.0, by=self.data_entry)
        self.assertTrue(ded.with_user(self.data_entry)
                        .x_can_request_commission_only)
        self.assertFalse(ded.with_user(self.data_entry_2)
                         .x_can_request_commission_only)
        with self.assertRaises(UserError):
            self._ask(ded, user=self.data_entry_2)
        req = self._ask(ded, user=self.data_entry)
        self.assertEqual(req.requested_by, self.data_entry)

    def test_06_refusing_needs_a_note_and_changes_nothing(self):
        ded = self._advance(1000.0)
        req = self._ask(ded)
        with self.assertRaises(UserError):
            req.with_user(self.gm).action_gm_refuse()
        req.with_user(self.gm).write({'decision_note': 'Salary can carry it.'})
        req.with_user(self.gm).action_gm_refuse()
        self.assertEqual(req.state, 'refused')
        self.assertFalse(ded.x_commission_only)
        self.assertTrue(self._on_payroll(ded))

    def test_07_a_short_month_leaves_the_rest_for_the_next(self):
        ded = self._advance(3000.0)
        self._ask(ded).with_user(self.gm).action_gm_approve()

        run1 = self._approve(self._commission_entry(self.emp_a, 1000.0)
                             .batch_id)
        paid = ded.line_ids.filtered(lambda l: l.state == 'paid')
        left = ded.line_ids.filtered(lambda l: l.state == 'pending')
        self.assertAlmostEqual(sum(paid.mapped('amount')), 1000.0)
        self.assertEqual(paid.x_paid_via_pay_run_line_id.run_id, run1)
        self.assertAlmostEqual(left.amount, 2000.0)
        self.assertFalse(self._on_payroll(ded), 'still not the payslip\'s')

        next_period = fields.Date.to_date(self.period) + relativedelta(
            months=1)
        batch = self._batch(self.sup_a, self.dept_a, component=self.meals,
                            period=next_period)
        self._entry(batch, self.emp_a, user=self.sup_a,
                    quantity=1.0, amount_override=1500.0)
        self._approve(batch)
        self.assertAlmostEqual(
            sum(ded.line_ids.filtered(lambda l: l.state == 'pending')
                .mapped('amount')), 500.0)

    def test_08_back_to_payroll_needs_no_gm_and_is_logged(self):
        ded = self._advance(1000.0)
        self._ask(ded).with_user(self.gm).action_gm_approve()
        back = ded.with_user(self.co_acc)._end_commission_only(
            'Commission stopped.')
        self.assertEqual(back.state, 'done')
        self.assertEqual(back.action, 'disable')
        self.assertFalse(ded.x_commission_only)
        self.assertTrue(self._on_payroll(ded))
        self.assertEqual(ded.x_commission_only_log_count, 2)

    def test_09_the_log_cannot_be_rewritten_or_deleted(self):
        ded = self._advance(1000.0)
        req = self._ask(ded)
        with self.assertRaises(UserError):
            req.with_user(self.co_acc).write({'reason': 'changed'})
        with self.assertRaises(UserError):
            req.with_user(self.co_acc).unlink()
        req.with_user(self.gm).action_gm_approve()
        with self.assertRaises(UserError):
            req.with_user(self.gm).write({'decision_note': 'later'})
