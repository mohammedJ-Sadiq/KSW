import calendar as _cal

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_round
from odoo.osv import expression as odoo_expr

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
            domain += [
                '|',
                ('user_id', '=', user.id),
                ('id', 'child_of', user.employee_ids.ids),
            ]
        else:
            domain += [('user_id', '=', user.id)]
        return domain

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
    @api.depends('x_annual_approval_state', 'employee_id', 'employee_id.leave_manager_id')
    def _compute_is_pending_my_action(self):
        user = self.env.user
        uid = user.id
        is_hr = user.has_group('KSW_annual_leave.group_annual_leave_hr')
        is_gm = user.has_group('KSW_annual_leave.group_annual_leave_gm')
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
                leave.x_is_pending_my_action = is_gm
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
        is_gm = user.has_group('KSW_annual_leave.group_annual_leave_gm')
        is_acc = user.has_group('KSW_annual_leave.group_annual_leave_acc')

        parts = [
            # DM step: current user is the configured leave manager
            [('x_annual_approval_state', '=', 'pending_dm'),
             ('employee_id.leave_manager_id', '=', uid)],
        ]
        if is_hr:
            parts.extend([
                # HR as DM fallback when no manager is configured
                [('x_annual_approval_state', '=', 'pending_dm'),
                 ('employee_id.leave_manager_id', '=', False)],
                [('x_annual_approval_state', '=', 'pending_hr')],
                [('x_annual_approval_state', '=', 'pending_employee_signature')],
            ])
        if is_gm:
            parts.append([('x_annual_approval_state', 'in', ['pending_gm_initial', 'pending_gm_final'])])
        if is_acc:
            parts.append([('x_annual_approval_state', '=', 'pending_acc')])

        positive = odoo_expr.OR(parts)
        if positive_wanted:
            return positive
        matching_ids = self.with_context(active_test=False).search(positive).ids
        return [('id', 'not in', matching_ids)]

    @api.depends_context('uid')
    @api.depends('x_annual_approval_state')
    def _compute_approval_role_gates(self):
        user = self.env.user
        is_hr = user.has_group('KSW_annual_leave.group_annual_leave_hr')
        is_acc = user.has_group('KSW_annual_leave.group_annual_leave_acc')
        is_gm = user.has_group('KSW_annual_leave.group_annual_leave_gm')
        for leave in self:
            s = leave.x_annual_approval_state
            has_id = bool(leave.id)
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

    @api.depends('state', 'employee_id', 'department_id',
                 'x_annual_approval_state', 'holiday_status_id')
    def _compute_can_refuse(self):
        """Hide 'Refuse' once an annual_multi leave has cleared GM final approval.

        Refuse remains available through pending_gm_final so an approver can
        still reject the request outright instead of advancing it. Once GM
        final approval is done (pending_employee_signature or approved),
        refusing no longer makes sense.
        """
        annual_multi_done = self.filtered(
            lambda l: self._is_annual_multi(l)
            and l.x_annual_approval_state in self._REFUSE_LOCKED_STATES
        )
        remaining = self - annual_multi_done
        if remaining:
            super(HrLeave, remaining)._compute_can_refuse()
        for leave in annual_multi_done:
            leave.can_refuse = False

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
            # For commission lines: treat create (0) or update (1) commands as writes
            comm_cmds = vals.get('x_commission_line_ids', [])
            has_comm_write = any(
                isinstance(cmd, (list, tuple)) and cmd[0] in (0, 1)
                for cmd in comm_cmds
            )
            if (acc_set or has_comm_write) and not self.env.user.has_group(
                    'KSW_annual_leave.group_annual_leave_acc'):
                raise UserError(
                    'Only Accounting Approvers can fill in commission, '
                    'loan, and flight ticket fields.')

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
        x_commission_line_ids (unlink is granted to group_leave_officer only,
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
        self._check_group(
            'KSW_annual_leave.group_annual_leave_gm',
            'Only the General Manager can approve this step.',
        )
        self._check_annual_approval_can_advance()
        for leave in self:
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
        self._check_group(
            'KSW_annual_leave.group_annual_leave_gm',
            'Only the General Manager can give final approval.',
        )
        self._check_annual_approval_can_advance()
        for leave in self:
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
        """Open the wizard allowing the GM to return the request to a previous approver."""
        self.ensure_one()
        if not self.env.su and not self.env.user.has_group(
                'KSW_annual_leave.group_annual_leave_gm'):
            raise UserError('Only the General Manager can return a leave to an approver.')
        if self.x_annual_approval_state not in ('pending_gm_initial', 'pending_gm_final'):
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

    _ANNUAL_MULTI_STEP_CONFIG = {
        'pending_dm':                 {'label': 'Direct Manager Approval',  'group': None},
        'pending_hr':                 {'label': 'HR Approval',              'group': 'KSW_annual_leave.group_annual_leave_hr'},
        'pending_gm_initial':         {'label': 'GM Initial Approval',      'group': 'KSW_annual_leave.group_annual_leave_gm'},
        'pending_acc':                {'label': 'Accounting Approval',      'group': 'KSW_annual_leave.group_annual_leave_acc'},
        'pending_gm_final':           {'label': 'GM Final Approval',        'group': 'KSW_annual_leave.group_annual_leave_gm'},
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

        leave.message_post(
            body=Markup(
                '<strong>&#9203; Action Required — %(label)s</strong><br/>'
                '<b>Employee:</b> %(employee)s<br/>'
                '<b>Period:</b> %(date_from)s &#8594; %(date_to)s<br/>'
                '<b>Days:</b> %(days).1f<br/>'
                '%(instruction)s'
            ) % {
                'label': config['label'],
                'employee': leave.employee_id.name,
                'date_from': leave.request_date_from,
                'date_to': leave.request_date_to,
                'days': leave.number_of_days,
                'instruction': instruction,
            },
            partner_ids=partner_ids,
            subtype_xmlid='mail.mt_comment',
        )

    def _reset_annual_multi_fields(self):
        """Reset all multi-step approval fields to their defaults."""
        # Delete commission lines (cascaded by ORM, but explicit is clearer)
        self.mapped('x_commission_line_ids').unlink()
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
            })

        if annual_multi:
            annual_multi._reset_annual_multi_fields()

        result = super()._move_validate_leave_to_confirm()

        # After super sets state='confirm', restart the approval chain
        if annual_multi:
            annual_multi.write({'x_annual_approval_state': 'pending_dm'})

        # Refresh accrual — leaves_taken changed
        if annual_emp_ids:
            self.env['ksw.annual.leave'].with_user(1)._refresh_accrual_for_employees(annual_emp_ids)

        return result

    def action_refuse(self):
        """Reset return and multi-step fields when refused."""
        if not self.env.su:
            locked = self.filtered(
                lambda l: self._is_annual_multi(l)
                and l.x_annual_approval_state in self._REFUSE_LOCKED_STATES
            )
            if locked:
                raise UserError(_(
                    'This annual leave has already received final GM '
                    'approval and can no longer be refused.'
                ))
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

    def _is_own_unapproved_request(self):
        """True when this leave is the current user's own request and it has
        not been fully approved yet.

        "Fully approved" means the Odoo state reached ``validate`` (or, on the
        KSW multi-step chains, ``x_annual_approval_state == 'approved'``).
        Every earlier step — including the whole 6-step annual/EOS/unpaid
        chain, which keeps ``state == 'confirm'`` throughout — counts as still
        pending, so the employee stays in control of their own request.
        """
        self.ensure_one()
        if self.employee_id.sudo().user_id != self.env.user:
            return False
        # `cancel` is deliberately excluded: those are handled by Odoo's own
        # cancellation flow, not by deletion.
        if self.state not in ('confirm', 'validate1'):
            return False
        return self.x_annual_approval_state != 'approved'

    @api.ondelete(at_uninstall=False)
    def _unlink_if_correct_states(self):
        """Override to relax Odoo's deletion rules for two KSW cases.

        1. KSW Supervisors/Officers may delete leaves that started in the
           past — they manage subordinate leaves that just started and need
           correction/deletion — and may also delete a **refused** request,
           which Odoo core reserves for Administrators.
        2. Any employee may delete their **own** request as long as it is not
           fully approved yet, whatever its start date. Odoo core blocks
           non-Officer users from deleting a leave whose ``date_from`` is in
           the past, which strands employees whose request is still crawling
           through the multi-step approval chain past its own start date.
        """
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

        # Own, still-unapproved requests bypass core's past-date guard. The
        # rest of the recordset still goes through Odoo's checks — never drop
        # the complement (see July 2026 audit, mixed-batch filter drop).
        own_pending = self.filtered(lambda l: l._is_own_unapproved_request())
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
