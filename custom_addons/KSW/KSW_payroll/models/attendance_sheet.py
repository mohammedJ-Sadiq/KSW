from odoo import _, api, models


class KswAttendanceSheet(models.Model):
    """Payroll's own confirmation rule for the monthly attendance sheet.

    KSW_attendance_sheet cannot ask this question itself: `x_return_state`
    is defined in KSW_annual_leave, and neither module depends on the
    other. KSW_payroll depends on both, so the rule lives here.
    """
    _inherit = 'ksw.attendance.sheet'

    def _unresolved_return_leaves(self):
        """Overlapping leaves whose return nobody has confirmed."""
        self.ensure_one()
        _date_from, date_to = self._period_bounds()
        return self.env['hr.payslip']._get_unresolved_vacation_leaves(
            self.employee_id.id, date_to)

    def _leave_coverage_end(self, leave, period_end):
        """An unconfirmed return has no end date yet.

        A vacation running 25 Jun → 16 Aug whose return nobody confirmed
        does NOT mean the employee was back on the 17th — it means nobody
        knows. Stopping the coverage on the requested end date would leave
        17–31 Aug marked Attended, i.e. the sheet asserting a return that
        never happened, and it would disagree with payroll twice over:
        the batch refuses to produce a payslip for this employee at all
        while the return is open, so those "attended" days pay nothing.

        So the leave covers through the end of the period until the return
        is confirmed. Confirming it calls _shorten_to_confirmed_return,
        which trims request_date_to to the real return date — at which
        point this override stops applying on its own and the ordinary
        end date takes over.
        """
        self.ensure_one()
        # Any leave using the return system, not annual only: an unpaid
        # leave whose return nobody confirmed leaves exactly the same hole —
        # nobody knows when the employee came back.
        is_open_return = leave.x_return_state == 'on_vacation'
        if is_open_return and leave.request_date_from:
            return max(period_end,
                       super()._leave_coverage_end(leave, period_end))
        return super()._leave_coverage_end(leave, period_end)

    def _confirmation_owner(self):
        """A pending return is held by the LEAVE manager, not the sheet's.

        `manager_id` is `employee_id.parent_id`; only
        `employee_id.leave_manager_id` can press Confirm Return. They are
        different fields and they do diverge in this database, so naming
        the sheet's manager here would send the supervisor to the wrong
        person — the single most confusing thing this screen could do.
        """
        self.ensure_one()
        if self.state != 'confirmed':
            unresolved = self._unresolved_return_leaves()
            if unresolved:
                leave_manager = unresolved[0].sudo().employee_id.leave_manager_id
                if leave_manager:
                    return leave_manager
        return super()._confirmation_owner()

    def _confirmation_blockers(self):
        """Also refuse while an overlapping vacation return is unconfirmed.

        The payslip batch already refuses to process an employee whose
        annual leave is still sitting in `x_return_state = 'on_vacation'`
        (see hr.payslip._get_unresolved_vacation_leaves). If the sheet could
        be confirmed anyway, the supervisor would be asserting a month of
        attendance for someone the system still believes is away — the exact
        disagreement this whole gate exists to prevent. Same question, same
        answer, both sides.
        """
        blockers = super()._confirmation_blockers()
        self.ensure_one()

        for leave in self._unresolved_return_leaves():
            leave_manager = leave.sudo().employee_id.leave_manager_id
            blockers.append(_(
                'ON VACATION — %(employee)s has not been marked as returned. '
                '%(type)s ran %(from_)s → %(to_)s and the request is still '
                '"On Vacation".\n'
                '      Waiting on: %(manager)s (the Time Off manager) to '
                'open the request and press "Confirm Return".\n'
                '      Until then payroll cannot process this employee at '
                'all, so this month cannot be sent.',
                employee=self.employee_id.name or '',
                type=leave.holiday_status_id.name,
                from_=leave.request_date_from,
                to_=leave.request_date_to,
                manager=(leave_manager.name
                         if leave_manager
                         else _('nobody — the employee has no Time Off '
                                'manager set, so ask HR to set one')),
            ))
        return blockers

    # ------------------------------------------------------------------
    # Keep the display flags fresh from the leave side
    # ------------------------------------------------------------------

    @api.model
    def _refresh_blocked_for_leaves(self, leaves):
        """Recompute the blocked flags of every sheet these leaves touch.

        Confirming a return, approving a leave or refusing one all change
        whether a sheet is blocked without writing anything on the sheet,
        so nothing else would refresh it.
        """
        leaves = leaves.sudo()
        employee_ids = leaves.employee_id.ids
        if not employee_ids:
            return
        dates = [d for d in leaves.mapped('request_date_from') if d]
        dates += [d for d in leaves.mapped('request_date_to') if d]
        self._recompute_blocked_for_employees(
            employee_ids,
            date_from=min(dates) if dates else None,
            date_to=max(dates) if dates else None,
        )
