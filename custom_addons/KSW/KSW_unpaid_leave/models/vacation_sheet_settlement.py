"""An annual vacation settles the attendance sheet itself, when it starts.

Replaces the DM's 'Update Attendance Sheet' dialog (removed Sept 2026). The
dialog opened at Step 1 of 6, so it marked a vacation that could still be
refused; nothing undid it; most DMs dismissed it.

Now: the moment the vacation becomes real — GM final approval, the same
moment 'On Vacation' starts (`_sync_gm_final_state`, which every route in
and out already calls) — the whole calendar month(s) it touches go absent,
because the vacation payslip settles those months (see KSW pitfall
"vacation settles the whole sheet month"). The days inside the vacation's
own dates are also LOCKED, exactly like unpaid leave. Any route back
(refuse, return to an earlier step, cancel, reset) releases precisely the
days it marked.

Never retroactive on a released month: outside the vacation's own dates
only DRAFT sheets are touched, and the backfill of vacations already under
way touches draft sheets only.
"""
from calendar import monthrange

from markupsafe import Markup

from odoo import _, api, models


class HrLeave(models.Model):
    _inherit = 'hr.leave'

    def _settles_attendance_sheet(self, leave):
        """Annual vacations only: unpaid leave and extensions lock their
        own days, and an EOS employee is leaving, not going on vacation."""
        return (self._is_annual_leave(leave)
                and not self._is_unpaid_leave(leave)
                and not getattr(leave, 'x_is_eos_leave', False))

    def _locks_attendance_sheet(self, leave):
        """Leaves whose own days are locked on the sheet at validation
        (see _action_validate → _lock_attendance_sheet_lines)."""
        return self._is_unpaid_leave(leave) or self._is_annual_leave(leave)

    def _sync_gm_final_state(self):
        res = super()._sync_gm_final_state()
        for leave in self.filtered(self._settles_attendance_sheet):
            if leave._is_past_gm_final():
                leave._settle_vacation_sheet()
            else:
                leave._release_vacation_sheet()
        return res

    def action_confirm_return_manager(self):
        """After the return is confirmed, send the confirmer to the sheet.

        User decision (2026-09-29): the whole month stays settled by the
        vacation, but whoever confirms the return is taken to the
        employee's attendance sheet for the return month and asked to check
        it again (a To-Do on the sheet, assigned to him, plus a note).
        """
        res = super().action_confirm_return_manager()
        sheets = self.env['ksw.attendance.sheet']
        for leave in self:
            sheets |= leave._flag_sheet_for_return_review()
        if len(self) == 1 and sheets:
            return {
                'type': 'ir.actions.act_window',
                'name': _('Check the Attendance Sheet'),
                'res_model': 'ksw.attendance.sheet',
                'view_mode': 'form' if len(sheets) == 1 else 'list,form',
                'res_id': sheets.id if len(sheets) == 1 else False,
                'domain': [('id', 'in', sheets.ids)],
                'target': 'current',
            }
        return res

    def _flag_sheet_for_return_review(self):
        self.ensure_one()
        emp = self.employee_id.sudo()
        back = self.x_return_date
        if not emp.x_is_attendance_sheet or not back:
            return self.env['ksw.attendance.sheet']
        sheets = self.env['ksw.attendance.sheet'].sudo().search([
            ('employee_id', '=', emp.id),
            ('month', '=', str(back.month)),
            ('year', '=', back.year),
        ])
        for sheet in sheets:
            sheet.activity_schedule(
                'mail.mail_activity_data_todo',
                user_id=self.env.uid,
                summary=_('Check the attendance sheet after the return'),
                note=_('%(emp)s returned on %(date)s. Please check this '
                       'month\'s attendance sheet again.',
                       emp=emp.name, date=back),
            )
            sheet.message_post(
                body=Markup(
                    '<strong>🔁 Return confirmed</strong><br/>'
                    '%(emp)s returned on %(date)s (confirmed by %(user)s). '
                    'The month is settled by the vacation; please check the '
                    'sheet again.'
                ) % {'emp': emp.name, 'date': back,
                     'user': self.env.user.name},
                subtype_xmlid='mail.mt_note',
            )
        return sheets.with_env(self.env)

    def _settle_vacation_sheet(self, draft_only=False):
        self.ensure_one()
        emp = self.employee_id.sudo()
        if not emp.x_is_attendance_sheet or not self.request_date_from:
            return
        start = self.request_date_from
        end = self.request_date_to or start
        month_start = start.replace(day=1)
        # The whole-month rule is for the month the vacation starts in (the
        # vacation payslip settles it). The return month is worked from the
        # return date on, so there it stops at the vacation's last day.
        month_end = max(
            end, start.replace(day=monthrange(start.year, start.month)[1]))
        Line = self.env['ksw.attendance.sheet.line'].sudo()
        candidates = Line.search([
            ('sheet_id.employee_id', '=', emp.id),
            ('date', '>=', month_start),
            ('date', '<=', month_end),
            ('is_workday', '=', True),
            ('is_attended', '=', True),
            ('x_leave_id', '=', False),
            ('x_settled_leave_id', '=', False),
        ])
        in_dates = candidates.filtered(lambda l: start <= l.date <= end)
        rest = (candidates - in_dates).filtered(
            lambda l: l.sheet_id.state == 'draft')
        if draft_only:
            in_dates = in_dates.filtered(lambda l: l.sheet_id.state == 'draft')
        # The vacation's own days are locked even when they were already
        # absent (marked by hand or by 'Apply Approved Time Off'): the lock
        # asserts ownership of the day. Their value is not ours, so they are
        # not marked settled and a release does not flip them to present.
        already_absent = Line.search([
            ('sheet_id.employee_id', '=', emp.id),
            ('date', '>=', start),
            ('date', '<=', end),
            ('is_workday', '=', True),
            ('is_attended', '=', False),
            ('x_leave_id', '=', False),
        ])
        if draft_only:
            already_absent = already_absent.filtered(
                lambda l: l.sheet_id.state == 'draft')
        already_absent.write({'x_leave_id': self.id, 'x_lock_only': True})
        lines = in_dates | rest
        if not lines:
            return
        lines.with_context(ksw_system_write=True).write({'is_attended': False})
        lines.write({'x_settled_leave_id': self.id})
        in_dates.write({'x_leave_id': self.id})
        for sheet in lines.mapped('sheet_id'):
            sheet._sync_line_attendance(
                lines.filtered(lambda l: l.sheet_id == sheet))

    def _release_vacation_sheet(self):
        self.ensure_one()
        Line = self.env['ksw.attendance.sheet.line'].sudo()
        # Every lock this vacation holds goes, including days that were
        # already absent (locked, never marked settled).
        Line.search([('x_leave_id', '=', self.id),
                     ('x_lock_only', '=', True)]).write(
            {'x_leave_id': False, 'x_lock_only': False})
        lines = Line.search([('x_settled_leave_id', '=', self.id)])
        if not lines:
            return
        lines.filtered(lambda l: l.x_leave_id == self).write(
            {'x_leave_id': False})
        lines.write({'x_settled_leave_id': False})
        lines.with_context(ksw_system_write=True).write({'is_attended': True})
        for sheet in lines.mapped('sheet_id'):
            sheet._sync_line_attendance(
                lines.filtered(lambda l: l.sheet_id == sheet))


class KswAttendanceSheet(models.Model):
    # mail.activity.mixin: the return-review To-Do lands on the sheet.
    _name = 'ksw.attendance.sheet'
    _inherit = ['ksw.attendance.sheet', 'mail.activity.mixin']

    def _covered_line_vals(self, leave, day):
        """Lock a day born absent exactly as the leave would have locked it.

        Only the leave's own dates: past the planned end an unconfirmed
        return keeps the day absent (KSW_payroll's coverage), but whoever
        confirms the return must be able to mark it present again.
        """
        vals = super()._covered_line_vals(leave, day)
        Leave = self.env['hr.leave']
        own_end = leave.request_date_to or leave.request_date_from
        if not (leave.request_date_from <= day <= own_end
                and Leave._locks_attendance_sheet(leave)):
            return vals
        vals['x_leave_id'] = leave.id
        if Leave._settles_attendance_sheet(leave):
            # Owned by the settlement, so every route back releases it.
            vals['x_settled_leave_id'] = leave.id
        return vals

    def _create_lines(self, dates):
        """A sheet opened mid-vacation (next month's, by the monthly job) is
        settled the moment its days exist."""
        lines = super()._create_lines(dates)
        if lines:
            first, last = min(lines.mapped('date')), max(lines.mapped('date'))
            leaves = self.env['hr.leave'].sudo().search([
                ('employee_id', '=', self.employee_id.id),
                ('state', 'not in', ('refuse', 'cancel', 'draft')),
                ('request_date_from', '<=', last.replace(
                    day=monthrange(last.year, last.month)[1])),
                ('request_date_to', '>=', first.replace(day=1)),
            ]).filtered(lambda l: l._settles_attendance_sheet(l)
                        and l._is_past_gm_final())
            for leave in leaves:
                leave._settle_vacation_sheet()
        return lines
