# -*- coding: utf-8 -*-
"""The supervisor's confirmation is what releases a month to payroll.

An attendance-sheet employee has no punches: their month is a supervisor's
assertion. Before this, that assertion flowed into payroll whether or not
anyone made it — a supervisor who never opened the sheet still produced a
full month of paid attendance for their whole team, and a monthly cron
rubber-stamped it. These tests cover the gate that replaced that: who may
confirm, what refuses confirmation, and what withdraws it again.
"""
from calendar import monthrange
from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase


class TestSheetConfirmation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.calendar_group = cls.env['resource.calendar.group'].create({
            'name': 'Confirm Test Group',
        })
        for day in ['0', '1', '2', '3', '6']:  # Mon-Thu + Sun
            cls.env['resource.calendar.group.line'].create({
                'name': f'Work Day {day}',
                'calendar_group_id': cls.calendar_group.id,
                'dayofweek': day,
                'day_period': 'full_day',
                'hour_from': 8.0,
                'hour_to': 16.0,
            })
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Confirm Test Calendar',
            'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, cls.calendar_group.id)],
        })

        def _mkuser(name, login, groups=()):
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@sheetconfirm.test',
                'group_ids': [(6, 0, [cls.env.ref('base.group_user').id]
                               + list(groups))],
            })

        cls.supervisor_user = _mkuser(
            'Confirm Supervisor', 'confirm_supervisor',
            [cls.env.ref(
                'KSW_attendance_sheet.group_attendance_sheet_supervisor').id],
        )
        # Holds the supervisor role but manages nobody in these tests — the
        # point is that the group alone is not authority over a record.
        cls.other_user = _mkuser(
            'Confirm Bystander', 'confirm_bystander',
            [cls.env.ref(
                'KSW_attendance_sheet.group_attendance_sheet_supervisor').id],
        )
        cls.hr_user = _mkuser(
            'Confirm HR', 'confirm_hr',
            [cls.env.ref('hr.group_hr_user').id,
             cls.env.ref(
                 'KSW_attendance_sheet.group_attendance_sheet_manager').id],
        )

        cls.supervisor = cls.env['hr.employee'].create({
            'name': 'Confirm Supervisor Employee',
            'user_id': cls.supervisor_user.id,
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Confirm Sheet Employee',
            'resource_calendar_id': cls.calendar.id,
            'parent_id': cls.supervisor.id,
            'x_is_attendance_sheet': True,
        })

        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'Confirm Test Leave',
            'requires_allocation': False,
            'leave_validation_type': 'no_validation',
        })

        today = fields.Date.context_today(cls.env['ksw.attendance.sheet'])
        cls.this_month = str(today.month)
        cls.this_year = today.year
        cls.today = today

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sheet(self, month=None, year=None, employee=None):
        return self.env['ksw.attendance.sheet'].create({
            'employee_id': (employee or self.employee).id,
            'month': month or self.this_month,
            'year': year or self.this_year,
        })

    def _workday_in_month(self, sheet, index=0):
        """A line the employee is scheduled to work, for clash-building."""
        workdays = sheet.line_ids.filtered('is_workday').sorted('date')
        return workdays[index]

    def _validated_leave(self, date_from, date_to):
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': date_from,
            'request_date_to': date_to,
        })
        leave.sudo().write({'state': 'validate'})
        return leave

    # ------------------------------------------------------------------
    # Who may confirm
    # ------------------------------------------------------------------

    def test_supervisor_confirms_own_team(self):
        sheet = self._sheet()
        sheet.with_user(self.supervisor_user).action_supervisor_confirm()

        self.assertEqual(sheet.state, 'confirmed')
        self.assertTrue(sheet.is_locked)
        self.assertEqual(sheet.x_confirmed_by, self.supervisor_user)
        self.assertTrue(sheet.x_confirmed_on)

    def test_unrelated_user_cannot_confirm(self):
        """View-level invisible= is cosmetic — the method itself must refuse.

        No .sudo() here on purpose: the guard exempts env.su (crons and
        system flows need that), so a sudo'd call would sail straight
        through and the test would prove nothing.
        """
        sheet = self._sheet()
        with self.assertRaises(UserError):
            sheet.with_user(self.other_user).action_supervisor_confirm()
        self.assertEqual(sheet.state, 'draft')

    def test_can_confirm_flag_matches_the_guard(self):
        """The button must never offer what the guard refuses."""
        sheet = self._sheet()
        self.assertTrue(
            sheet.with_user(self.supervisor_user).x_can_confirm)
        self.assertFalse(
            sheet.with_user(self.other_user).x_can_confirm)

    def test_supervisor_cannot_confirm_a_closed_month(self):
        """Supervisors confirm during the month; HR cleans up afterwards."""
        last_month = self.today.replace(day=1) - timedelta(days=1)
        sheet = self._sheet(
            month=str(last_month.month), year=last_month.year)

        with self.assertRaises(UserError):
            sheet.with_user(self.supervisor_user).action_supervisor_confirm()
        self.assertEqual(sheet.state, 'draft')

        # HR is the escape hatch for a late fix.
        sheet.with_user(self.hr_user).action_supervisor_confirm()
        self.assertEqual(sheet.state, 'confirmed')

    def test_already_confirmed_sheet_cannot_be_reconfirmed(self):
        sheet = self._sheet()
        sheet.with_user(self.supervisor_user).action_supervisor_confirm()
        with self.assertRaises(UserError):
            sheet.with_user(self.supervisor_user).action_supervisor_confirm()

    # ------------------------------------------------------------------
    # Contradictions refuse the confirmation
    # ------------------------------------------------------------------

    def test_attended_during_approved_leave_blocks_confirmation(self):
        """The case that prompted this: the whole month marked present
        while the employee was on an approved vacation."""
        sheet = self._sheet()
        line = self._workday_in_month(sheet)
        leave = self._validated_leave(line.date, line.date)
        # Put the sheet back into the contradicting state the supervisor
        # would have typed (validation auto-marks these days absent).
        line.with_context(ksw_system_write=True).write({'is_attended': True})

        with self.assertRaises(UserError) as err:
            sheet.with_user(self.supervisor_user).action_supervisor_confirm()

        message = str(err.exception)
        self.assertIn(line.date.strftime('%d %b'), message)
        self.assertIn(leave.holiday_status_id.name, message)
        self.assertEqual(sheet.state, 'draft')

    def test_blockers_are_empty_when_the_sheet_agrees(self):
        """A leave day recorded as absent is agreement, not contradiction."""
        sheet = self._sheet()
        line = self._workday_in_month(sheet)
        self._validated_leave(line.date, line.date)
        line.with_context(ksw_system_write=True).write({'is_attended': False})

        self.assertFalse(sheet._confirmation_blockers())
        sheet.with_user(self.supervisor_user).action_supervisor_confirm()
        self.assertEqual(sheet.state, 'confirmed')

    def test_absent_days_outside_a_leave_are_not_a_contradiction(self):
        """A plain absence is a legitimate thing for a supervisor to record."""
        sheet = self._sheet()
        self._workday_in_month(sheet).write({'is_attended': False})
        sheet.with_user(self.supervisor_user).action_supervisor_confirm()
        self.assertEqual(sheet.state, 'confirmed')

    # ------------------------------------------------------------------
    # Bulk confirmation
    # ------------------------------------------------------------------

    def test_bulk_confirm_reports_every_failure_at_once(self):
        """A supervisor with a team should not discover their problems one
        refusal at a time."""
        good = self._sheet()
        second_employee = self.env['hr.employee'].create({
            'name': 'Confirm Sheet Employee 2',
            'resource_calendar_id': self.calendar.id,
            'parent_id': self.supervisor.id,
            'x_is_attendance_sheet': True,
        })
        bad = self._sheet(employee=second_employee)
        bad_line = self._workday_in_month(bad)
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': second_employee.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': bad_line.date,
            'request_date_to': bad_line.date,
        })
        leave.sudo().write({'state': 'validate'})
        bad_line.with_context(ksw_system_write=True).write(
            {'is_attended': True})

        result = (good | bad).with_user(
            self.supervisor_user).action_confirm_my_team()

        # The clean sheet is confirmed and stays confirmed — a UserError
        # here would have rolled it back while claiming it succeeded.
        self.assertEqual(good.state, 'confirmed')
        self.assertEqual(bad.state, 'draft')
        self.assertEqual(result['tag'], 'display_notification')
        self.assertIn(
            second_employee.name, result['params']['message'])

    def test_bulk_confirm_raises_when_nothing_succeeds(self):
        sheet = self._sheet()
        line = self._workday_in_month(sheet)
        self._validated_leave(line.date, line.date)
        line.with_context(ksw_system_write=True).write({'is_attended': True})

        with self.assertRaises(UserError):
            sheet.with_user(self.supervisor_user).action_confirm_my_team()

    # ------------------------------------------------------------------
    # A late change withdraws the confirmation
    # ------------------------------------------------------------------

    def test_leave_after_confirmation_reopens_the_sheet(self):
        sheet = self._sheet()
        sheet.with_user(self.supervisor_user).action_supervisor_confirm()
        self.assertEqual(sheet.state, 'confirmed')

        line = self._workday_in_month(sheet, index=1)
        line.with_context(ksw_system_write=True).write({'is_attended': False})

        self.assertEqual(sheet.state, 'draft')
        self.assertFalse(sheet.is_locked)
        self.assertFalse(sheet.x_confirmed_by)
        self.assertFalse(sheet.x_confirmed_on)

    def test_reopen_notifies_the_supervisor(self):
        sheet = self._sheet()
        sheet.with_user(self.supervisor_user).action_supervisor_confirm()
        existing_ids = sheet.message_ids.ids

        line = self._workday_in_month(sheet, index=1)
        line.with_context(ksw_system_write=True).write({'is_attended': False})

        new_msgs = sheet.message_ids.filtered(
            lambda m: m.id not in existing_ids)
        self.assertTrue(new_msgs, 'The withdrawal must be recorded on the sheet.')

    def test_writing_the_same_value_does_not_reopen(self):
        """Only an actual change withdraws the confirmation."""
        sheet = self._sheet()
        sheet.with_user(self.supervisor_user).action_supervisor_confirm()

        line = self._workday_in_month(sheet, index=1)
        self.assertTrue(line.is_attended)
        line.with_context(ksw_system_write=True).write({'is_attended': True})

        self.assertEqual(sheet.state, 'confirmed')

    # ------------------------------------------------------------------
    # Crons
    # ------------------------------------------------------------------

    def test_month_end_cron_leaves_past_sheets_unconfirmed(self):
        """It used to auto-confirm them, which is the whole thing this
        feature exists to stop."""
        last_month = self.today.replace(day=1) - timedelta(days=1)
        sheet = self._sheet(
            month=str(last_month.month), year=last_month.year)

        self.env['ksw.attendance.sheet']._cron_generate_sheets(commit=False)

        self.assertEqual(sheet.state, 'draft')

    # The cron is database-wide and this DB carries real sheets, so these
    # assertions count only what reached THIS supervisor's partner rather
    # than the cron's global return value.
    def _messages_to_supervisor(self):
        return self.env['mail.message'].search([
            ('partner_ids', 'in', self.supervisor_user.partner_id.ids),
            ('model', '=', False),
        ])

    def test_reminder_sends_one_digest_per_supervisor(self):
        self._sheet()
        second_employee = self.env['hr.employee'].create({
            'name': 'Confirm Sheet Employee 3',
            'resource_calendar_id': self.calendar.id,
            'parent_id': self.supervisor.id,
            'x_is_attendance_sheet': True,
        })
        self._sheet(employee=second_employee)
        existing_ids = self._messages_to_supervisor().ids

        self.env['ksw.attendance.sheet'].\
            _cron_month_end_confirmation_reminder(force=True)

        new_msgs = self._messages_to_supervisor().filtered(
            lambda m: m.id not in existing_ids)
        self.assertEqual(
            len(new_msgs), 1,
            'Two sheets, one supervisor — one digest, not two.')
        for employee in (self.employee, second_employee):
            self.assertIn(employee.name, new_msgs.body)

    def test_reminder_is_silent_on_a_normal_day(self):
        self._sheet()
        today = self.today
        if today.day == monthrange(today.year, today.month)[1]:
            self.skipTest('Today IS the last day of the month.')
        existing_ids = self._messages_to_supervisor().ids

        self.env['ksw.attendance.sheet'].\
            _cron_month_end_confirmation_reminder()

        self.assertFalse(self._messages_to_supervisor().filtered(
            lambda m: m.id not in existing_ids))

    def test_reminder_skips_confirmed_sheets(self):
        sheet = self._sheet()
        sheet.with_user(self.supervisor_user).action_supervisor_confirm()
        existing_ids = self._messages_to_supervisor().ids

        self.env['ksw.attendance.sheet'].\
            _cron_month_end_confirmation_reminder(force=True)

        self.assertFalse(self._messages_to_supervisor().filtered(
            lambda m: m.id not in existing_ids))


class TestBlockedVisibility(TestSheetConfirmation):
    """The list has to show what needs doing, and say who is holding it.

    A gate nobody can see the state of is a gate people route around: the
    first thing tried in production was the header button, which answered
    "You have no unconfirmed attendance sheets for this month" — true,
    useless, and silent about the 363 sheets that did need confirming.
    """

    # ------------------------------------------------------------------
    # Blocked flags
    # ------------------------------------------------------------------

    def test_clash_sets_the_blocked_flag_and_reason(self):
        sheet = self._sheet()
        self.assertFalse(sheet.x_is_blocked)

        line = self._workday_in_month(sheet)
        self._validated_leave(line.date, line.date)
        line.with_context(ksw_system_write=True).write({'is_attended': True})

        self.assertTrue(sheet.x_is_blocked)
        self.assertIn(line.date.strftime('%d %b'), sheet.x_blocked_reason)

    def test_blocked_sheets_sort_first(self):
        """_order puts them on top so they cannot be scrolled past."""
        clean = self._sheet()
        other = self.env['hr.employee'].create({
            'name': 'Confirm Sheet Employee Blocked',
            'resource_calendar_id': self.calendar.id,
            'parent_id': self.supervisor.id,
            'x_is_attendance_sheet': True,
        })
        blocked = self._sheet(employee=other)
        line = self._workday_in_month(blocked)
        leave = self.env['hr.leave'].sudo().create({
            'employee_id': other.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': line.date,
            'request_date_to': line.date,
        })
        leave.sudo().write({'state': 'validate'})
        line.with_context(ksw_system_write=True).write({'is_attended': True})
        self.assertTrue(blocked.x_is_blocked)

        ordered = self.env['ksw.attendance.sheet'].search(
            [('id', 'in', (clean | blocked).ids)])
        self.assertEqual(ordered[0], blocked)

    def test_confirming_clears_the_flag(self):
        sheet = self._sheet()
        sheet.with_user(self.supervisor_user).action_supervisor_confirm()
        self.assertFalse(sheet.x_is_blocked)
        self.assertFalse(sheet.x_blocked_reason)

    def test_owner_defaults_to_the_sheet_manager(self):
        sheet = self._sheet()
        self.assertEqual(sheet.x_action_owner_id, self.supervisor_user)

    # ------------------------------------------------------------------
    # Apply approved time off
    # ------------------------------------------------------------------

    def test_apply_approved_leave_clears_the_clash(self):
        """The common production shape: a vacation approved in an earlier
        month, so its lock never reached this month's sheet."""
        sheet = self._sheet()
        line = self._workday_in_month(sheet)
        self._validated_leave(line.date, line.date)
        line.with_context(ksw_system_write=True).write({'is_attended': True})
        self.assertTrue(sheet.x_is_blocked)

        sheet.with_user(self.supervisor_user).action_apply_approved_leave()

        self.assertFalse(line.is_attended)
        self.assertFalse(sheet.x_is_blocked)
        sheet.with_user(self.supervisor_user).action_supervisor_confirm()
        self.assertEqual(sheet.state, 'confirmed')

    def test_apply_approved_leave_leaves_other_days_alone(self):
        sheet = self._sheet()
        workdays = sheet.line_ids.filtered('is_workday').sorted('date')
        covered, untouched = workdays[0], workdays[1]
        self._validated_leave(covered.date, covered.date)
        covered.with_context(ksw_system_write=True).write(
            {'is_attended': True})

        sheet.with_user(self.supervisor_user).action_apply_approved_leave()

        self.assertFalse(covered.is_attended)
        self.assertTrue(
            untouched.is_attended,
            'A day with no approved leave on it must not be touched.')

    def test_apply_approved_leave_refuses_an_outsider(self):
        sheet = self._sheet()
        with self.assertRaises(UserError):
            sheet.with_user(self.other_user).action_apply_approved_leave()

    def test_apply_approved_leave_refuses_a_confirmed_sheet(self):
        sheet = self._sheet()
        sheet.with_user(self.supervisor_user).action_supervisor_confirm()
        with self.assertRaises(UserError):
            sheet.with_user(
                self.supervisor_user).action_apply_approved_leave()

    # ------------------------------------------------------------------
    # The diagnostic message
    # ------------------------------------------------------------------

    def test_administrator_with_no_team_is_told_who_holds_what(self):
        """The exact case hit in production: admin manages nobody, so the
        fallback found nothing and said so without naming the 363 sheets
        that did need confirming, or who held them."""
        self._sheet()

        with self.assertRaises(UserError) as err:
            self.env['ksw.attendance.sheet'].with_user(
                self.hr_user).action_confirm_my_team()

        message = str(err.exception)
        self.assertIn(self.supervisor.name, message)
        self.assertIn('tick the rows', message)

    def test_supervisor_with_nothing_left_is_told_it_is_done(self):
        sheet = self._sheet()
        sheet.with_user(self.supervisor_user).action_supervisor_confirm()

        with self.assertRaises(UserError) as err:
            self.env['ksw.attendance.sheet'].with_user(
                self.supervisor_user).action_confirm_my_team()

        self.assertIn('already been sent to payroll', str(err.exception))

    def test_user_managing_nobody_is_told_why(self):
        stranger = self.env['res.users'].create({
            'name': 'Confirm Stranger', 'login': 'confirm_stranger',
            'email': 'confirm_stranger@sheetconfirm.test',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref(
                    'KSW_attendance_sheet.'
                    'group_attendance_sheet_supervisor').id,
            ])],
        })

        with self.assertRaises(UserError) as err:
            self.env['ksw.attendance.sheet'].with_user(
                stranger).action_confirm_my_team()

        self.assertIn('Manager field', str(err.exception))


class TestSheetScoping(TestSheetConfirmation):
    """A supervisor must not be handed authority over other teams.

    `hr.group_hr_user` is held by 14 users in KSWCO, several of them line
    supervisors with 56 and 90 direct reports who hold it for unrelated HR
    reasons. Treating that group as "attendance sheet administrator" gave
    them confirm rights over every other team's month, and its record rule
    ([(1,'=',1)] read/write/create) gave them edit rights to match.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A line supervisor who also happens to hold HR Officer.
        cls.hr_officer_supervisor = cls.env['res.users'].create({
            'name': 'Scoped Supervisor', 'login': 'scoped_supervisor',
            'email': 'scoped_supervisor@sheetconfirm.test',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('hr.group_hr_user').id,
                cls.env.ref(
                    'KSW_attendance_sheet.'
                    'group_attendance_sheet_supervisor').id,
            ])],
        })

    def test_hr_officer_is_not_a_sheet_administrator(self):
        Sheet = self.env['ksw.attendance.sheet']
        self.assertFalse(
            Sheet._is_sheet_administrator(self.hr_officer_supervisor),
            'HR Officer alone must not confer sheet administration.')
        self.assertTrue(
            Sheet._is_sheet_administrator(self.hr_user),
            'The dedicated Attendance Sheet Manager group still does.')

    def test_hr_officer_cannot_confirm_another_teams_sheet(self):
        sheet = self._sheet()   # managed by self.supervisor_user
        with self.assertRaises(UserError):
            sheet.with_user(
                self.hr_officer_supervisor).action_supervisor_confirm()
        self.assertEqual(sheet.state, 'draft')

    def test_hr_officer_cannot_write_another_teams_sheet(self):
        """View and action guards are not the only route — the ORM is."""
        sheet = self._sheet()
        with self.assertRaises(AccessError):
            sheet.with_user(self.hr_officer_supervisor).check_access('write')

    def test_hr_officer_cannot_read_another_teams_sheet(self):
        """The scope has to hold in the RULE, not in a default filter.

        A `search_default_my_team` context is one click away from being
        cleared, and clearing it used to reveal all 363 employees. Searching
        with no domain at all is exactly that gesture.
        """
        sheet = self._sheet()
        with self.assertRaises(AccessError):
            sheet.with_user(self.hr_officer_supervisor).check_access('read')

        found = self.env['ksw.attendance.sheet'].with_user(
            self.hr_officer_supervisor).search([])
        self.assertNotIn(sheet, found)

    def test_lines_are_scoped_too(self):
        """Scoping the sheet but not its lines would leak the day-by-day
        attendance, which is the actual content."""
        sheet = self._sheet()
        found = self.env['ksw.attendance.sheet.line'].with_user(
            self.hr_officer_supervisor).search([])
        self.assertFalse(found & sheet.line_ids)

    def test_sheet_manager_still_sees_everything(self):
        """The escape hatch has to keep working — it is now the only one."""
        sheet = self._sheet()
        found = self.env['ksw.attendance.sheet'].with_user(
            self.hr_user).search([])
        self.assertIn(sheet, found)

    def test_supervisor_still_writes_their_own_team(self):
        sheet = self._sheet()
        sheet.with_user(self.supervisor_user).check_access('write')
        sheet.with_user(self.supervisor_user).action_supervisor_confirm()
        self.assertEqual(sheet.state, 'confirmed')
