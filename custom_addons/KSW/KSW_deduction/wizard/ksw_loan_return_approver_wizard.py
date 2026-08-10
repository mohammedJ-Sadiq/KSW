from markupsafe import Markup
from odoo import api, fields, models
from odoo.exceptions import UserError

# The "administrator" tier for this module. Deliberately NOT
# base.group_system (Settings Administrator): unlike hr.leave, ksw.deduction
# grants that group no access at all (see security.xml), so a bare Settings
# Administrator couldn't even open a loan record. group_deduction_manager
# already has unconditional read/write on every deduction, loans included
# (ksw_deduction_rule_manager_all), so it is this module's real "can override
# anything" role — reuse it instead of inventing a new access grant.
ADMIN_OVERRIDE_GROUP = 'KSW_deduction.group_deduction_manager'

# The chain in order. A "return" is always a move *backwards*, so the
# targets on offer are exactly the steps sitting **strictly before** the
# request's current position — never the step it is already on, and never a
# later one. 'approved' is deliberately not reachable here: once the loan is
# disbursed, installment lines exist and unwinding it is the job of
# `action_reset_to_draft`, not this wizard.
_CHAIN_ORDER = ('pending_dm', 'pending_hr', 'pending_acc', 'pending_gm',
                 'pending_disbursement')

# The only step at which the GM (non-admin) may use the wizard.
_GM_RETURN_STATE = 'pending_gm'

_CLEAR_STAMPS = {
    'pending_dm': {
        'dm_approved_by': False, 'dm_approved_date': False,
        'hr_approved_by': False, 'hr_approved_date': False,
        'x_hr_no_penalties_confirmed': False,
        'acc_approved_by': False, 'acc_approved_date': False,
        'x_acc_budget_confirmed': False,
        'gm_approved_by': False, 'gm_approved_date': False,
    },
    'pending_hr': {
        'hr_approved_by': False, 'hr_approved_date': False,
        'x_hr_no_penalties_confirmed': False,
        'acc_approved_by': False, 'acc_approved_date': False,
        'x_acc_budget_confirmed': False,
        'gm_approved_by': False, 'gm_approved_date': False,
    },
    'pending_acc': {
        'acc_approved_by': False, 'acc_approved_date': False,
        'x_acc_budget_confirmed': False,
        'gm_approved_by': False, 'gm_approved_date': False,
    },
    'pending_gm': {
        'gm_approved_by': False, 'gm_approved_date': False,
    },
}

_TARGET_LABELS = {
    'pending_dm': 'Direct Manager',
    'pending_hr': 'HR Approver',
    'pending_acc': 'Accounting',
    'pending_gm': 'GM Final Approval',
}


class KswLoanReturnApproverWizard(models.TransientModel):
    _name = 'ksw.loan.return.approver.wizard'
    _description = 'GM: Return Loan Request to Approver'

    deduction_id = fields.Many2one(
        'ksw.deduction', string='Loan Request', required=True,
        ondelete='cascade',
    )
    employee_name = fields.Char(
        related='deduction_id.employee_id.name', readonly=True)
    amount = fields.Monetary(related='deduction_id.amount', readonly=True)
    installments = fields.Integer(
        related='deduction_id.installments', readonly=True)
    currency_id = fields.Many2one(
        related='deduction_id.currency_id', readonly=True)
    current_state_label = fields.Char(
        compute='_compute_current_state_label', readonly=True)

    # A Many2one, not a Selection: the radio widget resolves a Many2one's
    # domain against the record in front of it, while a Selection's options
    # come from the `get_views` payload — cached per model on the client, not
    # per record (see ksw.loan.return.step docstring).
    allowed_step_ids = fields.Many2many(
        'ksw.loan.return.step', compute='_compute_allowed_steps',
        string='Reachable Steps',
    )
    target_step_id = fields.Many2one(
        'ksw.loan.return.step', string='Return To', required=True,
        domain="[('id', 'in', allowed_step_ids)]",
        ondelete='cascade',
    )

    reason = fields.Text(string='Message to Approver', required=True)

    is_admin_mode = fields.Boolean(compute='_compute_is_admin_mode')

    @api.model
    def _allowed_targets(self, deduction):
        """The steps this user may send `deduction` back to, in chain order.

        One rule for both roles: **every step strictly before the request's
        current position**. Returning to the step it is already on is a
        no-op, and anything after it would be an approval nobody gave — so
        neither is offered. The GM additionally may only open the wizard at
        their own step (pending_gm); applying the rule there yields exactly
        the three earlier steps.
        """
        if not deduction or not deduction.is_loan:
            return []
        is_admin = self.env.su or self.env.user.has_group(ADMIN_OVERRIDE_GROUP)
        current = deduction.approval_state
        if not is_admin and current != _GM_RETURN_STATE:
            return []
        if current not in _CHAIN_ORDER:
            # Not (yet) submitted, refused, or already disbursed — nothing
            # sensible to return to.
            return []
        position = _CHAIN_ORDER.index(current)
        return list(_CHAIN_ORDER[:position])

    @api.model
    def _steps_for_codes(self, codes):
        """The step records for `codes`, in chain order."""
        if not codes:
            return self.env['ksw.loan.return.step']
        steps = self.env['ksw.loan.return.step'].sudo().search(
            [('code', 'in', list(codes))])
        return steps.sorted(key=lambda s: codes.index(s.code))

    @api.depends_context('uid')
    @api.depends('deduction_id', 'deduction_id.approval_state')
    def _compute_allowed_steps(self):
        for wiz in self:
            deduction = wiz.deduction_id
            if deduction and (self.env.su or self.env.user.has_group(
                    ADMIN_OVERRIDE_GROUP)):
                deduction = deduction.sudo()
            wiz.allowed_step_ids = self._steps_for_codes(
                self._allowed_targets(deduction))

    @api.depends_context('uid')
    def _compute_is_admin_mode(self):
        is_admin = self.env.su or self.env.user.has_group(ADMIN_OVERRIDE_GROUP)
        for wiz in self:
            wiz.is_admin_mode = is_admin

    def _compute_current_state_label(self):
        state_labels = dict(
            self.env['ksw.deduction']._fields['approval_state'].selection)
        for wiz in self:
            wiz.current_state_label = state_labels.get(
                wiz.deduction_id.approval_state, '')

    def action_confirm(self):
        self.ensure_one()
        deduction = self.deduction_id
        current = deduction.approval_state
        target = self.target_step_id.code
        is_admin = self.env.su or self.env.user.has_group(ADMIN_OVERRIDE_GROUP)

        if not is_admin and not self.env.user.has_group(
                'KSW_deduction.group_loan_gm'):
            raise UserError(
                'Only the General Manager can return a loan request to an approver.')

        if is_admin:
            deduction = deduction.sudo()

        # The radio only offers reachable steps, but the check has to hold
        # over RPC too.
        valid_targets = self._allowed_targets(deduction)
        if target not in valid_targets:
            valid_labels = ', '.join(_TARGET_LABELS[t] for t in valid_targets)
            raise UserError(
                'From "%s" you can only return to: %s.'
                % (current, valid_labels or '(nothing)')
            )

        write_vals = dict(_CLEAR_STAMPS.get(target, {}))
        write_vals['approval_state'] = target
        deduction.write(write_vals)

        target_label = _TARGET_LABELS[target]
        from_label = dict(deduction._fields['approval_state'].selection).get(
            current, current)

        deduction.sudo().message_post(
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

        # Reuses the same group/DM mapping the forward chain already
        # notifies with — the target step means "waiting on this approver"
        # either way.
        deduction._notify_pending_approvers(target)
        return {'type': 'ir.actions.act_window_close'}
