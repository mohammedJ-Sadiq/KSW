from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

class HrLeave(models.Model):
    _inherit = 'hr.leave'

    def _is_annual_leave_logic(self):
        """Check if the leave is an annual vacation handled by KSW_annual_leave."""
        return self.holiday_status_id and self.holiday_status_id.is_annual_leave

    def action_approve(self, check_state=True):
        """Enforce Direct Manager approval for non-annual leaves."""
        for leave in self:
            if not leave._is_annual_leave_logic() and leave.state == 'confirm':
                # Direct manager is leave_manager_id or parent_id.user_id
                manager_user = leave.employee_id.leave_manager_id or leave.employee_id.parent_id.user_id
                
                if not self.env.su:
                    if not manager_user:
                        raise UserError(_("No direct manager user found for %s. Please assign a manager with a linked user to this employee.") % leave.employee_id.name)
                    
                    if self.env.user != manager_user:
                        raise UserError(_("Only the direct manager (%s) can approve this step.") % manager_user.name)

        return super().action_approve(check_state=check_state)

    def _action_approve_attendance_based(self, check_state=True):
        """Override KSW_attendance_leave to remove second-step bypass."""
        attendance_leaves = self.filtered('x_attendance_ids')
        non_attendance = self - attendance_leaves

        if non_attendance:
            super(HrLeave, non_attendance)._action_approve_attendance_based(
                check_state=check_state)

        for leave in attendance_leaves:
            if not leave._is_annual_leave_logic():
                if check_state and leave.state != 'confirm':
                    raise ValidationError(_('Time off request must be confirmed ("To Approve") in order to approve it.'))

                current_employee = self.env.user.employee_id
                leave_no_track = leave.with_context(tracking_disable=True)

                if leave.holiday_status_id.leave_validation_type == 'both':
                    leave_no_track.write({
                        'state': 'validate1',
                        'first_approver_id': current_employee.id,
                    })
                else:
                    leave_no_track.write({
                        'state': 'validate',
                        'first_approver_id': current_employee.id,
                    })
                leave._validate_leave_request()
            else:
                super(HrLeave, leave)._action_approve_attendance_based(check_state=check_state)

    def _action_validate(self, check_state=True):
        """Override KSW_attendance_leave to ensure strict 2nd step for attendance leaves.

        This is the single choke point every route uses to finalise a leave
        into 'validate' — action_approve() calls it directly whenever
        can_validate is already True, which core grants to anyone holding
        hr_holidays.group_hr_holidays_user/_manager (is_officer) regardless
        of the current state. That let a Direct Manager who also happens to
        hold a Time-Off Officer/Administrator group jump confirm -> validate
        (or even refuse -> validate) in one click, completely skipping the
        HR Manager. The check below is therefore unconditional and applies
        before any state-specific branching, not just to the validate1 ->
        validate transition.
        """
        if not self.env.su:
            for leave in self:
                if not leave._is_annual_leave_logic() and leave.validation_type == 'both':
                    hr_manager = leave.company_id.x_hr_leave_manager_id
                    if not hr_manager:
                        raise UserError(_("HR Leave Manager is not configured in settings."))
                    if self.env.user != hr_manager:
                        raise UserError(_("Only the configured HR Manager (%s) can give the final approval.") % hr_manager.name)

        attendance_leaves = self.filtered('x_attendance_ids')
        non_attendance = self - attendance_leaves

        if non_attendance:
            super(HrLeave, non_attendance)._action_validate(check_state=check_state)

        for leave in attendance_leaves:
            if not leave._is_annual_leave_logic():
                # Now replicate the write to 'validate' but correctly
                current_employee = self.env.user.employee_id
                att_no_track = leave.with_context(tracking_disable=True)
                
                if leave.state == 'validate1':
                    att_no_track.write({
                        'state': 'validate',
                        'second_approver_id': current_employee.id,
                    })
                else:
                    att_no_track.write({
                        'state': 'validate',
                        'first_approver_id': current_employee.id,
                    })
                
                leave._validate_leave_request()
                if not self.env.context.get('leave_fast_create'):
                    leave.filtered(lambda h: h.validation_type != 'no_validation').activity_update()
            else:
                super(HrLeave, leave)._action_validate(check_state=check_state)

    def _get_responsible_for_approval(self):
        """Direct activities to the configured HR Manager for the second step."""
        if not self._is_annual_leave_logic():
            if self.validation_type == 'both' and self.state == 'validate1':
                hr_manager = self.company_id.x_hr_leave_manager_id
                if hr_manager:
                    return hr_manager
        return super()._get_responsible_for_approval()

    # ==================================================================
    # Deletion — the request may be withdrawn as long as the next
    # approver in the DM -> HR chain hasn't acted yet.
    # ==================================================================

    @api.ondelete(at_uninstall=False)
    def _unlink_if_correct_states(self):
        """Let whoever is waiting on the next step withdraw the request.

        - state == 'confirm' (DM hasn't approved yet): the creator of the
          request may delete it. This covers a supervisor/assistant who
          raised the leave on the employee's behalf, not just the employee
          themselves (Odoo core's own-leave rule only recognises
          employee_id.user_id == env.user).
        - state == 'validate1' (DM approved, HR hasn't yet): the DM may
          delete it, since they are the one who put it there and can
          equally decide to withdraw it instead of waiting on HR.

        Leaves that don't qualify (wrong state, wrong user, annual leaves —
        handled by KSW_annual_leave's own chain-aware override) fall
        through to super(), which still grants its own bypasses (Settings
        Administrator, Time Off Administrator, own-untouched-request, etc.)
        — never dropped, per the mixed-batch filter rule.
        """
        remaining = self.browse()
        for leave in self:
            if leave._is_annual_leave_logic() or leave.state not in ('confirm', 'validate1'):
                remaining += leave
                continue

            user = self.env.user
            manager_user = leave.employee_id.leave_manager_id or leave.employee_id.parent_id.user_id
            hr_manager = leave.company_id.x_hr_leave_manager_id

            if leave.state == 'confirm':
                allowed = user == leave.create_uid or (manager_user and user == manager_user)
            else:  # validate1
                allowed = (manager_user and user == manager_user) or (hr_manager and user == hr_manager)

            if not allowed:
                remaining += leave

        if remaining:
            return super(HrLeave, remaining)._unlink_if_correct_states()
        return None
