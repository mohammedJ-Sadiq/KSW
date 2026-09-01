from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError


class HrLeaveUnpaid(models.Model):
    _inherit = 'hr.leave'

    # ------------------------------------------------------------------
    # View helper fields (stored for reliable invisible expressions)
    # ------------------------------------------------------------------

    x_is_unpaid_leave_type = fields.Boolean(
        string='Is Unpaid Leave Type',
        related='holiday_status_id.is_unpaid_leave',
        store=True,
        help='Stored related field for reliable use in view invisible expressions.',
    )

    # ------------------------------------------------------------------
    # Unpaid-specific accounting fields (filled by Accountant)
    # ------------------------------------------------------------------

    x_financial_consideration_excess = fields.Float(
        string='Financial Consideration for Excess Leave',
        digits=(16, 2), copy=False, tracking=True,
        help='Financial consideration for excess unpaid leave days (filled by Accounting).',
    )
    x_financial_consideration_excess_description = fields.Text(
        string='Financial Consideration Description', copy=False,
    )
    x_visa_cost_recovery = fields.Float(
        string='Visa Cost Recovery for Excess Leave',
        digits=(16, 2), copy=False, tracking=True,
        help='Visa cost recovery for excess unpaid leave days (filled by Accounting).',
    )
    x_visa_cost_recovery_description = fields.Text(
        string='Visa Cost Recovery Description', copy=False,
    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_unpaid_leave(self, leave):
        """Check if the leave type is flagged as unpaid leave."""
        return (
            leave.holiday_status_id
            and leave.holiday_status_id.is_unpaid_leave
        )

    def _is_unpaid_multi(self, leave):
        """Check if the leave type uses unpaid multi-step approval."""
        return (
            leave.holiday_status_id
            and leave.holiday_status_id.leave_validation_type == 'unpaid_multi'
        )

    def _excuses_absence(self):
        """An unpaid leave explains the absence but does not pay for it.

        The absence records stay linked to the leave (so the attendance view
        and the report still show which request the day belongs to), but they
        must NOT be flagged covered: the day has to stay absent so ATT_ABS /
        ATTDED deduct it on the payslip.  Before this, an unpaid month was
        paid in full — the exact opposite of what was requested.
        """
        return super()._excuses_absence().filtered(
            lambda l: not self._is_unpaid_leave(l))

    # ------------------------------------------------------------------
    # Return confirmation — the DM closes the leave on the actual date
    # ------------------------------------------------------------------

    def _uses_unpaid_return(self):
        """True when the *unpaid* return handling applies to this leave.

        A type flagged both `is_annual_leave` and `is_unpaid_leave`
        ("Unpaid Vacation") keeps the annual behaviour end to end — it is
        settled against the annual balance and its return drives the accrual
        restart.  Only a purely unpaid leave takes the branch below.
        """
        self.ensure_one()
        return (
            self._is_unpaid_leave(self)
            and not self._is_annual_leave(self)
        )

    def _shorten_to_confirmed_return(self):
        """End the unpaid leave on the day the employee actually came back.

        The opposite of the annual case in one decisive respect: there the
        money is already paid and the duration is deliberately preserved, so
        shortening only changes the period the request *covers*.  Unpaid days
        are deducted in arrears, so here the duration **must** move — every
        day trimmed off the end is a day the employee is paid for again.
        `_compute_duration` re-derives it from the calendar, which is exactly
        what we want, so no figures are captured or restored.
        """
        self.ensure_one()
        if not self._uses_unpaid_return():
            return super()._shorten_to_confirmed_return()
        if not (self.x_return_date and self.request_date_from
                and self.request_date_to):
            return False

        new_end = self.x_return_date - timedelta(days=1)
        if new_end >= self.request_date_to:
            # Back on or after the planned end — nothing to shorten, and a
            # late return is not an extension of the leave.
            return False
        if new_end < self.request_date_from:
            # Back before the leave began; too odd to fix silently.
            return False

        old_end = self.request_date_to
        leave = self.sudo().with_context(_skip_toggle_validity=True)
        leave.write({'request_date_to': new_end})
        leave.env.flush_all()

        # Absences after the return are no longer this leave's business —
        # from that day the employee is attending, and any gap is an
        # ordinary absence to be judged on its own.
        stale = leave.x_attendance_ids.filtered(
            lambda a: a.check_in and a.check_in.date() > new_end)
        if stale:
            leave.write({'x_attendance_ids': [(3, a.id) for a in stale]})
            stale._recompute_deductions()

        # Sheet employees: the lock was written for the original range.
        # Unlock restores every line it owns, then re-lock marks only the
        # shortened range absent — so the days after the return go back to
        # attended.
        self._unlock_attendance_sheet_lines(self)
        self._lock_attendance_sheet_lines(self)
        return old_end

    def _apply_confirmed_return(self):
        """Unpaid: correct the period, then refresh the accrual — nothing else.

        Deliberately does NOT call `_sync_opening_reset_to_return`.  That
        moves `ksw.annual.leave.x_opening_reset_date` onto the return date and
        restarts the annual accrual **from zero**, which is right for a
        vacation (taking it consumes the entitlement) and catastrophic for an
        unpaid leave: the employee would lose their whole accrued balance for
        having taken leave that paid them nothing.
        """
        self.ensure_one()
        if not self._uses_unpaid_return():
            return super()._apply_confirmed_return()

        old_end = self._shorten_to_confirmed_return()
        if old_end:
            self.sudo().message_post(
                body=Markup(
                    '<strong>&#9986; Unpaid Period Shortened to the Actual '
                    'Return</strong><br/>'
                    '<b>Ends:</b> %(old_end)s &#8594; %(new_end)s<br/>'
                    '<b>Unpaid days:</b> %(days).0f<br/>'
                    '<i>The employee is recorded as attending from '
                    '%(return_date)s, so those days are paid again.</i>'
                ) % {
                    'old_end': old_end,
                    'new_end': self.request_date_to,
                    'days': self.number_of_days,
                    'return_date': self.x_return_date,
                },
                subtype_xmlid='mail.mt_note',
            )
        # Unpaid days reduce effective service, and there are now fewer.
        self.env['ksw.annual.leave']._refresh_accrual_for_employees(
            self.employee_id.ids)

    def _multi_step_validation_types(self):
        """Declare 'unpaid_multi' as a KSW multi-step chain.

        Lets KSW_annual_leave's chain/type re-sync (_resync_multi_step_chain)
        cover unpaid leaves without hardcoding a validation type it doesn't own.
        """
        return super()._multi_step_validation_types() | {'unpaid_multi'}

    # ------------------------------------------------------------------
    # Duration: calendar-day counting (same as annual, no full-clearance)
    # ------------------------------------------------------------------

    @api.depends('holiday_status_id')
    def _compute_duration(self):
        unpaid = self.filtered(self._is_unpaid_leave)
        remaining = self - unpaid
        if remaining:
            super(HrLeaveUnpaid, remaining)._compute_duration()
        for leave in unpaid:
            cal_days, cal_hours = self._annual_cal_days(leave)
            leave.number_of_days = cal_days
            leave.number_of_hours = cal_hours
            # Ensure full-clearance fields stay 0 for unpaid
            leave.x_actual_vacation_days = 0
            leave.x_clearance_balance = 0
            leave.x_exceeds_annual_balance = False

    def _get_durations(self, check_leave_type=True, resource_calendar=None):
        unpaid = self.filtered(self._is_unpaid_leave)
        remaining = self - unpaid
        result = {}
        if remaining:
            result.update(super(HrLeaveUnpaid, remaining)._get_durations(
                check_leave_type=check_leave_type,
                resource_calendar=resource_calendar,
            ))
        for leave in unpaid:
            result[leave.id] = self._annual_cal_days(leave)
        return result

    def _get_number_of_days(self, date_from, date_to, employee_id):
        if self and self._is_unpaid_leave(self):
            if date_from and date_to:
                start = (
                    date_from.date() if hasattr(date_from, 'date')
                    else date_from
                )
                end = (
                    date_to.date() if hasattr(date_to, 'date')
                    else date_to
                )
                cal_days = (end - start).days + 1
                employee = self.env['hr.employee'].browse(employee_id)
                daily_hours = (
                    self._get_daily_work_hours(employee)
                    if employee_id else 8.0
                )
                return {'days': cal_days, 'hours': cal_days * daily_hours}
            return {'days': 0, 'hours': 0}
        return super()._get_number_of_days(date_from, date_to, employee_id)

    # ------------------------------------------------------------------
    # Hide standard approve/validate for unpaid_multi
    # ------------------------------------------------------------------

    @api.depends('state', 'employee_id', 'department_id')
    def _compute_can_approve(self):
        unpaid_multi = self.filtered(self._is_unpaid_multi)
        remaining = self - unpaid_multi
        if remaining:
            super(HrLeaveUnpaid, remaining)._compute_can_approve()
        for leave in unpaid_multi:
            leave.can_approve = False

    @api.depends('state', 'employee_id', 'department_id')
    def _compute_can_validate(self):
        unpaid_multi = self.filtered(self._is_unpaid_multi)
        remaining = self - unpaid_multi
        if remaining:
            super(HrLeaveUnpaid, remaining)._compute_can_validate()
        for leave in unpaid_multi:
            leave.can_validate = False

    # ------------------------------------------------------------------
    # Create hook — start multi-step chain
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for leave in records:
            if self._is_unpaid_multi(leave):
                leave.sudo().write({
                    'x_annual_approval_state': 'pending_dm',
                })
                self._notify_pending_approvers(leave, 'pending_dm')
        return records

    # ------------------------------------------------------------------
    # action_approve intercept
    # ------------------------------------------------------------------

    def action_approve(self, check_state=True):
        unpaid_multi = self.filtered(self._is_unpaid_multi)
        remaining = self - unpaid_multi
        if unpaid_multi:
            for leave in unpaid_multi:
                if leave.x_annual_approval_state == 'pending_dm':
                    leave.action_dm_approve()
        if remaining:
            return super(HrLeaveUnpaid, remaining).action_approve(
                check_state=check_state)
        return True

    # ------------------------------------------------------------------
    # Override Accounting Approve for unpaid — no payslip fields,
    # just record unpaid-specific accounting fields
    # ------------------------------------------------------------------

    def action_acc_approve(self):
        """Override: for unpaid leaves, log the unpaid accounting fields.
        For combined annual+unpaid leaves, also log unpaid fields."""
        unpaid = self.filtered(self._is_unpaid_leave)
        remaining = self - unpaid

        if remaining:
            super(HrLeaveUnpaid, remaining).action_acc_approve()

        # For combined annual leaves, also log the unpaid accounting fields
        combined = remaining.filtered(
            lambda l: l.x_excess_days_accepted and l.x_unpaid_portion_days > 0
        )
        for leave in combined:
            body = Markup('')
            if leave.x_financial_consideration_excess:
                body += Markup(
                    '<b>Financial Consideration for Excess Leave:</b>'
                    ' %(amt).2f SAR'
                ) % {'amt': leave.x_financial_consideration_excess}
                if leave.x_financial_consideration_excess_description:
                    body += Markup(' — %(desc)s') % {
                        'desc': leave.x_financial_consideration_excess_description}
                body += Markup('<br/>')
            if leave.x_visa_cost_recovery:
                body += Markup(
                    '<b>Visa Cost Recovery for Excess Leave:</b>'
                    ' %(amt).2f SAR'
                ) % {'amt': leave.x_visa_cost_recovery}
                if leave.x_visa_cost_recovery_description:
                    body += Markup(' — %(desc)s') % {
                        'desc': leave.x_visa_cost_recovery_description}
                body += Markup('<br/>')
            if body:
                leave.message_post(
                    body=body,
                    subtype_xmlid='mail.mt_note',
                )

        for leave in unpaid:
            self._check_group(
                'KSW_annual_leave.group_annual_leave_acc',
                'Only Accounting Approvers can approve this step.',
            )
            if leave.x_annual_approval_state != 'pending_acc':
                raise UserError(
                    'This leave is not pending accounting approval.')
            leave.write({
                'x_annual_approval_state': 'pending_gm_final',
                'x_acc_approved_by': self.env.user.employee_id.id,
                'x_acc_approved_date': fields.Datetime.now(),
            })
            body = Markup(
                '<strong>✅ Step 4 — Accounting Approval (Unpaid)</strong>'
                '<br/><b>Approved by:</b> %(user)s<br/>'
            ) % {'user': self.env.user.name}
            if leave.x_financial_consideration_excess:
                body += Markup(
                    '<b>Financial Consideration for Excess Leave:</b>'
                    ' %(amt).2f SAR'
                ) % {'amt': leave.x_financial_consideration_excess}
                if leave.x_financial_consideration_excess_description:
                    body += Markup(' — %(desc)s') % {
                        'desc': leave.x_financial_consideration_excess_description}
                body += Markup('<br/>')
            if leave.x_visa_cost_recovery:
                body += Markup(
                    '<b>Visa Cost Recovery for Excess Leave:</b>'
                    ' %(amt).2f SAR'
                ) % {'amt': leave.x_visa_cost_recovery}
                if leave.x_visa_cost_recovery_description:
                    body += Markup(' — %(desc)s') % {
                        'desc': leave.x_visa_cost_recovery_description}
                body += Markup('<br/>')
            leave.message_post(
                body=body,
                subtype_xmlid='mail.mt_note',
            )

    # ------------------------------------------------------------------
    # Override GM Final → no payslip, no return tracking for unpaid
    # ------------------------------------------------------------------

    def action_gm_final_approve(self):
        """Override: for unpaid leaves, skip payslip and return tracking."""
        unpaid = self.filtered(self._is_unpaid_leave)
        remaining = self - unpaid

        if remaining:
            super(HrLeaveUnpaid, remaining).action_gm_final_approve()

        if unpaid:
            for leave in unpaid:
                self._check_department_gm(leave)
                if leave.x_annual_approval_state != 'pending_gm_final':
                    raise UserError(
                        'This leave is not pending GM final approval.')

                leave.write({
                    'x_annual_approval_state': 'approved',
                    'x_gm_final_approved_by':
                        self.env.user.employee_id.id,
                    'x_gm_final_approved_date': fields.Datetime.now(),
                })
                # NO payslip creation for unpaid leave
                leave.message_post(
                    body=Markup(
                        '<strong>✅ Step 5 — GM Final Approval '
                        '(Unpaid)</strong>'
                        '<br/><b>Approved by:</b> %(approver)s<br/>'
                        '<b>Status:</b> Fully approved — no payslip.'
                    ) % {'approver': self.env.user.name},
                    subtype_xmlid='mail.mt_note',
                )

            # Standard Odoo validation
            unpaid._action_validate(check_state=False)

    # ------------------------------------------------------------------
    # _action_validate — lock attendance sheet lines + refresh accrual
    # ------------------------------------------------------------------

    def _reset_annual_multi_fields(self):
        """Extend reset to also clear unpaid-specific accounting fields."""
        super()._reset_annual_multi_fields()
        self.write({
            'x_financial_consideration_excess': 0,
            'x_financial_consideration_excess_description': False,
            'x_visa_cost_recovery': 0,
            'x_visa_cost_recovery_description': False,
        })

    def _action_validate(self, check_state=True):
        result = super()._action_validate(check_state=check_state)

        unpaid = self.filtered(self._is_unpaid_leave)
        annual = self.filtered(self._is_annual_leave)

        # Lock attendance sheet lines for BOTH annual and unpaid leaves
        for leave in (unpaid | annual):
            self._lock_attendance_sheet_lines(leave)

        if unpaid:
            # The return has to be confirmed by the direct manager, exactly
            # like an annual vacation: nobody but the DM knows the day the
            # employee actually came back, and on unpaid leave that date is
            # the difference between a deducted day and a paid one.  Until
            # it is confirmed the payslip batch skips the employee
            # (hr.payslip._get_unresolved_vacation_leaves).
            #
            # Filtered on 'not_applicable' so a type flagged BOTH annual and
            # unpaid ("Unpaid Vacation") is not stamped twice — the annual
            # _action_validate above has already done it.
            pending_return = unpaid.filtered(
                lambda l: l.x_return_state == 'not_applicable')
            if pending_return:
                pending_return.write({'x_return_state': 'on_vacation'})
                pending_return._notify_return_confirmation_due()

            # Refresh accrual — unpaid days now reduce effective service
            emp_ids = unpaid.mapped('employee_id').ids
            self.env['ksw.annual.leave']._refresh_accrual_for_employees(
                emp_ids)

        return result

    # ------------------------------------------------------------------
    # action_refuse / reset / draft / unlink — unlock lines + accrual
    # ------------------------------------------------------------------

    def action_refuse(self):
        unpaid = self.filtered(self._is_unpaid_leave)
        annual = self.filtered(self._is_annual_leave)
        unpaid_multi = self.filtered(self._is_unpaid_multi)
        unpaid_emp_ids = unpaid.mapped('employee_id').ids

        # Unlock BEFORE super (super may change state)
        for leave in (unpaid | annual):
            self._unlock_attendance_sheet_lines(leave)

        result = super().action_refuse()

        # There is no longer a return to confirm; leaving the stamp behind
        # would block the employee's payslip over a refused request.
        # (Annual leaves are reset by the super() call itself.)
        (unpaid - annual)._reset_return_tracking()

        if unpaid_multi:
            unpaid_multi._reset_annual_multi_fields()

        if unpaid_emp_ids:
            self.env['ksw.annual.leave']._refresh_accrual_for_employees(
                unpaid_emp_ids)

        return result

    def _move_validate_leave_to_confirm(self):
        unpaid_multi = self.filtered(self._is_unpaid_multi)
        unpaid = self.filtered(self._is_unpaid_leave)
        annual = self.filtered(self._is_annual_leave)
        unpaid_emp_ids = unpaid.mapped('employee_id').ids

        for leave in (unpaid | annual):
            self._unlock_attendance_sheet_lines(leave)

        # See the KSW_annual_leave override: a targeted admin return keeps the
        # approval data and picks its own target state.
        keep_data = self.env.context.get('ksw_keep_approval_data')

        if unpaid_multi and not keep_data:
            unpaid_multi._reset_annual_multi_fields()

        result = super()._move_validate_leave_to_confirm()

        (unpaid - annual)._reset_return_tracking()

        if unpaid_multi and not keep_data:
            unpaid_multi.write({'x_annual_approval_state': 'pending_dm'})

        if unpaid_emp_ids:
            self.env['ksw.annual.leave']._refresh_accrual_for_employees(
                unpaid_emp_ids)

        return result

    def action_draft(self):
        unpaid = self.filtered(self._is_unpaid_leave)
        annual = self.filtered(self._is_annual_leave)
        unpaid_emp_ids = unpaid.mapped('employee_id').ids

        for leave in (unpaid | annual):
            self._unlock_attendance_sheet_lines(leave)

        result = super().action_draft()

        unpaid_multi = self.filtered(self._is_unpaid_multi)
        if unpaid_multi:
            unpaid_multi._reset_annual_multi_fields()
            for leave in unpaid_multi:
                leave.x_annual_approval_state = 'pending_dm'

        if unpaid_emp_ids:
            self.env['ksw.annual.leave']._refresh_accrual_for_employees(
                unpaid_emp_ids)

        return result

    def unlink(self):
        unpaid = self.filtered(self._is_unpaid_leave)
        annual = self.filtered(self._is_annual_leave)
        unpaid_emp_ids = unpaid.mapped('employee_id').ids

        for leave in (unpaid | annual):
            self._unlock_attendance_sheet_lines(leave)

        result = super().unlink()

        if unpaid_emp_ids:
            self.env['ksw.annual.leave']._refresh_accrual_for_employees(
                unpaid_emp_ids)

        return result

    # ------------------------------------------------------------------
    # Attendance sheet line lock / unlock
    # ------------------------------------------------------------------

    def _lock_attendance_sheet_lines(self, leave):
        """Mark attendance sheet lines as absent and lock them for the
        leave's date range.  Only affects sheet-type employees."""
        if not leave.employee_id or not leave.request_date_from:
            return
        # Only for attendance-sheet employees
        if not leave.employee_id.sudo().x_is_attendance_sheet:
            return

        date_from = leave.request_date_from
        date_to = leave.request_date_to or date_from

        lines = self.env['ksw.attendance.sheet.line'].sudo().search([
            ('sheet_id.employee_id', '=', leave.employee_id.id),
            ('date', '>=', date_from),
            ('date', '<=', date_to),
            ('x_leave_id', '=', False),
            ('is_attended', '=', True),
        ])
        if lines:
            # ksw_system_write bypasses both the off-day guard (the leave
            # range may include a weekly rest day, not directly editable
            # otherwise) and the leave-lock guard (x_leave_id isn't set
            # yet, so that one wouldn't fire anyway).
            lines.with_context(ksw_system_write=True).write({
                'is_attended': False,
            })
            lines.write({'x_leave_id': leave.id})
            # Sync attendance records (delete auto-generated ones)
            for sheet in lines.mapped('sheet_id'):
                sheet._sync_line_attendance(
                    lines.filtered(lambda l: l.sheet_id == sheet))

    def _unlock_attendance_sheet_lines(self, leave):
        """Restore attendance sheet lines locked by this leave."""
        lines = self.env['ksw.attendance.sheet.line'].sudo().search([
            ('x_leave_id', '=', leave.id),
        ])
        if lines:
            # Clear the lock first, then restore attended. ksw_system_write
            # bypasses the off-day guard (see _lock_attendance_sheet_lines).
            lines.write({'x_leave_id': False})
            lines.with_context(ksw_system_write=True).write({
                'is_attended': True,
            })
            # Re-sync attendance records
            for sheet in lines.mapped('sheet_id'):
                sheet._sync_line_attendance(
                    lines.filtered(lambda l: l.sheet_id == sheet))






