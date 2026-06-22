from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswAttendanceSheetLine(models.Model):
    _name = 'ksw.attendance.sheet.line'
    _description = 'Attendance Sheet Daily Line'
    _order = 'date'

    sheet_id = fields.Many2one(
        'ksw.attendance.sheet', string='Sheet',
        required=True, ondelete='cascade',
    )
    date = fields.Date(string='Date', required=True)
    day_name = fields.Char(
        string='Day', compute='_compute_day_name', store=True,
    )
    is_workday = fields.Boolean(
        string='Workday', default=True,
        help='False for weekends / non-scheduled days.',
    )
    is_attended = fields.Boolean(
        string='Attended', default=True,
        help='Toggle off to mark the employee as absent on this day.',
    )
    attendance_id = fields.Many2one(
        'hr.attendance', string='Attendance Record',
        readonly=True, ondelete='set null',
        help='Linked hr.attendance record (auto-synced).',
    )

    @api.depends('date')
    def _compute_day_name(self):
        for line in self:
            line.day_name = line.date.strftime('%A') if line.date else False

    def write(self, vals):
        if 'is_attended' in vals:
            is_system_write = self.env.context.get('ksw_system_write')
            if not is_system_write and self.filtered(
                    lambda l: not l.is_workday):
                raise UserError(
                    'Off-day attendance (e.g. the weekly rest day) is '
                    'determined automatically and cannot be edited directly.')
            old_values = {line.id: line.is_attended for line in self}
            # System writes (leave approval locking/unlocking days, off-day
            # pay recomputation) must go through even on a confirmed/locked
            # sheet or an older month — those guards are for manual edits.
            if not is_system_write:
                self.mapped('sheet_id')._check_editable()
        result = super().write(vals)
        if 'is_attended' in vals:
            self._log_attendance_change(old_values)
            # Sync hr.attendance records for changed lines
            for sheet in self.mapped('sheet_id'):
                sheet_lines = self.filtered(lambda l: l.sheet_id == sheet)
                sheet._sync_line_attendance(sheet_lines)
            # A workday's attendance can forfeit/restore the paid weekly
            # rest day bordering it (e.g. Friday) — see _recompute_off_day_pay.
            workday_changes = self.filtered(
                lambda l: l.is_workday and old_values.get(l.id) != l.is_attended)
            if workday_changes:
                workday_changes.mapped('sheet_id')._recompute_off_day_pay(
                    workday_changes)
        return result

    def _log_attendance_change(self, old_values):
        """Post a chatter message on the sheet for each day that changed."""
        changed = self.filtered(
            lambda l: old_values.get(l.id) != l.is_attended)
        for sheet in changed.mapped('sheet_id'):
            lines = changed.filtered(lambda l: l.sheet_id == sheet)
            items = []
            for line in lines:
                status = _('Present') if line.is_attended else _('Absent')
                items.append(Markup('<li>%s (%s): %s</li>') % (
                    line.date.strftime('%d %b %Y'), line.day_name, status,
                ))
            body = Markup('%s<ul>%s</ul>') % (
                _('Attendance updated:'), Markup('').join(items),
            )
            sheet.message_post(body=body)
