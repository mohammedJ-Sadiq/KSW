import calendar as cal

from markupsafe import Markup
from odoo import _, api, fields, models


class KswLeaveAttendanceWizard(models.TransientModel):
    _name = 'ksw.leave.attendance.wizard'
    _description = 'Attendance Sheet Update on Annual Leave DM Approval'

    leave_id = fields.Many2one('hr.leave', required=True, readonly=True, ondelete='cascade')
    employee_name = fields.Char(related='leave_id.employee_id.name', readonly=True)
    date_from = fields.Date(related='leave_id.request_date_from', readonly=True)
    date_to = fields.Date(related='leave_id.request_date_to', readonly=True)
    affected_count = fields.Integer(
        string='Workdays Currently Attended',
        compute='_compute_affected', store=False,
    )
    affected_months = fields.Char(
        string='Affected Month(s)',
        compute='_compute_affected', store=False,
    )

    @api.depends('leave_id')
    def _compute_affected(self):
        Line = self.env['ksw.attendance.sheet.line'].sudo()
        for wiz in self:
            leave = wiz.leave_id
            if not leave:
                wiz.affected_count = 0
                wiz.affected_months = ''
                continue
            lines = Line.search([
                ('sheet_id.employee_id', '=', leave.employee_id.id),
                ('sheet_id.state', '=', 'draft'),
                ('date', '>=', leave.request_date_from),
                ('date', '<=', leave.request_date_to),
                ('is_workday', '=', True),
                ('is_attended', '=', True),
            ])
            wiz.affected_count = len(lines)
            months = sorted({(l.date.year, l.date.month) for l in lines})
            wiz.affected_months = ', '.join(
                '%s %d' % (cal.month_name[m], y) for y, m in months
            ) or _('none')

    def _get_affected_lines(self):
        leave = self.leave_id
        return self.env['ksw.attendance.sheet.line'].sudo().search([
            ('sheet_id.employee_id', '=', leave.employee_id.id),
            ('sheet_id.state', '=', 'draft'),
            ('date', '>=', leave.request_date_from),
            ('date', '<=', leave.request_date_to),
            ('is_workday', '=', True),
            ('is_attended', '=', True),
        ])

    def action_mark_absent(self):
        """Mark all affected workday lines absent and close the dialog."""
        lines = self._get_affected_lines()
        if lines:
            lines.with_context(ksw_system_write=True).write({'is_attended': False})
            months = sorted({(l.date.year, l.date.month) for l in lines})
            month_strs = ', '.join('%s %d' % (cal.month_name[m], y) for y, m in months)
            self.leave_id.message_post(
                body=Markup(
                    '<strong>📋 Attendance Sheet Updated by DM</strong><br/>'
                    '<b>%(emp)s</b>: %(count)d workday(s) marked absent '
                    'across %(months)s (leave %(from_)s – %(to_)s).'
                ) % {
                    'emp': self.leave_id.employee_id.name,
                    'count': len(lines),
                    'months': month_strs,
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
