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

from odoo import api, models


class HrLeave(models.Model):
    _inherit = 'hr.leave'

    def _settles_attendance_sheet(self, leave):
        """Annual vacations only: unpaid leave and extensions lock their
        own days, and an EOS employee is leaving, not going on vacation."""
        return (self._is_annual_leave(leave)
                and not self._is_unpaid_leave(leave)
                and not getattr(leave, 'x_is_eos_leave', False))

    def _sync_gm_final_state(self):
        res = super()._sync_gm_final_state()
        for leave in self.filtered(self._settles_attendance_sheet):
            if leave._is_past_gm_final():
                leave._settle_vacation_sheet()
            else:
                leave._release_vacation_sheet()
        return res

    def _settle_vacation_sheet(self, draft_only=False):
        self.ensure_one()
        emp = self.employee_id.sudo()
        if not emp.x_is_attendance_sheet or not self.request_date_from:
            return
        start = self.request_date_from
        end = self.request_date_to or start
        month_start = start.replace(day=1)
        month_end = end.replace(day=monthrange(end.year, end.month)[1])
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
        lines = self.env['ksw.attendance.sheet.line'].sudo().search([
            ('x_settled_leave_id', '=', self.id),
        ])
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
    _inherit = 'ksw.attendance.sheet'

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
