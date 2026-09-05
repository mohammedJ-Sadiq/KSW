# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools.translate import _


def _issue_minutes(hour_from, hour_to):
    """Length of [hour_from, hour_to) in minutes, wrapping past midnight.

    Hours are real clock times, so a night-shift issue may wrap: 20:55 -> 05:00
    is 8h05, not minus 15h55.  Unlike `_shift_duration` in
    `biometric_schedule_helper`, an empty range stays 0 rather than a full day.
    """
    delta = hour_to - hour_from
    if delta < 0:
        delta += 24.0
    return round(delta * 60.0, 1)


class HrLeaveAttendanceLine(models.Model):
    _name = 'hr.leave.attendance.line'
    _description = 'Leave Attendance Issue Hours'
    _order = 'date, hour_from'

    leave_id = fields.Many2one(
        'hr.leave',
        string='Leave Request',
        required=True,
        ondelete='cascade',
    )
    attendance_id = fields.Many2one(
        'hr.attendance',
        string='Attendance Record',
        ondelete='set null',
        help='Deleting attendance for a re-download (the sanctioned repair: '
             'clear + re-download) must not destroy the accepted_minutes an '
             'HR user already approved here. The line survives as an orphan '
             '(attendance_id = False, date/hour_from/hour_to/accepted_minutes '
             'intact) and hr.attendance._relink_attendance_issue_lines() '
             're-attaches it once the punch comes back under a new id.',
    )
    issue_type = fields.Selection([
        ('late', 'Late'),
        ('early_leave', 'Early Leave'),
    ], string='Issue Type', required=True)
    date = fields.Date(
        string='Date',
        help='Set once at creation from attendance_id.check_in — plain, not '
             'computed. attendance_id can later be nulled by a re-download '
             '(see its ondelete help) and a compute depending on it would '
             'wipe this back to False at that exact moment, destroying the '
             'only way left to match the orphaned line back to its date.',
    )
    hour_from = fields.Float(string='From')
    hour_to = fields.Float(string='To')
    duration_minutes = fields.Float(
        string='Duration (min)',
        compute='_compute_duration_minutes',
        store=True,
    )
    accepted_minutes = fields.Float(
        string='Accepted (min)',
        help='The approved portion of this issue in minutes. Cannot exceed the total duration.',
    )

    @api.depends('hour_from', 'hour_to')
    def _compute_duration_minutes(self):
        for line in self:
            line.duration_minutes = _issue_minutes(line.hour_from, line.hour_to)

    @api.constrains('accepted_minutes', 'duration_minutes')
    def _check_accepted_minutes(self):
        for line in self:
            if line.accepted_minutes < 0:
                raise ValidationError(
                    _('Accepted minutes cannot be negative.')
                )
            if line.accepted_minutes > line.duration_minutes:
                raise ValidationError(
                    _('Accepted minutes (%(accepted)s) cannot exceed the total duration (%(total)s).',
                      accepted=line.accepted_minutes,
                      total=line.duration_minutes)
                )

    @api.onchange('accepted_minutes')
    def _onchange_accepted_minutes(self):
        """Clamp accepted_minutes so it never exceeds duration or goes negative."""
        if self.accepted_minutes < 0:
            self.accepted_minutes = 0
        duration = _issue_minutes(self.hour_from, self.hour_to)
        if self.accepted_minutes > duration:
            self.accepted_minutes = duration
            return {
                'warning': {
                    'title': _('Value Adjusted'),
                    'message': _('Accepted minutes cannot exceed the total duration (%(total)s min).', total=duration),
                }
            }





