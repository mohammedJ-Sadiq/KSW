from markupsafe import Markup
from odoo import fields, models
from odoo.exceptions import UserError

# Targets the GM may return to, keyed by current approval state
_VALID_RETURN_TARGETS = {
    'pending_gm_initial': {'pending_dm', 'pending_hr'},
    'pending_gm_final':   {'pending_dm', 'pending_hr', 'pending_gm_initial', 'pending_acc'},
}

# Approval stamps to clear when returning to a given target state.
# Clears the target step's own stamp and every subsequent step.
_CLEAR_STAMPS = {
    'pending_dm': {
        'x_dm_approved_by': False, 'x_dm_approved_date': False,
        'x_hr_approved_by': False, 'x_hr_approved_date': False,
        'x_gm_initial_approved_by': False, 'x_gm_initial_approved_date': False,
        'x_acc_approved_by': False, 'x_acc_approved_date': False,
        'x_gm_final_approved_by': False, 'x_gm_final_approved_date': False,
    },
    'pending_hr': {
        'x_hr_approved_by': False, 'x_hr_approved_date': False,
        'x_gm_initial_approved_by': False, 'x_gm_initial_approved_date': False,
        'x_acc_approved_by': False, 'x_acc_approved_date': False,
        'x_gm_final_approved_by': False, 'x_gm_final_approved_date': False,
    },
    'pending_gm_initial': {
        'x_gm_initial_approved_by': False, 'x_gm_initial_approved_date': False,
        'x_acc_approved_by': False, 'x_acc_approved_date': False,
        'x_gm_final_approved_by': False, 'x_gm_final_approved_date': False,
    },
    'pending_acc': {
        'x_acc_approved_by': False, 'x_acc_approved_date': False,
        'x_gm_final_approved_by': False, 'x_gm_final_approved_date': False,
    },
}

_TARGET_LABELS = {
    'pending_dm':        'Direct Manager',
    'pending_hr':        'HR Approver',
    'pending_gm_initial': 'GM (Initial Review)',
    'pending_acc':       'Accounting',
}

_TARGET_GROUP = {
    'pending_hr':        'KSW_annual_leave.group_annual_leave_hr',
    'pending_gm_initial': 'KSW_annual_leave.group_annual_leave_gm',
    'pending_acc':       'KSW_annual_leave.group_annual_leave_acc',
}


class GmReturnApproverWizard(models.TransientModel):
    _name = 'ksw.gm.return.approver.wizard'
    _description = 'GM: Return Annual Leave to Approver'

    leave_id = fields.Many2one(
        'hr.leave', string='Annual Leave', required=True, ondelete='cascade',
    )
    employee_name = fields.Char(related='leave_id.employee_id.name', readonly=True)
    leave_period = fields.Char(compute='_compute_leave_period', readonly=True)
    current_state_label = fields.Char(compute='_compute_current_state_label', readonly=True)

    target_state = fields.Selection([
        ('pending_dm',        'Direct Manager'),
        ('pending_hr',        'HR Approver'),
        ('pending_gm_initial', 'GM (Initial Review)'),
        ('pending_acc',       'Accounting'),
    ], string='Return To', required=True)

    reason = fields.Text(string='Message to Approver', required=True)

    def _compute_leave_period(self):
        for wiz in self:
            leave = wiz.leave_id
            if leave.request_date_from and leave.request_date_to:
                wiz.leave_period = '%s → %s (%g days)' % (
                    leave.request_date_from, leave.request_date_to,
                    leave.number_of_days,
                )
            else:
                wiz.leave_period = ''

    def _compute_current_state_label(self):
        state_labels = dict(self.env['hr.leave']._fields['x_annual_approval_state'].selection)
        for wiz in self:
            wiz.current_state_label = state_labels.get(
                wiz.leave_id.x_annual_approval_state, '')

    def action_confirm(self):
        self.ensure_one()
        leave = self.leave_id
        current = leave.x_annual_approval_state
        target = self.target_state

        if not self.env.su and not self.env.user.has_group(
                'KSW_annual_leave.group_annual_leave_gm'):
            raise UserError(
                'Only the General Manager can return a leave request to an approver.')

        valid_targets = _VALID_RETURN_TARGETS.get(current, set())
        if target not in valid_targets:
            valid_labels = ', '.join(_TARGET_LABELS[t] for t in valid_targets)
            raise UserError(
                'From "%s" you can only return to: %s.'
                % (current, valid_labels)
            )

        write_vals = dict(_CLEAR_STAMPS.get(target, {}))
        write_vals['x_annual_approval_state'] = target
        leave.write(write_vals)

        target_label = _TARGET_LABELS[target]
        from_label = dict(
            self.env['hr.leave']._fields['x_annual_approval_state'].selection
        ).get(current, current)

        leave.sudo().message_post(
            body=Markup(
                '<strong>↩ Returned for Revision — %(target)s</strong><br/>'
                '<b>Returned by:</b> %(gm)s<br/>'
                '<b>From step:</b> %(from)s<br/>'
                '<b>Message:</b> %(reason)s'
            ) % {
                'target': target_label,
                'gm':     self.env.user.name,
                'from':   from_label,
                'reason': self.reason,
            },
            subtype_xmlid='mail.mt_note',
        )

        self._notify_return(leave, target, target_label)
        return {'type': 'ir.actions.act_window_close'}

    def _notify_return(self, leave, target, target_label):
        """Send inbox notification to the target approver with the GM's reason."""
        if target == 'pending_dm':
            dm_user = leave.employee_id.leave_manager_id
            partner_ids = (
                [dm_user.partner_id.id] if dm_user and dm_user.partner_id else []
            )
        else:
            group_xmlid = _TARGET_GROUP.get(target)
            group = self.env.ref(group_xmlid, raise_if_not_found=False) if group_xmlid else None
            partner_ids = group.user_ids.mapped('partner_id').ids if group else []

        if not partner_ids:
            return

        leave.sudo().message_post(
            body=Markup(
                '<strong>&#9203; Action Required — %(target)s</strong><br/>'
                '<b>Employee:</b> %(employee)s<br/>'
                '<b>Period:</b> %(date_from)s &#8594; %(date_to)s<br/>'
                '<b>Days:</b> %(days).1f<br/>'
                '<b>GM\'s Message:</b> %(reason)s<br/>'
                'Please review, make any corrections, and re-submit your approval.'
            ) % {
                'target':    target_label,
                'employee':  leave.employee_id.name,
                'date_from': leave.request_date_from,
                'date_to':   leave.request_date_to,
                'days':      leave.number_of_days,
                'reason':    self.reason,
            },
            partner_ids=partner_ids,
            subtype_xmlid='mail.mt_comment',
        )
