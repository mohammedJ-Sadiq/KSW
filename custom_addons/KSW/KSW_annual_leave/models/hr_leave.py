import calendar as _cal
from datetime import timedelta

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_round
from odoo.osv import expression as odoo_expr

# Reversing a finalised request (refuse / back to approval / reset to draft /
# cancel / delete) is reserved for this group alone — see
# HrLeave._check_final_reversal_rights.
SETTINGS_ADMIN_GROUP = 'base.group_system'

ANNUAL_MULTI_STATES = [
    ('pending_dm', 'Pending DM Approval'),
    ('pending_hr', 'Pending HR Approval'),
    ('pending_gm_initial', 'Pending GM Initial'),
    ('pending_acc', 'Pending Accounting'),
    ('pending_gm_final', 'Pending GM Final'),
    ('pending_employee_signature', 'Pending HR Confirmation'),
    ('approved', 'Approved'),
]


class HrLeave(models.Model):
    _inherit = 'hr.leave'

    # ------------------------------------------------------------------
    # Employee picker domain
    # ------------------------------------------------------------------
    # Odoo's stock `_get_employee_domain` (hr_holidays/models/hr_leave.py)
    # restricts non-Officer users to `user_id == uid OR leave_manager_id
    # == uid`. KSW Leaves uses the `hr.employee.parent_id` chain
    # (matching KSW_deduction) traversed recursively via `child_of`.
    #
    # IMPORTANT: the picker scope follows the **Create** tier only —
    # not the union of all four CRUD tiers. The picker's job is to
    # answer "for whom can the user submit a new leave?". View tier
    # widens visibility of existing leaves but does NOT widen the
    # creation scope, and showing employees the user can SEE but not
    # CREATE for in the picker would let them pick a subordinate and
    # then hit a "create access denied" wall on save. Driving the
    # picker by the Create tier keeps the four privileges orthogonal
    # AND keeps the UX honest:
    #   * Create=Officer  -> all employees
    #   * Create=Supervisor -> own + parent_id descendants
    #   * Create=Self    -> own only
    #
    # The Manager Assistant delegation is ADDITIVE on top of whichever
    # tier the user holds, because an assistant very often has a tier of
    # their own as well. It is also a Create tier — the assistant may
    # file for the delegated team — so the invariant above still holds.
    #
    # NOTE this method is a security boundary, not just UX: an assistant
    # with no hr.employee ACL row has their queries answered by
    # hr.employee.public, whose only rule is multi-company. So this
    # domain is the only thing restricting the picker. The create record
    # rule in security.xml is the backstop; the two must always be
    # changed together.
    def _get_employee_domain(self):
        domain = [
            ('active', '=', True),
            ('company_id', 'in', self.env.companies.ids),
        ]
        user = self.env.user
        if user.has_group('KSW_annual_leave.group_leave_officer'):
            return domain
        if user.has_group('KSW_annual_leave.group_leave_supervisor'):
            # `child_of` on hr.employee traverses _parent_name (= parent_id)
            # and includes the anchor itself, so this returns the user's
            # own employee record + every descendant in the org chart.
            scope = [
                '|',
                ('user_id', '=', user.id),
                ('id', 'child_of', user.employee_ids.ids),
            ]
        else:
            scope = [('user_id', '=', user.id)]
        # Direct reports of every manager who nominated this user as their
        # assistant. Empty list (the undelegated case) leaves scope alone.
        manager_ids = user._ksw_assisted_manager_ids()
        if manager_ids:
            scope = odoo_expr.OR([scope, [
                '|',
                ('parent_id.user_id', 'in', manager_ids),
                ('leave_manager_id', 'in', manager_ids),
            ]])
        return domain + scope

    def _check_assistant_employee_scope(self, employee):
        """Block a Manager Assistant from targeting an employee outside
        their delegation.

        Record rules are evaluated on the PRE-write values only (there is
        no post-write re-check in BaseModel.write), so the scope-gated
        write rule cannot by itself stop an assistant re-pointing an
        in-scope request at an out-of-scope employee. Reusing
        `_get_employee_domain()` keeps this guard and the picker
        provably identical.

        Also called from create() so the assistant gets a readable
        UserError instead of a bare AccessError.
        """
        user = self.env.user
        if self.env.su:
            return
        if not user.has_group('KSW_base_security.group_manager_assistant'):
            return
        if user.has_group('KSW_annual_leave.group_leave_officer'):
            return
        if not employee:
            return
        allowed = self.env['hr.employee'].sudo().search_count(
            self._get_employee_domain() + [('id', '=', employee.id)])
        if not allowed:
            raise UserError(_(
                'You may only prepare requests for the direct reports of '
                'the managers you assist.'))

    # ------------------------------------------------------------------
    # Override `validation_type` to be readonly to prevent propagating
    # writes back to hr.leave.type. The upstream definition declares it
    # `related='holiday_status_id.leave_validation_type', readonly=False`,
    # which causes the form's save payload to write the value back to
    # the related type record. Because hr.leave.type only allows write
    # to the Administrator group (`hr_holidays.group_hr_holidays_manager`),
    # any non-admin user submitting a leave hits an AccessError on the
    # type record even though they aren't actually trying to change the
    # validation policy. Making this field readonly here breaks that
    # propagation while keeping the value accessible everywhere it is
    # only **read** in core code (e.g. _action_validate, activity_update).
    # ------------------------------------------------------------------
    validation_type = fields.Selection(
        related='holiday_status_id.leave_validation_type',
        readonly=True,
    )



    # ------------------------------------------------------------------
    # Annual-leave duration: calendar days (including weekends/holidays)
    # per Saudi labor law.  Only applies to leave types flagged with
    # is_annual_leave = True.
    # ------------------------------------------------------------------

    def _is_annual_leave(self, leave):
        """Check if the leave type is flagged as annual leave."""
        return (
            leave.holiday_status_id
            and leave.holiday_status_id.is_annual_leave
        )

    def _is_annual_multi(self, leave):
        """Check if the leave type uses multi-step approval."""
        return (
            leave.holiday_status_id
            and leave.holiday_status_id.leave_validation_type == 'annual_multi'
        )

    # Validation types whose approval progress lives in
    # ``x_annual_approval_state`` rather than in the stock ``state`` field.
    # This module only owns 'annual_multi'; sibling chain modules extend the
    # set through _multi_step_validation_types() so KSW_annual_leave never
    # has to name a validation type it doesn't own.
    _KSW_MULTI_STEP_TYPES = frozenset({'annual_multi'})

    def _multi_step_validation_types(self):
        """Return the leave_validation_type values driven by the KSW chain."""
        return set(self._KSW_MULTI_STEP_TYPES)

    def _uses_multi_step_chain(self, leave):
        """True when this leave's type is driven by x_annual_approval_state."""
        return bool(
            leave.holiday_status_id
            and leave.holiday_status_id.leave_validation_type
            in self._multi_step_validation_types()
        )

    def _annual_cal_days(self, leave):
        """Return (days, hours) using calendar-day counting for annual leave."""
        if leave.request_date_from and leave.request_date_to:
            cal_days = (leave.request_date_to - leave.request_date_from).days + 1
            daily_hours = (
                self._get_daily_work_hours(leave.employee_id)
                if leave.employee_id else 8.0
            )
            return (cal_days, cal_days * daily_hours)
        return (0, 0)

    def _get_remaining_balance(self, leave, as_of_date=None):
        """Get the remaining annual leave balance for an employee.

        When as_of_date is supplied, accrual is computed up to that date
        instead of today — used for EOS and full-clearance payslips to
        pin the vacation balance to the leave's request_date_from so that
        recomputing on a later day doesn't change the payout figure.
        """
        if not leave.employee_id or not leave.holiday_status_id:
            return 0.0
        ksw_rec = self.env['ksw.annual.leave'].sudo().search([
            ('employee_id', '=', leave.employee_id.id),
        ], limit=1)
        if not ksw_rec:
            return 0.0
        if as_of_date:
            accrued = ksw_rec._compute_accrued_as_of(as_of_date)
            try:
                taken = (
                    ksw_rec.leaves_taken
                    if ksw_rec.allocation_id and ksw_rec.allocation_id.exists()
                    else 0.0
                )
            except Exception:
                taken = 0.0
            return max(round(accrued - taken, 4), 0.0)
        return ksw_rec.remaining_balance

    # ------------------------------------------------------------------
    # View helper fields (stored for reliable invisible expressions)
    # ------------------------------------------------------------------

    x_is_annual_leave_type = fields.Boolean(
        string='Is Annual Leave Type',
        related='holiday_status_id.is_annual_leave',
        store=True,
        help='Stored related field for reliable use in view invisible expressions.',
    )
    x_exceeds_annual_balance = fields.Boolean(
        string='Exceeds Annual Balance',
        default=False, store=True, copy=False,
        help='True when the requested calendar days exceed the '
             'remaining annual leave balance.  Set by _compute_duration.',
    )
    x_balance_at_request = fields.Float(
        string='Balance at Request',
        compute='_compute_balance_at_request',
        digits=(10, 2),
        help='Annual leave balance available when this request was made. '
             'For validated leaves, reconstructed by adding back the days '
             'deducted by this leave.',
    )

    @api.depends('employee_id', 'holiday_status_id', 'state', 'number_of_days')
    def _compute_balance_at_request(self):
        for leave in self:
            if not leave.employee_id or not self._is_annual_leave(leave):
                leave.x_balance_at_request = 0.0
                continue
            raw = self._get_remaining_balance(leave)
            if leave.state == 'validate':
                # Allocation already deducted — add back to show pre-request balance
                leave.x_balance_at_request = raw + leave.number_of_days
            else:
                leave.x_balance_at_request = raw

    # ------------------------------------------------------------------
    # Requested calendar days (as opposed to the balance-paid duration)
    # ------------------------------------------------------------------
    # `number_of_days` — and therefore the core `duration_display` shown in
    # parentheses next to the picked dates — carries the duration Odoo
    # deducts from the allocation.  For combined leaves that is only the
    # annual portion and for full-clearance leaves only the balance
    # consumed, so a reviewer sees the same figure twice (next to the dates
    # AND as "Balance at Request") and cannot tell how long the employee
    # actually asked to be away.  These two fields always report the full
    # requested span.
    # ------------------------------------------------------------------

    x_requested_days = fields.Float(
        string='Total Days Requested',
        compute='_compute_requested_days',
        digits=(10, 2),
        help='Total calendar days covered by the request dates, whether or '
             'not the annual leave balance pays for all of them.  On '
             'combined and full-clearance leaves this is larger than the '
             'duration deducted from the allocation.',
    )
    x_requested_days_display = fields.Char(
        string='Requested',
        compute='_compute_requested_days',
        help='Human-readable form of Total Days Requested, used next to the '
             'request dates in place of the core Requested duration.',
    )

    @api.depends('holiday_status_id', 'request_date_from', 'request_date_to',
                 'employee_id', 'duration_display')
    def _compute_requested_days(self):
        for leave in self:
            if not self._is_annual_leave(leave):
                # Non-annual leaves keep the core figure verbatim.
                leave.x_requested_days = 0.0
                leave.x_requested_days_display = leave.duration_display
                continue
            cal_days, _hours = self._annual_cal_days(leave)
            leave.x_requested_days = cal_days
            leave.x_requested_days_display = '%g %s' % (
                float_round(cal_days, precision_digits=2), _('days'))

    # ------------------------------------------------------------------
    # Service reference dates (shown next to the balance from step 1)
    # ------------------------------------------------------------------

    x_joining_date = fields.Date(
        string='Joining Date',
        compute='_compute_service_reference_dates',
        help='The employee joining date — the earliest contract start date '
             'across all of the employee versions.',
    )
    x_last_return_date = fields.Date(
        string='Last Return Date',
        compute='_compute_service_reference_dates',
        help='Return date of the employee most recent annual vacation whose '
             'return was confirmed.  When there is no previous confirmed '
             'return, the annual-leave effective start date is shown instead '
             '(the opening reset date if one is set, otherwise the joining '
             'date).',
    )

    def _get_ksw_annual_rec(self, employee):
        """Return the employee's ksw.annual.leave record — sudo, may be empty.

        sudo() because `ksw.annual.leave` only grants read to the KSW Leaves
        tier groups (self / supervisor / cascading / officer) and write to
        officers alone; `group_annual_leave_hr` has no ACL row at all.  Every
        balance figure surfaced on the leave form must therefore be read with
        elevated rights and exposed through ungated computed fields.
        """
        if not employee:
            return self.env['ksw.annual.leave']
        return self.env['ksw.annual.leave'].sudo().search(
            [('employee_id', '=', employee.id)], limit=1)

    @api.depends('employee_id')
    def _compute_service_reference_dates(self):
        # sudo(): hr.version.contract_date_start is gated behind
        # hr.group_hr_manager, and the panel must be readable by the
        # requesting employee from the moment the request is created.
        Leave = self.env['hr.leave'].sudo()
        for leave in self:
            leave.x_joining_date = False
            leave.x_last_return_date = False
            employee = leave.employee_id
            if not employee:
                continue

            ksw_rec = self._get_ksw_annual_rec(employee)

            joining = ksw_rec.joining_date if ksw_rec else False
            if not joining:
                starts = employee.sudo().version_ids.filtered(
                    'contract_date_start'
                ).mapped('contract_date_start')
                joining = min(starts) if starts else False
            leave.x_joining_date = joining

            domain = [
                ('employee_id', '=', employee.id),
                ('holiday_status_id.is_annual_leave', '=', True),
                ('x_return_state', '=', 'hr_confirmed'),
                ('x_return_date', '!=', False),
            ]
            # _origin.id is 0 for a record still being created in the form.
            if leave._origin.id:
                domain.append(('id', '!=', leave._origin.id))
            previous = Leave.search(
                domain, order='x_return_date desc', limit=1)

            leave.x_last_return_date = (
                previous.x_return_date
                or (ksw_rec.x_effective_start_date if ksw_rec else False)
                or joining
            )

    # ------------------------------------------------------------------
    # Balance calculation breakdown (audit panel)
    # ------------------------------------------------------------------
    # Mirrors the derivation held on `ksw.annual.leave` so a reviewer can
    # audit the balance without leaving the request.  All read-only.
    #
    # NOTE none of these carry a model-level `groups=`.  The tab has no
    # `groups=` either, and its elements reference these fields in
    # `invisible=` expressions — a model gate would drop them from
    # `fields_get()` for outside users and crash the form with the OWL
    # "field is undefined" error (Odoo 19 pitfall #31).  The protection is
    # the `sudo()` inside `_get_ksw_annual_rec`, matching the approach used
    # for `x_gross_salary` on `ksw.deduction`.
    # ------------------------------------------------------------------

    x_bal_opening_reset_date = fields.Date(
        string='Opening Reset Date',
        compute='_compute_balance_breakdown',
        help='Go-live baseline. When set, accrual is counted from this date '
             'instead of the joining date.',
    )
    x_bal_effective_start_date = fields.Date(
        string='Effective Start Date',
        compute='_compute_balance_breakdown',
        help='The date accrual actually starts from — the opening reset date '
             'when one is set, otherwise the joining date.',
    )
    x_bal_daily_rate = fields.Float(
        string='Daily Accrual Rate', digits=(10, 6),
        compute='_compute_balance_breakdown',
        help='21/365 for the first five years of service, 30/365 after that '
             '(Saudi Labour Law Art. 109).',
    )
    x_bal_accrued_since_start = fields.Float(
        string='Accrued since Effective Start', digits=(10, 4),
        compute='_compute_balance_breakdown',
        help='Days earned by daily accrual since the effective start date, '
             'excluding any manual opening adjustment.',
    )
    x_bal_opening_extra_days = fields.Float(
        string='Opening Extra Days', digits=(10, 4),
        compute='_compute_balance_breakdown',
        help='One-time manual adjustment applied at the opening reset date '
             '(carry-over from a prior system, or a negative correction).',
    )
    x_bal_total_accrued = fields.Float(
        string='Total Accrued', digits=(10, 4),
        compute='_compute_balance_breakdown',
        help='Accrued since effective start plus the opening extra days. '
             'Gross entitlement — leaves taken are not deducted here.',
    )
    x_bal_leaves_taken = fields.Float(
        string='Leaves Taken', digits=(10, 4),
        compute='_compute_balance_breakdown',
        help='Approved annual leave days already consumed, from the linked '
             'allocation.',
    )
    x_bal_remaining = fields.Float(
        string='Remaining Balance', digits=(10, 4),
        compute='_compute_balance_breakdown',
        help='Total accrued minus leaves taken.',
    )
    x_can_open_balance_record = fields.Boolean(
        string='Can Open Balance Record',
        compute='_compute_balance_breakdown',
        help='True when the current user may read the underlying annual '
             'leave balance record.',
    )
    x_can_refresh_balance = fields.Boolean(
        string='Can Refresh Balance',
        compute='_compute_balance_breakdown',
        help='True for HR approvers and Leave Officers, who may re-run the '
             'accrual for this employee.',
    )

    @api.depends_context('uid')
    @api.depends('employee_id')
    def _compute_balance_breakdown(self):
        may_refresh = self.env.su or (
            self.env.user.has_group('KSW_annual_leave.group_leave_officer')
            or self.env.user.has_group('KSW_annual_leave.group_annual_leave_hr')
        )
        for leave in self:
            ksw_rec = self._get_ksw_annual_rec(leave.employee_id)

            leave.x_can_refresh_balance = bool(may_refresh and ksw_rec)
            # has_access() answers with the *real* user's rights even though
            # ksw_rec itself was fetched sudo, so the jump button is hidden
            # rather than raising AccessError when it is clicked.
            leave.x_can_open_balance_record = bool(
                ksw_rec
                and ksw_rec.with_user(self.env.user).has_access('read')
            )

            if not ksw_rec:
                leave.x_bal_opening_reset_date = False
                leave.x_bal_effective_start_date = False
                leave.x_bal_daily_rate = 0.0
                leave.x_bal_accrued_since_start = 0.0
                leave.x_bal_opening_extra_days = 0.0
                leave.x_bal_total_accrued = 0.0
                leave.x_bal_leaves_taken = 0.0
                leave.x_bal_remaining = 0.0
                continue

            extra = ksw_rec.x_opening_extra_days or 0.0
            total = ksw_rec.total_accrued_days or 0.0

            leave.x_bal_opening_reset_date = ksw_rec.x_opening_reset_date
            leave.x_bal_effective_start_date = ksw_rec.x_effective_start_date
            leave.x_bal_daily_rate = ksw_rec.daily_rate
            # total_accrued_days already folds the opening extra days in
            # (see ksw.annual.leave._compute_leave_data), so the pure
            # accrual component has to be derived by subtraction.
            leave.x_bal_accrued_since_start = round(total - extra, 4)
            leave.x_bal_opening_extra_days = extra
            leave.x_bal_total_accrued = total
            leave.x_bal_leaves_taken = ksw_rec.leaves_taken
            leave.x_bal_remaining = ksw_rec.remaining_balance

    def action_open_balance_record(self):
        """Open the employee's ksw.annual.leave record for review."""
        self.ensure_one()
        ksw_rec = self._get_ksw_annual_rec(self.employee_id)
        if not ksw_rec:
            raise UserError(
                'No annual leave balance record exists for %s yet.'
                % (self.employee_id.name or 'this employee'))
        if not ksw_rec.with_user(self.env.user).has_access('read'):
            raise UserError(
                'You are not allowed to open the annual leave balance '
                'record of %s.' % self.employee_id.name)
        return {
            'type': 'ir.actions.act_window',
            'name': 'Annual Leave Balance',
            'res_model': 'ksw.annual.leave',
            'view_mode': 'form',
            'res_id': ksw_rec.id,
            'target': 'current',
        }

    def action_refresh_annual_balance(self):
        """Re-run the accrual for this employee and reload the form.

        Useful when a contract date or wage was corrected after the request
        was filed, leaving the displayed balance stale.
        """
        self.ensure_one()
        if not self.env.su and not (
            self.env.user.has_group('KSW_annual_leave.group_leave_officer')
            or self.env.user.has_group('KSW_annual_leave.group_annual_leave_hr')
        ):
            raise UserError(
                'Only HR Approvers and Leave Officers can refresh the '
                'annual leave balance.')
        ksw_rec = self._get_ksw_annual_rec(self.employee_id)
        if not ksw_rec:
            raise UserError(
                'No annual leave balance record exists for %s yet.'
                % (self.employee_id.name or 'this employee'))
        # sudo(): authorisation was checked above; HR approvers hold no ACL
        # on ksw.annual.leave, and _refresh_accrual writes the allocation.
        ksw_rec.sudo()._refresh_accrual()
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    # ------------------------------------------------------------------
    # Full Balance Clearance
    # ------------------------------------------------------------------

    x_is_full_clearance = fields.Boolean(
        string='Full Balance Clearance',
        default=False, copy=False, tracking=True,
        help='When checked, this leave consumes the entire remaining '
             'annual leave balance instead of only the requested days.',
    )
    x_actual_vacation_days = fields.Float(
        string='Actual Vacation Days',
        digits=(10, 4), readonly=True, copy=False,
        help='The real number of calendar days the employee is on vacation '
             '(from request dates). Shown when Full Balance Clearance is used.',
    )
    x_clearance_balance = fields.Float(
        string='Balance Consumed',
        digits=(10, 4), readonly=True, copy=False,
        help='The full remaining balance consumed by this clearance leave.',
    )

    # ------------------------------------------------------------------
    # Excess Days (Combined Annual + Unpaid)
    # ------------------------------------------------------------------

    x_excess_days_accepted = fields.Boolean(
        string='Accept Excess as Unpaid',
        default=False, copy=False, tracking=True,
        help='When checked, the days exceeding the annual leave balance '
             'are treated as unpaid leave within this same request.',
    )
    x_annual_portion_days = fields.Float(
        string='Annual Portion (Days)',
        digits=(10, 4), readonly=True, copy=False,
        help='Number of days covered by the annual leave balance.',
    )
    x_unpaid_portion_days = fields.Float(
        string='Unpaid Portion (Days)',
        digits=(10, 4), readonly=True, copy=False,
        help='Number of excess days treated as unpaid leave.',
    )

    # ==================================================================
    # Multi-Step Approval Fields
    # ==================================================================

    x_annual_approval_state = fields.Selection(
        ANNUAL_MULTI_STATES,
        string='Approval Progress',
        copy=False, tracking=True, store=True,
        help='Tracks the multi-step annual leave approval chain.',
    )

    # --- HR-filled fields (penalty & iqama renewal) ---
    x_penalty_amount = fields.Float(
        string='Penalty Amount', digits=(16, 2), copy=False, tracking=True,
        help='Penalty amount to deduct from the vacation payslip (filled by HR).',
    )
    x_penalty_description = fields.Text(
        string='Penalty Description', copy=False,
    )
    x_iqama_renewal_amount = fields.Float(
        string='Iqama Renewal Amount', digits=(16, 2), copy=False, tracking=True,
        help='Iqama renewal cost to deduct from the vacation payslip (filled by HR).',
    )
    x_iqama_renewal_description = fields.Text(
        string='Iqama Renewal Description', copy=False,
    )

    # --- HR-filled fields (flight ticket) ---
    x_flight_ticket_amount = fields.Float(
        string='Flight Ticket Amount', digits=(16, 2), copy=False, tracking=True,
        help='Flight ticket allowance to add to the vacation payslip (filled by HR).',
    )
    x_flight_ticket_description = fields.Text(
        string='Flight Ticket Description', copy=False,
    )
    x_commission_line_ids = fields.One2many(
        'hr.leave.commission.line', 'leave_id',
        string='Commission Lines', copy=False,
        help='Individual monthly commission entries (filled by Accounting).',
    )
    x_additional_commissions = fields.Float(
        string='Total Additional Commissions', digits=(16, 2),
        compute='_compute_additional_commissions', store=True,
        tracking=True,
        help='Sum of all commission lines. Automatically computed.',
    )
    x_remaining_loans = fields.Float(
        string='Remaining Loans', digits=(16, 2), copy=False, tracking=True,
        help='Remaining loan amount to deduct from the vacation payslip (filled by Accounting).',
    )
    x_remaining_loans_description = fields.Text(
        string='Remaining Loans Description', copy=False,
    )
    x_deduction_line_ids = fields.One2many(
        'hr.leave.deduction.line', 'leave_id',
        string='Other Deduction Lines', copy=False,
        help='Individual other-deduction entries (filled by Accounting).',
    )
    x_other_deductions = fields.Float(
        string='Other Deductions', digits=(16, 2),
        compute='_compute_other_deductions', store=True,
        tracking=True,
        help='Sum of all other-deduction lines. Automatically computed. '
             'Deducted from the vacation payslip.',
    )

    # --- End-of-Service support data (read-only reference for HR/Accounting) ---
    x_eos_service_years = fields.Float(
        string='Service Years', compute='_compute_eos_fields', digits=(5, 2),
    )
    x_eos_last_wage = fields.Float(
        string='Last Wage (SAR)', compute='_compute_eos_fields', digits=(16, 2),
    )
    x_eos_termination_amount = fields.Float(
        string='EOS — Termination (Art. 84)', compute='_compute_eos_fields', digits=(16, 2),
    )
    x_eos_resignation_amount = fields.Float(
        string='EOS — Resignation (Art. 85)', compute='_compute_eos_fields', digits=(16, 2),
    )

    x_attachment_ids = fields.Many2many(
        'ir.attachment', 'hr_leave_attachment_rel',
        'leave_id', 'attachment_id',
        string='Attachments', copy=False,
        help='Supporting documents (penalty notices, flight tickets, '
             'accounting documents, etc.).',
    )

    @api.depends('x_commission_line_ids.amount')
    def _compute_additional_commissions(self):
        for leave in self:
            leave.x_additional_commissions = sum(
                leave.x_commission_line_ids.mapped('amount'))

    @api.depends('x_deduction_line_ids.amount')
    def _compute_other_deductions(self):
        for leave in self:
            leave.x_other_deductions = sum(
                leave.x_deduction_line_ids.mapped('amount'))

    @api.depends(
        'employee_id',
        'employee_id.version_ids.contract_date_start',
        'employee_id.version_ids.active',
        'employee_id.current_version_id.wage',
    )
    def _compute_eos_fields(self):
        today = fields.Date.context_today(self)
        for leave in self:
            wage = 0.0
            years = 0.0
            if leave.employee_id:
                emp = leave.employee_id.sudo()
                wage = emp.current_version_id.wage or 0.0
                versions = emp.version_ids.filtered(lambda v: v.contract_date_start)
                if versions:
                    joining = min(versions.mapped('contract_date_start'))
                    years = max((today - joining).days, 0) / 365.25
            leave.x_eos_service_years = years
            leave.x_eos_last_wage = wage
            first = min(years, 5.0)
            extra = max(years - 5.0, 0.0)
            term = 0.5 * wage * first + 1.0 * wage * extra
            leave.x_eos_termination_amount = term
            if years < 2.0:
                resig = 0.0
            elif years < 5.0:
                resig = term / 3.0
            elif years < 10.0:
                resig = term * 2.0 / 3.0
            else:
                resig = term
            leave.x_eos_resignation_amount = resig

    # --- Approver tracking ---
    x_dm_approved_by = fields.Many2one(
        'hr.employee', string='DM Approved By', readonly=True, copy=False,
    )
    x_dm_approved_date = fields.Datetime(
        string='DM Approved On', readonly=True, copy=False,
    )
    x_hr_approved_by = fields.Many2one(
        'hr.employee', string='HR Approved By', readonly=True, copy=False,
    )
    x_hr_approved_date = fields.Datetime(
        string='HR Approved On', readonly=True, copy=False,
    )
    x_gm_initial_approved_by = fields.Many2one(
        'hr.employee', string='GM Initial Approved By', readonly=True, copy=False,
    )
    x_gm_initial_approved_date = fields.Datetime(
        string='GM Initial Approved On', readonly=True, copy=False,
    )
    x_acc_approved_by = fields.Many2one(
        'hr.employee', string='ACC Approved By', readonly=True, copy=False,
    )
    x_acc_approved_date = fields.Datetime(
        string='ACC Approved On', readonly=True, copy=False,
    )
    x_gm_final_approved_by = fields.Many2one(
        'hr.employee', string='GM Final Approved By', readonly=True, copy=False,
    )
    x_gm_final_approved_date = fields.Datetime(
        string='GM Final Approved On', readonly=True, copy=False,
    )

    # --- Employee signature step ---
    x_employee_signed_by = fields.Many2one(
        'hr.employee', string='Signature Confirmed By', readonly=True, copy=False,
    )
    x_employee_signed_date = fields.Datetime(
        string='Signature Confirmed On', readonly=True, copy=False,
    )
    x_can_dm_approve = fields.Boolean(
        compute='_compute_can_dm_approve',
        help='True when the current user may perform the DM approval step.',
    )
    x_can_sign = fields.Boolean(
        compute='_compute_can_sign',
        help='True when the current user may confirm the employee signature step.',
    )
    # Role-gate fields: computed per-user so invisible= expressions
    # hide buttons and lock fields without relying on groups= in the view.
    x_can_hr_approve = fields.Boolean(compute='_compute_approval_role_gates')
    x_can_gm_initial_approve = fields.Boolean(compute='_compute_approval_role_gates')
    x_can_acc_approve = fields.Boolean(compute='_compute_approval_role_gates')
    x_can_gm_final_approve = fields.Boolean(compute='_compute_approval_role_gates')
    x_can_gm_return = fields.Boolean(compute='_compute_approval_role_gates')
    # No model-level groups= on purpose: it is read by an invisible=
    # expression on a button every user can see (gotcha #31).
    x_can_admin_return = fields.Boolean(
        compute='_compute_approval_role_gates',
        help='True for the Settings Administrator, who may return a request '
             'to any approval step at any time.',
    )
    x_is_hr_approver = fields.Boolean(compute='_compute_approval_role_gates')
    x_is_acc_approver = fields.Boolean(compute='_compute_approval_role_gates')

    # Searchable field: True when the current user is the designated approver
    # for the leave's current step. Used by the "Waiting For Me" search filter
    # so each role only sees leaves that are actually pending their action.
    x_is_pending_my_action = fields.Boolean(
        compute='_compute_is_pending_my_action',
        search='_search_is_pending_my_action',
        string='Pending My Action',
    )

    @api.depends_context('uid')
    @api.depends('x_annual_approval_state', 'employee_id',
                 'employee_id.leave_manager_id',
                 'employee_id.department_id.x_effective_gm_id')
    def _compute_is_pending_my_action(self):
        user = self.env.user
        uid = user.id
        is_hr = user.has_group('KSW_annual_leave.group_annual_leave_hr')
        is_acc = user.has_group('KSW_annual_leave.group_annual_leave_acc')
        for leave in self:
            s = leave.x_annual_approval_state
            if not s or not leave.id:
                leave.x_is_pending_my_action = False
                continue
            if s == 'pending_dm':
                dm = leave.employee_id.leave_manager_id
                leave.x_is_pending_my_action = (
                    (dm and dm.id == uid) or (not dm and is_hr)
                )
            elif s == 'pending_hr':
                leave.x_is_pending_my_action = is_hr
            elif s in ('pending_gm_initial', 'pending_gm_final'):
                leave.x_is_pending_my_action = (
                    self._department_gm_user(leave) == user)
            elif s == 'pending_acc':
                leave.x_is_pending_my_action = is_acc
            elif s == 'pending_employee_signature':
                leave.x_is_pending_my_action = is_hr
            else:
                leave.x_is_pending_my_action = False

    def _search_is_pending_my_action(self, operator, value):
        """Build the ORM domain for the "Waiting For Me" filter.

        Returns records where the current user is the active approver for
        the leave's current KSW multi-step state.

        Odoo 19 rewrites boolean conditions to 'in'/'not in' with a
        collection value before calling search= methods (see
        odoo.orm.domains._operator_equal_as_in), so both forms must be
        handled — a bare '='/'!=' guard would never match and silently
        return a match-all domain.
        """
        if operator in ('in', 'not in'):
            positive_wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            positive_wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        user = self.env.user
        uid = user.id
        is_hr = user.has_group('KSW_annual_leave.group_annual_leave_hr')
        is_acc = user.has_group('KSW_annual_leave.group_annual_leave_acc')

        parts = [
            # DM step: current user is the configured leave manager
            [('x_annual_approval_state', '=', 'pending_dm'),
             ('employee_id.leave_manager_id', '=', uid)],
            # GM steps: not "am I a GM" but "am I THIS department's GM".
            # No group gate — being named on the department is the whole
            # qualification, and x_effective_gm_id is stored so this is a
            # plain join rather than a Python-built id list.
            [('x_annual_approval_state', 'in',
              ['pending_gm_initial', 'pending_gm_final']),
             ('employee_id.department_id.x_effective_gm_id.user_id', '=', uid)],
        ]
        if is_hr:
            parts.extend([
                # HR as DM fallback when no manager is configured
                [('x_annual_approval_state', '=', 'pending_dm'),
                 ('employee_id.leave_manager_id', '=', False)],
                [('x_annual_approval_state', '=', 'pending_hr')],
                [('x_annual_approval_state', '=', 'pending_employee_signature')],
            ])
        if is_acc:
            parts.append([('x_annual_approval_state', '=', 'pending_acc')])

        positive = odoo_expr.OR(parts)
        if positive_wanted:
            return positive
        matching_ids = self.with_context(active_test=False).search(positive).ids
        return [('id', 'not in', matching_ids)]

    @api.depends_context('uid')
    @api.depends('x_annual_approval_state', 'state', 'holiday_status_id',
                 'employee_id',
                 'employee_id.department_id.x_effective_gm_id')
    def _compute_approval_role_gates(self):
        user = self.env.user
        is_hr = user.has_group('KSW_annual_leave.group_annual_leave_hr')
        is_acc = user.has_group('KSW_annual_leave.group_annual_leave_acc')
        is_admin = self.env.su or user.has_group(SETTINGS_ADMIN_GROUP)
        wizard = self.env['ksw.gm.return.approver.wizard']
        for leave in self:
            s = leave.x_annual_approval_state
            has_id = bool(leave.id)
            # The administrator may return a request from *any* state, on
            # every leave type — but only where there is somewhere earlier to
            # send it back to. A request still sitting at the first step has
            # no return target, so the button stays hidden.
            leave.x_can_admin_return = bool(
                is_admin and has_id and wizard._allowed_targets(leave))
            # Per record, not per user: whether this caller is a GM at all is
            # the wrong question — the only one that matters is whether he is
            # the GM of THIS employee's department.
            is_gm = has_id and self._department_gm_user(leave) == user
            leave.x_can_hr_approve = is_hr and s == 'pending_hr' and has_id
            leave.x_can_gm_initial_approve = is_gm and s == 'pending_gm_initial' and has_id
            leave.x_can_acc_approve = is_acc and s == 'pending_acc' and has_id
            leave.x_can_gm_final_approve = is_gm and s == 'pending_gm_final' and has_id
            leave.x_can_gm_return = (
                is_gm
                and s in ('pending_gm_initial', 'pending_gm_final')
                and has_id
            )
            leave.x_is_hr_approver = is_hr
            leave.x_is_acc_approver = is_acc

    @api.depends_context('uid')
    @api.depends('x_annual_approval_state', 'employee_id',
                 'employee_id.leave_manager_id')
    def _compute_can_dm_approve(self):
        user = self.env.user
        is_hr = user.has_group('KSW_annual_leave.group_annual_leave_hr')
        for leave in self:
            if leave.x_annual_approval_state != 'pending_dm' or not leave.id:
                leave.x_can_dm_approve = False
                continue
            dm = leave.employee_id.leave_manager_id
            if not dm:
                # No DM configured: HR approvers act as fallback
                leave.x_can_dm_approve = is_hr
            else:
                leave.x_can_dm_approve = (dm == user)

    @api.depends_context('uid')
    @api.depends('x_annual_approval_state')
    def _compute_can_sign(self):
        user = self.env.user
        is_hr = user.has_group('KSW_annual_leave.group_annual_leave_hr')
        for leave in self:
            if leave.x_annual_approval_state != 'pending_employee_signature' or not leave.id:
                leave.x_can_sign = False
                continue
            leave.x_can_sign = is_hr

    # --- Link to vacation payslip ---
    # x_vacation_payslip_id lives in KSW_payroll (depends on om_hr_payroll).

    # ==================================================================
    # Duration computation (annual-leave calendar-day counting)
    # ==================================================================

    @api.depends('holiday_status_id', 'x_is_full_clearance', 'x_excess_days_accepted',
                 'request_date_from', 'request_date_to',
                 'date_from', 'date_to',
                 'employee_id', 'resource_calendar_id')
    def _compute_duration(self):
        annual = self.filtered(self._is_annual_leave)
        remaining = self - annual

        if remaining:
            super(HrLeave, remaining)._compute_duration()
            # Ensure new fields are set for non-annual leaves and
            # clear stale toggles if leave type changed from annual
            for leave in remaining:
                leave.x_exceeds_annual_balance = False
                if leave.x_is_full_clearance:
                    leave.with_context(_skip_toggle_validity=True).x_is_full_clearance = False
                if leave.x_excess_days_accepted:
                    leave.with_context(_skip_toggle_validity=True).x_excess_days_accepted = False

        for leave in annual:
            cal_days, cal_hours = self._annual_cal_days(leave)
            daily_hours = cal_hours / cal_days if cal_days else 8.0

            # Compute whether days exceed the remaining balance
            balance = self._get_remaining_balance(leave)
            exceeds = bool(cal_days > 0 and balance > 0 and cal_days > balance)
            leave.x_exceeds_annual_balance = exceeds

            # Auto-clear excess toggle when days no longer exceed balance
            if not exceeds and leave.x_excess_days_accepted:
                leave.with_context(_skip_toggle_validity=True).x_excess_days_accepted = False

            # Auto-clear full clearance when days exceed balance —
            # full clearance only makes sense when vacation days ≤ balance.
            # When days > balance the user must use "Accept Excess as Unpaid".
            if exceeds and leave.x_is_full_clearance:
                leave.with_context(_skip_toggle_validity=True).x_is_full_clearance = False

            if leave.x_is_full_clearance and cal_days > 0:
                if balance > 0:
                    leave.number_of_days = balance
                    leave.number_of_hours = balance * daily_hours
                    leave.x_actual_vacation_days = cal_days
                    leave.x_clearance_balance = balance
                else:
                    leave.number_of_days = cal_days
                    leave.number_of_hours = cal_hours
                    leave.x_actual_vacation_days = cal_days
                    leave.x_clearance_balance = 0
                # Clear excess fields when full clearance
                leave.x_annual_portion_days = 0
                leave.x_unpaid_portion_days = 0

            elif leave.x_excess_days_accepted and cal_days > 0:
                if balance > 0 and cal_days > balance:
                    # Combined leave: annual portion + unpaid excess
                    leave.number_of_days = balance
                    leave.number_of_hours = balance * daily_hours
                    leave.x_actual_vacation_days = cal_days
                    leave.x_annual_portion_days = balance
                    leave.x_unpaid_portion_days = cal_days - balance
                else:
                    # Balance covers all days — no excess
                    leave.number_of_days = cal_days
                    leave.number_of_hours = cal_hours
                    leave.x_actual_vacation_days = cal_days
                    leave.x_annual_portion_days = cal_days
                    leave.x_unpaid_portion_days = 0
                leave.x_clearance_balance = 0

            else:
                leave.number_of_days = cal_days
                leave.number_of_hours = cal_hours
                leave.x_actual_vacation_days = 0
                leave.x_clearance_balance = 0
                leave.x_annual_portion_days = 0
                leave.x_unpaid_portion_days = 0

    def _get_durations(self, check_leave_type=True, resource_calendar=None):
        annual = self.filtered(self._is_annual_leave)
        remaining = self - annual

        result = {}
        if remaining:
            result.update(super(HrLeave, remaining)._get_durations(
                check_leave_type=check_leave_type,
                resource_calendar=resource_calendar,
            ))

        for leave in annual:
            daily_hours = (
                self._get_daily_work_hours(leave.employee_id)
                if leave.employee_id else 8.0
            )
            if leave.x_is_full_clearance and leave.x_clearance_balance > 0:
                result[leave.id] = (leave.x_clearance_balance,
                                    leave.x_clearance_balance * daily_hours)
            elif leave.x_excess_days_accepted and leave.x_annual_portion_days > 0:
                result[leave.id] = (leave.x_annual_portion_days,
                                    leave.x_annual_portion_days * daily_hours)
            else:
                result[leave.id] = self._annual_cal_days(leave)

        return result

    def _get_number_of_days(self, date_from, date_to, employee_id):
        if self and self._is_annual_leave(self):
            if date_from and date_to:
                start = date_from.date() if hasattr(date_from, 'date') else date_from
                end = date_to.date() if hasattr(date_to, 'date') else date_to
                cal_days = (end - start).days + 1
                employee = self.env['hr.employee'].browse(employee_id)
                daily_hours = (
                    self._get_daily_work_hours(employee)
                    if employee_id else 8.0
                )
                if self.x_is_full_clearance:
                    balance = self._get_remaining_balance(self)
                    if balance > 0:
                        return {'days': balance, 'hours': balance * daily_hours}
                elif self.x_excess_days_accepted:
                    balance = self._get_remaining_balance(self)
                    if balance > 0 and cal_days > balance:
                        return {'days': balance, 'hours': balance * daily_hours}
                return {'days': cal_days, 'hours': cal_days * daily_hours}
            return {'days': 0, 'hours': 0}
        return super()._get_number_of_days(date_from, date_to, employee_id)

    # ==================================================================
    # Vacation Return Confirmation
    # ==================================================================

    x_return_date = fields.Date(
        string='Return Date',
        tracking=True,
        help='The actual date the employee returned from annual vacation.',
    )
    x_return_state = fields.Selection([
        ('not_applicable', 'N/A'),
        ('on_vacation', 'On Vacation'),
        ('hr_confirmed', 'Return Confirmed'),
    ], string='Return Status', default='not_applicable',
        tracking=True, copy=False,
    )
    x_manager_return_confirmed_by = fields.Many2one(
        'hr.employee', string='Manager Confirmed By',
        readonly=True, copy=False,
    )
    x_manager_return_date = fields.Datetime(
        string='Manager Confirmed On',
        readonly=True, copy=False,
    )
    x_hr_return_confirmed_by = fields.Many2one(
        'hr.employee', string='HR Confirmed By',
        readonly=True, copy=False,
    )
    x_hr_return_date = fields.Datetime(
        string='HR Confirmed On',
        readonly=True, copy=False,
    )
    x_is_on_vacation = fields.Boolean(
        string='Currently On Vacation',
        compute='_compute_is_on_vacation',
        store=True,
    )
    x_can_confirm_return_manager = fields.Boolean(
        compute='_compute_return_permissions',
    )
    x_can_confirm_return_hr = fields.Boolean(
        compute='_compute_return_permissions',
    )
    x_can_edit_return_date = fields.Boolean(
        string='Can Amend Return Date',
        compute='_compute_return_permissions',
        help='True for the employee leave manager, who may correct the '
             'return date even after the return has been confirmed.',
    )
    x_return_reminder_last_sent = fields.Date(
        string='Return Reminder Last Sent',
        readonly=True, copy=False,
        help='Last day the direct manager was reminded to confirm this '
             'return. Stops the daily cron sending twice in one day.',
    )
    x_return_reminder_count = fields.Integer(
        string='Return Reminders Sent',
        readonly=True, copy=False, default=0,
    )

    @api.depends('state', 'x_return_state')
    def _compute_is_on_vacation(self):
        for leave in self:
            leave.x_is_on_vacation = (
                leave.state == 'validate'
                and leave.x_return_state == 'on_vacation'
            )

    @api.depends_context('uid')
    @api.depends('state', 'x_return_state', 'x_return_date', 'employee_id.leave_manager_id')
    def _compute_return_permissions(self):
        uid = self.env.uid
        for leave in self:
            leave.x_can_confirm_return_manager = (
                leave.state == 'validate'
                and leave.x_return_state == 'on_vacation'
                and leave.x_return_date
                and leave.employee_id.leave_manager_id.id == uid
            )
            # HR cannot confirm returns — only the leave's direct manager
            leave.x_can_confirm_return_hr = False
            # The same manager may also correct the date afterwards.
            leave.x_can_edit_return_date = (
                leave.x_return_state in ('on_vacation', 'hr_confirmed')
                and leave.employee_id.leave_manager_id.id == uid
            )

    def _check_can_edit_return_date(self):
        """Only the leave manager may amend an already-confirmed return date."""
        self.ensure_one()
        manager = self.employee_id.leave_manager_id
        if not manager or manager != self.env.user:
            raise UserError(
                'Only %s (the leave manager) can change the return date '
                'after the return has been confirmed.' % (
                    manager.name if manager else 'the leave manager'
                )
            )

    # Figures that represent money already paid out. They are restored
    # verbatim after the request period is corrected — see
    # _shorten_to_confirmed_return.
    _PAID_DURATION_FIELDS = (
        'number_of_days', 'number_of_hours', 'x_annual_portion_days',
        'x_unpaid_portion_days', 'x_clearance_balance',
    )

    def _shorten_to_confirmed_return(self):
        """End the vacation on the day the employee actually came back.

        The employee is paid up front for every day requested, so an early
        return refunds nothing — **the money figures are not recomputed**.
        What does change is the period the request *covers*: from the
        confirmed return date the employee is attending and is paid as
        attended, not as on vacation.

        It also has to change, or the balance can never recover: a request
        that still covers those days keeps consuming the restarted
        allocation, pinning the remaining balance at zero (KSWCO leave 4787
        would have read 0 for ~14 months).

        Writing `request_date_to` re-runs `_compute_duration`, which
        re-derives the charged days from *today's* already-depleted balance
        (measured on 4787: 46.7671 → 0.8028 days). So the paid figures are
        captured first and written straight back afterwards.

        Returns the previous end date when it shortened, else False.
        """
        self.ensure_one()
        if not (self.x_return_date and self.request_date_from
                and self.request_date_to):
            return False
        if not self._is_annual_leave(self):
            return False

        new_end = self.x_return_date - timedelta(days=1)
        if new_end >= self.request_date_to:
            # Returned on or after the planned end — nothing to shorten, and
            # a late return is not a vacation extension.
            return False
        if new_end < self.request_date_from:
            # Returned before the vacation started; too odd to fix silently.
            return False

        old_end = self.request_date_to
        leave = self.sudo()
        paid = {f: leave[f] for f in self._PAID_DURATION_FIELDS}

        leave.write({'request_date_to': new_end})
        leave.env.flush_all()

        restore = dict(paid)
        restore['x_actual_vacation_days'] = (
            new_end - self.request_date_from).days + 1
        leave.write(restore)
        return old_end

    def _apply_confirmed_return(self):
        """Make the whole record agree with the date the manager confirmed.

        Order matters: the request period is corrected first (it needs the
        pre-restart allocation to still cover the leave), then the accrual is
        restarted on the return date.
        """
        self.ensure_one()
        old_end = self._shorten_to_confirmed_return()
        if old_end:
            self.sudo().message_post(
                body=Markup(
                    '<strong>✂️ Vacation Period Shortened to the Actual '
                    'Return</strong><br/>'
                    '<b>Ends:</b> %(old_end)s → %(new_end)s<br/>'
                    '<b>Days on vacation:</b> %(actual).0f<br/>'
                    '<i>Paid days are unchanged (%(paid).4f charged to the '
                    'balance): the employee was paid up front for the days '
                    'requested, and an early return refunds nothing. From '
                    '%(return_date)s the employee is recorded as attending, '
                    'so the request no longer covers those days.</i>'
                ) % {
                    'old_end': old_end,
                    'new_end': self.request_date_to,
                    'actual': self.x_actual_vacation_days,
                    'paid': self.number_of_days,
                    'return_date': self.x_return_date,
                },
                subtype_xmlid='mail.mt_note',
            )
        # Compare against the period as requested, not as just shortened:
        # otherwise the reset this very call is about to write looks like a
        # "more recent" one and the guard below rejects it.
        self._sync_opening_reset_to_return(period_end=old_end or None)

    def _sync_opening_reset_to_return(self, period_end=None):
        """Move the employee's accrual restart date onto the confirmed return.

        HR sets ``ksw.annual.leave.x_opening_reset_date`` when it settles a
        vacation, using the *planned* return (``request_date_to + 1``).  When
        the manager confirms a different actual return date, the accrual would
        otherwise keep restarting on the planned date — an employee who came
        back early loses those days of accrual (and one who came back late
        gains them).

        The annual vacation is the settlement: its duration is capped at the
        employee's balance (`_get_number_of_days`), so taking it consumes the
        entitlement.  Accrual therefore **restarts from zero on the confirmed
        return date, always** — `x_opening_reset_date` is set to it and
        `x_opening_extra_days` cleared.

        Chosen deliberately over the safer variants (restart only when the
        balance is used up; or carry the unused days into
        `x_opening_extra_days`): an employee who took only *part* of their
        entitlement loses the remainder here.  That is the accepted trade-off,
        not an oversight.

        The single exception is a reset dated *after* this vacation — it
        belongs to a more recent settlement, and letting an old leave (or a
        late amendment to one) rewrite it would corrupt the newer return date
        rather than protect an old balance.

        `x_opening_is_locked` does not block the write.  That lock guards
        against accidental manual edits to the go-live figures; a restart onto
        a date the manager explicitly confirmed is not one, and 75% of the
        balance records are locked.  The override is named in the note.
        """
        self.ensure_one()
        if not (self.x_return_date
                and self.request_date_from
                and self.request_date_to):
            return
        if not self._is_annual_leave(self):
            return

        ksw_rec = self._get_ksw_annual_rec(self.employee_id)
        if not ksw_rec:
            return

        current = ksw_rec.x_opening_reset_date
        # ``period_end`` is the vacation's end *as requested*; the caller
        # passes it when the period has just been shortened to the return.
        planned_return = (period_end or self.request_date_to) + timedelta(days=1)
        if current and current == self.x_return_date:
            # Already aligned — nothing to do and nothing to report.
            return

        if current and current > planned_return:
            # A later settlement already governs the balance — never let an
            # older vacation (or a late amendment to one) walk it back.
            self.sudo().message_post(
                body=Markup(
                    '<strong>ℹ️ Balance Restart Date Unchanged</strong><br/>'
                    '<b>Accrual restarts on:</b> %(current)s<br/>'
                    '<b>Confirmed return date:</b> %(return_date)s<br/>'
                    '<i>The restart date is later than this vacation, so it '
                    'belongs to a more recent return and was left '
                    'untouched.</i>'
                ) % {
                    'current': current,
                    'return_date': self.x_return_date,
                },
                subtype_xmlid='mail.mt_note',
            )
            return

        was_locked = ksw_rec.x_opening_is_locked
        dropped = ksw_rec.x_opening_extra_days or 0.0

        # This write() triggers _refresh_accrual(), which resyncs
        # x_effective_start_date, total_accrued_days and the allocation.
        # The context key lets it through the opening-data lock — see the
        # docstring for why that is the right call here.
        ksw_rec.sudo().with_context(
            ksw_allow_locked_opening_write=True,
        ).write({
            'x_opening_reset_date': self.x_return_date,
            'x_opening_extra_days': 0.0,
        })

        body = Markup(
            '<strong>🔄 Accrual Restarted on the Confirmed Return</strong><br/>'
            '<b>Was:</b> %(old)s<br/>'
            '<b>Now:</b> %(new)s<br/>'
            '<b>Remaining balance:</b> %(balance).4f days<br/>'
            '<i>The vacation settles the entitlement, so the annual leave '
            'balance now accrues from the confirmed return date.</i><br/>'
        ) % {
            'old': current or _('not set'),
            'new': self.x_return_date,
            'balance': ksw_rec.remaining_balance or 0.0,
        }
        if dropped:
            body += Markup(
                '<i>⚠️ %(dropped).4f carry-over day(s) were cleared by the '
                'restart.</i><br/>'
            ) % {'dropped': dropped}
        if was_locked:
            body += Markup(
                '<i>⚠️ The balance record was locked; the restart date was '
                'updated anyway because the direct manager confirmed this '
                'return date.</i>'
            )
        self.sudo().message_post(body=body, subtype_xmlid='mail.mt_note')

    def action_confirm_return_manager(self):
        for leave in self:
            if leave.x_return_state != 'on_vacation':
                raise UserError('This leave is not in "On Vacation" status.')
            if not leave.x_return_date:
                raise UserError(
                    'Please set the Return Date before confirming.')
            if not self.env.su:
                is_manager = (
                    leave.employee_id.leave_manager_id
                    and leave.employee_id.leave_manager_id == self.env.user
                )
                if not is_manager:
                    raise UserError(
                        'Only %s (the leave manager) can confirm the '
                        'return.' % (
                            leave.employee_id.leave_manager_id.name
                            if leave.employee_id.leave_manager_id
                            else 'the leave manager'
                        )
                    )
            leave.write({
                'x_return_state': 'hr_confirmed',
                'x_manager_return_confirmed_by':
                    self.env.user.employee_id.id,
                'x_manager_return_date': fields.Datetime.now(),
            })
            leave.message_post(
                body=Markup(
                    '<strong>✅ Return Confirmed</strong><br/>'
                    '<b>Employee:</b> %(employee)s<br/>'
                    '<b>Return Date:</b> %(return_date)s<br/>'
                    '<b>Confirmed by:</b> %(confirmer)s'
                ) % {
                    'employee': leave.employee_id.name,
                    'return_date': leave.x_return_date,
                    'confirmer': self.env.user.name,
                },
                subtype_xmlid='mail.mt_note',
            )
            # Shorten first, then restart the accrual: while the request
            # still covers the days after the return it consumes the new
            # allocation, and the shortening itself needs the old allocation
            # to still cover the leave.
            leave._apply_confirmed_return()

    # ------------------------------------------------------------------
    # Daily reminder: confirm the return
    # ------------------------------------------------------------------

    # Days past the expected return after which HR is copied on the
    # reminder as well as the direct manager.
    _RETURN_REMINDER_ESCALATE_DAYS = 7

    @api.model
    def _cron_return_confirmation_reminders(self, commit=True):
        """Remind each direct manager to confirm an employee's return.

        Only leaves still sitting in ``x_return_state = 'on_vacation'`` are
        picked up, so a manager who already confirmed — including one who
        confirmed an *early* return before the expected date — is never
        reminded.  The reminder repeats daily until the return is confirmed,
        because an unconfirmed return blocks that employee's monthly payslip.

        ``commit`` is a cron progress checkpoint; tests pass False (Odoo
        forbids committing from inside a test).
        """
        today = fields.Date.context_today(self)
        leaves = self.sudo().search([
            ('state', '=', 'validate'),
            ('x_return_state', '=', 'on_vacation'),
            ('holiday_status_id.is_annual_leave', '=', True),
            ('request_date_to', '<', today),
            '|', ('x_return_reminder_last_sent', '=', False),
                 ('x_return_reminder_last_sent', '<', today),
        ], order='request_date_to')

        hr_group = self.env.ref(
            'KSW_annual_leave.group_annual_leave_hr',
            raise_if_not_found=False,
        )
        hr_partners = (
            hr_group.user_ids.partner_id if hr_group
            else self.env['res.partner']
        )

        sent = 0
        for leave in leaves:
            planned_return = leave.request_date_to + timedelta(days=1)
            overdue = (today - planned_return).days

            manager_partner = leave.employee_id.leave_manager_id.partner_id
            escalated = (
                overdue >= self._RETURN_REMINDER_ESCALATE_DAYS
                or not manager_partner
            )
            partners = manager_partner
            if escalated:
                partners |= hr_partners
            if not partners:
                # Nobody to tell — don't burn the daily stamp on it.
                continue

            body = Markup(
                '<strong>⏰ Return Confirmation Pending</strong><br/>'
                '<b>Employee:</b> %(employee)s<br/>'
                '<b>Vacation:</b> %(date_from)s → %(date_to)s<br/>'
                '<b>Expected Return:</b> %(planned)s<br/>'
                '<b>Days Overdue:</b> %(overdue)s<br/>'
                '<i>Open this request, set <b>Return Date</b> to the date the '
                'employee actually resumed work — it may be earlier than the '
                'expected date — then click <b>Confirm Return</b>. The '
                'employee\'s monthly payslip stays blocked until this is '
                'confirmed.</i>'
            ) % {
                'employee': leave.employee_id.name,
                'date_from': leave.request_date_from,
                'date_to': leave.request_date_to,
                'planned': planned_return,
                'overdue': overdue,
            }
            if escalated:
                body += Markup(
                    '<br/><i>HR has been copied on this reminder%(reason)s.'
                    '</i>'
                ) % {
                    'reason': (
                        Markup(' because the employee has no Direct Manager set')
                        if not manager_partner else Markup('')
                    ),
                }

            leave.sudo().message_post(
                body=body,
                partner_ids=partners.ids,
                subtype_xmlid='mail.mt_comment',
            )
            leave.sudo().write({
                'x_return_reminder_last_sent': today,
                'x_return_reminder_count': leave.x_return_reminder_count + 1,
            })
            sent += 1
            if commit:
                self.env.cr.commit()

        return sent

    # ==================================================================
    # Multi-Step Approval: can_approve / can_validate overrides
    # ==================================================================

    @api.depends('state', 'employee_id', 'department_id', 'holiday_status_id')
    def _compute_can_approve(self):
        """Hide the standard 'Approve' button for annual_multi leaves."""
        annual_multi = self.filtered(self._is_annual_multi)
        remaining = self - annual_multi
        if remaining:
            super(HrLeave, remaining)._compute_can_approve()
        for leave in annual_multi:
            leave.can_approve = False

    @api.depends('state', 'employee_id', 'department_id', 'holiday_status_id')
    def _compute_can_validate(self):
        """Hide the standard 'Validate' button for annual_multi leaves."""
        annual_multi = self.filtered(self._is_annual_multi)
        remaining = self - annual_multi
        if remaining:
            super(HrLeave, remaining)._compute_can_validate()
        for leave in annual_multi:
            leave.can_validate = False

    # Once the GM has given final approval, the decision is locked in —
    # only the HR confirmation step and the return-date confirmation flow
    # remain. Refuse no longer applies from here on.
    _REFUSE_LOCKED_STATES = frozenset({'pending_employee_signature', 'approved'})

    @api.depends_context('uid')
    @api.depends('state', 'employee_id', 'department_id',
                 'x_annual_approval_state', 'holiday_status_id')
    def _compute_can_refuse(self):
        """Hide 'Refuse' once an annual_multi leave has cleared GM final approval.

        Refuse remains available through pending_gm_final so an approver can
        still reject the request outright instead of advancing it. Once GM
        final approval is done (pending_employee_signature or approved),
        refusing no longer makes sense.

        The Settings Administrator keeps the button — they are the only role
        allowed to undo a finalised request (_check_final_reversal_rights).
        """
        is_admin = self.env.su or self.env.user.has_group(SETTINGS_ADMIN_GROUP)
        annual_multi_done = self.env['hr.leave'] if is_admin else self.filtered(
            lambda l: self._is_annual_multi(l)
            and l.x_annual_approval_state in self._REFUSE_LOCKED_STATES
        )
        remaining = self - annual_multi_done
        if remaining:
            super(HrLeave, remaining)._compute_can_refuse()
        for leave in annual_multi_done:
            leave.can_refuse = False

    @api.depends_context('uid')
    @api.depends('state', 'employee_id', 'x_annual_approval_state',
                 'holiday_status_id', 'first_approver_id', 'second_approver_id')
    def _compute_can_cancel(self):
        """Hide 'Cancel' once anybody has approved the request.

        Odoo lets an employee cancel their own *approved* leave through the
        Cancel wizard. KSW policy is the opposite: the request stops being
        the employee's the moment an approver acts on it, and once it is
        finalised only the Settings Administrator may undo it.
        """
        super()._compute_can_cancel()
        if self.env.su or self.env.user.has_group(SETTINGS_ADMIN_GROUP):
            # The mirror case: core only offers Cancel on the user's **own**
            # leave (`is_own_leave` in _get_next_states_by_state), so without
            # this the administrator we just made the sole owner of the
            # reversal routes cannot cancel anybody else's request.
            for leave in self:
                if not leave.can_cancel and leave.state in (
                        'validate1', 'validate', 'refuse'):
                    leave.can_cancel = True
            return
        for leave in self:
            if leave.can_cancel and (
                    leave._is_finalised() or leave._has_approver_action()):
                leave.can_cancel = False

    @api.depends_context('uid')
    @api.depends('state', 'employee_id', 'department_id')
    def _compute_can_back_to_approve(self):
        """'Back to Approval' only exists on a validated leave — i.e. always
        on a finalised one — so it is Settings-Administrator only."""
        super()._compute_can_back_to_approve()
        if self.env.su or self.env.user.has_group(SETTINGS_ADMIN_GROUP):
            return
        for leave in self:
            if leave.can_back_to_approve and leave._is_finalised():
                leave.can_back_to_approve = False

    # ==================================================================
    # Finalised requests: reversal restricted to the Settings Administrator
    # ==================================================================

    # Every ``state`` transition that walks a request *backwards*. Guarded in
    # write() so RPC callers cannot sidestep the action methods below.
    _REVERSAL_STATES = frozenset({'refuse', 'cancel', 'confirm', 'draft'})

    def _is_finalised(self):
        """True once the request may no longer be undone by a normal user.

        Two independent finish lines, because the KSW chains do not use the
        stock ``state`` field to record their progress:

        * ``state == 'validate'`` — covers every ordinary leave type
          (sick, business trip, umrah, the stock 1/2-step flows);
        * a KSW multi-step chain (annual / unpaid / EOS) that cleared **GM
          final approval** — those sit in ``state == 'confirm'`` right up
          until HR confirms the signed form, so ``state`` alone sees nothing.

        KSW_payroll and KSW_eos_leave extend this with "the leave already has
        a confirmed (done) payslip".
        """
        self.ensure_one()
        if self.state == 'validate':
            return True
        return bool(
            self._uses_multi_step_chain(self)
            and self.x_annual_approval_state in self._REFUSE_LOCKED_STATES
        )

    def _has_confirmed_payslip(self):
        """True when a **confirmed (done)** payslip hangs off this request.

        Overridden in KSW_payroll (vacation payslip) and KSW_eos_leave (EOS
        payslip), exactly like `_is_finalised`. Split out from it because the
        admin return needs to refuse *only* this case: reversing a request
        whose slip is already paid would strand a paid payslip on a
        mid-chain request, and cancelling it re-collects its installments
        next month (KSWCO SLIP/11307).
        """
        self.ensure_one()
        return False

    def _has_approver_action(self):
        """True once any approver has confirmed a step on this request.

        Used for the employee's own delete/cancel window: it closes as soon
        as somebody in the chain has acted, not only at final approval.
        """
        self.ensure_one()
        if self._uses_multi_step_chain(self):
            # The chain starts at 'pending_dm'; anything beyond it means a
            # step was signed off. A GM return-to-approver that lands back on
            # 'pending_dm' clears every stamp, so it reopens the window too.
            return self.x_annual_approval_state not in (False, 'pending_dm')
        return bool(
            self.state in ('validate1', 'validate')
            or self.first_approver_id
            or self.second_approver_id
        )

    def _check_final_reversal_rights(self, what):
        """Guard every route that undoes a finalised time off request.

        Same rationale as ``hr.payslip._check_payroll_manager``: view-level
        ``invisible=`` / ``can_refuse`` is cosmetic, this is the check that
        holds over RPC. ``self.env.su`` stays exempt so internal flows
        (payslip cancellation, employee archiving, crons) keep working —
        ``sudo()`` is not reachable from the web client.
        """
        if self.env.su or self.env.user.has_group(SETTINGS_ADMIN_GROUP):
            return
        if any(leave._is_finalised() for leave in self):
            raise UserError(_(
                'This time off request is already approved and can no longer '
                'be %s. Only the system administrator can do that.'
            ) % what)

    def _action_user_cancel(self, reason=None):
        """The Cancel wizard is another way out of an approved request.

        ``_force_cancel`` writes the state through ``sudo()``, so the write()
        guard never sees it — check here instead.
        """
        self._check_final_reversal_rights(_('cancelled'))
        is_admin = self.env.su or self.env.user.has_group(SETTINGS_ADMIN_GROUP)
        if not (is_admin or self.env.user.has_group(
                'KSW_annual_leave.group_leave_officer')):
            if any(leave._has_approver_action() for leave in self):
                raise UserError(_(
                    'This time off request has already been approved by one '
                    'of the approvers and can no longer be cancelled. Ask HR '
                    'to refuse it instead.'))
        if is_admin and not self.env.su:
            # Cancelling posts to the chatter, and mail.message create is
            # gated on access to the document. The administrator's leave
            # scope depends on which HR tier they hold; this action must not
            # (auth was settled above — gotcha #11).
            return super(HrLeave, self.sudo())._action_user_cancel(reason)
        return super()._action_user_cancel(reason)

    # ==================================================================
    # Write override — re-check allocation validity on toggle changes
    # ==================================================================

    # Fields that only the HR Approver role may set to meaningful values
    _HR_ONLY_FIELDS = frozenset({
        'x_penalty_amount', 'x_penalty_description',
        'x_iqama_renewal_amount', 'x_iqama_renewal_description',
    })
    # Fields that only the Accounting Approver role may set to meaningful values
    _ACC_ONLY_FIELDS = frozenset({
        'x_remaining_loans', 'x_remaining_loans_description',
        'x_flight_ticket_amount', 'x_flight_ticket_description',
    })

    def write(self, vals):
        """Re-trigger allocation validity on save for annual leaves.

        Two gaps we plug here:

        1. Base hr.leave.write() only calls _check_validity() when
           ``request_date_from`` (or date_from/to, holiday_status_id,
           employee_id, state) is in vals.  The base list has a typo —
           ``'request_date_from'`` appears twice and ``'request_date_to'``
           is missing — so modifying only the end date silently skips
           the check.  We explicitly cover all four date keys for
           annual leaves.

        2. Toggling x_excess_days_accepted or x_is_full_clearance
           changes number_of_days via _compute_duration but base does
           not re-check allocations on those field changes.

        Important UX choice: validation is **not** raised eagerly when
        the user only toggles a checkbox back-and-forth mid-edit.  Base
        already raises ValidationError at save-time when date/state/
        employee/type/toggle keys are in vals (covered below).  We also
        re-check at the start of each action_*_approve so advancing the
        multi-step chain never lets an out-of-balance leave through.

        The _skip_toggle_validity context flag is set by _compute_duration
        when auto-clearing stale toggles — those internal clears should
        NOT trigger validity checks (the compute is still in progress).
        """
        # Walking a finalised request backwards is Settings-Administrator
        # only. Guarding write() and not just the action_* methods closes the
        # direct-RPC route: core's own _check_approval_update happily lets the
        # employee's leave manager write state='refuse' on a validated leave.
        if vals.get('state') in self._REVERSAL_STATES:
            self._check_final_reversal_rights(_('reversed'))

        # Record rules only see the pre-write values, so re-pointing a
        # request at somebody outside the delegation would otherwise slip
        # past the scope-gated assistant rule.
        if vals.get('employee_id'):
            self._check_assistant_employee_scope(
                self.env['hr.employee'].sudo().browse(vals['employee_id']))

        # Enforce role-based write restrictions for sensitive fields.
        # Clearing fields to 0/False (e.g. from _reset_annual_multi_fields)
        # is allowed for all roles; only *setting* meaningful values is guarded.
        if not self.env.su:
            hr_set = {k for k in vals if k in self._HR_ONLY_FIELDS and vals[k]}
            if hr_set and not self.env.user.has_group(
                    'KSW_annual_leave.group_annual_leave_hr'):
                raise UserError(
                    'Only HR Approvers can fill in penalty, iqama renewal, '
                    'and flight ticket fields.')
            acc_set = {k for k in vals if k in self._ACC_ONLY_FIELDS and vals[k]}
            # For commission / other-deduction lines: treat create (0) or
            # update (1) commands as writes
            line_cmds = (vals.get('x_commission_line_ids', [])
                         + vals.get('x_deduction_line_ids', []))
            has_line_write = any(
                isinstance(cmd, (list, tuple)) and cmd[0] in (0, 1)
                for cmd in line_cmds
            )
            if (acc_set or has_line_write) and not self.env.user.has_group(
                    'KSW_annual_leave.group_annual_leave_acc'):
                raise UserError(
                    'Only Accounting Approvers can fill in commission, '
                    'other deduction, loan, and flight ticket fields.')

        # A confirmed return date may still be corrected, but only by the
        # employee's leave manager (the same person who confirmed it).
        # View-level readonly is cosmetic — this is the real gate.
        amended_from = {}
        if 'x_return_date' in vals:
            new_date = fields.Date.to_date(vals['x_return_date'])
            for leave in self:
                if (leave.x_return_state != 'hr_confirmed'
                        or leave.x_return_date == new_date):
                    continue
                if not self.env.su:
                    leave._check_can_edit_return_date()
                amended_from[leave.id] = leave.x_return_date

        # Snapshot the pre-write leave type so _resync_multi_step_chain can
        # tell a genuine type switch from a re-save of the same type. The
        # *type id* is needed, not just its validation type: EOS types are
        # 'annual_multi' too, so Annual -> EOS is a real chain restart even
        # though the validation type never changes.
        types_before = {}
        if 'holiday_status_id' in vals:
            types_before = {l.id: l.holiday_status_id.id for l in self}

        result = super().write(vals)

        for leave in self.filtered(lambda l: l.id in amended_from):
            leave.sudo().message_post(
                body=Markup(
                    '<strong>📅 Return Date Amended</strong><br/>'
                    '<b>Was:</b> %(old)s<br/>'
                    '<b>Now:</b> %(new)s<br/>'
                    '<b>Changed by:</b> %(user)s<br/>'
                    '<i>Vacation/monthly payroll figures that depend on the '
                    'return date may need to be recomputed.</i>'
                ) % {
                    'old': amended_from[leave.id] or '—',
                    'new': leave.x_return_date or '—',
                    'user': self.env.user.name,
                },
                subtype_xmlid='mail.mt_note',
            )
            # A corrected return date moves the covered period and the
            # accrual restart with it.
            leave._apply_confirmed_return()

        # Keep the multi-step chain in sync with the leave type. Runs AFTER
        # super() so holiday_status_id (and the stored relateds derived from
        # it) already carry the new value, and before the
        # _skip_toggle_validity early return below — that flag is about
        # allocation re-validation, not about the chain.
        if types_before:
            self._resync_multi_step_chain(types_before)

        if self.env.context.get('_skip_toggle_validity'):
            return result

        # Keys that must re-trigger _check_validity for annual leaves.
        # Covers the base typo (missing request_date_to) + toggle
        # changes that alter number_of_days through _compute_duration.
        revalidate_keys = {
            'request_date_from', 'request_date_to',
            'date_from', 'date_to',
            'x_excess_days_accepted', 'x_is_full_clearance',
        }
        if revalidate_keys & vals.keys():
            annual_to_check = self.filtered(
                lambda l: self._is_annual_leave(l)
                and l.holiday_status_id.requires_allocation
                and l.state not in ('cancel', 'refuse')
            )
            if annual_to_check:
                annual_to_check._check_validity()
                self.env['hr.leave.allocation'].invalidate_model(
                    ['leaves_taken', 'max_leaves'])

        return result

    # ==================================================================
    # Multi-Step Approval: keep the chain in sync with the leave type
    # ==================================================================

    _CHAIN_RESYNC_MESSAGES = {
        'started': 'Approval chain started — this leave type uses the KSW '
                   'multi-step approval chain.',
        'restarted': 'Approval chain restarted from Direct Manager — the '
                     'approvals already recorded were given for the previous '
                     'leave type and no longer apply.',
        'repaired': 'Approval chain re-synchronised with the leave type.',
        'cleared': 'Approval chain cleared — this leave type uses the '
                   'standard approval flow.',
    }

    def _resync_multi_step_chain(self, types_before=None):
        """Re-sync ``x_annual_approval_state`` with the current leave type.

        ``x_annual_approval_state`` used to be stamped at create() time only,
        so a leave whose type was edited afterwards kept whatever the
        *original* type implied: a Sick -> Annual switch left the field False
        (the KSW statusbar is hidden and the record falls back to the stock
        2-step bar), and an Annual -> Sick switch left a stale ``pending_*``
        value behind (which hides the *stock* statusbar instead).

        This method reconciles the two and is safe to call repeatedly — it is
        a no-op once the record is consistent, so simply re-saving a record
        broken by the old behaviour heals it.

        Only draft/confirm records are touched. From validate1 onwards the
        decision is made and the type field is readonly in the view; a
        programmatic write at that point must not silently rewind approvals.

        :param types_before: optional {leave_id: previous holiday_status_id}
            captured before the write. Needed to tell a genuine type switch
            from a re-save: Annual and EOS are both ``annual_multi``, so the
            validation type alone cannot tell them apart. When omitted, only
            the self-healing branches fire.
        """
        types_before = types_before or {}
        for leave in self:
            if leave.state not in ('draft', 'confirm'):
                continue

            wants_chain = self._uses_multi_step_chain(leave)
            current = leave.x_annual_approval_state
            type_changed = (
                leave.id in types_before
                and types_before[leave.id] != leave.holiday_status_id.id
            )

            if wants_chain and not current:
                # Switched into a chain, or a record broken by the old code.
                reason = 'started' if type_changed else 'repaired'
            elif not wants_chain and current:
                # Switched out of a chain: drop it so the stock statusbar and
                # the standard Approve/Validate buttons come back.
                leave._reset_chain_for_type_change(notify=False)
                leave._post_chain_resync_note('cleared')
                continue
            elif wants_chain and current and type_changed:
                if current == 'pending_dm':
                    # Nothing approved yet — clear the type-specific figures
                    # but stay at step 1 without re-notifying the DM.
                    leave._reset_chain_for_type_change(notify=False)
                    leave.sudo().write({
                        'x_annual_approval_state': 'pending_dm'})
                    continue
                # Approvals already given were given for a *different* leave
                # type and no longer mean anything. Restart from the top.
                reason = 'restarted'
            else:
                continue

            leave._reset_chain_for_type_change(notify=True)
            leave._post_chain_resync_note(reason)

    def _reset_chain_for_type_change(self, notify):
        """Clear every chain stamp and, when entering a chain, restart at DM.

        sudo() is required on two counts: _reset_annual_multi_fields unlinks
        x_commission_line_ids / x_deduction_line_ids (unlink is granted to
        group_leave_officer only,
        yet the chain reaches pending_acc while state is still 'confirm' and
        the type field is still editable), and the reset writes fields behind
        the _HR_ONLY_FIELDS / _ACC_ONLY_FIELDS role guards.

        _skip_toggle_validity suppresses the redundant _check_validity that
        the reset's own write() would trigger through x_excess_days_accepted /
        x_is_full_clearance — base hr.leave.write already validated the new
        type inside the enclosing super() call.
        """
        self.ensure_one()
        record = self.sudo().with_context(_skip_toggle_validity=True)
        record._reset_annual_multi_fields()
        if notify:
            record.write({'x_annual_approval_state': 'pending_dm'})
            # Notify as the acting user (not sudo) so the DM sees who made
            # the change. leave_manager_id carries no field-level groups= and
            # the pending_dm branch never dereferences a res.groups, so this
            # is safe for employee-level users — it is the same call create()
            # already makes on their behalf.
            self._notify_pending_approvers(self, 'pending_dm')

    def _post_chain_resync_note(self, reason):
        self.ensure_one()
        self.sudo().message_post(
            body=Markup(
                '<strong>🔄 Leave Type Changed</strong><br/>'
                '<b>New type:</b> %(type)s<br/>'
                '<b>Changed by:</b> %(user)s<br/>'
                '<i>%(detail)s</i>'
            ) % {
                'type': self.holiday_status_id.display_name or '—',
                'user': self.env.user.name,
                'detail': self._CHAIN_RESYNC_MESSAGES[reason],
            },
            subtype_xmlid='mail.mt_note',
        )

    # ==================================================================
    # Multi-Step Approval: create hook
    # ==================================================================

    @api.model_create_multi
    def create(self, vals_list):
        # Checked before super() so a Manager Assistant filing for someone
        # outside their delegation gets an explanatory UserError rather
        # than the bare AccessError the create record rule would raise.
        for vals in vals_list:
            if vals.get('employee_id'):
                self._check_assistant_employee_scope(
                    self.env['hr.employee'].sudo().browse(vals['employee_id']))
        records = super().create(vals_list)
        for leave in records:
            # Deliberately _is_annual_multi, NOT _uses_multi_step_chain:
            # KSW_unpaid_leave.create() stamps its own leaves after its own
            # super() call, so widening this check would stamp and notify
            # twice for unpaid leaves. The chain/type re-sync in write()
            # (_resync_multi_step_chain) is the one that uses the hook.
            if self._is_annual_multi(leave):
                leave.sudo().write({'x_annual_approval_state': 'pending_dm'})
                # In Odoo 19, leaves are created directly in 'confirm' state
                # (no action_confirm). Notify the DM here.
                self._notify_pending_approvers(leave, 'pending_dm')
        return records

    # ==================================================================
    # Multi-Step Approval: action_approve intercept
    # ==================================================================

    def action_approve(self, check_state=True):
        """Intercept annual_multi leaves — route through multi-step."""
        annual_multi = self.filtered(self._is_annual_multi)
        remaining = self - annual_multi

        if annual_multi:
            for leave in annual_multi:
                if leave.x_annual_approval_state == 'pending_dm':
                    leave.action_dm_approve()

        if remaining:
            return super(HrLeave, remaining).action_approve(
                check_state=check_state)
        return True

    # ==================================================================
    # Multi-Step Approval: Step-by-step action methods
    # ==================================================================

    def _check_annual_approval_can_advance(self):
        """Validate that an annual leave is fit to move to the next step.

        Re-runs the allocation check because the multi-step chain writes
        only ``x_annual_approval_state`` (not ``state``), so base
        hr.leave's state-change validation never fires.  Without this
        guard, an employee whose dates were edited to exceed the
        balance (with all toggles cleared) could still be approved.
        """
        annual_to_check = self.filtered(
            lambda l: self._is_annual_leave(l)
            and l.holiday_status_id.requires_allocation
            and l.state not in ('cancel', 'refuse')
        )
        if annual_to_check:
            annual_to_check._check_validity()

    def action_dm_approve(self):
        """Step 1: Direct Manager approves the initial request."""
        self._check_annual_approval_can_advance()
        for leave in self:
            if leave.x_annual_approval_state != 'pending_dm':
                raise UserError(
                    'This leave is not pending DM approval.')
            if not self.env.su:
                dm = leave.employee_id.leave_manager_id
                if dm:
                    # DM is configured: only that specific user may approve
                    if dm != self.env.user:
                        raise UserError(
                            'Only %s (the leave manager) can approve this step.'
                            % dm.name
                        )
                else:
                    # No DM configured: HR Approver acts as fallback
                    if not self.env.user.has_group(
                            'KSW_annual_leave.group_annual_leave_hr'):
                        raise UserError(
                            'No Direct Manager is configured for this employee. '
                            'An HR Approver must perform this step.'
                        )
            leave.write({
                'x_annual_approval_state': 'pending_hr',
                'x_dm_approved_by': self.env.user.employee_id.id,
                'x_dm_approved_date': fields.Datetime.now(),
            })
            leave.message_post(
                body=Markup(
                    '<strong>✅ Step 1 — DM Approval</strong><br/>'
                    '<b>Approved by:</b> %(approver)s<br/>'
                    '<b>Employee:</b> %(employee)s<br/>'
                    '<b>Leave Period:</b> %(date_from)s → %(date_to)s'
                    '<br/><b>Days:</b> %(days)s'
                ) % {
                    'approver': self.env.user.name,
                    'employee': leave.employee_id.name,
                    'date_from': leave.request_date_from,
                    'date_to': leave.request_date_to,
                    'days': leave.number_of_days,
                },
                subtype_xmlid='mail.mt_note',
            )
            self._notify_pending_approvers(leave, 'pending_hr')

        # If a single attendance-sheet employee's leave is being approved and
        # there are draft sheet lines covering the leave period, open the wizard
        # so the DM can mark those days absent immediately.
        if 'ksw.attendance.sheet' in self.env and len(self) == 1:
            leave = self
            if leave.employee_id.sudo().x_is_attendance_sheet:
                has_lines = self.env['ksw.attendance.sheet.line'].sudo().search_count([
                    ('sheet_id.employee_id', '=', leave.employee_id.id),
                    ('sheet_id.state', '=', 'draft'),
                    ('date', '>=', leave.request_date_from),
                    ('date', '<=', leave.request_date_to),
                    ('is_workday', '=', True),
                    ('is_attended', '=', True),
                ])
                if has_lines:
                    return {
                        'type': 'ir.actions.act_window',
                        'name': _('Update Attendance Sheet'),
                        'res_model': 'ksw.leave.attendance.wizard',
                        'view_mode': 'form',
                        'target': 'new',
                        'context': {'default_leave_id': leave.id},
                    }

    def action_hr_approve(self):
        """Step 2: HR approves and fills penalty + iqama renewal."""
        self._check_group(
            'KSW_annual_leave.group_annual_leave_hr',
            'Only HR Approvers can approve this step.',
        )
        self._check_annual_approval_can_advance()
        for leave in self:
            if leave.x_annual_approval_state != 'pending_hr':
                raise UserError(
                    'This leave is not pending HR approval.')
            leave.write({
                'x_annual_approval_state': 'pending_gm_initial',
                'x_hr_approved_by': self.env.user.employee_id.id,
                'x_hr_approved_date': fields.Datetime.now(),
            })
            body = Markup(
                '<strong>✅ Step 2 — HR Approval</strong><br/>'
                '<b>Approved by:</b> %(user)s<br/>'
            ) % {'user': self.env.user.name}
            if leave.x_penalty_amount:
                body += Markup(
                    '<b>Penalty:</b> %(amt).2f SAR'
                ) % {'amt': leave.x_penalty_amount}
                if leave.x_penalty_description:
                    body += Markup(' — %(desc)s') % {
                        'desc': leave.x_penalty_description}
                body += Markup('<br/>')
            if leave.x_iqama_renewal_amount:
                body += Markup(
                    '<b>Iqama Renewal:</b> %(amt).2f SAR'
                ) % {'amt': leave.x_iqama_renewal_amount}
                if leave.x_iqama_renewal_description:
                    body += Markup(' — %(desc)s') % {
                        'desc': leave.x_iqama_renewal_description}
                body += Markup('<br/>')
            if leave.x_flight_ticket_amount:
                body += Markup(
                    '<b>Flight Ticket:</b> %(amt).2f SAR'
                ) % {'amt': leave.x_flight_ticket_amount}
                if leave.x_flight_ticket_description:
                    body += Markup(' — %(desc)s') % {
                        'desc': leave.x_flight_ticket_description}
                body += Markup('<br/>')
            leave.message_post(
                body=body,
                subtype_xmlid='mail.mt_note',
            )
            self._notify_pending_approvers(leave, 'pending_gm_initial')

    def action_gm_initial_approve(self):
        """Step 3: GM gives initial approval (read-only review)."""
        self._check_annual_approval_can_advance()
        for leave in self:
            self._check_department_gm(leave)
            if leave.x_annual_approval_state != 'pending_gm_initial':
                raise UserError(
                    'This leave is not pending GM initial approval.')
            leave.write({
                'x_annual_approval_state': 'pending_acc',
                'x_gm_initial_approved_by':
                    self.env.user.employee_id.id,
                'x_gm_initial_approved_date': fields.Datetime.now(),
            })
            leave.message_post(
                body=Markup(
                    '<strong>✅ Step 3 — GM Initial Approval</strong>'
                    '<br/><b>Approved by:</b> %(approver)s'
                ) % {'approver': self.env.user.name},
                subtype_xmlid='mail.mt_note',
            )
            self._notify_pending_approvers(leave, 'pending_acc')

    def action_acc_approve(self):
        """Step 4: Accounting approves and fills flight ticket."""
        self._check_group(
            'KSW_annual_leave.group_annual_leave_acc',
            'Only Accounting Approvers can approve this step.',
        )
        self._check_annual_approval_can_advance()
        for leave in self:
            if leave.x_annual_approval_state != 'pending_acc':
                raise UserError(
                    'This leave is not pending accounting approval.')
            leave.write({
                'x_annual_approval_state': 'pending_gm_final',
                'x_acc_approved_by': self.env.user.employee_id.id,
                'x_acc_approved_date': fields.Datetime.now(),
            })
            body = Markup(
                '<strong>✅ Step 4 — Accounting Approval</strong><br/>'
                '<b>Approved by:</b> %(user)s<br/>'
            ) % {'user': self.env.user.name}
            if leave.x_commission_line_ids:
                body += Markup('<b>Additional Commissions:</b><br/>')
                for line in leave.x_commission_line_ids:
                    body += Markup(
                        '&nbsp;&nbsp;• %(name)s: %(amt).2f SAR<br/>'
                    ) % {'name': line.name, 'amt': line.amount}
                body += Markup(
                    '<b>Total:</b> %(total).2f SAR<br/>'
                ) % {'total': leave.x_additional_commissions}
            if leave.x_remaining_loans:
                body += Markup(
                    '<b>Remaining Loans:</b> %(amt).2f SAR'
                ) % {'amt': leave.x_remaining_loans}
                if leave.x_remaining_loans_description:
                    body += Markup(' — %(desc)s') % {
                        'desc': leave.x_remaining_loans_description}
                body += Markup('<br/>')
            if leave.x_deduction_line_ids:
                body += Markup('<b>Other Deductions:</b><br/>')
                for line in leave.x_deduction_line_ids:
                    body += Markup(
                        '&nbsp;&nbsp;• %(name)s: %(amt).2f SAR<br/>'
                    ) % {'name': line.name, 'amt': line.amount}
                body += Markup(
                    '<b>Total:</b> %(total).2f SAR<br/>'
                ) % {'total': leave.x_other_deductions}
            leave.message_post(
                body=body,
                subtype_xmlid='mail.mt_note',
            )
            self._notify_pending_approvers(leave, 'pending_gm_final')

    def action_gm_final_approve(self):
        """Step 5: GM gives final approval.

        Creates the vacation payslip (all financial data is now ready)
        then moves to 'pending_employee_signature' and notifies HR.
        The final Odoo validation (state → validate) happens only after
        HR confirms the document upload in Step 6.
        """
        self._check_annual_approval_can_advance()
        for leave in self:
            self._check_department_gm(leave)
            if leave.x_annual_approval_state != 'pending_gm_final':
                raise UserError(
                    'This leave is not pending GM final approval.')

            leave.write({
                'x_annual_approval_state': 'pending_employee_signature',
                'x_gm_final_approved_by':
                    self.env.user.employee_id.id,
                'x_gm_final_approved_date': fields.Datetime.now(),
            })

            # Create vacation payslip now — all financial inputs are
            # finalised and the payslip guard (x_return_state) has not
            # been set yet (that happens in _action_validate / Step 6).
            leave._create_vacation_payslip()

            leave.message_post(
                body=Markup(
                    '<strong>✅ Step 5 — GM Final Approval</strong>'
                    '<br/><b>Approved by:</b> %(approver)s<br/>'
                    '<b>Next:</b> Awaiting HR document confirmation to finalise.'
                ) % {'approver': self.env.user.name},
                subtype_xmlid='mail.mt_note',
            )
            self._notify_pending_approvers(leave, 'pending_employee_signature')

    def action_open_gm_return_wizard(self):
        """Open the return wizard — two audiences, one button.

        The GM may return a request to an earlier approver from either of the
        two GM steps. The Settings Administrator may return *any* request to
        *any* step at any time (including one already validated); the wizard
        itself decides which targets each role may pick.
        """
        self.ensure_one()
        is_admin = self.env.su or self.env.user.has_group(SETTINGS_ADMIN_GROUP)
        if not is_admin:
            self._check_department_gm(self)
            if self.x_annual_approval_state not in (
                    'pending_gm_initial', 'pending_gm_final'):
                raise UserError(
                    'The leave must be at a GM approval step to use this action.')
        return {
            'type': 'ir.actions.act_window',
            'name': 'Return to Approver',
            'res_model': 'ksw.gm.return.approver.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_leave_id': self.id},
        }

    def action_open_hr_confirm_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Confirm Signed Vacation Form',
            'res_model': 'ksw.hr.confirm.signature.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_leave_id': self.id},
        }

    def action_employee_confirm_signature(self):
        """Step 6: HR confirms document upload and finalises the leave.

        Requires at least one attachment to be present on the leave
        (the signed vacation form). After confirmation the leave is
        fully validated by Odoo (state → 'validate', allocation deducted,
        x_return_state set to 'on_vacation').
        """
        user = self.env.user
        for leave in self:
            if leave.x_annual_approval_state != 'pending_employee_signature':
                raise UserError(
                    'This leave is not pending HR confirmation.')
            if not self.env.su:
                is_hr = user.has_group('KSW_annual_leave.group_annual_leave_hr')
                if not is_hr:
                    raise UserError(
                        'Only an HR Approver can confirm the document upload.')
            if not leave.x_attachment_ids:
                raise UserError(
                    'Please upload the signed vacation form as an attachment '
                    'before confirming.')

            leave.write({
                'x_annual_approval_state': 'approved',
                'x_employee_signed_by': user.employee_id.id,
                'x_employee_signed_date': fields.Datetime.now(),
            })
            leave.message_post(
                body=Markup(
                    '<strong>✅ Step 6 — HR Document Confirmation</strong><br/>'
                    '<b>Confirmed by:</b> %(user)s<br/>'
                    '<b>Employee:</b> %(employee)s<br/>'
                    '<b>Status:</b> Fully approved.'
                ) % {
                    'user': user.name,
                    'employee': leave.employee_id.name,
                },
                subtype_xmlid='mail.mt_note',
            )

        # Standard Odoo validation — skip leaves already in 'validate'
        # state (e.g. retroactive signature requests on leaves approved
        # before this step existed).
        to_validate = self.filtered(lambda l: l.state != 'validate')
        if to_validate:
            to_validate._action_validate(check_state=False)
            # Safety net: mark any remaining attended workday lines absent.
            # No-op if the DM already did this via the wizard at step 1.
            to_validate._mark_attendance_sheet_leave_absent()

    # ==================================================================
    # Vacation payslip hook (overridden by KSW_payroll)
    # ==================================================================

    def _create_vacation_payslip(self):
        """Hook for KSW_payroll to create a vacation payslip.

        Base implementation does nothing. KSW_payroll overrides this
        to create an actual payslip with vacation inputs.
        """
        pass

    # ==================================================================
    # Helpers
    # ==================================================================

    def _check_group(self, group_xmlid, message):
        """Raise UserError if current user doesn't belong to the group."""
        if not self.env.user.has_group(group_xmlid):
            raise UserError(message)

    # ------------------------------------------------------------------
    # Department GM
    # ------------------------------------------------------------------
    #
    # A group answers "may you act as a GM at all"; it can never answer
    # "for whom". `_check_group` does not even iterate `self` -- it is
    # blind to the record -- which is exactly how one GM group came to
    # mean one GM over the whole company. The department carries the
    # answer instead: `hr.department.x_effective_gm_id`, seeded to the
    # sitting GM and editable per department.
    #
    # There is deliberately NO company-wide override here. A GM who is not
    # named on a department cannot approve its requests, whoever he is.

    @api.model
    def _department_gm_user(self, leave):
        """The user who must clear the GM steps for this request.

        sudo() throughout: the acting user has no `hr.employee` model
        access, so reading the requester's department is impossible in
        their own right. This is an identity read, not a scope one --
        the same reason `_manager_user_id` in KSW_deduction is sudo'd.
        """
        employee = leave.employee_id.sudo()
        gm = employee.department_id.x_effective_gm_id
        if not gm:
            # No department at all -- 103 active employees are in that
            # position -- so the company default is the only answer left.
            gm = (leave.company_id or self.env.company).sudo().x_default_gm_id
        return gm.sudo().user_id

    def _check_department_gm(self, leave):
        """Raise unless the caller is this request's department GM."""
        if self.env.su:
            return
        gm_user = self._department_gm_user(leave)
        if not gm_user:
            raise UserError(_(
                "No General Manager is set for %(dept)s, so this step "
                "cannot be approved. Ask HR to set the department's "
                "General Manager.",
                dept=(leave.employee_id.sudo().department_id.display_name
                      or _('this employee')),
            ))
        if self.env.user != gm_user:
            raise UserError(_(
                "Only %(gm)s, the General Manager of %(dept)s, can approve "
                "this step.",
                gm=gm_user.name,
                dept=(leave.employee_id.sudo().department_id.display_name
                      or _('this department')),
            ))

    def _mark_attendance_sheet_leave_absent(self):
        """Mark remaining attended workday lines absent on draft attendance sheets.

        Called at final validation as a safety net. If the DM already marked
        the lines via the wizard, this search returns nothing and is a no-op.
        Only acts when KSW_attendance_sheet is installed.
        """
        if 'ksw.attendance.sheet' not in self.env:
            return
        Line = self.env['ksw.attendance.sheet.line'].sudo()
        for leave in self:
            emp = leave.employee_id
            if not emp.sudo().x_is_attendance_sheet:
                continue
            lines = Line.search([
                ('sheet_id.employee_id', '=', emp.id),
                ('sheet_id.state', '=', 'draft'),
                ('date', '>=', leave.request_date_from),
                ('date', '<=', leave.request_date_to),
                ('is_workday', '=', True),
                ('is_attended', '=', True),
            ])
            if not lines:
                continue
            lines.with_context(ksw_system_write=True).write({'is_attended': False})
            months = sorted({(l.date.year, l.date.month) for l in lines})
            month_strs = ', '.join(
                '%s %d' % (_cal.month_name[m], y) for y, m in months
            )
            leave.message_post(
                body=Markup(
                    '<strong>📋 Attendance Sheet Auto-Updated at Validation</strong><br/>'
                    '<b>%(emp)s</b>: %(count)d remaining workday(s) marked absent '
                    'across %(months)s (leave %(from_)s – %(to_)s).'
                ) % {
                    'emp': emp.name,
                    'count': len(lines),
                    'months': month_strs,
                    'from_': leave.request_date_from,
                    'to_': leave.request_date_to,
                },
                subtype_xmlid='mail.mt_note',
            )

    # 'department_gm' marks the steps whose recipient is one named person
    # derived from the request, not a whole group. Data-driven rather than a
    # state-name test in the body, so a new GM-style step cannot be added
    # without deciding how it routes.
    _ANNUAL_MULTI_STEP_CONFIG = {
        'pending_dm':                 {'label': 'Direct Manager Approval',  'group': None},
        'pending_hr':                 {'label': 'HR Approval',              'group': 'KSW_annual_leave.group_annual_leave_hr'},
        'pending_gm_initial':         {'label': 'GM Initial Approval',      'group': None, 'department_gm': True},
        'pending_acc':                {'label': 'Accounting Approval',      'group': 'KSW_annual_leave.group_annual_leave_acc'},
        'pending_gm_final':           {'label': 'GM Final Approval',        'group': None, 'department_gm': True},
        'pending_employee_signature': {'label': 'HR Confirmation',           'group': 'KSW_annual_leave.group_annual_leave_hr'},
    }

    def _notify_pending_approvers(self, leave, pending_state):
        """Send an inbox + email notification to whoever must act on this step."""
        config = self._ANNUAL_MULTI_STEP_CONFIG.get(pending_state)
        if not config:
            return

        if pending_state == 'pending_dm':
            dm_user = leave.employee_id.leave_manager_id  # res.users
            partner_ids = [dm_user.partner_id.id] if dm_user and dm_user.partner_id else []
        elif config.get('department_gm'):
            gm_user = self._department_gm_user(leave)
            partner_ids = (
                [gm_user.partner_id.id]
                if gm_user and gm_user.partner_id else []
            )
        else:
            group = self.env.ref(config['group'], raise_if_not_found=False)
            partner_ids = group.user_ids.mapped('partner_id').ids if group else []

        if not partner_ids:
            return

        if pending_state == 'pending_employee_signature':
            instruction = (
                'Please upload the signed vacation request form as an '
                'attachment, then click "Confirm & Finalise" to complete the approval.'
            )
        else:
            instruction = 'This annual leave request is awaiting your approval.'

        # Name whoever actually filed the request when that is not the
        # employee themselves — typically a Manager Assistant preparing it
        # for the DM. create_uid is the single source of truth here; it is
        # immutable and already what the vacation report prints as
        # "Requested By". Lives in _notify_pending_approvers rather than
        # create() so KSW_unpaid_leave's own stamp+notify pair inherits it.
        prepared_by = Markup('')
        if leave.create_uid and leave.create_uid != leave.employee_id.sudo().user_id:
            prepared_by = Markup('<b>Prepared by:</b> %(by)s<br/>') % {
                'by': leave.create_uid.name,
            }

        leave.message_post(
            body=Markup(
                '<strong>&#9203; Action Required — %(label)s</strong><br/>'
                '<b>Employee:</b> %(employee)s<br/>'
                '<b>Period:</b> %(date_from)s &#8594; %(date_to)s<br/>'
                '<b>Days:</b> %(days).1f<br/>'
                '%(prepared_by)s'
                '%(instruction)s'
            ) % {
                'label': config['label'],
                'employee': leave.employee_id.name,
                'date_from': leave.request_date_from,
                'date_to': leave.request_date_to,
                'days': leave.number_of_days,
                'prepared_by': prepared_by,
                'instruction': instruction,
            },
            partner_ids=partner_ids,
            subtype_xmlid='mail.mt_comment',
        )

    def _reset_annual_multi_fields(self):
        """Reset all multi-step approval fields to their defaults."""
        # Delete commission / other-deduction lines (cascaded by ORM, but
        # explicit is clearer)
        self.mapped('x_commission_line_ids').unlink()
        self.mapped('x_deduction_line_ids').unlink()
        self.write({
            'x_annual_approval_state': False,
            'x_is_full_clearance': False,
            'x_penalty_amount': 0,
            'x_penalty_description': False,
            'x_iqama_renewal_amount': 0,
            'x_iqama_renewal_description': False,
            'x_flight_ticket_amount': 0,
            'x_flight_ticket_description': False,
            'x_remaining_loans': 0,
            'x_remaining_loans_description': False,
            'x_excess_days_accepted': False,
            'x_annual_portion_days': 0,
            'x_unpaid_portion_days': 0,
            'x_dm_approved_by': False,
            'x_dm_approved_date': False,
            'x_hr_approved_by': False,
            'x_hr_approved_date': False,
            'x_gm_initial_approved_by': False,
            'x_gm_initial_approved_date': False,
            'x_acc_approved_by': False,
            'x_acc_approved_date': False,
            'x_gm_final_approved_by': False,
            'x_gm_final_approved_date': False,
            'x_employee_signed_by': False,
            'x_employee_signed_date': False,
        })

    # ==================================================================
    # Override _action_validate — set return state on vacation
    # ==================================================================

    def _action_validate(self, check_state=True):
        """Set return state to 'on_vacation' when annual leave validated."""
        result = super()._action_validate(check_state=check_state)
        annual = self.filtered(self._is_annual_leave)
        if annual:
            annual.write({'x_return_state': 'on_vacation'})
            # Refresh accrual — leaves_taken changed (leave now validated)
            emp_ids = annual.mapped('employee_id').ids
            self.env['ksw.annual.leave']._refresh_accrual_for_employees(emp_ids)
        return result

    # ==================================================================
    # Override action_refuse — reset multi-step fields
    # ==================================================================

    def _move_validate_leave_to_confirm(self):
        """Override 'Back to Approval' to reset multi-step approval,
        return state, and restart the approval chain."""
        self._check_final_reversal_rights(_('sent back to approval'))
        annual = self.filtered(
            lambda l: self._is_annual_leave(l)
            and l.x_return_state != 'not_applicable'
        )
        annual_multi = self.filtered(self._is_annual_multi)

        # Collect employee IDs before state changes
        annual_emp_ids = self.filtered(self._is_annual_leave).mapped('employee_id').ids

        if annual:
            annual.write({
                'x_return_state': 'not_applicable',
                'x_return_date': False,
                'x_manager_return_confirmed_by': False,
                'x_manager_return_date': False,
                'x_hr_return_confirmed_by': False,
                'x_hr_return_date': False,
                'x_return_reminder_last_sent': False,
                'x_return_reminder_count': 0,
            })

        # A *targeted* return (the admin wizard picking a specific step) keeps
        # every figure the approvers already entered and sets its own target
        # state afterwards — only the plain "Back to Approval" restarts the
        # chain from scratch.
        keep_data = self.env.context.get('ksw_keep_approval_data')

        if annual_multi and not keep_data:
            annual_multi._reset_annual_multi_fields()

        result = super()._move_validate_leave_to_confirm()

        # After super sets state='confirm', restart the approval chain
        if annual_multi and not keep_data:
            annual_multi.write({'x_annual_approval_state': 'pending_dm'})

        # Refresh accrual — leaves_taken changed
        if annual_emp_ids:
            self.env['ksw.annual.leave'].with_user(1)._refresh_accrual_for_employees(annual_emp_ids)

        # The leave is no longer validated, so its days are uncovered again —
        # revoke any weekend they had earned. Idempotent in both directions.
        self._recheck_weekend_grants()

        return result

    def action_refuse(self):
        """Reset return and multi-step fields when refused."""
        self._check_final_reversal_rights(_('refused'))
        annual = self.filtered(
            lambda l: self._is_annual_leave(l)
            and l.x_return_state != 'not_applicable'
        )
        annual_multi = self.filtered(self._is_annual_multi)

        # Collect employee IDs before state changes
        annual_emp_ids = self.filtered(self._is_annual_leave).mapped('employee_id').ids

        result = super().action_refuse()

        if annual:
            annual.write({
                'x_return_state': 'not_applicable',
                'x_return_date': False,
                'x_manager_return_confirmed_by': False,
                'x_manager_return_date': False,
                'x_hr_return_confirmed_by': False,
                'x_hr_return_date': False,
                'x_return_reminder_last_sent': False,
                'x_return_reminder_count': 0,
            })

        if annual_multi:
            annual_multi._reset_annual_multi_fields()
            # Vacation payslip cancellation handled by KSW_payroll override

        # Refresh accrual — leaves_taken changed (leave no longer validated)
        if annual_emp_ids:
            self.env['ksw.annual.leave']._refresh_accrual_for_employees(annual_emp_ids)

        return result

    # ==================================================================
    # Override action_draft — restart multi-step chain
    # ==================================================================

    def action_draft(self):
        """When resetting to draft, restart the approval chain."""
        self._check_final_reversal_rights(_('reset to draft'))
        # Collect employee IDs before state changes
        annual_emp_ids = self.filtered(self._is_annual_leave).mapped('employee_id').ids

        result = super().action_draft()
        annual_multi = self.filtered(self._is_annual_multi)
        if annual_multi:
            annual_multi._reset_annual_multi_fields()
            for leave in annual_multi:
                leave.x_annual_approval_state = 'pending_dm'
            # Vacation payslip cancellation handled by KSW_payroll override

        # Refresh accrual — leaves_taken changed
        if annual_emp_ids:
            self.env['ksw.annual.leave'].with_user(1)._refresh_accrual_for_employees(annual_emp_ids)

        return result

    # ==================================================================
    # Override _unlink_if_correct_states — allow KSW managers to delete past leaves
    # ==================================================================

    def _is_own_untouched_request(self):
        """True when this leave is the current user's own request and no
        approver has acted on it yet.

        The employee owns the request only until somebody signs off a step:
        on the KSW chains that is any ``x_annual_approval_state`` past
        ``pending_dm``, on the stock flows the first approval (``validate1``
        / ``first_approver_id``). Up to that point the request may be deleted
        whatever its start date — Odoo core blocks non-Officer users from
        deleting a leave whose ``date_from`` is in the past, which strands
        employees whose request is still crawling through the chain past its
        own start date.
        """
        self.ensure_one()
        if self.employee_id.sudo().user_id != self.env.user:
            return False
        # `cancel` is deliberately excluded: those are handled by Odoo's own
        # cancellation flow, not by deletion.
        if self.state != 'confirm':
            return False
        return not self._has_approver_action()

    @api.ondelete(at_uninstall=False)
    def _unlink_if_correct_states(self):
        """Override Odoo's deletion rules: one tightening, two relaxations.

        0. A **finalised** request (validated, or a KSW chain past GM final
           approval, or one carrying a confirmed payslip) can only be deleted
           by the Settings Administrator — ahead of every branch below,
           including Odoo's own Time-Off-Administrator bypass. A multi-step
           request sits in ``state == 'confirm'`` even after GM final
           approval, so the state checks further down would wave it through.
        1. KSW Supervisors/Officers may delete leaves that started in the
           past — they manage subordinate leaves that just started and need
           correction/deletion — and may also delete a **refused** request,
           which Odoo core reserves for Administrators.
        2. Any employee may delete their **own** request as long as no
           approver has acted on it yet, whatever its start date. Odoo core
           blocks non-Officer users from deleting a leave whose ``date_from``
           is in the past, which strands employees whose request is still
           crawling through the multi-step approval chain past its own start
           date.
        """
        self._check_final_reversal_rights(_('deleted'))

        if self.env.user.has_group(SETTINGS_ADMIN_GROUP):
            # The Settings Administrator owns every reversal route, so they
            # must not be stopped by the state checks below. Spelled out
            # rather than leaning on the Time-Off-Administrator bypass that
            # follows: holding one group does not imply the other, and in
            # KSWCO it is only ever true by coincidence of assignment.
            return

        if self.env.user.has_group('hr_holidays.group_hr_holidays_manager'):
            # Core Time-Off Administrators have no restrictions at all (matches
            # Odoo core's own bypass) — don't let the KSW state check below
            # shadow that for them.
            return

        is_ksw_manager = self.env.user.has_group('KSW_annual_leave.group_leave_supervisor') or \
                         self.env.user.has_group('KSW_annual_leave.group_leave_officer')

        if is_ksw_manager:
            # We enforce a state check (confirm/validate1/cancel/refuse)
            # but SKIP the date check for KSW managers.
            #
            # 'refuse' is KSW-specific: a supervisor who refused a wrong
            # request (typically one they raised themselves for a
            # subordinate) must be able to clear it away afterwards.
            # A refused leave has no payroll or attendance effect left —
            # action_refuse already cancelled the vacation payslip,
            # released the attendance lines and refreshed the accrual.
            error_message = self.env._('Oops! %(state)s Time-Off requests can only be deleted by Administrators.')
            state_description_values = {elem[0]: elem[1] for elem in self._fields['state']._description_selection(self.env)}
            for holiday in self:
                if holiday.state not in ['confirm', 'validate1', 'cancel', 'refuse']:
                    raise UserError(error_message % {'state': state_description_values.get(holiday.state)})
            return # Bypass Odoo's core check

        # Own requests: still-untouched ones bypass core's past-date guard,
        # but the window closes the moment an approver signs off a step —
        # core would still allow deleting an own future 'validate1' leave, so
        # refuse it explicitly instead of falling through. Refused/cancelled
        # requests are not "approved" and keep their existing treatment.
        own = self.filtered(
            lambda l: l.employee_id.sudo().user_id == self.env.user)
        own_touched = own.filtered(
            lambda l: l.state not in ('refuse', 'cancel')
            and l._has_approver_action())
        if own_touched:
            raise UserError(_(
                'This time off request has already been approved by one of '
                'the approvers and can no longer be deleted. Ask HR to '
                'refuse it instead.'))
        # The rest of the recordset still goes through Odoo's checks — never
        # drop the complement (see July 2026 audit, mixed-batch filter drop).
        own_pending = own.filtered(lambda l: l._is_own_untouched_request())
        remaining = self - own_pending
        if remaining:
            return super(HrLeave, remaining)._unlink_if_correct_states()
        return None

    # ==================================================================
    # Override unlink — refresh accrual when annual leave is deleted
    # ==================================================================

    def unlink(self):
        """Refresh accrual when an annual leave record is deleted."""
        annual = self.filtered(self._is_annual_leave)
        annual_emp_ids = annual.mapped('employee_id').ids
        result = super().unlink()
        if annual_emp_ids:
            self.env['ksw.annual.leave'].with_user(1)._refresh_accrual_for_employees(annual_emp_ids)
        return result
