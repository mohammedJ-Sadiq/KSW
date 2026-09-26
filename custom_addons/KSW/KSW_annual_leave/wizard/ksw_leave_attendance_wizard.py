import calendar as cal
from calendar import monthrange

from markupsafe import Markup
from odoo import _, api, fields, models


class KswLeaveAttendanceWizard(models.TransientModel):
    _name = 'ksw.leave.attendance.wizard'
    _description = 'Attendance Sheet Update on Annual Leave DM Approval'

    leave_id = fields.Many2one('hr.leave', required=True, readonly=True, ondelete='cascade')
    employee_name = fields.Char(related='leave_id.employee_id.name', readonly=True)
    date_from = fields.Date(related='leave_id.request_date_from', readonly=True)
    date_to = fields.Date(related='leave_id.request_date_to', readonly=True)

    # The sheet and the leave answer two different questions.  The leave
    # record keeps its real dates (start → return) and nothing here touches
    # them.  The monthly attendance sheet is a different document: once the
    # employee is on vacation the whole month is settled by the vacation
    # payslip, so the sheet is marked absent for the entire month(s) the
    # leave touches, not only for the leave's own dates.  Leaving the tail
    # of the month Attended is what blocks the sheet
    # (ksw.attendance.sheet._confirmation_blockers) with no way out.
    scope = fields.Selection(
        [
            ('month', 'Whole month(s) — the sheet is settled by the vacation'),
            ('leave_period', 'Leave dates only'),
        ],
        string='Mark Absent', default='month', required=True,
    )

    range_from = fields.Date(
        string='Mark From', compute='_compute_range', store=False)
    range_to = fields.Date(
        string='Mark To', compute='_compute_range', store=False)
    affected_count = fields.Integer(
        string='Workdays Currently Attended',
        compute='_compute_affected', store=False,
    )
    affected_months = fields.Char(
        string='Affected Month(s)',
        compute='_compute_affected', store=False,
    )

    # ------------------------------------------------------------------
    # Range
    # ------------------------------------------------------------------

    def _marking_range(self):
        """First and last date the chosen scope marks absent."""
        self.ensure_one()
        leave = self.leave_id
        if not leave or not leave.request_date_from:
            return False, False
        start = leave.request_date_from
        end = leave.request_date_to or start
        if self.scope == 'month':
            start = start.replace(day=1)
            end = end.replace(day=monthrange(end.year, end.month)[1])
        return start, end

    @api.depends('leave_id', 'scope')
    def _compute_range(self):
        for wiz in self:
            wiz.range_from, wiz.range_to = wiz._marking_range()

    @api.depends('leave_id', 'scope')
    def _compute_affected(self):
        for wiz in self:
            lines = wiz._get_affected_lines()
            wiz.affected_count = len(lines)
            months = sorted({(l.date.year, l.date.month) for l in lines})
            wiz.affected_months = ', '.join(
                '%s %d' % (cal.month_name[m], y) for y, m in months
            ) or _('none')

    def _get_affected_lines(self):
        self.ensure_one()
        start, end = self._marking_range()
        if not start or not end:
            return self.env['ksw.attendance.sheet.line'].sudo().browse()
        return self.env['ksw.attendance.sheet.line'].sudo().search([
            ('sheet_id.employee_id', '=', self.leave_id.employee_id.id),
            ('sheet_id.state', '=', 'draft'),
            ('date', '>=', start),
            ('date', '<=', end),
            ('is_workday', '=', True),
            ('is_attended', '=', True),
        ])

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    def action_mark_absent(self):
        """Mark all affected workday lines absent and close the dialog."""
        lines = self._get_affected_lines()
        if lines:
            start, end = self._marking_range()
            lines.with_context(ksw_system_write=True).write({'is_attended': False})
            months = sorted({(l.date.year, l.date.month) for l in lines})
            month_strs = ', '.join('%s %d' % (cal.month_name[m], y) for y, m in months)
            self.leave_id.message_post(
                body=Markup(
                    '<strong>📋 Attendance Sheet Updated by DM</strong><br/>'
                    '<b>%(emp)s</b>: %(count)d workday(s) marked absent '
                    'across %(months)s (sheet marked %(mark_from)s – '
                    '%(mark_to)s for leave %(from_)s – %(to_)s).'
                ) % {
                    'emp': self.leave_id.employee_id.name,
                    'count': len(lines),
                    'months': month_strs,
                    'mark_from': start,
                    'mark_to': end,
                    'from_': self.leave_id.request_date_from,
                    'to_': self.leave_id.request_date_to,
                },
                subtype_xmlid='mail.mt_note',
            )
        return {'type': 'ir.actions.act_window_close'}

    def action_dismiss(self):
        """Close without marking — DM will update the sheet manually."""
        self.leave_id.message_post(
            body=Markup(
                '<strong>ℹ️ Attendance Sheet — Manual Update Pending</strong><br/>'
                'DM chose to update the attendance sheet manually for the '
                'leave period %(from_)s – %(to_)s.'
            ) % {
                'from_': self.leave_id.request_date_from,
                'to_': self.leave_id.request_date_to,
            },
            subtype_xmlid='mail.mt_note',
        )
        return {'type': 'ir.actions.act_window_close'}
