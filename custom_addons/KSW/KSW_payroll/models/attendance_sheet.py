from odoo import _, api, models


class KswAttendanceSheet(models.Model):
    """Payroll's own confirmation rule for the monthly attendance sheet.

    KSW_attendance_sheet cannot ask this question itself: `x_return_state`
    is defined in KSW_annual_leave, and neither module depends on the
    other. KSW_payroll depends on both, so the rule lives here.
    """
    _inherit = 'ksw.attendance.sheet'

    def _unresolved_return_leaves(self):
        """Overlapping leaves whose return nobody has confirmed *and is due*.

        The question this blocker asks is "could this month contain days the
        employee actually worked, which the sheet is calling absent?" Those
        days exist only *after* the vacation's planned end — which is why the
        filter is on `request_date_to`, not on the leave overlapping at all.

        A vacation still running past the end of this month asks nothing of
        anyone: every day of the month is inside it, so the month is fully
        determined, and there is no return to confirm yet. Blocking it
        demanded that the Time Off manager press "Confirm Return" on a
        return that had not happened and would not for months — a deadlock
        with no legal move (KSWCO leave 5144, ALOMGIR HOSSAIN, 17 Sep →
        15 Dec 2026: the September sheet could not be released until
        mid-December).

        The month containing the planned end still blocks, which is the case
        the rule was written for. And a sheet claiming *attendance* during a
        covered day is still refused by the clashing-days rule in
        `_confirmation_blockers`, whatever this returns — so relaxing here
        cannot let an asserted-attendance contradiction through.
        """
        self.ensure_one()
        _date_from, date_to = self._period_bounds()
        leaves = self.env['hr.payslip']._get_unresolved_vacation_leaves(
            self.employee_id.id, date_to)
        return leaves.filtered(
            lambda l: (l.request_date_to or l.request_date_from) < date_to)

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

    # ------------------------------------------------------------------
    # Withdrawing a month payroll has already paid
    # ------------------------------------------------------------------

    def _paid_payslips(self):
        """Confirmed payslips covering this sheet's month."""
        self.ensure_one()
        date_from, date_to = self._period_bounds()
        return self.env['hr.payslip'].sudo().search([
            ('employee_id', '=', self.employee_id.id),
            ('state', '=', 'done'),
            ('date_from', '<=', date_to),
            ('date_to', '>=', date_from),
        ])

    def _reset_denial_reason(self, user):
        """Also refuse a supervisor once the month has actually been paid.

        `done` means the transfer already left the bank in this shop (the
        payslip is marked done *after* the payment), so the figures in the
        sheet are no longer a proposal — they are the record of what was
        paid. Withdrawing it would leave payroll reading zero attendance
        for a month that was paid in full, with nothing saying why.

        Administrators keep the route: correcting a paid month is real
        work someone has to be able to do, and they already own the
        Payslip Revision flow that goes with it. This only stops the
        supervisor's one-click undo from reaching that far.
        """
        reason = super()._reset_denial_reason(user)
        if reason or self.env.su or self._is_sheet_administrator(user):
            return reason
        slips = self._paid_payslips()
        if slips:
            return _(
                '%(period)s has already been paid (payslip %(slip)s is '
                'confirmed), so its attendance can no longer be withdrawn. '
                'Ask HR — correcting a paid month is a payslip revision.',
                period=self.display_name, slip=slips[0].number or slips[0].id,
            )
        return ''
