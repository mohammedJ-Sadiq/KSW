# -*- coding: utf-8 -*-
"""An unpaid leave can never be an annual-accrual restart point.

Moving `ksw.annual.leave.x_opening_reset_date` forward is a settlement: it
asserts that everything accrued up to that date has been consumed, and it
deletes it. That is true of an annual vacation and false of an unpaid leave —
the employee kept the entitlement precisely by not spending it.

KSWCO leave 4927 is the case these tests are written from. The employee
refused his annual request (4926) and took the same twelve days as *unpaid*
leave; when his manager confirmed the return on 22 Aug 2026, the balance
record was reset by hand onto that date, deleting 15.6 accrued days two days
before his end-of-service payout was computed from them. The code path was
already correct (`_apply_confirmed_return` declines to move the date for an
unpaid leave) — nothing stopped a person from doing it by hand.
"""
from datetime import date

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestOpeningResetGuard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Opening Reset Guard Calendar',
            'tz': 'Asia/Riyadh',
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Opening Reset Guard Employee',
            'resource_calendar_id': cls.calendar.id,
            'tz': 'Asia/Riyadh',
        })
        cls.employee.version_id.write({'contract_date_start': date(2024, 4, 1)})

        cls.unpaid_type = cls.env['hr.leave.type'].create({
            'name': 'Guard Unpaid Leave',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
            'is_unpaid_leave': True,
        })
        cls.annual_type = cls.env['hr.leave.type'].create({
            'name': 'Guard Annual Vacation',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
            'is_annual_leave': True,
        })
        cls.excuse_type = cls.env['hr.leave.type'].create({
            'name': 'Guard Missed Check-in',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
        })

        cls.balance = cls.env['ksw.annual.leave'].create({
            'employee_id': cls.employee.id,
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _leave(self, leave_type, date_from, date_to, return_date=None):
        leave = self.env['hr.leave'].with_context(
            tracking_disable=True, mail_create_nosubscribe=True,
        ).create({
            'employee_id': self.employee.id,
            'holiday_status_id': leave_type.id,
            'request_date_from': date_from,
            'request_date_to': date_to,
        })
        if return_date:
            leave.sudo().write({'x_return_date': return_date})
        return leave

    def _set_reset(self, new_date):
        self.balance.write({'x_opening_reset_date': new_date})

    # ------------------------------------------------------------------
    # Blocked: the date belongs to a leave that spent nothing
    # ------------------------------------------------------------------
    def test_reset_onto_an_unpaid_confirmed_return_is_refused(self):
        """The 4927 shape: a late return, after the requested end."""
        self._leave(self.unpaid_type, date(2026, 8, 5), date(2026, 8, 16),
                    return_date=date(2026, 8, 22))
        with self.assertRaises(UserError):
            self._set_reset(date(2026, 8, 22))
        self.assertFalse(self.balance.x_opening_reset_date)

    def test_reset_onto_the_day_after_an_unpaid_leave_is_refused(self):
        """HR's settlement convention is `request_date_to + 1`."""
        self._leave(self.unpaid_type, date(2026, 8, 5), date(2026, 8, 16))
        with self.assertRaises(UserError):
            self._set_reset(date(2026, 8, 17))

    def test_reset_inside_an_unpaid_leave_is_refused(self):
        self._leave(self.unpaid_type, date(2026, 8, 5), date(2026, 8, 16))
        with self.assertRaises(UserError):
            self._set_reset(date(2026, 8, 10))

    def test_a_validated_leave_blocks_even_with_an_unwalked_chain(self):
        """`state` is its own arm, not left to the chain bookkeeping.

        `_is_past_gm_final` reads `x_annual_approval_state` first for a
        multi-step type, so a validated leave whose chain was never walked
        (imported, or validated by hand) answers False there and would have
        slipped through.
        """
        leave = self._leave(self.unpaid_type, date(2026, 8, 5),
                            date(2026, 8, 16), return_date=date(2026, 8, 22))
        self.unpaid_type.sudo().write({'leave_validation_type': 'unpaid_multi'})
        leave.sudo().write({'x_annual_approval_state': False})
        self.assertEqual(leave.state, 'validate')
        self.assertFalse(leave._is_past_gm_final())
        with self.assertRaises(UserError):
            self._set_reset(date(2026, 8, 22))

    def test_the_error_names_the_leave_and_the_days_at_stake(self):
        self._leave(self.unpaid_type, date(2026, 8, 5), date(2026, 8, 16),
                    return_date=date(2026, 8, 22))
        with self.assertRaises(UserError) as caught:
            self._set_reset(date(2026, 8, 22))
        message = str(caught.exception)
        self.assertIn('Guard Unpaid Leave', message)
        self.assertIn('2026-08-22', message)

    # ------------------------------------------------------------------
    # Allowed: a real settlement, or a date nothing claims
    # ------------------------------------------------------------------
    def test_reset_onto_an_annual_return_is_allowed(self):
        self._leave(self.annual_type, date(2026, 8, 5), date(2026, 8, 16),
                    return_date=date(2026, 8, 22))
        self._set_reset(date(2026, 8, 22))
        self.assertEqual(self.balance.x_opening_reset_date, date(2026, 8, 22))

    def test_an_annual_leave_over_the_same_date_unblocks_it(self):
        """Both govern the date: the settlement is real either way."""
        self._leave(self.unpaid_type, date(2026, 8, 5), date(2026, 8, 16),
                    return_date=date(2026, 8, 22))
        self._leave(self.annual_type, date(2026, 8, 18), date(2026, 8, 21),
                    return_date=date(2026, 8, 22))
        self._set_reset(date(2026, 8, 22))
        self.assertEqual(self.balance.x_opening_reset_date, date(2026, 8, 22))

    def test_an_excuse_leave_does_not_block(self):
        """Neither settles nor blocks — not this guard's business."""
        self._leave(self.excuse_type, date(2026, 8, 10), date(2026, 8, 10))
        self._set_reset(date(2026, 8, 11))
        self.assertEqual(self.balance.x_opening_reset_date, date(2026, 8, 11))

    def test_a_date_no_leave_governs_is_allowed(self):
        self._leave(self.unpaid_type, date(2026, 8, 5), date(2026, 8, 16))
        self._set_reset(date(2026, 6, 1))
        self.assertEqual(self.balance.x_opening_reset_date, date(2026, 6, 1))

    def test_moving_the_baseline_backwards_is_allowed(self):
        """Repairing a wrong restart must never be blocked by the guard."""
        self._leave(self.unpaid_type, date(2026, 8, 5), date(2026, 8, 16),
                    return_date=date(2026, 8, 22))
        self._set_reset(date(2026, 9, 30))
        self._set_reset(date(2026, 8, 10))
        self.assertEqual(self.balance.x_opening_reset_date, date(2026, 8, 10))

    def test_clearing_the_reset_date_is_allowed(self):
        self._leave(self.unpaid_type, date(2026, 8, 5), date(2026, 8, 16))
        self.balance.write({'x_opening_reset_date': False})
        self.assertFalse(self.balance.x_opening_reset_date)

    def test_rewriting_the_same_blocked_date_does_not_raise(self):
        """Only a *change* is judged, so an unrelated save cannot deadlock."""
        self._leave(self.unpaid_type, date(2026, 8, 5), date(2026, 8, 16),
                    return_date=date(2026, 8, 22))
        self.balance.invalidate_recordset()
        self.env.cr.execute(
            "UPDATE ksw_annual_leave SET x_opening_reset_date = %s WHERE id = %s",
            (date(2026, 8, 22), self.balance.id),
        )
        self.balance.invalidate_recordset()
        self.balance.write({'x_opening_reset_date': date(2026, 8, 22)})
        self.assertEqual(self.balance.x_opening_reset_date, date(2026, 8, 22))

    # ------------------------------------------------------------------
    # sudo() is not an escape hatch — this is about the figure, not rights
    # ------------------------------------------------------------------
    def test_sudo_is_not_exempt(self):
        self._leave(self.unpaid_type, date(2026, 8, 5), date(2026, 8, 16),
                    return_date=date(2026, 8, 22))
        with self.assertRaises(UserError):
            self.balance.sudo().write({
                'x_opening_reset_date': date(2026, 8, 22),
            })

    # ------------------------------------------------------------------
    # The audit trail that made this take forensics
    # ------------------------------------------------------------------
    def test_the_opening_fields_are_tracked(self):
        # Settle the creation first: a record created and edited inside one
        # unflushed transaction has its write folded into the create, and
        # tracking has nothing to compare against. Production never does that.
        self.env.cr.flush()
        before = self.balance.message_ids.ids
        self.balance.write({'x_opening_extra_days': 4.0})
        # Tracking messages are generated in a precommit callback, which a
        # test transaction never reaches on its own.
        self.env.cr.flush()
        new_messages = self.balance.message_ids.filtered(
            lambda m: m.id not in before)
        self.assertTrue(
            new_messages.tracking_value_ids,
            'A change to the opening balance must be recorded in the chatter.',
        )
