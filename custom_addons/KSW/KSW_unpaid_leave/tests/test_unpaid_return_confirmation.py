# -*- coding: utf-8 -*-
"""Unpaid leave uses the same return confirmation as an annual vacation.

Nobody but the direct manager knows the day the employee actually came back,
and on unpaid leave that date is the line between a deducted day and a paid
one. So an approved unpaid leave now opens `x_return_state = 'on_vacation'`,
the DM is told once that closing it is theirs to do, and payroll skips the
employee until they do (the payroll half is tested in
`KSW_payroll/tests/test_unpaid_return_gate.py` — the gate lives there).

Confirming the return also *shortens* the request to the real return date,
which is the opposite of the annual case: an annual vacation is paid up front
so its duration is deliberately preserved, while unpaid days are deducted in
arrears, so every day trimmed is a day paid again.
"""
from datetime import date

from odoo.tests.common import TransactionCase


class TestUnpaidReturnConfirmation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Unpaid Return Test Calendar',
            'tz': 'Asia/Riyadh',
        })

        cls.manager_user = cls.env['res.users'].create({
            'name': 'Unpaid Return Test Manager',
            'login': 'unpaid_return_dm',
            'email': 'unpaid_return_dm@example.com',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Unpaid Return Test Employee',
            'resource_calendar_id': cls.calendar.id,
            'tz': 'Asia/Riyadh',
            'leave_manager_id': cls.manager_user.id,
        })

        cls.unpaid_type = cls.env['hr.leave.type'].create({
            'name': 'Test Unpaid Leave (return)',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
            'is_unpaid_leave': True,
        })
        cls.paid_type = cls.env['hr.leave.type'].create({
            'name': 'Test Sick Leave (return)',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
        })

        cls.date_from = date(2026, 8, 3)
        cls.date_to = date(2026, 8, 20)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _leave(self, leave_type=None, date_to=None):
        return self.env['hr.leave'].with_context(
            tracking_disable=True, mail_create_nosubscribe=True,
        ).create({
            'employee_id': self.employee.id,
            'holiday_status_id': (leave_type or self.unpaid_type).id,
            'request_date_from': self.date_from,
            'request_date_to': date_to or self.date_to,
        })

    # ------------------------------------------------------------------
    # The gate opens on approval
    # ------------------------------------------------------------------
    def test_approved_unpaid_leave_awaits_a_return_confirmation(self):
        leave = self._leave()
        self.assertEqual(leave.state, 'validate')
        self.assertEqual(
            leave.x_return_state, 'on_vacation',
            'An approved unpaid leave must wait for the manager to confirm '
            'the employee actually came back.',
        )

    def test_the_direct_manager_is_asked_once(self):
        leave = self._leave()
        dm_partner = self.manager_user.partner_id
        asked = leave.message_ids.filtered(
            lambda m: dm_partner in m.partner_ids
            and 'Confirm the Return' in (m.body or ''))
        self.assertEqual(
            len(asked), 1,
            'The DM is told once, at approval — a gate nobody was told '
            'about is just a stuck payroll.',
        )

    def test_a_paid_leave_opens_no_gate(self):
        """The control: an ordinary leave type is unaffected."""
        leave = self._leave(leave_type=self.paid_type)
        self.assertEqual(leave.x_return_state, 'not_applicable')

    # ------------------------------------------------------------------
    # Only the DM can close it
    # ------------------------------------------------------------------
    def test_only_the_leave_manager_may_confirm(self):
        leave = self._leave()
        leave.sudo().write({'x_return_date': date(2026, 8, 21)})
        # with_user + sudo: the compute reads env.uid, while sudo only widens
        # the record-rule scope so a plain user can read the leave at all
        # (gotcha #16).
        self.assertTrue(
            leave.with_user(self.manager_user).sudo()
            .x_can_confirm_return_manager)

        other = self.env['res.users'].create({
            'name': 'Somebody Else', 'login': 'unpaid_return_other',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.assertFalse(
            leave.with_user(other).sudo().x_can_confirm_return_manager)

    def test_confirming_the_return_closes_the_gate(self):
        leave = self._leave()
        leave.sudo().write({'x_return_date': date(2026, 8, 21)})
        leave.with_user(self.manager_user).sudo().\
            action_confirm_return_manager()

        self.assertEqual(leave.x_return_state, 'hr_confirmed')
        self.assertEqual(
            leave.x_manager_return_confirmed_by,
            self.manager_user.employee_id)

    # ------------------------------------------------------------------
    # An early return gives the days back
    # ------------------------------------------------------------------
    def test_an_early_return_shortens_the_unpaid_period(self):
        leave = self._leave()
        full_days = leave.number_of_days
        self.assertEqual(full_days, 18)  # 3 → 20 Aug, calendar days

        # Back on the 11th: the leave should end on the 10th.
        leave.sudo().write({'x_return_date': date(2026, 8, 11)})
        leave.with_user(self.manager_user).sudo().\
            action_confirm_return_manager()

        self.assertEqual(leave.request_date_to, date(2026, 8, 10))
        self.assertEqual(
            leave.number_of_days, 8,
            'Unpaid days are deducted in arrears, so shortening the period '
            'must reduce the duration — every trimmed day is paid again.',
        )

    def test_a_late_return_is_not_an_extension(self):
        leave = self._leave()
        leave.sudo().write({'x_return_date': date(2026, 8, 30)})
        leave.with_user(self.manager_user).sudo().\
            action_confirm_return_manager()

        self.assertEqual(leave.request_date_to, self.date_to)
        self.assertEqual(leave.number_of_days, 18)

    def test_returning_on_the_planned_day_changes_nothing(self):
        leave = self._leave()
        leave.sudo().write({'x_return_date': date(2026, 8, 21)})
        leave.with_user(self.manager_user).sudo().\
            action_confirm_return_manager()

        self.assertEqual(leave.request_date_to, self.date_to)
        self.assertEqual(leave.number_of_days, 18)

    def test_the_annual_accrual_is_not_restarted(self):
        """Unpaid leave is not a settlement of the annual balance.

        `_sync_opening_reset_to_return` restarts the accrual from zero on the
        return date, which is right for a vacation and would wipe the
        employee's balance for taking leave that paid them nothing.
        """
        leave = self._leave()
        balance = self.env['ksw.annual.leave'].sudo().search(
            [('employee_id', '=', self.employee.id)], limit=1)
        before = balance.x_opening_reset_date if balance else None

        leave.sudo().write({'x_return_date': date(2026, 8, 11)})
        leave.with_user(self.manager_user).sudo().\
            action_confirm_return_manager()

        if balance:
            balance.invalidate_recordset()
            self.assertEqual(balance.x_opening_reset_date, before)

    # ------------------------------------------------------------------
    # Reversals clear the gate
    # ------------------------------------------------------------------
    def test_refusing_clears_the_pending_return(self):
        leave = self._leave()
        self.assertEqual(leave.x_return_state, 'on_vacation')
        leave.sudo().action_refuse()
        self.assertEqual(
            leave.x_return_state, 'not_applicable',
            'A refused request has no return to confirm, and must not go on '
            'blocking the payslip.',
        )
