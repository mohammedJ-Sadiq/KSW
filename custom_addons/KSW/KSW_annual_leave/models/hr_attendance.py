import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class HrAttendance(models.Model):
    """A punch is proof the employee is back — tell the employee.

    For biometric employees the system already knows the return happened:
    a real punch dated on or after the vacation start, while the leave still
    sits in ``x_return_state = 'on_vacation'``, is direct evidence. Only the
    direct manager could act on it and only the direct manager was told,
    which is why the old daily manager reminder achieved nothing. The
    employee is the party with the incentive (their payslip is blocked), so
    the alert goes to them.
    """
    _inherit = 'hr.attendance'

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._flag_unconfirmed_vacation_returns()
        return records

    def _is_real_punch(self):
        """Records that represent the employee physically showing up.

        Excludes generated absence rows (`x_is_absent`) and the fabricated
        attendance the monthly attendance sheet writes for its employees
        (`x_is_auto_generated`) — neither is evidence of anything.

        `x_is_auto_generated` is declared by KSW_attendance_sheet, which
        this module does not depend on, so it is read only when present.
        """
        has_auto_flag = 'x_is_auto_generated' in self._fields
        return self.filtered(
            lambda a: a.check_in
            and a.employee_id
            and not a.x_is_absent
            and not (has_auto_flag and a.x_is_auto_generated)
        )

    def _flag_unconfirmed_vacation_returns(self):
        """Notify employees whose punch contradicts an open vacation."""
        punches = self._is_real_punch()
        if not punches:
            return

        HrLeave = self.env['hr.leave'].sudo()
        # One query for the whole batch: a device sync imports hundreds of
        # punches at a time and must not run a search per record.
        candidates = HrLeave.search([
            ('employee_id', 'in', punches.employee_id.ids),
            ('state', '=', 'validate'),
            ('holiday_status_id.is_annual_leave', '=', True),
            ('x_return_state', '=', 'on_vacation'),
            ('x_return_punch_notified_on', '=', False),
        ])
        if not candidates:
            return

        by_employee = {}
        for leave in candidates:
            by_employee.setdefault(leave.employee_id.id, HrLeave.browse())
            by_employee[leave.employee_id.id] |= leave

        for punch in punches:
            leaves = by_employee.get(punch.employee_id.id)
            if not leaves:
                continue
            punch_date = punch.check_in.date()
            # On or after the vacation START — not the end. An employee who
            # is back early is exactly the case worth catching, and it is
            # also the case the old end-date-based reminder never saw.
            due = leaves.filtered(
                lambda l: l.request_date_from
                and l.request_date_from <= punch_date
                and not l.x_return_punch_notified_on
            )
            for leave in due:
                try:
                    leave._notify_unconfirmed_return_punch(punch)
                except Exception:
                    # A failed notification must never abort a device sync
                    # or block the employee from clocking in.
                    _logger.exception(
                        'Failed to notify employee about unconfirmed return '
                        'on leave id=%s (attendance id=%s).',
                        leave.id, punch.id,
                    )
