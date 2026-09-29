# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.fields import Domain
from odoo.exceptions import AccessError, UserError, ValidationError
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

    # Fields that decide how much of an issue is excused. Once the leave is
    # decided they are part of a paid (or refused) record and never move.
    _DECIDED_FIELDS = frozenset({
        'leave_id', 'issue_type', 'date', 'hour_from', 'hour_to',
        'accepted_minutes',
    })

    @api.model
    def _search(self, domain, *args, **kwargs):
        """Reads go through _search, not _check_access (Odoo 19 fetch()):
        only lines of leaves the user can read are ever returned."""
        if not self.env.su:
            readable_leaves = self.env['hr.leave'].with_context(
                active_test=False)._search([])
            domain = Domain(domain) & Domain('leave_id', 'in', readable_leaves)
        return super()._search(domain, *args, **kwargs)

    def _check_access(self, operation):
        """A line is part of its leave, so it follows the leave's access.

        Read needs read on the leave; create/write/unlink need write on it.
        Without this the model was open to every internal user, who could
        read anyone's lateness and raise anyone's accepted minutes (the
        figure that removes the late/early deduction).
        """
        result = super()._check_access(operation)
        if result or not any(self._ids):
            return result
        lines = self.sudo()
        leaves = lines.leave_id.with_env(self.env)
        allowed = leaves._filtered_access(
            'read' if operation == 'read' else 'write')
        forbidden_ids = [l.id for l in lines if l.leave_id not in allowed]
        if not forbidden_ids:
            return None
        forbidden = self.browse(forbidden_ids)
        return forbidden, lambda: AccessError(_(
            'You cannot %(op)s the attendance issue lines of a time off '
            'request you do not have access to.', op=operation))

    def _check_not_decided(self, changed_fields=None):
        """Refuse to change the excuse once the leave is decided.

        Approvers with broad write rules on hr.leave could otherwise still
        edit a validated request's minutes. System paths (the re-link after
        an attendance re-download runs under sudo) are exempt.
        """
        if self.env.su:
            return
        if changed_fields is not None and not (
                set(changed_fields) & self._DECIDED_FIELDS):
            return
        decided = self.sudo().filtered(
            lambda l: l.leave_id.state in ('validate', 'validate1',
                                           'refuse', 'cancel'))
        if decided:
            raise UserError(_(
                'The accepted minutes of a time off request cannot be changed '
                'once it has been approved, refused or cancelled.'))

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines._check_not_decided()
        return lines

    def write(self, vals):
        self._check_not_decided(vals.keys())
        res = super().write(vals)
        if 'leave_id' in vals:
            self._check_not_decided()
        return res

    def unlink(self):
        self._check_not_decided()
        return super().unlink()

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





