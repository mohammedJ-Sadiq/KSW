# -*- coding: utf-8 -*-
"""An annual vacation settles the attendance sheet itself at GM final
approval, and releases it on every route back. Replaces the DM dialog.
"""
from datetime import date

from odoo.exceptions import UserError

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
        workdays = self._workdays(july)
        self.assertTrue(workdays and all(not l.is_attended for l in workdays))
        self.assertTrue(all(l.x_leave_id == leave for l in
                            self._workdays(july, date(2027, 7, 1), date(2027, 7, 12))))

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
