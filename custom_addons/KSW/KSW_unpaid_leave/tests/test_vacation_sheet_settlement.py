# -*- coding: utf-8 -*-
"""An annual vacation settles the attendance sheet itself at GM final
approval, and releases it on every route back. Replaces the DM dialog.
"""
from datetime import date

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.KSW_annual_leave.tests.test_leave_attendance_sheet import (
    LeaveAttendanceSheetCommon,
)


class TestVacationSheetSettlement(LeaveAttendanceSheetCommon):

    def _workdays(self, sheet, start=None, end=None):
        return sheet.sudo().line_ids.filtered(
            lambda l: l.is_workday and (not start or l.date >= start)
            and (not end or l.date <= end))

    def test_nothing_marked_before_gm_final(self):
        sheet = self._make_sheet(self.sheet_emp, 2027, 3)
        leave = self._make_leave(self.sheet_emp, date(2027, 3, 8), date(2027, 3, 18))
        self._advance_to(leave, 'pending_gm_final')
        self.assertTrue(all(l.is_attended for l in self._workdays(sheet)))

    def test_gm_final_settles_whole_month_and_locks_vacation_dates(self):
        sheet = self._make_sheet(self.sheet_emp, 2027, 4)
        leave = self._make_leave(self.sheet_emp, date(2027, 4, 11), date(2027, 4, 22))
        self._advance_to(leave, 'pending_employee_signature')
        workdays = self._workdays(sheet)
        self.assertTrue(workdays and all(not l.is_attended for l in workdays),
                        'the whole month is settled by the vacation')
        in_dates = self._workdays(sheet, date(2027, 4, 11), date(2027, 4, 22))
        self.assertTrue(all(l.x_leave_id == leave for l in in_dates))
        outside = workdays - in_dates
        self.assertFalse(outside.filtered('x_leave_id'),
                         'the lock stays on the vacation dates only')
        # the supervisor cannot flip a locked day back
        with self.assertRaises(UserError):
            in_dates[:1].write({'is_attended': True})

    def test_route_back_releases_exactly_what_it_marked(self):
        sheet = self._make_sheet(self.sheet_emp, 2027, 5)
        absent_before = self._workdays(sheet, date(2027, 5, 2), date(2027, 5, 2))
        absent_before.with_context(ksw_system_write=True).write(
            {'is_attended': False})
        leave = self._make_leave(self.sheet_emp, date(2027, 5, 9), date(2027, 5, 20))
        self._advance_to(leave, 'pending_employee_signature')
        self.assertTrue(all(not l.is_attended for l in self._workdays(sheet)))

        leave.sudo().action_refuse()
        workdays = self._workdays(sheet)
        self.assertFalse(workdays.filtered('x_leave_id'))
        self.assertFalse(workdays.filtered('x_settled_leave_id'))
        self.assertEqual((workdays - absent_before).filtered(
            lambda l: not l.is_attended), self.env['ksw.attendance.sheet.line'],
            'every day the vacation marked is restored')
        self.assertFalse(absent_before.is_attended,
                         'a day that was absent before stays absent')

    def test_sheet_opened_mid_vacation_is_settled(self):
        leave = self._make_leave(self.sheet_emp, date(2027, 6, 20), date(2027, 7, 12))
        self._advance_to(leave, 'pending_employee_signature')
        july = self._make_sheet(self.sheet_emp, 2027, 7)
        away = self._workdays(july, end=date(2027, 7, 12))
        self.assertTrue(away and all(not l.is_attended for l in away))
        self.assertTrue(all(l.x_leave_id == leave for l in away))
        # July is the return month: worked from the return date on.
        back = self._workdays(july, start=date(2027, 7, 13))
        self.assertTrue(back and all(l.is_attended for l in back))

    def test_confirmed_month_outside_vacation_dates_not_reopened(self):
        sheet = self._make_sheet(self.sheet_emp, 2027, 8)
        sheet.sudo().write({'state': 'confirmed'})
        leave = self._make_leave(self.sheet_emp, date(2027, 8, 29), date(2027, 9, 5))
        self._advance_to(leave, 'pending_employee_signature')
        before = self._workdays(sheet, end=date(2027, 8, 28))
        self.assertTrue(all(l.is_attended for l in before),
                        'a released month is only touched on the vacation dates')

    def test_plain_employee_untouched(self):
        leave = self._make_leave(self.plain_emp, date(2027, 10, 3), date(2027, 10, 7))
        self._advance_to(leave, 'pending_employee_signature')
        self.assertFalse(self.env['ksw.attendance.sheet.line'].sudo().search(
            [('x_settled_leave_id', '=', leave.id)]))

    def test_confirmed_return_sends_the_confirmer_to_the_sheet(self):
        sheet = self._make_sheet(self.sheet_emp, 2027, 11)
        leave = self._make_leave(self.sheet_emp, date(2027, 11, 7), date(2027, 11, 18))
        self._advance_to(leave, 'pending_employee_signature')
        leave.sudo().write({'x_return_state': 'on_vacation',
                            'x_return_date': date(2027, 11, 16)})
        action = leave.with_user(self.user_dm).sudo().action_confirm_return_manager()
        self.assertEqual(action.get('res_model'), 'ksw.attendance.sheet')
        self.assertEqual(action.get('res_id'), sheet.id)
        todo = sheet.sudo().activity_ids
        self.assertEqual(len(todo), 1)
        self.assertEqual(todo.user_id, self.user_dm)
        # the month stays settled by the vacation
        self.assertTrue(all(not l.is_attended for l in self._workdays(sheet)))

    def test_already_absent_vacation_day_is_locked_not_owned(self):
        sheet = self._make_sheet(self.sheet_emp, 2027, 12)
        day = self._workdays(sheet, date(2027, 12, 13), date(2027, 12, 13))
        day.with_context(ksw_system_write=True).write({'is_attended': False})
        leave = self._make_leave(self.sheet_emp, date(2027, 12, 12), date(2027, 12, 16))
        self._advance_to(leave, 'pending_employee_signature')
        self.assertEqual(day.x_leave_id, leave, 'already-absent vacation day is locked')
        self.assertFalse(day.x_settled_leave_id, 'but its value is not the vacation\'s')
        leave.sudo().action_refuse()
        self.assertFalse(day.x_leave_id)
        self.assertFalse(day.is_attended, 'released, and it stays absent as it was')

    # ------------------------------------------------------------------
    # Generation applies approved time off (KSWCO sheet 4705)
    # ------------------------------------------------------------------

    def _validate(self, leave):
        self._advance_to(leave, 'pending_employee_signature')
        self._add_stub_attachment(leave)
        leave.with_user(self.user_hr).sudo().action_employee_confirm_signature()
        self.assertEqual(leave.state, 'validate')

    def test_sheet_generated_after_return_is_absent_until_return(self):
        """Vacation approved months before the sheet exists, return
        confirmed: the return-month sheet is born with the days before the
        return absent and locked, the rest attended, nothing to fix."""
        leave = self._make_leave(self.sheet_emp, date(2028, 1, 20), date(2028, 2, 6))
        self._validate(leave)
        leave.sudo().write({'x_return_state': 'hr_confirmed',
                            'x_return_date': date(2028, 2, 7)})
        feb = self._make_sheet(self.sheet_emp, 2028, 2)
        away = feb.sudo().line_ids.filtered(lambda l: l.date <= date(2028, 2, 6))
        self.assertEqual(len(away), 6)
        self.assertFalse(away.filtered('is_attended'),
                         'no day before the return is born attended')
        self.assertTrue(all(l.x_leave_id == leave for l in away))
        self.assertTrue(all(l.x_settled_leave_id == leave for l in away))
        back = self._workdays(feb, start=date(2028, 2, 7))
        self.assertTrue(back and all(l.is_attended for l in back))
        self.assertFalse(feb.x_is_blocked, feb.x_blocked_reason)
        with self.assertRaises(UserError):
            away.filtered('is_workday')[:1].write({'is_attended': True})
        # Any route back releases the born-locked days too.
        leave.sudo().action_refuse()
        self.assertFalse(feb.sudo().line_ids.filtered('x_leave_id'))
        self.assertTrue(all(l.is_attended for l in self._workdays(feb)))

    def test_apply_approved_leave_locks_like_generation(self):
        """The repair of an older sheet ends where generation starts."""
        leave = self._make_leave(self.sheet_emp, date(2028, 5, 20), date(2028, 6, 4))
        self._validate(leave)
        leave.sudo().write({'x_return_state': 'hr_confirmed'})
        jun = self._make_sheet(self.sheet_emp, 2028, 6)
        # Reproduce a sheet generated before generation knew about leave.
        lines = jun.sudo().line_ids
        lines.write({'x_leave_id': False, 'x_settled_leave_id': False})
        lines.with_context(ksw_system_write=True).write({'is_attended': True})
        self.assertTrue(jun.x_is_blocked)
        jun.sudo().action_apply_approved_leave()
        away = lines.filtered(lambda l: l.date <= date(2028, 6, 4))
        self.assertFalse(away.filtered('is_attended'))
        self.assertTrue(all(l.x_leave_id == leave for l in away))
        self.assertFalse(jun.x_is_blocked, jun.x_blocked_reason)

    def test_apply_reaches_a_locked_day_left_attended(self):
        """KSWCO 4559: the vacation's first day, a rest day, was locked
        by the vacation yet still paid. Applying the leave must take it,
        not abort on the lock."""
        leave = self._make_leave(self.sheet_emp, date(2028, 7, 10), date(2028, 7, 20))
        self._validate(leave)
        leave.sudo().write({'x_return_state': 'hr_confirmed'})
        jul = self._make_sheet(self.sheet_emp, 2028, 7)
        day = jul.sudo().line_ids.filtered(lambda l: l.date == date(2028, 7, 14))
        self.assertEqual(day.x_leave_id, leave)
        self.env.cr.execute(
            'UPDATE ksw_attendance_sheet_line SET is_attended = TRUE '
            'WHERE id = %s', (day.id,))
        day.invalidate_recordset()
        jul.sudo().action_apply_approved_leave()
        self.assertFalse(day.is_attended)
        self.assertEqual(day.x_leave_id, leave)
        with self.assertRaises(UserError):
            day.write({'is_attended': True})


@tagged('post_install', '-at_install')
class TestVacationSheetOpenReturn(LeaveAttendanceSheetCommon):
    """Post-install: an unconfirmed return covering the rest of the month
    comes from KSW_payroll's _leave_coverage_end, loaded after this module."""

    _validate = TestVacationSheetSettlement._validate

    def test_unconfirmed_return_keeps_month_absent_but_unlocked(self):
        leave = self._make_leave(self.sheet_emp, date(2028, 3, 20), date(2028, 4, 6))
        self._validate(leave)
        leave.sudo().write({'x_return_state': 'on_vacation'})
        apr = self._make_sheet(self.sheet_emp, 2028, 4)
        self.assertFalse(apr.sudo().line_ids.filtered('is_attended'),
                         'nobody knows he came back: the month stays absent')
        after = apr.sudo().line_ids.filtered(lambda l: l.date > date(2028, 4, 6))
        self.assertFalse(after.filtered('x_leave_id'),
                         'past the planned end the confirmer can still mark him present')
        # Blocked only by the open return itself, never by a clashing day.
        self.assertNotIn('marked Attended', apr.x_blocked_reason or '')
