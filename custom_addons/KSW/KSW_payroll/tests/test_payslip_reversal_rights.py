# -*- coding: utf-8 -*-
"""Only a Payroll Manager may reverse a payslip.

Prod incident (KSWCO, 2026-07-09): payslip SLIP/11307 — the June 2026
batch had been confirmed by admin three days earlier — was moved to
``cancel`` ("Rejected" in om_hr_payroll's labelling) by a user holding
only ``om_hr_payroll.group_hr_payroll_user``. The stock "Cancel Payslip",
"Set to Draft" and "Refund" buttons carry no ``groups=`` and stay visible
in the ``done`` state, and ``action_payslip_cancel`` is a bare
``write({'state': 'cancel'})`` with no chatter post — so nothing recorded
who or why beyond ``write_uid`` / ``write_date``.

The view-level ``groups=`` added alongside this is cosmetic; these tests
cover the server-side guard, which is what actually holds over RPC.
"""
from datetime import date

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestPayslipReversalRights(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.group_user = cls.env.ref('base.group_user')
        cls.group_officer = cls.env.ref('om_hr_payroll.group_hr_payroll_user')
        cls.group_manager = cls.env.ref(
            'om_hr_payroll.group_hr_payroll_manager')

        def _mkuser(name, login, groups):
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': '%s@revrights.test' % login,
                'group_ids': [(6, 0, [g.id for g in groups])],
            })

        cls.user_officer = _mkuser(
            'Payroll Officer', 'revrights_officer',
            [cls.group_user, cls.group_officer])
        cls.user_manager = _mkuser(
            'Payroll Manager', 'revrights_manager',
            [cls.group_user, cls.group_manager])

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Reversal Rights Employee',
        })

    def _new_payslip(self):
        return self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'name': 'Payslip for reversal-rights test',
            'date_from': date(2026, 6, 1),
            'date_to': date(2026, 6, 30),
        })

    # ------------------------------------------------------------------
    # Officer is blocked on all three reversal actions
    # ------------------------------------------------------------------

    def test_officer_cannot_cancel_payslip(self):
        """The exact prod scenario: a confirmed payslip, an Officer, Cancel."""
        slip = self._new_payslip()
        slip.write({'state': 'done'})
        with self.assertRaises(UserError):
            slip.with_user(self.user_officer).action_payslip_cancel()
        self.assertEqual(slip.state, 'done')

    def test_officer_cannot_reset_payslip_to_draft(self):
        slip = self._new_payslip()
        slip.write({'state': 'done'})
        with self.assertRaises(UserError):
            slip.with_user(self.user_officer).action_payslip_draft()
        self.assertEqual(slip.state, 'done')

    def test_officer_cannot_refund_payslip(self):
        slip = self._new_payslip()
        slip.write({'state': 'done'})
        with self.assertRaises(UserError):
            slip.with_user(self.user_officer).refund_sheet()

    def test_officer_cannot_reopen_batch(self):
        run = self.env['hr.payslip.run'].create({
            'name': 'Reversal Rights Batch',
            'date_start': date(2026, 6, 1),
            'date_end': date(2026, 6, 30),
        })
        run.write({'state': 'close'})
        with self.assertRaises(UserError):
            run.with_user(self.user_officer).draft_payslip_run()
        self.assertEqual(run.state, 'close')

    # ------------------------------------------------------------------
    # Manager keeps every reversal action
    # ------------------------------------------------------------------

    def test_manager_can_cancel_payslip(self):
        slip = self._new_payslip()
        slip.write({'state': 'done'})
        slip.with_user(self.user_manager).action_payslip_cancel()
        self.assertEqual(slip.state, 'cancel')

    def test_manager_can_reset_payslip_to_draft(self):
        slip = self._new_payslip()
        slip.write({'state': 'done'})
        slip.with_user(self.user_manager).action_payslip_draft()
        self.assertEqual(slip.state, 'draft')

    def test_manager_can_reopen_batch(self):
        run = self.env['hr.payslip.run'].create({
            'name': 'Reversal Rights Batch (manager)',
            'date_start': date(2026, 6, 1),
            'date_end': date(2026, 6, 30),
        })
        run.write({'state': 'close'})
        run.with_user(self.user_manager).draft_payslip_run()
        self.assertEqual(run.state, 'draft')

    # ------------------------------------------------------------------
    # Server code (sudo) is not affected
    # ------------------------------------------------------------------

    def test_sudo_is_exempt(self):
        """`self.env.su` short-circuits the guard, so an internal flow that
        legitimately cancels a payslip as a side effect keeps working for a
        non-manager caller. `sudo()` is not reachable over RPC."""
        slip = self._new_payslip()
        slip.write({'state': 'done'})
        slip.with_user(self.user_officer).sudo().action_payslip_cancel()
        self.assertEqual(slip.state, 'cancel')
