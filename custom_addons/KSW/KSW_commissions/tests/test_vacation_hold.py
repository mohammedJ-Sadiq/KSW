"""The vacation hold — a commission month is not paid twice.

When an employee goes on vacation the company settles his whole position on
the leave request: Accounting lists the commission months still owed
(``hr.leave.x_commission_line_ids``) and they are paid with the vacation
payslip. So the monthly commission run must not pay those months again.

The rule this file pins down is a **period** rule, not a state one, and that
is the whole point. Payroll asks "has anybody confirmed he is back?" because
a payslip is computed inside the month it covers. A commission month is
prepared a month or two later, by which time he is back and the state has
cleared — so the same question here would wave through exactly the month the
settlement paid. The worked example throughout: away 11 Aug, back 17 Sep.

  * August    — he left during it          → held in full
  * September — he came back during it     → payable from the 17th
  * October   — after the return           → untouched
  * July      — before the vacation        → untouched
"""
from datetime import date

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from odoo.addons.KSW_commissions.models.ksw_vacation_hold import (
    vacation_hold,
)

AUG = date(2026, 8, 1)
SEP = date(2026, 9, 1)
OCT = date(2026, 10, 1)
JUL = date(2026, 7, 1)


class VacationHoldCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env

        cls.dept = env['hr.department'].create({'name': 'Hold Dept'})
        cls.gm_user = env['res.users'].sudo().create({
            'name': 'hold_gm', 'login': 'hold_gm',
            'group_ids': [(6, 0, [env.ref('base.group_user').id])],
        })
        cls.gm_employee = env['hr.employee'].sudo().create({
            'name': 'Hold GM', 'user_id': cls.gm_user.id})
        cls.dept.sudo().write({'x_gm_id': cls.gm_employee.id})
        env.company.sudo().x_default_gm_id = cls.gm_employee.id

        cls.manager_user = env['res.users'].sudo().create({
            'name': 'hold_dm', 'login': 'hold_dm',
            'group_ids': [(6, 0, [env.ref('base.group_user').id])],
        })
        cls.emp = env['hr.employee'].sudo().create({
            'name': 'Hold Driver', 'department_id': cls.dept.id,
            'leave_manager_id': cls.manager_user.id,
        })
        if cls.emp.current_version_id:
            cls.emp.current_version_id.sudo().write({'wage': 7200.0})

        # An Administrator: full ORM reach, and — unlike a plain sudo() —
        # NOT exempt from the guard, which is what these tests are about.
        cls.officer = env['res.users'].sudo().create({
            'name': 'hold_officer', 'login': 'hold_officer',
            'group_ids': [(6, 0, [
                env.ref('base.group_user').id,
                env.ref('KSW_commissions.group_commission_officer').id,
            ])],
        })

        cls.leave_type = env['hr.leave.type'].sudo().create({
            'name': 'Hold Test Annual Leave',
            # A Python bool, never the string 'no' — 'no' is truthy and
            # trips _check_validity's allocation guard.
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
        })
        cls.component = env.ref('KSW_commissions.pay_component_overtime')

    # ------------------------------------------------------------------
    @classmethod
    def _leave(cls, date_from, date_to, return_date=None):
        leave = cls.env['hr.leave'].sudo().with_context(
            tracking_disable=True, mail_create_nosubscribe=True,
        ).create({
            'employee_id': cls.emp.id,
            'holiday_status_id': cls.leave_type.id,
            'request_date_from': date_from,
            'request_date_to': date_to,
        })
        leave.sudo().write({
            'state': 'validate',
            'x_return_state': 'on_vacation',
        })
        if return_date:
            leave.sudo().write({
                'x_return_date': return_date,
                'x_return_state': 'hr_confirmed',
            })
        return leave

    def _batch(self, period, component=None):
        return self.env['ksw.pay.batch'].sudo().create({
            'component_id': (component or self.component).id,
            'department_id': self.dept.id,
            'period': period,
        })

    def _entry(self, batch, day=None, employee=None, **kwargs):
        vals = {
            'batch_id': batch.id,
            'employee_id': (employee or self.emp).id,
            'quantity': 4.0,
            'reason': 'probe',
        }
        if batch.component_id.needs_date:
            vals['date'] = day or batch.period
        vals.update(kwargs)
        # with_user, not sudo(): every guard in this module exempts env.su,
        # so a sudo() create would prove nothing at all.
        return self.env['ksw.pay.entry'].with_user(self.officer).create(vals)


class TestVacationHoldPredicate(VacationHoldCommon):
    """The question itself, before any screen asks it."""

    def test_departure_month_is_held_whole(self):
        """He worked 1-10 Aug, but that was settled on the leave."""
        self._leave(date(2026, 8, 11), date(2026, 9, 16),
                    return_date=date(2026, 9, 17))
        hold = vacation_hold(self.env, self.emp, AUG)
        self.assertTrue(hold)
        self.assertEqual(hold.kind, 'full')

    def test_return_month_is_payable_from_the_return_date(self):
        self._leave(date(2026, 8, 11), date(2026, 9, 16),
                    return_date=date(2026, 9, 17))
        hold = vacation_hold(self.env, self.emp, SEP)
        self.assertTrue(hold)
        self.assertEqual(hold.kind, 'partial')
        self.assertEqual(hold.payable_from, date(2026, 9, 17))

    def test_a_month_inside_the_vacation_is_held_whole(self):
        self._leave(date(2026, 8, 11), date(2026, 10, 4),
                    return_date=date(2026, 10, 5))
        hold = vacation_hold(self.env, self.emp, SEP)
        self.assertTrue(hold)
        self.assertEqual(hold.kind, 'full')

    def test_the_month_before_is_untouched(self):
        self._leave(date(2026, 8, 11), date(2026, 9, 16),
                    return_date=date(2026, 9, 17))
        self.assertFalse(vacation_hold(self.env, self.emp, JUL))

    def test_the_month_after_the_return_is_untouched(self):
        self._leave(date(2026, 8, 11), date(2026, 9, 16),
                    return_date=date(2026, 9, 17))
        self.assertFalse(vacation_hold(self.env, self.emp, OCT))

    def test_an_unconfirmed_return_holds_every_later_month(self):
        """Nobody has said he is back, so nothing after he left is known.

        This is the case the payroll guard catches too — it just catches it
        by asking about the state, which only works inside the month.
        """
        self._leave(date(2026, 8, 11), date(2026, 8, 20))
        for period in (AUG, SEP, OCT):
            hold = vacation_hold(self.env, self.emp, period)
            self.assertTrue(hold, period)
            self.assertEqual(hold.kind, 'full', period)

    def test_a_refused_request_holds_nothing(self):
        leave = self._leave(date(2026, 8, 11), date(2026, 9, 16),
                            return_date=date(2026, 9, 17))
        leave.sudo().write({'state': 'refuse'})
        self.assertFalse(vacation_hold(self.env, self.emp, AUG))

    def test_a_type_outside_the_return_system_holds_nothing(self):
        """Sick leave settles nothing at the request, so it holds nothing."""
        sick = self.env['hr.leave.type'].sudo().create({
            'name': 'Hold Test Sick Leave',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
            'request_unit': 'day',
        })
        leave = self.env['hr.leave'].sudo().with_context(
            tracking_disable=True, mail_create_nosubscribe=True,
        ).create({
            'employee_id': self.emp.id,
            'holiday_status_id': sick.id,
            'request_date_from': date(2026, 8, 11),
            'request_date_to': date(2026, 8, 20),
        })
        leave.sudo().write({'state': 'validate'})
        self.assertEqual(leave.x_return_state, 'not_applicable')
        self.assertFalse(vacation_hold(self.env, self.emp, AUG))


class TestSettledOnALaterVacation(VacationHoldCommon):
    """A vacation that starts AFTER the month still settles it.

    The settlement pays everything outstanding, not only the month he
    happened to leave in. A driver who left on 3 September was handed his
    August that morning — and August is only being prepared now, so the
    run would pay it a second time.

    KSWCO, Sep 2026: NIAZ GUL KHAN BAHADAR, away from 3 Sep, sitting in
    the August batch with the first pass of this rule waving him through.
    """

    def test_a_september_departure_holds_august(self):
        self._leave(date(2026, 9, 3), date(2026, 12, 1))
        hold = vacation_hold(self.env, self.emp, AUG)
        self.assertTrue(hold, 'August was still owed to him on 3 Sep.')
        self.assertEqual(hold.kind, 'settled_later')

    def test_it_blocks_an_entry_and_the_message_explains_why(self):
        self._leave(date(2026, 9, 3), date(2026, 12, 1))
        batch = self._batch(AUG)
        with self.assertRaises(UserError) as caught:
            self._entry(batch, day=date(2026, 8, 4))
        message = str(caught.exception)
        self.assertIn('August 2026', message)
        self.assertIn('Vacation Releases', message)

    def test_a_month_already_paid_is_not_held_by_a_later_vacation(self):
        """The bound. Once the register has gone out, the money left
        through it and no later settlement paid that month again.
        """
        self._leave(date(2026, 9, 3), date(2026, 12, 1))
        self.assertTrue(vacation_hold(self.env, self.emp, AUG))

        run = self.env['ksw.pay.run'].sudo().search(
            [('period', '=', AUG)], limit=1)
        if not run:
            run = self.env['ksw.pay.run'].sudo().create({'period': AUG})
        run.sudo().write({'state': 'approved'})

        self.assertFalse(
            vacation_hold(self.env, self.emp, AUG),
            'An approved month was paid by its own register, not by a '
            'settlement two months later.')

    def test_a_vacation_that_ended_before_the_month_holds_nothing(self):
        """He came back in July; August is ordinary worked time."""
        self._leave(date(2026, 6, 1), date(2026, 7, 20),
                    return_date=date(2026, 7, 21))
        self.assertFalse(vacation_hold(self.env, self.emp, AUG))

    def test_being_away_outranks_being_settled_later(self):
        """Two requests, one month: the better explanation wins."""
        self._leave(date(2026, 8, 16), date(2026, 8, 20),
                    return_date=date(2026, 8, 21))
        self._leave(date(2026, 9, 3), date(2026, 12, 1))
        hold = vacation_hold(self.env, self.emp, AUG)
        self.assertEqual(hold.kind, 'full')


class TestVacationHoldOnEntries(VacationHoldCommon):
    """The entry screen: the supervisor is stopped where he is typing."""

    def test_an_entry_in_the_departure_month_is_refused(self):
        self._leave(date(2026, 8, 11), date(2026, 9, 16),
                    return_date=date(2026, 9, 17))
        batch = self._batch(AUG)
        with self.assertRaises(UserError) as caught:
            self._entry(batch, day=date(2026, 8, 4))
        message = str(caught.exception)
        self.assertIn('August 2026', message)
        self.assertIn('Vacation Releases', message,
                      'The message must say how to override it.')

    def test_an_entry_before_the_return_date_is_refused(self):
        self._leave(date(2026, 8, 11), date(2026, 9, 16),
                    return_date=date(2026, 9, 17))
        batch = self._batch(SEP)
        with self.assertRaises(UserError):
            self._entry(batch, day=date(2026, 9, 3))

    def test_an_entry_after_the_return_date_is_accepted(self):
        self._leave(date(2026, 8, 11), date(2026, 9, 16),
                    return_date=date(2026, 9, 17))
        batch = self._batch(SEP)
        entry = self._entry(batch, day=date(2026, 9, 20))
        self.assertTrue(entry.exists())
        self.assertFalse(entry.x_vacation_hold,
                         'A day he really worked is not a warning.')

    def test_moving_an_entry_back_into_the_vacation_is_refused(self):
        self._leave(date(2026, 8, 11), date(2026, 9, 16),
                    return_date=date(2026, 9, 17))
        batch = self._batch(SEP)
        entry = self._entry(batch, day=date(2026, 9, 20))
        with self.assertRaises(UserError):
            entry.with_user(self.officer).write({'date': date(2026, 9, 3)})

    def test_an_undated_entry_in_the_return_month_is_warned_not_refused(self):
        """A monthly figure has no day to compare — but it is the row that
        bills a whole month of meals for half a month present."""
        self._leave(date(2026, 8, 11), date(2026, 9, 16),
                    return_date=date(2026, 9, 17))
        meals = self.env.ref('KSW_commissions.pay_component_meals')
        batch = self._batch(SEP, component=meals)
        entry = self._entry(
            batch, option_id=meals.option_ids[0].id, quantity=20.0)
        self.assertTrue(entry.exists())
        self.assertTrue(entry.x_vacation_hold)

    def test_a_held_row_that_already_exists_announces_itself(self):
        """The guard only stops new rows; the batch has to show the old."""
        batch = self._batch(AUG)
        entry = self._entry(batch, day=date(2026, 8, 4))
        self._leave(date(2026, 8, 11), date(2026, 9, 16),
                    return_date=date(2026, 9, 17))
        entry.invalidate_recordset()
        batch.invalidate_recordset()
        self.assertTrue(entry.x_vacation_hold)
        self.assertIn(self.emp.name, batch.x_vacation_hold_warning)

    def test_submitting_a_batch_with_held_rows_is_refused(self):
        batch = self._batch(AUG)
        self._entry(batch, day=date(2026, 8, 4))
        self._leave(date(2026, 8, 11), date(2026, 9, 16),
                    return_date=date(2026, 9, 17))
        batch.invalidate_recordset()
        with self.assertRaises(UserError) as caught:
            batch.with_user(self.officer).action_submit()
        self.assertIn(self.emp.name, str(caught.exception))


class TestVacationHoldOnTheRegister(VacationHoldCommon):
    """The last gate: whatever reaches the register is not paid from it."""

    def test_a_held_employee_is_left_out_of_the_register(self):
        batch = self._batch(AUG)
        self._entry(batch, day=date(2026, 8, 4))
        self._leave(date(2026, 8, 11), date(2026, 9, 16),
                    return_date=date(2026, 9, 17))
        batch.invalidate_recordset()

        run = batch.submission_id.run_id
        run.sudo()._build_register(preview=True)
        self.assertNotIn(self.emp, run.line_ids.employee_id)

    def test_the_register_says_so_on_the_month(self):
        """The real sequence: typed and handed over BEFORE the leave.

        The definitive register only reads departments their own GM
        approved, so the test has to get the batch that far — which is also
        the only way a held row reaches the register at all now that the
        submit guard exists.
        """
        batch = self._batch(AUG)
        self._entry(batch, day=date(2026, 8, 4))
        batch.submission_id.sudo().action_submit()
        batch.submission_id.with_user(self.gm_user).sudo().action_approve()

        self._leave(date(2026, 8, 11), date(2026, 9, 16),
                    return_date=date(2026, 9, 17))
        batch.invalidate_recordset()

        run = batch.submission_id.run_id
        existing = run.message_ids.ids
        run.sudo()._build_register(preview=False)
        new = run.message_ids.filtered(lambda m: m.id not in existing)
        self.assertTrue(
            new.filtered(lambda m: self.emp.name in (m.body or '')),
            'The month must name whose money it did not pay.')
        self.assertNotIn(self.emp, run.line_ids.employee_id)

    def test_the_warning_is_on_the_run_before_approval(self):
        batch = self._batch(AUG)
        self._entry(batch, day=date(2026, 8, 4))
        self._leave(date(2026, 8, 11), date(2026, 9, 16),
                    return_date=date(2026, 9, 17))
        run = batch.submission_id.run_id
        run.invalidate_recordset()
        self.assertIn(self.emp.name, run.x_vacation_held_warning or '')


class TestVacationRelease(VacationHoldCommon):
    """The override: a policy an approver may set aside, on the record."""

    def _release(self, user=None, period=AUG):
        env_user = user or self.gm_user
        return self.env['ksw.pay.vacation.release'].with_user(
            env_user).create({
                'employee_id': self.emp.id,
                'period': period,
                'reason': 'August was never listed on his leave.',
            })

    def test_a_release_makes_the_month_payable_again(self):
        self._leave(date(2026, 8, 11), date(2026, 9, 16),
                    return_date=date(2026, 9, 17))
        self.assertTrue(vacation_hold(self.env, self.emp, AUG))
        self._release()
        self.assertFalse(vacation_hold(self.env, self.emp, AUG))
        batch = self._batch(AUG)
        self.assertTrue(self._entry(batch, day=date(2026, 8, 4)).exists())

    def test_a_release_records_what_it_released(self):
        leave = self._leave(date(2026, 8, 11), date(2026, 9, 16),
                            return_date=date(2026, 9, 17))
        release = self._release()
        self.assertEqual(release.leave_id, leave)
        self.assertTrue(release.held_reason)

    def test_a_release_only_covers_its_own_month(self):
        self._leave(date(2026, 8, 11), date(2026, 9, 16),
                    return_date=date(2026, 9, 17))
        self._release(period=AUG)
        hold = vacation_hold(self.env, self.emp, SEP)
        self.assertTrue(hold, 'September was never released.')

    def test_a_release_is_posted_on_the_vacation(self):
        leave = self._leave(date(2026, 8, 11), date(2026, 9, 16),
                            return_date=date(2026, 9, 17))
        existing = leave.message_ids.ids
        self._release()
        new = leave.message_ids.filtered(lambda m: m.id not in existing)
        self.assertTrue(new, 'The leave is where anyone will look.')

    def test_a_supervisor_cannot_release(self):
        supervisor = self.env['res.users'].sudo().create({
            'name': 'hold_supervisor', 'login': 'hold_supervisor',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref(
                    'KSW_commissions.group_commission_supervisor').id,
            ])],
        })
        self._leave(date(2026, 8, 11), date(2026, 9, 16),
                    return_date=date(2026, 9, 17))
        # He has no create right on the model at all, so this is an
        # AccessError rather than the UserError the GM path raises —
        # caught as Exception because Odoo 19's assertRaises takes one
        # class at a time (CLAUDE.md gotcha #9).
        with self.assertRaises(Exception):
            self._release(user=supervisor)

    def test_a_gm_of_another_department_cannot_release(self):
        other_dept = self.env['hr.department'].sudo().create(
            {'name': 'Hold Other Dept'})
        other_gm_user = self.env['res.users'].sudo().create({
            'name': 'hold_gm_2', 'login': 'hold_gm_2',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('KSW_commissions.group_commission_gm').id,
            ])],
        })
        other_gm = self.env['hr.employee'].sudo().create({
            'name': 'Hold GM 2', 'user_id': other_gm_user.id})
        other_dept.sudo().write({'x_gm_id': other_gm.id})
        self._leave(date(2026, 8, 11), date(2026, 9, 16),
                    return_date=date(2026, 9, 17))
        with self.assertRaises(Exception):
            self._release(user=other_gm_user)
