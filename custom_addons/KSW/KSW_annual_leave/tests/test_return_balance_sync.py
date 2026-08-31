"""The confirmed return date must drive the annual leave balance.

HR settles a vacation by setting ``ksw.annual.leave.x_opening_reset_date`` to
the *planned* return (``request_date_to + 1``) — that date is where accrual
restarts.  When the manager then confirms a different *actual* return date,
the restart date has to follow it, otherwise an employee who came back early
keeps losing accrual (KSWCO leave 4753: returned Jul 28, accrual restarted
Aug 8).

The annual vacation settles the entitlement (its duration is capped at the
balance), so accrual **restarts from zero on the confirmed return date,
always** — reset date set, carry-over cleared.  An employee who took only part
of their entitlement loses the rest: an accepted trade-off, chosen over the
safer variants.

Two things still hold it back:
  - a reset dated *after* this vacation belongs to a more recent return and is
    never walked back
  - nothing happens on a non-annual leave or without a confirmed return date

`x_opening_is_locked` does NOT block the write (75% of prod records are
locked); the override is named in the chatter note.
"""
from datetime import date, timedelta

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestReturnBalanceSync(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, extra_groups=()):
            group_ids = [cls.env.ref('base.group_user').id]
            group_ids.extend(extra_groups)
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@balsync.test',
                'group_ids': [(6, 0, group_ids)],
            })

        cls.user_dm = _mkuser('Sync DM', 'balsync_dm')
        cls.user_hr = _mkuser(
            'Sync HR', 'balsync_hr',
            [cls.env.ref('KSW_annual_leave.group_annual_leave_hr').id])

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Balance Sync Employee',
            'user_id': _mkuser('Sync Emp', 'balsync_emp').id,
            'leave_manager_id': cls.user_dm.id,
        })

        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave Balance Sync Test',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })

        cls.VAC_FROM = date(2028, 3, 1)
        cls.VAC_TO = date(2028, 3, 7)
        cls.PLANNED_RETURN = date(2028, 3, 8)   # request_date_to + 1
        cls.EARLY_RETURN = date(2028, 3, 5)
        cls.LATE_RETURN = date(2028, 3, 12)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _balance_record(self, reset_date=None, locked=False):
        rec = self.env['ksw.annual.leave'].sudo().search(
            [('employee_id', '=', self.employee.id)], limit=1)
        if not rec:
            rec = self.env['ksw.annual.leave'].sudo().create(
                {'employee_id': self.employee.id})
        rec.write({'x_opening_reset_date': reset_date})
        if locked:
            rec.write({'x_opening_is_locked': True})
        return rec

    def _on_vacation_leave(self):
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': self.VAC_FROM,
            'request_date_to': self.VAC_TO,
        })
        leave.sudo().write({
            'state': 'validate',
            'x_annual_approval_state': 'approved',
            'x_return_state': 'on_vacation',
        })
        return leave

    def _confirm(self, leave, return_date):
        leave.sudo().write({'x_return_date': return_date})
        leave.with_user(self.user_dm).sudo().action_confirm_return_manager()
        return leave

    @staticmethod
    def _bodies_since(leave, existing_ids):
        return ' '.join(
            m.body or '' for m in leave.message_ids
            if m.id not in existing_ids
        )

    # ==================================================================
    # Realignment
    # ==================================================================

    def test_early_return_moves_reset_date_back(self):
        """The user's case: returned early, accrual must restart early."""
        rec = self._balance_record(reset_date=self.PLANNED_RETURN)
        leave = self._on_vacation_leave()

        self._confirm(leave, self.EARLY_RETURN)

        self.assertEqual(rec.x_opening_reset_date, self.EARLY_RETURN)
        self.assertEqual(rec.x_effective_start_date, self.EARLY_RETURN)

    def test_late_return_moves_reset_date_forward(self):
        """Symmetric: a late return must not accrue days not yet worked."""
        rec = self._balance_record(reset_date=self.PLANNED_RETURN)
        leave = self._on_vacation_leave()

        self._confirm(leave, self.LATE_RETURN)

        self.assertEqual(rec.x_opening_reset_date, self.LATE_RETURN)

    def test_restart_posts_chatter_note(self):
        self._balance_record(reset_date=self.PLANNED_RETURN)
        leave = self._on_vacation_leave()
        existing_ids = leave.message_ids.ids

        self._confirm(leave, self.EARLY_RETURN)

        self.assertIn(
            'Accrual Restarted',
            self._bodies_since(leave, existing_ids))

    def test_return_matching_planned_date_is_a_no_op(self):
        rec = self._balance_record(reset_date=self.PLANNED_RETURN)
        leave = self._on_vacation_leave()
        existing_ids = leave.message_ids.ids

        self._confirm(leave, self.PLANNED_RETURN)

        self.assertEqual(rec.x_opening_reset_date, self.PLANNED_RETURN)
        self.assertNotIn(
            'Accrual Restarted',
            self._bodies_since(leave, existing_ids))

    def test_second_call_is_idempotent(self):
        """Re-driving the helper (e.g. the prod backfill) changes nothing."""
        rec = self._balance_record(reset_date=self.PLANNED_RETURN)
        leave = self._on_vacation_leave()
        self._confirm(leave, self.EARLY_RETURN)
        existing_ids = leave.message_ids.ids

        leave.sudo()._sync_opening_reset_to_return()

        self.assertEqual(rec.x_opening_reset_date, self.EARLY_RETURN)
        self.assertNotIn(
            'Accrual Restarted',
            self._bodies_since(leave, existing_ids))

    # ==================================================================
    # Restart from zero, whatever was there before
    # ==================================================================

    def test_older_baseline_is_replaced_by_the_return_date(self):
        """A go-live baseline is superseded once a vacation is taken."""
        rec = self._balance_record(reset_date=date(2026, 1, 1))
        leave = self._on_vacation_leave()

        self._confirm(leave, self.EARLY_RETURN)

        self.assertEqual(rec.x_opening_reset_date, self.EARLY_RETURN)
        self.assertEqual(rec.x_effective_start_date, self.EARLY_RETURN)

    def test_reset_after_the_planned_return_is_left_alone(self):
        """A more recent return must survive an older confirmation."""
        later = date(2028, 6, 1)
        rec = self._balance_record(reset_date=later)
        leave = self._on_vacation_leave()
        existing_ids = leave.message_ids.ids

        self._confirm(leave, self.EARLY_RETURN)

        self.assertEqual(rec.x_opening_reset_date, later)
        self.assertIn(
            'Balance Restart Date Unchanged',
            self._bodies_since(leave, existing_ids))

    def test_missing_reset_date_is_created_from_the_return(self):
        """An employee with no reset yet still restarts on their return."""
        rec = self._balance_record(reset_date=False)
        leave = self._on_vacation_leave()

        self._confirm(leave, self.EARLY_RETURN)

        self.assertEqual(rec.x_opening_reset_date, self.EARLY_RETURN)
        self.assertEqual(rec.x_effective_start_date, self.EARLY_RETURN)

    def test_carry_over_days_are_cleared_by_the_restart(self):
        """Restart from zero: unused carry-over does not survive."""
        rec = self._balance_record(reset_date=self.PLANNED_RETURN)
        rec.write({'x_opening_extra_days': 12.0})
        leave = self._on_vacation_leave()
        existing_ids = leave.message_ids.ids

        self._confirm(leave, self.EARLY_RETURN)

        self.assertEqual(rec.x_opening_reset_date, self.EARLY_RETURN)
        self.assertEqual(rec.x_opening_extra_days, 0.0)
        self.assertIn(
            'carry-over', self._bodies_since(leave, existing_ids))

    def test_locked_record_is_written_through_and_says_so(self):
        """The lock stops accidental manual edits, not a confirmed return."""
        rec = self._balance_record(
            reset_date=self.PLANNED_RETURN, locked=True)
        leave = self._on_vacation_leave()
        existing_ids = leave.message_ids.ids

        self._confirm(leave, self.EARLY_RETURN)

        self.assertEqual(rec.x_opening_reset_date, self.EARLY_RETURN)
        self.assertTrue(rec.x_opening_is_locked)   # still locked afterwards
        bodies = self._bodies_since(leave, existing_ids)
        self.assertIn('Accrual Restarted', bodies)
        self.assertIn('was locked', bodies)

    def test_lock_still_blocks_an_ordinary_write(self):
        """Only the sync's context key gets through — not a manual edit."""
        rec = self._balance_record(
            reset_date=self.PLANNED_RETURN, locked=True)
        with self.assertRaises(UserError):
            rec.write({'x_opening_reset_date': self.EARLY_RETURN})

    def test_non_annual_leave_is_ignored(self):
        other_type = self.env['hr.leave.type'].create({
            'name': 'Sick Leave Balance Sync Test',
            'requires_allocation': False,
            'is_annual_leave': False,
        })
        rec = self._balance_record(reset_date=self.PLANNED_RETURN)
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': other_type.id,
            'request_date_from': self.VAC_FROM,
            'request_date_to': self.VAC_TO,
        })
        leave.sudo().write({
            'state': 'validate',
            'x_return_state': 'on_vacation',
            'x_return_date': self.EARLY_RETURN,
        })

        leave.sudo()._sync_opening_reset_to_return()

        self.assertEqual(rec.x_opening_reset_date, self.PLANNED_RETURN)

    # ==================================================================
    # The request period follows the actual return
    # ==================================================================

    def test_early_return_shortens_the_request_period(self):
        self._balance_record(reset_date=self.PLANNED_RETURN)
        leave = self._on_vacation_leave()

        self._confirm(leave, self.EARLY_RETURN)

        self.assertEqual(
            leave.request_date_to, self.EARLY_RETURN - timedelta(days=1))
        self.assertEqual(
            leave.x_actual_vacation_days,
            (leave.request_date_to - leave.request_date_from).days + 1)

    def test_paid_days_survive_the_shortening(self):
        """Paid up front: an early return refunds nothing."""
        self._balance_record(reset_date=self.PLANNED_RETURN)
        leave = self._on_vacation_leave()
        paid = {f: leave[f] for f in leave._PAID_DURATION_FIELDS}

        self._confirm(leave, self.EARLY_RETURN)

        for field, value in paid.items():
            self.assertAlmostEqual(leave[field], value, places=4, msg=field)

    def test_shortening_posts_chatter_note(self):
        self._balance_record(reset_date=self.PLANNED_RETURN)
        leave = self._on_vacation_leave()
        existing_ids = leave.message_ids.ids

        self._confirm(leave, self.EARLY_RETURN)

        self.assertIn(
            'Vacation Period Shortened',
            self._bodies_since(leave, existing_ids))

    def test_return_on_the_planned_day_does_not_shorten(self):
        self._balance_record(reset_date=self.PLANNED_RETURN)
        leave = self._on_vacation_leave()

        self._confirm(leave, self.PLANNED_RETURN)

        self.assertEqual(leave.request_date_to, self.VAC_TO)

    def test_late_return_does_not_extend_the_period(self):
        """A late return is not a vacation extension."""
        self._balance_record(reset_date=self.PLANNED_RETURN)
        leave = self._on_vacation_leave()

        self._confirm(leave, self.LATE_RETURN)

        self.assertEqual(leave.request_date_to, self.VAC_TO)

    # ==================================================================
    # The prod shape: balance already settled at the planned return
    # ==================================================================

    def test_confirm_works_when_allocation_starts_after_the_leave(self):
        """KSWCO shape: HR reset the balance at the planned return, so the
        allocation begins *after* the vacation started and the leave is
        covered by nothing. Confirming a return must still work — this
        raised ValidationError("You do not have any allocation for this
        time off type") on 8 of 9 prod records."""
        alloc_type = self.env['hr.leave.type'].create({
            'name': 'Annual Leave Alloc Required Test',
            'requires_allocation': True,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })
        rec = self._balance_record(reset_date=self.PLANNED_RETURN)
        alloc = self.env['hr.leave.allocation'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': alloc_type.id,
            'number_of_days': 30.0,
            'date_from': self.VAC_FROM,
        })
        alloc.action_approve()
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': alloc_type.id,
            'request_date_from': self.VAC_FROM,
            'request_date_to': self.VAC_TO,
        })
        leave.sudo().write({
            'state': 'validate',
            'x_annual_approval_state': 'approved',
            'x_return_state': 'on_vacation',
        })
        # HR settles the balance at the planned return: the allocation now
        # opens *after* the vacation started, leaving the leave uncovered.
        alloc.sudo().write({'date_from': self.PLANNED_RETURN})

        self._confirm(leave, self.EARLY_RETURN)   # must not raise

        self.assertEqual(
            leave.request_date_to, self.EARLY_RETURN - timedelta(days=1))
        self.assertEqual(rec.x_opening_reset_date, self.EARLY_RETURN)

    # ==================================================================
    # Amendment path
    # ==================================================================

    def test_amending_the_return_date_restarts_again(self):
        rec = self._balance_record(reset_date=self.PLANNED_RETURN)
        leave = self._on_vacation_leave()
        self._confirm(leave, self.EARLY_RETURN)

        corrected = self.EARLY_RETURN + timedelta(days=1)
        leave.with_user(self.user_dm).sudo().write(
            {'x_return_date': corrected})

        self.assertEqual(rec.x_opening_reset_date, corrected)
        self.assertEqual(rec.x_effective_start_date, corrected)
