from datetime import timedelta

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

# The passport renewal charge is a fixed government fee. HR either owes it or
# does not; there is no case where it is a different figure, so the amount is
# derived from a tick box and can never be typed.
EXT_PASSPORT_FEE = 120.0

# Filled by HR, at the HR step only.
_EXT_HR_FIELDS = frozenset({
    'x_ext_work_permit_fee',
    'x_ext_iqama_fee',
    'x_ext_bank_fee',
    'x_ext_passport_fee_applied',
    'x_ext_violations_fee',
    'x_ext_extra_fee',
})
# Filled by Accounting, at the Accounting step only.
_EXT_ACC_FIELDS = frozenset({'x_ext_acc_note'})

# Derived from the fields above and never written by hand. Odoo 19 does NOT
# refuse a write to a stored readonly compute — it accepts it silently and the
# value sticks until something recomputes it, so `readonly` on the field and on
# the view is decoration, not a lock: an RPC call put 500 into the fixed 120
# SAR passport fee and the total followed it. Guarded in write() instead.
_EXT_DERIVED_FIELDS = frozenset({'x_ext_passport_fee', 'x_ext_total_fees'})

_EXT_HR_STATE = 'pending_hr'
_EXT_ACC_STATE = 'pending_acc'

# Everything the chain reset has to put back to nothing.
_EXT_RESET_VALUES = {
    'x_ext_work_permit_fee': 0.0,
    'x_ext_iqama_fee': 0.0,
    'x_ext_bank_fee': 0.0,
    'x_ext_passport_fee_applied': False,
    'x_ext_violations_fee': 0.0,
    'x_ext_extra_fee': 0.0,
    'x_ext_acc_note': False,
}


class HrLeaveExtension(models.Model):
    _inherit = 'hr.leave'

    # ------------------------------------------------------------------
    # View helper fields (stored for reliable invisible expressions)
    # ------------------------------------------------------------------

    x_is_leave_extension = fields.Boolean(
        string='Is Vacation Extension',
        related='holiday_status_id.is_leave_extension',
        store=True,
        help='Stored related field for reliable use in view invisible expressions.',
    )

    # ------------------------------------------------------------------
    # The link to the vacation being extended
    # ------------------------------------------------------------------

    x_extended_leave_id = fields.Many2one(
        'hr.leave', string='Extends Vacation',
        ondelete='restrict', index=True, copy=False, tracking=True,
        help='The approved vacation this request continues. The extension '
             'starts the day after that vacation ends.',
    )
    x_extension_ids = fields.One2many(
        'hr.leave', 'x_extended_leave_id', string='Extensions',
        help='Extension requests that continue this vacation.',
    )
    x_extendable_leave_ids = fields.Many2many(
        'hr.leave', string='Extendable Vacations',
        compute='_compute_extendable_leave_ids',
        help='The vacation this request may extend — the employee\'s current '
             'one, and only that. Published as a field because a Many2one '
             'domain can name a field but cannot call a method.',
    )
    x_can_extend = fields.Boolean(
        string='Can Be Extended',
        compute='_compute_can_extend',
        help='True on the one vacation the Extend button applies to, for a '
             'user entitled to request the extension.',
    )

    # ------------------------------------------------------------------
    # HR-filled fees (Step 2). Plain Floats, no model-level groups= :
    # they are read by invisible=/readonly= expressions on elements the
    # GM and Accounting also see, and a model gate drops the field from
    # fields_get() for them, which crashes the form (gotcha #31).
    # ------------------------------------------------------------------

    x_ext_work_permit_fee = fields.Float(
        string='Work Permit Fees', digits=(16, 2), copy=False, tracking=True,
        help='Work permit (رخصة العمل) cost due from the employee for the '
             'extension period (filled by HR).',
    )
    x_ext_iqama_fee = fields.Float(
        string='Iqama Fees', digits=(16, 2), copy=False, tracking=True,
        help='Iqama (الإقامة) cost due from the employee for the extension '
             'period (filled by HR).',
    )
    x_ext_bank_fee = fields.Float(
        string='Bank Fees', digits=(16, 2), copy=False, tracking=True,
        help='Bank charges due from the employee (filled by HR).',
    )
    x_ext_passport_fee_applied = fields.Boolean(
        string='Passport Fees Apply', copy=False, tracking=True,
        help='Tick when the passport (الجوازات) fee is due. It is a fixed '
             'government charge of %s SAR.' % EXT_PASSPORT_FEE,
    )
    x_ext_passport_fee = fields.Float(
        string='Passport Fees', digits=(16, 2), readonly=True,
        compute='_compute_ext_passport_fee', store=True, tracking=True,
        help='A fixed %s SAR when the passport fee applies, otherwise 0. '
             'Derived from the tick box — never typed.' % EXT_PASSPORT_FEE,
    )
    x_ext_violations_fee = fields.Float(
        string='Violations', digits=(16, 2), copy=False, tracking=True,
        help='Traffic or labour violations (مخالفات) due from the employee '
             '(filled by HR).',
    )
    x_ext_extra_fee = fields.Float(
        string='Extra Fees', digits=(16, 2), copy=False, tracking=True,
        help='Any further cost the extension makes due from the employee '
             '(filled by HR).',
    )
    x_ext_total_fees = fields.Float(
        string='Total Fees Due', digits=(16, 2), readonly=True,
        compute='_compute_ext_total_fees', store=True, tracking=True,
        help='Sum of every extension fee. Recorded on the request for the '
             'approvers; it is not posted to payroll.',
    )

    # ------------------------------------------------------------------
    # Accounting note (Step 4). Supporting documents go on the existing
    # x_attachment_ids many2many and its 📎 Attachments tab.
    # ------------------------------------------------------------------

    x_ext_acc_note = fields.Text(
        string='Accounting Note', copy=False, tracking=True,
        help='Accounting remarks on the extension (filled by Accounting).',
    )

    # ------------------------------------------------------------------
    # Return status: one more way for a vacation's gate to be closed
    # ------------------------------------------------------------------

    x_return_state = fields.Selection(
        selection_add=[('extended', 'Continued by Extension')],
        ondelete={'extended': 'set default'},
    )

    # ==================================================================
    # Predicates
    # ==================================================================

    def _is_leave_extension(self, leave):
        """Check if the leave type is flagged as a vacation extension."""
        return (
            leave.holiday_status_id
            and leave.holiday_status_id.is_leave_extension
        )

    def _is_extension_multi(self, leave):
        """Check if the leave type uses the extension multi-step chain."""
        return (
            leave.holiday_status_id
            and leave.holiday_status_id.leave_validation_type
            == 'extension_multi'
        )

    def _extension_leaves(self):
        """The vacation extensions in ``self``."""
        return self.filtered(self._is_leave_extension)

    def _multi_step_validation_types(self):
        """Declare 'extension_multi' as a KSW multi-step chain.

        That single hook is what makes _uses_multi_step_chain, _is_finalised,
        _is_past_gm_final, _has_approver_action, _resync_multi_step_chain and
        the GM return wizard treat an extension as a chain request, without
        KSW_annual_leave having to name a validation type it does not own.
        """
        return super()._multi_step_validation_types() | {'extension_multi'}

    # ==================================================================
    # Walking the extension chain
    # ==================================================================

    def _extension_ancestors(self):
        """Every vacation this request continues, nearest first.

        An extension can itself be extended, so this walks ``x_extended_
        leave_id`` all the way up. Guarded against a cycle even though
        _check_extension_link refuses to create one — a walk that can loop
        must not depend on a constraint staying correct.
        """
        self.ensure_one()
        chain = []
        seen = {self.id}
        node = self.x_extended_leave_id
        while node and node.id not in seen:
            seen.add(node.id)
            chain.append(node.id)
            node = node.x_extended_leave_id
        return self.browse(chain)

    def _extension_root(self):
        """The original vacation at the bottom of the extension chain.

        Returns ``self`` when this request extends nothing.
        """
        self.ensure_one()
        ancestors = self._extension_ancestors()
        return ancestors[-1] if ancestors else self

    def _extension_owns_return(self):
        """True once this extension, not the vacation under it, is the record
        the return has to be confirmed on.

        Deliberately ``state == 'validate'`` rather than
        ``_is_past_gm_final()``: the gate payroll reads
        (``hr.payslip._get_unresolved_vacation_leaves``) only ever looks at
        validated requests. Handing the vacation over any earlier would open
        a window — GM final approval through HR confirmation — in which
        neither record blocks the employee's payslip and he would be paid for
        days nobody has confirmed he was back for.
        """
        self.ensure_one()
        return (
            self._is_leave_extension(self)
            and self.state == 'validate'
            and self._is_past_gm_final()
        )

    # ==================================================================
    # Computes
    # ==================================================================

    @api.depends('x_ext_passport_fee_applied')
    def _compute_ext_passport_fee(self):
        for leave in self:
            leave.x_ext_passport_fee = (
                EXT_PASSPORT_FEE if leave.x_ext_passport_fee_applied else 0.0
            )

    @api.depends(
        'x_ext_work_permit_fee', 'x_ext_iqama_fee', 'x_ext_bank_fee',
        'x_ext_passport_fee', 'x_ext_violations_fee', 'x_ext_extra_fee',
    )
    def _compute_ext_total_fees(self):
        for leave in self:
            leave.x_ext_total_fees = (
                leave.x_ext_work_permit_fee
                + leave.x_ext_iqama_fee
                + leave.x_ext_bank_fee
                + leave.x_ext_passport_fee
                + leave.x_ext_violations_fee
                + leave.x_ext_extra_fee
            )

    # ==================================================================
    # Unpaid semantics — implemented here rather than borrowed from the
    # is_unpaid_leave flag, which would also route this request through
    # KSW_unpaid_leave's GM-final shortcut and skip Step 6.
    # ==================================================================

    def _excuses_absence(self):
        """An extension explains the absence; it does not pay for it.

        The absence rows stay linked (the sheet and the report still show
        which request owns the day) but must not be flagged covered, or the
        day would be credited as worked and its deduction cleared — the
        mistake that once paid a whole month of unpaid leave in full
        (gotcha #44).
        """
        return super()._excuses_absence().filtered(
            lambda l: not self._is_leave_extension(l))

    def _blocks_annual_reset(self):
        """An extension may only be a restart point if the vacation it
        continues was one.

        The restart deletes everything accrued up to that date on the grounds
        that a vacation spent it. An extension spends nothing itself — but it
        is the tail of a vacation that may well have, and the employee was
        away continuously across both. So the question is asked of the root:
        extend an annual vacation and the return still settles it; extend an
        unpaid leave and there is nothing to settle, so the date is refused
        exactly as the unpaid leave's own end would be.
        """
        return super()._blocks_annual_reset() | self.filtered(
            lambda l: self._is_leave_extension(l)
            and not l._extension_root()._settles_annual_balance())

    def _uses_unpaid_return(self):
        """An extension confirms its return the unpaid way.

        Its days are deducted in arrears, so an early return must *shorten*
        the request — every day trimmed off the end is a day paid again. The
        annual branch does the opposite on purpose (paid up front, duration
        preserved), which would leave the employee deducted for days he
        worked.
        """
        self.ensure_one()
        if self._is_leave_extension(self):
            return True
        return super()._uses_unpaid_return()

    # ------------------------------------------------------------------
    # Duration: calendar days including weekends, as for every KSW vacation.
    # Only _get_durations / _get_number_of_days are overridden — core's
    # _compute_duration funnels through _get_durations, so there is no need
    # to re-declare a compute (and no risk of narrowing its @api.depends).
    # ------------------------------------------------------------------

    def _get_durations(self, check_leave_type=True, resource_calendar=None):
        extensions = self._extension_leaves()
        remaining = self - extensions
        result = {}
        if remaining:
            result.update(super(HrLeaveExtension, remaining)._get_durations(
                check_leave_type=check_leave_type,
                resource_calendar=resource_calendar,
            ))
        for leave in extensions:
            result[leave.id] = self._annual_cal_days(leave)
        return result

    def _get_number_of_days(self, date_from, date_to, employee_id):
        if self and self._is_leave_extension(self):
            if not (date_from and date_to):
                return {'days': 0, 'hours': 0}
            start = date_from.date() if hasattr(date_from, 'date') else date_from
            end = date_to.date() if hasattr(date_to, 'date') else date_to
            cal_days = (end - start).days + 1
            employee = self.env['hr.employee'].browse(employee_id)
            daily_hours = (
                self._get_daily_work_hours(employee) if employee_id else 8.0
            )
            return {'days': cal_days, 'hours': cal_days * daily_hours}
        return super()._get_number_of_days(date_from, date_to, employee_id)

    # ==================================================================
    # The link: start date, and what a valid link looks like
    # ==================================================================

    @api.model
    def _extension_start_after(self, parent):
        """The only legal first day of an extension of ``parent``."""
        if not parent or not parent.request_date_to:
            return False
        return parent.request_date_to + timedelta(days=1)

    @api.model
    def _extendable_vacation(self, employee, exclude=None):
        """The one vacation of ``employee`` an extension may still attach to.

        At most one record, ever — the employee's **current** granted vacation.
        Anything older has been overtaken: a vacation followed by another
        vacation is finished, whatever its own dates say. Offering the whole
        history would be offering a list of wrong answers, and a picker is
        never wider than the answer in this codebase.

        A chained extension falls out of the same rule instead of needing one
        of its own: an extension always ends after the vacation it continues,
        so it is the current record and the one underneath it is not. That
        also replaces the old "one live extension per parent" check — a parent
        that already has one is, by construction, no longer current.
        """
        if not employee:
            return self.browse()
        candidates = self.sudo().search(
            [
                ('employee_id', '=', employee.id),
                ('state', 'not in', ('refuse', 'cancel', 'draft')),
                '|', '|',
                ('holiday_status_id.is_annual_leave', '=', True),
                ('holiday_status_id.is_unpaid_leave', '=', True),
                ('holiday_status_id.is_leave_extension', '=', True),
            ],
            order='request_date_to desc, id desc',
        )
        if exclude:
            candidates -= exclude
        candidates = candidates.filtered(lambda l: l._is_past_gm_final())
        if not candidates:
            return self.browse()
        current = candidates[0]
        if current.x_return_state == 'hr_confirmed':
            # The manager has recorded the employee back at work. There is no
            # absence left to extend.
            return self.browse()
        return current

    @api.model
    def _extension_leave_type(self):
        """The Vacation Extension leave type, by xml id then by flag.

        The flag search is the fallback for a database where the seeded record
        was renamed or replaced: the type is configuration, and the Extend
        button must not die because somebody made their own.
        """
        leave_type = self.env.ref(
            'KSW_leave_extension.leave_type_extension',
            raise_if_not_found=False)
        if leave_type and leave_type.active:
            return leave_type
        return self.env['hr.leave.type'].sudo().search(
            [('is_leave_extension', '=', True)], limit=1)

    @api.depends('employee_id', 'holiday_status_id')
    def _compute_extendable_leave_ids(self):
        """Drives the picker's domain.

        A Many2one `domain=` has to name a field, not call a method, so the
        one candidate is published as a relational field and resolved against
        the record every time — a dynamic `selection=`/domain string would be
        computed once and cached by the client (gotcha #39).
        """
        for leave in self:
            if not leave._is_leave_extension(leave):
                leave.x_extendable_leave_ids = False
                continue
            leave.x_extendable_leave_ids = leave._extendable_vacation(
                leave.employee_id, exclude=leave)

    @api.depends_context('uid')
    @api.depends('employee_id', 'state', 'x_annual_approval_state',
                 'x_return_state', 'holiday_status_id')
    def _compute_can_extend(self):
        user = self.env.user
        has_type = bool(self._extension_leave_type())
        is_hr = user.has_group('KSW_annual_leave.group_annual_leave_hr')
        for leave in self:
            if not (has_type and leave.id and leave.employee_id):
                leave.x_can_extend = False
                continue
            if leave._extendable_vacation(leave.employee_id) != leave:
                leave.x_can_extend = False
                continue
            # Identity through sudo(): an employee's own record rule forbids
            # reading their manager, and this compute loads on their own form.
            employee = leave.employee_id.sudo()
            leave.x_can_extend = bool(
                is_hr
                or employee.user_id == user
                or employee.leave_manager_id == user
            )

    def action_extend_vacation(self):
        """Open a new extension request for this vacation.

        `x_can_extend` only hides the button; an RPC caller reaches this method
        whatever the view says, so the same questions are asked again here
        (gotcha #15).
        """
        self.ensure_one()
        leave_type = self._extension_leave_type()
        if not leave_type:
            raise UserError(_(
                'No Vacation Extension leave type is configured.'))
        current = self._extendable_vacation(self.employee_id)
        if current != self:
            if not self._is_past_gm_final():
                raise UserError(_(
                    'A vacation can only be extended once it has passed GM '
                    'final approval.'))
            if current:
                raise UserError(_(
                    'Only the current vacation can be extended, and that is '
                    '%(current)s, not this one. Extend that request instead — '
                    'extensions run one after another.',
                    current=current.display_name))
            raise UserError(_(
                'This vacation can no longer be extended: the manager has '
                'already confirmed that the employee came back.'))
        if not self.x_can_extend:
            raise UserError(_(
                'Only %(employee)s, their direct manager or HR can request an '
                'extension of this vacation.',
                employee=self.employee_id.name or ''))
        start = self._extension_start_after(self)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Extend Vacation'),
            'res_model': 'hr.leave',
            'view_mode': 'form',
            'target': 'current',
            'context': {
                'default_employee_id': self.employee_id.id,
                'default_holiday_status_id': leave_type.id,
                'default_x_extended_leave_id': self.id,
                'default_request_date_from': start,
                'default_request_date_to': start,
            },
        }

    @api.model
    def _apply_extension_start_date(self, vals):
        """Force the start date onto the day after the extended vacation.

        Done in create()/write() and not only in the onchange, because the
        onchange is the browser's copy of the rule and an RPC call never runs
        it. The constraint below is what proves it stuck.
        """
        parent_id = vals.get('x_extended_leave_id')
        if not parent_id:
            return
        start = self._extension_start_after(self.browse(parent_id))
        if start:
            vals['request_date_from'] = start

    @api.onchange('x_extended_leave_id', 'employee_id', 'holiday_status_id')
    def _onchange_extension_link(self):
        """Fill the vacation in, then show the start date it forces.

        There is only ever one candidate, so it is preselected rather than
        asked for — the picker stays on the form so the requester can see
        *which* vacation they are extending, but its domain leaves nothing
        else to choose.
        """
        for leave in self:
            if not leave._is_leave_extension(leave):
                continue
            if not leave.x_extended_leave_id:
                leave.x_extended_leave_id = leave._extendable_vacation(
                    leave.employee_id, exclude=leave)
            start = leave._extension_start_after(leave.x_extended_leave_id)
            if start:
                leave.request_date_from = start
                if (not leave.request_date_to
                        or leave.request_date_to < start):
                    leave.request_date_to = start

    @api.constrains(
        'x_extended_leave_id', 'holiday_status_id', 'employee_id',
        'request_date_from',
    )
    def _check_extension_link(self):
        for leave in self:
            is_extension = leave._is_leave_extension(leave)
            parent = leave.x_extended_leave_id
            if not is_extension:
                if parent:
                    raise ValidationError(_(
                        'Only a Vacation Extension request can extend another '
                        'vacation.'))
                continue
            if not parent:
                raise ValidationError(_(
                    'A Vacation Extension must name the vacation it extends.'))
            if parent == leave:
                raise ValidationError(_(
                    'A request cannot extend itself.'))
            if leave in parent._extension_ancestors():
                raise ValidationError(_(
                    'This would make the extension chain loop back on itself.'))
            if parent.employee_id != leave.employee_id:
                raise ValidationError(_(
                    'The extension and the vacation it extends must belong to '
                    'the same employee.'))
            # The same predicate the picker and the Extend button use, so the
            # three cannot drift. It subsumes "one live extension per parent":
            # a vacation that already has one is no longer the current one.
            extendable = leave._extendable_vacation(
                leave.employee_id, exclude=leave)
            if parent != extendable:
                if not parent._is_past_gm_final():
                    raise ValidationError(_(
                        'Only a vacation that has passed GM final approval can '
                        'be extended. %(name)s is still being approved.',
                        name=parent.display_name))
                if extendable:
                    raise ValidationError(_(
                        'Only the current vacation can be extended, and that '
                        'is %(current)s, not %(name)s. Extensions run one '
                        'after another.',
                        current=extendable.display_name,
                        name=parent.display_name))
                raise ValidationError(_(
                    '%(name)s can no longer be extended: the manager has '
                    'already confirmed that the employee came back.',
                    name=parent.display_name))
            start = leave._extension_start_after(parent)
            if start and leave.request_date_from != start:
                raise ValidationError(_(
                    'A Vacation Extension starts the day after the vacation '
                    'it extends. Expected %(expected)s, got %(actual)s.',
                    expected=start, actual=leave.request_date_from))

    # ==================================================================
    # Who may fill what, and when
    # ==================================================================

    def _check_extension_input_rights(self, vals):
        """Guard the HR fee sheet and the Accounting note server-side.

        Only *setting* a value is guarded; clearing stays open so the chain
        reset (_reset_annual_multi_fields) can blank the figures on a GM
        return without impersonating HR. Same idiom as the EOS inputs.
        """
        hr_written = {k for k in vals if k in _EXT_HR_FIELDS and vals[k]}
        acc_written = {k for k in vals if k in _EXT_ACC_FIELDS and vals[k]}
        derived = _EXT_DERIVED_FIELDS & vals.keys()
        if not (hr_written or acc_written or derived):
            return
        if self.env.su:
            return
        if derived:
            # Not "you may not, at this step" — these are never typed at all.
            # The recompute writes them through the ORM's own column path,
            # which does not come through here.
            raise UserError(_(
                'The passport fee and the extension total are worked out from '
                'the fees above — they cannot be entered directly.'))
        user = self.env.user
        for leave in self:
            if not leave._is_leave_extension(leave):
                continue
            if hr_written:
                if not user.has_group(
                        'KSW_annual_leave.group_annual_leave_hr'):
                    raise UserError(_(
                        'Only HR Approvers can fill the extension fees.'))
                if leave.x_annual_approval_state != _EXT_HR_STATE:
                    raise UserError(_(
                        'The extension fees can only be filled while the '
                        'request is at the HR Approval step. Ask the GM to '
                        'return it to HR if a figure has to change.'))
            if acc_written:
                if not user.has_group(
                        'KSW_annual_leave.group_annual_leave_acc'):
                    raise UserError(_(
                        'Only Accounting Approvers can fill the accounting '
                        'note.'))
                if leave.x_annual_approval_state != _EXT_ACC_STATE:
                    raise UserError(_(
                        'The accounting note can only be filled while the '
                        'request is at the Accounting step.'))

    # ==================================================================
    # Chain wiring
    # ==================================================================

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._apply_extension_start_date(vals)
        records = super().create(vals_list)
        for leave in records:
            # Deliberately its own predicate and its own stamp: widening
            # KSW_annual_leave's create() would notify the DM twice.
            if self._is_extension_multi(leave):
                leave.sudo().write({
                    'x_annual_approval_state': 'pending_dm',
                })
                self._notify_pending_approvers(leave, 'pending_dm')
        return records

    def write(self, vals):
        self._check_extension_input_rights(vals)
        if vals.get('x_extended_leave_id'):
            self._apply_extension_start_date(vals)
        return super().write(vals)

    @api.depends('state', 'employee_id', 'department_id')
    def _compute_can_approve(self):
        extension_multi = self.filtered(self._is_extension_multi)
        remaining = self - extension_multi
        if remaining:
            super(HrLeaveExtension, remaining)._compute_can_approve()
        for leave in extension_multi:
            leave.can_approve = False

    @api.depends('state', 'employee_id', 'department_id')
    def _compute_can_validate(self):
        extension_multi = self.filtered(self._is_extension_multi)
        remaining = self - extension_multi
        if remaining:
            super(HrLeaveExtension, remaining)._compute_can_validate()
        for leave in extension_multi:
            leave.can_validate = False

    def action_approve(self, check_state=True):
        extension_multi = self.filtered(self._is_extension_multi)
        remaining = self - extension_multi
        for leave in extension_multi:
            if leave.x_annual_approval_state == 'pending_dm':
                leave.action_dm_approve()
        if remaining:
            return super(HrLeaveExtension, remaining).action_approve(
                check_state=check_state)
        return True

    def _reset_annual_multi_fields(self):
        """Extend the chain reset to the extension inputs."""
        super()._reset_annual_multi_fields()
        self.write(dict(_EXT_RESET_VALUES))

    # ------------------------------------------------------------------
    # No vacation payslip for an extension
    # ------------------------------------------------------------------

    def _create_vacation_payslip(self, preview=False):
        """An extension is unpaid time — it settles nothing and pays nothing.

        KSW_annual_leave's action_gm_final_approve calls this for every chain
        request with no type test, so the filter has to live here. The
        complement still goes through, or an ordinary annual vacation
        approved in the same call would silently lose its payslip
        (gotcha #27).
        """
        extensions = self._extension_leaves()
        remaining = self - extensions
        if remaining:
            return super(
                HrLeaveExtension, remaining
            )._create_vacation_payslip(preview=preview)
        return True

    # ==================================================================
    # Chatter: what HR and Accounting entered
    # ==================================================================

    def action_hr_approve(self):
        result = super().action_hr_approve()
        for leave in self._extension_leaves():
            rows = [
                (_('Work Permit'), leave.x_ext_work_permit_fee),
                (_('Iqama'), leave.x_ext_iqama_fee),
                (_('Bank'), leave.x_ext_bank_fee),
                (_('Passport'), leave.x_ext_passport_fee),
                (_('Violations'), leave.x_ext_violations_fee),
                (_('Extra'), leave.x_ext_extra_fee),
            ]
            body = Markup(
                '<strong>&#129534; Extension Fees</strong><br/>'
            )
            for label, amount in rows:
                if amount:
                    body += Markup('%(label)s: %(amt).2f SAR<br/>') % {
                        'label': label, 'amt': amount}
            body += Markup(
                '<b>Total due from the employee:</b> %(total).2f SAR'
            ) % {'total': leave.x_ext_total_fees}
            leave.sudo().message_post(
                body=body, subtype_xmlid='mail.mt_note')
        return result

    def action_acc_approve(self):
        result = super().action_acc_approve()
        for leave in self._extension_leaves():
            if not leave.x_ext_acc_note:
                continue
            leave.sudo().message_post(
                body=Markup(
                    '<strong>&#128176; Accounting Note</strong><br/>'
                    '%(note)s'
                ) % {'note': leave.x_ext_acc_note},
                subtype_xmlid='mail.mt_note',
            )
        return result

    # ==================================================================
    # The return gate — opening the extension's, closing the vacation's
    # ==================================================================

    def _reconcile_extension_gate(self):
        """Re-derive, for each vacation in ``self``, whether an extension has
        taken its return over.

        Desired-vs-actual rather than a stamp per transition, because a
        request has seven ways out of GM final approval and remembering to
        undo the hand-over on each of them is exactly the bookkeeping that
        goes stale (gotcha #37/#48). A return the manager has already
        confirmed is never reopened, and a vacation that is not past GM
        final belongs to its own reconciliation, not this one.
        """
        for vacation in self:
            if vacation.x_return_state == 'hr_confirmed':
                continue
            if not vacation._is_past_gm_final():
                continue
            live = vacation.x_extension_ids.filtered(
                lambda e: e._extension_owns_return())
            wanted = 'extended' if live else 'on_vacation'
            if vacation.x_return_state != wanted:
                vacation.sudo().write({'x_return_state': wanted})

    def _sync_gm_final_state(self):
        """Extend the GM-final reconciliation to the extension hand-over.

        Two additions to the annual rule, both derived the same way:
        an extension opens its own return gate when it is past GM final, and
        the vacation underneath it is marked 'Continued by Extension' once
        the extension actually owns that return.

        A vacation whose gate a live extension holds is kept out of super()
        entirely: the annual rule only knows 'hr_confirmed' as a reason not
        to re-open a gate and would put it straight back to 'on_vacation',
        blocking the employee's payslip on two records at once. That is safe
        because the only other thing riding this hook is KSW_eos_leave's
        employee archive, and an EOS request can never be a parent — the
        extension picker takes annual, unpaid and extension types only.
        """
        extensions = self._extension_leaves()
        held = (self - extensions).filtered(
            lambda l: l.x_return_state == 'extended'
            and l.x_extension_ids.filtered(
                lambda e: e._extension_owns_return()))
        remaining = self - held
        if remaining:
            super(HrLeaveExtension, remaining)._sync_gm_final_state()

        for leave in extensions:
            if leave.x_return_state == 'hr_confirmed':
                continue
            if leave._is_past_gm_final():
                if leave.x_return_state != 'on_vacation':
                    leave.write({'x_return_state': 'on_vacation'})
                    leave._notify_return_confirmation_due()
            else:
                leave._reset_return_tracking()

        (extensions.mapped('x_extended_leave_id') | held
         )._reconcile_extension_gate()

    def _apply_confirmed_return(self):
        """Close the vacations underneath on the same real return date."""
        self.ensure_one()
        result = super()._apply_confirmed_return()
        if self._is_leave_extension(self):
            self._propagate_return_to_ancestors()
        return result

    def _propagate_return_to_ancestors(self):
        """Confirm the return on every vacation this extension continues.

        The extension took the gate over, so nobody is ever asked to close
        the vacation underneath it — and on an annual vacation that
        confirmation is the *only* thing that restarts the accrual
        (_sync_opening_reset_to_return). Without this the employee would keep
        accruing from the baseline the vacation was supposed to settle.

        Nothing is shortened on the way up: for every ancestor the confirmed
        return lands after its own end, and both _shorten_to_confirmed_return
        implementations decline that case ("a late return is not an
        extension"), so each ancestor keeps the period it was approved for.
        """
        self.ensure_one()
        if not self.x_return_date:
            return
        for ancestor in self._extension_ancestors():
            if ancestor.x_return_state == 'hr_confirmed':
                continue
            ancestor.sudo().write({
                'x_return_date': self.x_return_date,
                'x_return_state': 'hr_confirmed',
                'x_manager_return_confirmed_by':
                    self.x_manager_return_confirmed_by.id,
                'x_manager_return_date': self.x_manager_return_date,
            })
            ancestor.sudo().message_post(
                body=Markup(
                    '<strong>&#9989; Return Confirmed via the Extension'
                    '</strong><br/>'
                    '<b>Returned:</b> %(date)s<br/>'
                    '<b>Confirmed on:</b> %(ref)s<br/>'
                    '<i>This vacation was continued by an extension, so the '
                    'manager closed it there.</i>'
                ) % {
                    'date': self.x_return_date,
                    'ref': self.display_name,
                },
                subtype_xmlid='mail.mt_note',
            )
            ancestor.sudo()._apply_confirmed_return()

    # ==================================================================
    # Validate / reverse — attendance sheet lock and chain reset
    # ==================================================================

    def _action_validate(self, check_state=True):
        result = super()._action_validate(check_state=check_state)
        extensions = self._extension_leaves()
        if extensions:
            for leave in extensions:
                self._lock_attendance_sheet_lines(leave)
            # The gate itself is opened by _sync_gm_final_state, which every
            # route already calls; this is only the hand-over, which waits
            # for the extension to be validated (see _extension_owns_return).
            extensions.mapped(
                'x_extended_leave_id')._reconcile_extension_gate()
            self.env['ksw.annual.leave']._refresh_accrual_for_employees(
                extensions.mapped('employee_id').ids)
        return result

    def action_refuse(self):
        extensions = self._extension_leaves()
        extension_multi = self.filtered(self._is_extension_multi)
        parents = extensions.mapped('x_extended_leave_id')
        emp_ids = extensions.mapped('employee_id').ids

        # Unlock before super(), which changes the state the lock keys on.
        for leave in extensions:
            self._unlock_attendance_sheet_lines(leave)

        result = super().action_refuse()

        extensions._reset_return_tracking()
        if extension_multi:
            extension_multi._reset_annual_multi_fields()
        parents._reconcile_extension_gate()
        if emp_ids:
            self.env['ksw.annual.leave']._refresh_accrual_for_employees(
                emp_ids)
        return result

    def action_draft(self):
        extensions = self._extension_leaves()
        parents = extensions.mapped('x_extended_leave_id')
        emp_ids = extensions.mapped('employee_id').ids

        for leave in extensions:
            self._unlock_attendance_sheet_lines(leave)

        result = super().action_draft()

        extension_multi = self.filtered(self._is_extension_multi)
        if extension_multi:
            extension_multi._reset_annual_multi_fields()
            extension_multi.write({'x_annual_approval_state': 'pending_dm'})
        parents._reconcile_extension_gate()
        if emp_ids:
            self.env['ksw.annual.leave']._refresh_accrual_for_employees(
                emp_ids)
        return result

    def _move_validate_leave_to_confirm(self):
        extensions = self._extension_leaves()
        extension_multi = self.filtered(self._is_extension_multi)
        parents = extensions.mapped('x_extended_leave_id')
        emp_ids = extensions.mapped('employee_id').ids

        for leave in extensions:
            self._unlock_attendance_sheet_lines(leave)

        # A targeted admin return keeps every figure the approvers entered
        # and picks its own target state — see the KSW_annual_leave override.
        keep_data = self.env.context.get('ksw_keep_approval_data')
        if extension_multi and not keep_data:
            extension_multi._reset_annual_multi_fields()

        result = super()._move_validate_leave_to_confirm()

        extensions._reset_return_tracking()
        if extension_multi and not keep_data:
            extension_multi.write({'x_annual_approval_state': 'pending_dm'})
        parents._reconcile_extension_gate()
        if emp_ids:
            self.env['ksw.annual.leave']._refresh_accrual_for_employees(
                emp_ids)
        return result

    def unlink(self):
        extensions = self._extension_leaves()
        # Read the parents now: after the delete there is no row left to
        # walk back from, and the gate would stay handed over to a request
        # that no longer exists.
        parents = extensions.mapped('x_extended_leave_id')
        emp_ids = extensions.mapped('employee_id').ids

        for leave in extensions:
            self._unlock_attendance_sheet_lines(leave)

        result = super().unlink()

        parents.exists()._reconcile_extension_gate()
        if emp_ids:
            self.env['ksw.annual.leave']._refresh_accrual_for_employees(
                emp_ids)
        return result
