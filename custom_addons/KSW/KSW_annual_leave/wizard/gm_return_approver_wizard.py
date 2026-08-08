from markupsafe import Markup
from odoo import api, fields, models
from odoo.exceptions import UserError

SETTINGS_ADMIN_GROUP = 'base.group_system'

# The chain in order. A "return" is always a move *backwards*, so the targets
# on offer are exactly the steps sitting **strictly before** the request's
# current position — never the step it is already on, and never a later one.
# 'approved' is deliberately not a target: the end of the chain is reached by
# walking it, never by stamping it, so the approval trail is never fabricated.
_CHAIN_ORDER = (
    'pending_dm', 'pending_hr', 'pending_gm_initial', 'pending_acc',
    'pending_gm_final', 'pending_employee_signature',
)

# Steps at which the GM may use the wizard. (Their target list then falls out
# of the rule above and matches, step for step, the map they had before.)
_GM_RETURN_STATES = ('pending_gm_initial', 'pending_gm_final')

# A leave type with no KSW chain has one meaningful "return": undo the
# validation and put it back in the approver's queue.
_PLAIN_RETURN_TARGET = 'confirm'

# Stamps cleared when a step is re-opened: the target step's own stamp and
# every later one. The signature stamp is in every entry because the admin
# (unlike the GM) can return from a post-signature state.
_SIGNATURE_STAMPS = {
    'x_employee_signed_by': False, 'x_employee_signed_date': False,
}
_CLEAR_STAMPS = {
    'pending_dm': {
        'x_dm_approved_by': False, 'x_dm_approved_date': False,
        'x_hr_approved_by': False, 'x_hr_approved_date': False,
        'x_gm_initial_approved_by': False, 'x_gm_initial_approved_date': False,
        'x_acc_approved_by': False, 'x_acc_approved_date': False,
        'x_gm_final_approved_by': False, 'x_gm_final_approved_date': False,
        **_SIGNATURE_STAMPS,
    },
    'pending_hr': {
        'x_hr_approved_by': False, 'x_hr_approved_date': False,
        'x_gm_initial_approved_by': False, 'x_gm_initial_approved_date': False,
        'x_acc_approved_by': False, 'x_acc_approved_date': False,
        'x_gm_final_approved_by': False, 'x_gm_final_approved_date': False,
        **_SIGNATURE_STAMPS,
    },
    'pending_gm_initial': {
        'x_gm_initial_approved_by': False, 'x_gm_initial_approved_date': False,
        'x_acc_approved_by': False, 'x_acc_approved_date': False,
        'x_gm_final_approved_by': False, 'x_gm_final_approved_date': False,
        **_SIGNATURE_STAMPS,
    },
    'pending_acc': {
        'x_acc_approved_by': False, 'x_acc_approved_date': False,
        'x_gm_final_approved_by': False, 'x_gm_final_approved_date': False,
        **_SIGNATURE_STAMPS,
    },
    'pending_gm_final': {
        'x_gm_final_approved_by': False, 'x_gm_final_approved_date': False,
        **_SIGNATURE_STAMPS,
    },
    'pending_employee_signature': dict(_SIGNATURE_STAMPS),
}

_TARGET_LABELS = {
    'pending_dm':        'Direct Manager',
    'pending_hr':        'HR Approver',
    'pending_gm_initial': 'GM (Initial Review)',
    'pending_acc':       'Accounting',
    'pending_gm_final':  'GM (Final Approval)',
    'pending_employee_signature': 'HR Confirmation',
    # Only ever offered on a leave type with no chain, where "To Approve" (the
    # Odoo state name) says nothing about who is being asked to act.
    _PLAIN_RETURN_TARGET: 'Back to Approval',
}

_TARGET_GROUP = {
    'pending_hr':        'KSW_annual_leave.group_annual_leave_hr',
    'pending_gm_initial': 'KSW_annual_leave.group_annual_leave_gm',
    'pending_acc':       'KSW_annual_leave.group_annual_leave_acc',
    'pending_gm_final':  'KSW_annual_leave.group_annual_leave_gm',
    'pending_employee_signature': 'KSW_annual_leave.group_annual_leave_hr',
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

    # A Many2one, not a Selection: the radio widget resolves a Many2one's
    # domain against the record in front of it, while a Selection's options
    # come from the `get_views` payload — which the web client fetches with a
    # context stripped down to `lang` / `*_view_ref` and then caches on disk
    # per model. A per-record option list is only achievable this way.
    allowed_step_ids = fields.Many2many(
        'ksw.leave.return.step', compute='_compute_allowed_steps',
        string='Reachable Steps',
    )
    target_step_id = fields.Many2one(
        'ksw.leave.return.step', string='Return To', required=True,
        domain="[('id', 'in', allowed_step_ids)]",
        ondelete='cascade',
    )

    reason = fields.Text(string='Message to Approver', required=True)

    is_admin_mode = fields.Boolean(compute='_compute_is_admin_mode')

    @api.model
    def _allowed_targets(self, leave):
        """The steps this user may send `leave` back to, in chain order.

        One rule for both roles: **every step strictly before the request's
        current position**. Returning to the step it is already on is a no-op,
        and anything after it would be an approval nobody gave — so neither is
        offered. The GM additionally may only open the wizard at their own two
        steps; applying the rule there reproduces, step for step, the map they
        had before.

        A leave type with no chain has a single backward move: out of
        validation, into the approver's queue.
        """
        if not leave:
            return []
        is_admin = self.env.su or self.env.user.has_group(SETTINGS_ADMIN_GROUP)

        if not leave._uses_multi_step_chain(leave):
            if is_admin and leave.state != 'confirm':
                return [_PLAIN_RETURN_TARGET]
            return []

        current = leave.x_annual_approval_state
        if not is_admin and current not in _GM_RETURN_STATES:
            return []
        # A request that is validated or fully approved has left the chain
        # behind: everything in it is a legitimate target.
        position = (_CHAIN_ORDER.index(current) if current in _CHAIN_ORDER
                    else len(_CHAIN_ORDER))
        return list(_CHAIN_ORDER[:position])

    @api.model
    def _steps_for_codes(self, codes):
        """The step records for `codes`, in chain order."""
        if not codes:
            return self.env['ksw.leave.return.step']
        steps = self.env['ksw.leave.return.step'].sudo().search(
            [('code', 'in', list(codes))])
        return steps.sorted(key=lambda s: codes.index(s.code))

    @api.depends_context('uid')
    @api.depends('leave_id', 'leave_id.state', 'leave_id.x_annual_approval_state')
    def _compute_allowed_steps(self):
        for wiz in self:
            leave = wiz.leave_id
            if leave and (self.env.su or self.env.user.has_group(
                    SETTINGS_ADMIN_GROUP)):
                leave = leave.sudo()
            wiz.allowed_step_ids = self._steps_for_codes(
                self._allowed_targets(leave))

    @api.depends_context('uid')
    def _compute_is_admin_mode(self):
        is_admin = self.env.su or self.env.user.has_group(SETTINGS_ADMIN_GROUP)
        for wiz in self:
            wiz.is_admin_mode = is_admin

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
        leave_state_before = leave.state
        target = self.target_step_id.code
        is_admin = self.env.su or self.env.user.has_group(SETTINGS_ADMIN_GROUP)

        if not is_admin and not self.env.user.has_group(
                'KSW_annual_leave.group_annual_leave_gm'):
            raise UserError(
                'Only the General Manager can return a leave request to an approver.')

        if is_admin:
            # Authorisation is settled; elevate for the rest. The
            # administrator's leave scope comes from whichever HR tier they
            # happen to hold, and this action must not depend on that (see
            # gotcha #11 — auth first, then sudo).
            leave = leave.sudo()

        # The radio only offers reachable steps, but the check has to hold
        # over RPC too.
        valid_targets = self._allowed_targets(leave)
        if target not in valid_targets:
            valid_labels = ', '.join(_TARGET_LABELS[t] for t in valid_targets)
            raise UserError(
                'From "%s" you can only return to: %s.'
                % (current or leave.state, valid_labels or '(nothing)')
            )

        if is_admin:
            # A paid settlement must not be left hanging off a request that
            # has been pushed back into the approval chain, and cancelling it
            # would re-collect its installments next month (SLIP/11307).
            if leave._has_confirmed_payslip():
                raise UserError(
                    'This request already has a confirmed (paid) payslip. '
                    'Handle the payslip first — returning the request now '
                    'would strand it, and cancelling a paid payslip '
                    're-collects its deductions on the next run.')
            if leave.state != 'confirm':
                # Undo the Odoo validation but keep every figure the
                # approvers entered — see _move_validate_leave_to_confirm.
                # sudo(): core routes a state write through
                # _check_approval_update, which wants the Time Off Officer
                # group; a Settings Administrator need not hold it. The
                # authorisation was checked above.
                leave.with_context(
                    ksw_keep_approval_data=True,
                )._move_validate_leave_to_confirm()

        if target == _PLAIN_RETURN_TARGET:
            # A leave type with no KSW chain: being back in 'confirm' (done
            # just above) *is* the return, there is no chain state to stamp.
            write_vals = {}
        else:
            write_vals = dict(_CLEAR_STAMPS.get(target, {}))
            write_vals['x_annual_approval_state'] = target
        if write_vals:
            leave.write(write_vals)

        target_label = _TARGET_LABELS[target]
        # A leave with no chain (or one already validated) has no chain state
        # to name — fall back to the Odoo state label so the note still says
        # where the request came from.
        from_label = dict(
            self.env['hr.leave']._fields['x_annual_approval_state'].selection
        ).get(current) or dict(
            self.env['hr.leave']._fields['state']._description_selection(self.env)
        ).get(leave_state_before, leave_state_before)

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
        if target in ('pending_dm', _PLAIN_RETURN_TARGET):
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
