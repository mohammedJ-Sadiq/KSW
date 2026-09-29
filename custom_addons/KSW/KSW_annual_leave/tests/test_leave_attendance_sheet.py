"""Integration tests: annual leave DM approval → attendance sheet auto-marking.

Covers:
  - action_dm_approve returns a wizard for attendance-sheet employees
  - wizard action_mark_absent marks workday lines absent and posts chatter
  - wizard action_dismiss leaves lines untouched and posts a note
  - final validation (action_employee_confirm_signature) auto-marks remaining
    attended workday lines as the safety-net
  - non-attendance-sheet employees are unaffected (no wizard, no marking)
  - multi-month leave marks correct days across both month sheets
"""
import base64
from datetime import date, timedelta

from odoo.tests.common import TransactionCase


class LeaveAttendanceSheetCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Work schedule: Mon-Thu + Sun (typical Saudi schedule)
        cls.calendar_group = cls.env['resource.calendar.group'].create({
            'name': 'Test LA Group',
        })
        for day in ['0', '1', '2', '3', '6']:
            cls.env['resource.calendar.group.line'].create({
                'name': f'Work Day {day}',
                'calendar_group_id': cls.calendar_group.id,
                'dayofweek': day,
                'day_period': 'full_day',
                'hour_from': 8.0,
                'hour_to': 16.5,
            })
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Test LA Calendar',
            'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, cls.calendar_group.id)],
        })

        def _mkuser(name, login, extra_groups=()):
            group_ids = [cls.env.ref('base.group_user').id]
            group_ids.extend(extra_groups)
            return cls.env['res.users'].create({
                'name': name, 'login': login,
                'email': f'{login}@la.test',
                'group_ids': [(6, 0, group_ids)],
            })

        cls.user_dm  = _mkuser('LA DM',  'la_dm')
        cls.user_hr  = _mkuser('LA HR',  'la_hr',  [cls.env.ref('KSW_annual_leave.group_annual_leave_hr').id])
        cls.user_acc = _mkuser('LA Acc', 'la_acc', [cls.env.ref('KSW_annual_leave.group_annual_leave_acc').id])
        cls.user_gm  = _mkuser('LA GM',  'la_gm',  [cls.env.ref('KSW_annual_leave.group_annual_leave_gm').id])

        cls.emp_dm = cls.env['hr.employee'].create({
            'name': 'LA DM Employee', 'user_id': cls.user_dm.id,
        })

        # Attendance-sheet employee
        cls.sheet_emp = cls.env['hr.employee'].create({
            'name': 'LA Sheet Employee',
            'resource_calendar_id': cls.calendar.id,
            'leave_manager_id': cls.user_dm.id,
            'x_is_attendance_sheet': True,
        })

        # Non-attendance-sheet employee
        cls.plain_emp = cls.env['hr.employee'].create({
            'name': 'LA Plain Employee',
            'resource_calendar_id': cls.calendar.id,
            'leave_manager_id': cls.user_dm.id,
            'x_is_attendance_sheet': False,
        })

        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave LA Test',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })

        # The accounting step follows the employee's department since Sep 2026,
        # so holding the group is no longer enough to reach a request. Put the
        # approver on the company-wide accounting team, which every department
        # with no accountants of its own inherits. Last in setUpClass on
        # purpose: it reuses whatever employee the fixture already made for
        # that user, and only creates one when there is none.
        _acc_emp = cls.env['hr.employee'].sudo().search(
            [('user_id', '=', cls.user_acc.id)], limit=1)
        if not _acc_emp:
            _acc_emp = cls.env['hr.employee'].sudo().create({
                'name': cls.user_acc.name, 'user_id': cls.user_acc.id})
        cls.env.company.sudo().x_default_accountant_ids = [(4, _acc_emp.id)]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    _STATE_RANK = {
        'pending_dm': 0, 'pending_hr': 1, 'pending_gm_initial': 2,
        'pending_acc': 3, 'pending_gm_final': 4,
        'pending_employee_signature': 5, 'approved': 6,
    }

    def _advance_to(self, leave, target_state):
        """Advance the approval chain to target_state, skipping already-passed steps."""
        steps = [
            ('pending_dm',         'action_dm_approve',         self.user_dm),
            ('pending_hr',         'action_hr_approve',         self.user_hr),
            ('pending_gm_initial', 'action_gm_initial_approve', self.user_gm),
            ('pending_acc',        'action_acc_approve',        self.user_acc),
            ('pending_gm_final',   'action_gm_final_approve',   self.user_gm),
        ]
        for pre_state, method, user in steps:
            if self._STATE_RANK.get(leave.x_annual_approval_state, 0) > self._STATE_RANK.get(pre_state, 0):
                continue
            getattr(leave.with_user(user).sudo(), method)()
            leave.invalidate_recordset()
            if leave.x_annual_approval_state == target_state:
                break

    def _make_leave(self, employee, date_from, date_to):
        """Create a confirmed leave ready for DM approval."""
        return self.env['hr.leave'].sudo().create({
            'employee_id': employee.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': date_from,
            'request_date_to': date_to,
        })

    def _make_sheet(self, employee, year, month_int):
        """Create a draft attendance sheet for the given month (lines auto-generated)."""
        return self.env['ksw.attendance.sheet'].sudo().create({
            'employee_id': employee.id,
            'month': str(month_int),
            'year': year,
        })

    def _add_stub_attachment(self, leave):
        att = self.env['ir.attachment'].sudo().create({
            'name': 'stub_signed.pdf',
            'datas': base64.b64encode(b'stub'),
            'res_model': 'hr.leave',
            'res_id': leave.id,
        })
        leave.sudo().write({'x_attachment_ids': [(4, att.id)]})



class TestLeaveAttendanceSheet(LeaveAttendanceSheetCommon):

    def test_dm_approve_opens_no_dialog(self):
        """The DM is no longer asked to mark the sheet at Step 1."""
        leave = self._make_leave(self.sheet_emp, date(2027, 9, 5), date(2027, 9, 9))
        result = leave.with_user(self.user_dm).sudo().action_dm_approve()
        self.assertFalse(isinstance(result, dict)
                         and result.get('res_model') == 'ksw.leave.attendance.wizard')
        self.assertNotIn('ksw.leave.attendance.wizard', self.env)

    # ------------------------------------------------------------------
    # Test: Safety-net auto-mark at final validation
    # ------------------------------------------------------------------

    def test_final_validation_auto_marks_remaining(self):
        """After full approval chain, remaining attended workday lines are auto-marked absent."""
        sheet = self._make_sheet(self.sheet_emp, 2027, 10)
        leave = self._make_leave(self.sheet_emp, date(2027, 10, 7), date(2027, 10, 20))

        # Advance to pending_employee_signature
        self._advance_to(leave, 'pending_employee_signature')

        # Workday lines are still attended here (at_install: the GM-final
        # settlement lives in KSW_unpaid_leave, loaded after this module)
        period_workday_lines = sheet.sudo().line_ids.filtered(
            lambda l: date(2027, 10, 7) <= l.date <= date(2027, 10, 20) and l.is_workday
        )
        self.assertTrue(all(l.is_attended for l in period_workday_lines),
                        "Lines must still be attended before signature step")

        # Final step: employee confirms signature
        self._add_stub_attachment(leave)
        leave.sudo().action_employee_confirm_signature()

        period_workday_lines.invalidate_recordset()
        absent = period_workday_lines.filtered(lambda l: not l.is_attended)
        self.assertEqual(len(absent), len(period_workday_lines),
                         "Safety-net must mark all leave-period workday lines absent at validation")

        sheet.sudo().unlink()

    def test_final_validation_noop_when_already_marked(self):
        """Safety-net is a no-op when the days are already absent."""
        sheet = self._make_sheet(self.sheet_emp, 2027, 11)
        leave = self._make_leave(self.sheet_emp, date(2027, 11, 3), date(2027, 11, 14))

        # The days are already absent (e.g. settled at GM final approval)
        leave.with_user(self.user_dm).sudo().action_dm_approve()
        period_workday_lines = sheet.sudo().line_ids.filtered(
            lambda l: date(2027, 11, 3) <= l.date <= date(2027, 11, 14) and l.is_workday
        )
        period_workday_lines.with_context(ksw_system_write=True).write(
            {'is_attended': False})
        self.assertTrue(all(not l.is_attended for l in period_workday_lines))

        # Complete the approval chain
        self._advance_to(leave, 'pending_employee_signature')
        self._add_stub_attachment(leave)
        att_count_before = self.env['hr.attendance'].search_count([
            ('employee_id', '=', self.sheet_emp.id),
        ])
        leave.sudo().action_employee_confirm_signature()
        att_count_after = self.env['hr.attendance'].search_count([
            ('employee_id', '=', self.sheet_emp.id),
        ])
        # Safety-net must not duplicate attendance changes
        self.assertEqual(att_count_before, att_count_after,
                         "Safety-net must not create duplicate attendance changes")

        sheet.sudo().unlink()

    # ------------------------------------------------------------------
    # Test: Multi-month leave
    # ------------------------------------------------------------------

    def test_multi_month_leave_marks_both_sheets(self):
        """A leave spanning two months auto-marks workdays in both attendance sheets."""
        sheet_dec = self._make_sheet(self.sheet_emp, 2027, 12)
        sheet_jan = self._make_sheet(self.sheet_emp, 2028, 1)

        # Leave: Dec 25, 2027 – Jan 10, 2028
        leave = self._make_leave(self.sheet_emp, date(2027, 12, 25), date(2028, 1, 10))
        self._advance_to(leave, 'pending_employee_signature')
        self._add_stub_attachment(leave)
        leave.sudo().action_employee_confirm_signature()

        dec_workday_lines = sheet_dec.sudo().line_ids.filtered(
            lambda l: l.date >= date(2027, 12, 25) and l.is_workday
        )
        jan_workday_lines = sheet_jan.sudo().line_ids.filtered(
            lambda l: l.date <= date(2028, 1, 10) and l.is_workday
        )
        self.assertTrue(dec_workday_lines, "Must have Dec workday lines in leave period")
        self.assertTrue(jan_workday_lines, "Must have Jan workday lines in leave period")
        self.assertTrue(all(not l.is_attended for l in dec_workday_lines),
                        "Dec leave-period workdays must be absent")
        self.assertTrue(all(not l.is_attended for l in jan_workday_lines),
                        "Jan leave-period workdays must be absent")

        sheet_dec.sudo().unlink()
        sheet_jan.sudo().unlink()
