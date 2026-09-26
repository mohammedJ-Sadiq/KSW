from odoo import fields, models
from odoo.exceptions import UserError


class KswAttendanceSheetLineLeave(models.Model):
    _inherit = 'ksw.attendance.sheet.line'

    x_leave_id = fields.Many2one(
        'hr.leave', string='Linked Leave',
        ondelete='set null', readonly=True, copy=False,
        help='When set, this day is locked by an approved leave '
             'and cannot be toggled by the supervisor.',
    )

    def _filter_derivable_off_days(self):
        """A day locked by an approved leave is owned by that leave.

        The weekly-rest-day derivation must leave it alone — write()
        below refuses the change anyway, so without this the whole
        recompute (and the 'Mark All Absent' action that triggers it)
        would abort with a UserError.
        """
        return super()._filter_derivable_off_days().filtered(
            lambda l: not l.x_leave_id)

    def write(self, vals):
        if 'is_attended' in vals:
            # Only a write that would actually CHANGE a locked day is an
            # attempt to take it back from the leave. Re-asserting the value
            # the leave already put there is a no-op and must go through:
            # 'Mark All Absent' writes False over every workday of the month,
            # locked days included, and refusing that aborted the whole
            # action — leaving the supervisor no way to mark the rest of the
            # month absent when a vacation covers part of it.
            wanted = bool(vals['is_attended'])
            locked_by_leave = self.filtered(
                lambda l: l.x_leave_id and l.is_attended != wanted)
            if locked_by_leave:
                raise UserError(
                    'Cannot modify attendance for days locked by an '
                    'approved leave (%s).'
                    % ', '.join(
                        locked_by_leave.mapped(
                            'x_leave_id.holiday_status_id.name'
                        )
                    )
                )
        return super().write(vals)
