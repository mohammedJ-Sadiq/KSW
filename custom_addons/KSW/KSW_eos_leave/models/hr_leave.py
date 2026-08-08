import logging
from datetime import date, timedelta

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# EOS fields that only HR may write, enforced server-side.
_EOS_HR_FIELDS = frozenset({
    'x_eos_unpaid_days',
    'x_eos_termination_reason',
    'x_eos_previous_payments',
    'x_eos_notice_pay',
})

# The one state at which HR may fill or correct the EOS figures: their own
# step. An approver editing a request that is sitting in someone else's queue
# would change figures the later approvers have already signed off on. The
# correction path is the GM's return-to-approver wizard, which lists
# 'pending_hr' as a valid target from both GM steps
# (see _VALID_RETURN_TARGETS in KSW_annual_leave/wizard/).
_EOS_INPUT_STATE = 'pending_hr'


class HrLeave(models.Model):
    _inherit = 'hr.leave'

    # ------------------------------------------------------------------
    # Type flag — stored so it can be used safely in invisible expressions
    # ------------------------------------------------------------------

    x_is_eos_leave = fields.Boolean(
        string='Is EOS Leave',
        related='holiday_status_id.is_eos_leave',
        store=True,
        help='True when the leave type is flagged as an EOS request.',
    )

    # ------------------------------------------------------------------
    # HR-filled EOS fields (only writable by HR at pending_hr state)
    # ------------------------------------------------------------------

    x_eos_unpaid_days = fields.Float(
        string='Unpaid Vacations to be Deducted (Days)',
        digits=(10, 2),
        copy=False,
        help='Number of unpaid leave days taken during the service period. '
             'These days are excluded from the service period before recomputing '
             'the Article 84/85 EOS entitlement.',
    )
    x_eos_termination_reason = fields.Selection(
        selection=[
            ('84', 'Article 84 — Termination by Employer'),
            ('85', 'Article 85 — Resignation'),
        ],
        string='Termination Reason',
        copy=False,
        help='Determines which EOS formula applies. '
             'Article 84 = employer-initiated termination (full entitlement). '
             'Article 85 = employee resignation (tiered entitlement).',
    )
    x_eos_previous_payments = fields.Float(
        string='Previous Payments',
        digits=(16, 2),
        copy=False,
        help='Amount of EOS payments already made to the employee in prior '
             'instalments. Deducted from the EOS payslip.',
    )
    x_eos_notice_pay = fields.Float(
        string='Notice Pay (Deduction)',
        digits=(16, 2),
        copy=False,
        help='Notice period pay deducted from the EOS payslip '
             '(e.g. when the employee did not serve the notice period).',
    )

    # ------------------------------------------------------------------
    # Computed EOS amounts — base (unadjusted) reference values
    # ------------------------------------------------------------------

    x_eos_service_years = fields.Float(
        string='Base Service Years (Unadjusted)',
        compute='_compute_eos_base',
        digits=(5, 2),
    )
    x_eos_last_wage = fields.Float(
        string='Last Wage (SAR)',
        compute='_compute_eos_base',
        digits=(16, 2),
    )
    x_eos_termination_amount = fields.Float(
        string='Base EOS — Termination (Art. 84, Unadjusted)',
        compute='_compute_eos_base',
        digits=(16, 2),
    )
    x_eos_resignation_amount = fields.Float(
        string='Base EOS — Resignation (Art. 85, Unadjusted)',
        compute='_compute_eos_base',
        digits=(16, 2),
    )

    # ------------------------------------------------------------------
    # Computed EOS amounts — adjusted for unpaid days
    # ------------------------------------------------------------------

    x_eos_adjusted_service_years = fields.Float(
        string='Adjusted Service Years',
        compute='_compute_eos_adjusted',
        digits=(5, 2),
        help='Service years after subtracting unpaid leave days from the '
             'total service period.',
    )
    x_eos_adjusted_termination_amount = fields.Float(
        string='Adjusted EOS — Termination (Art. 84)',
        compute='_compute_eos_adjusted',
        digits=(16, 2),
        help='Article 84 EOS amount computed on the adjusted service years.',
    )
    x_eos_adjusted_resignation_amount = fields.Float(
        string='Adjusted EOS — Resignation (Art. 85)',
        compute='_compute_eos_adjusted',
        digits=(16, 2),
        help='Article 85 EOS amount computed on the adjusted service years.',
    )
    x_eos_payout_amount = fields.Float(
        string='EOS Payout Amount',
        compute='_compute_eos_payout',
        digits=(16, 2),
        help='Final EOS amount: the adjusted Art. 84 or Art. 85 figure, '
             'depending on the selected Termination Reason.',
    )

    x_eos_inputs_editable = fields.Boolean(
        string='EOS Inputs Editable',
        compute='_compute_eos_inputs_editable',
        help='True when the current user may still fill or correct the '
             'HR-owned EOS financial inputs on this request.',
    )

    # ------------------------------------------------------------------
    # EOS payslip link (hr.payslip model only available via KSW_payroll)
    # ------------------------------------------------------------------

    x_eos_payslip_id = fields.Many2one(
        'hr.payslip',
        string='EOS Payslip',
        readonly=True,
        copy=False,
        help='The EOS payslip generated at GM Final Approval.',
    )

    # ------------------------------------------------------------------
    # Computations
    # ------------------------------------------------------------------

    @staticmethod
    def _eos_calc(total_days, wage):
        """Return (years, termination_amount, resignation_amount) for given days/wage."""
        years = total_days / 365.25
        first = min(years, 5.0)
        extra = max(years - 5.0, 0.0)
        term = 0.5 * wage * first + 1.0 * wage * extra
        if years < 2.0:
            resig = 0.0
        elif years < 5.0:
            resig = term / 3.0
        elif years < 10.0:
            resig = term * 2.0 / 3.0
        else:
            resig = term
        return years, term, resig

    @api.depends(
        'x_is_eos_leave',
        'request_date_from',
        'employee_id',
        'employee_id.version_ids.contract_date_start',
        'employee_id.version_ids.active',
        'employee_id.current_version_id.wage',
    )
    def _compute_eos_base(self):
        today = fields.Date.context_today(self)
        for leave in self:
            if not leave.employee_id:
                leave.x_eos_service_years = 0.0
                leave.x_eos_last_wage = 0.0
                leave.x_eos_termination_amount = 0.0
                leave.x_eos_resignation_amount = 0.0
                continue
            as_of = leave.request_date_from or today
            emp = leave.employee_id.sudo()
            wage = emp.current_version_id.wage or 0.0
            versions = emp.version_ids.filtered(lambda v: v.contract_date_start)
            if not versions or not wage:
                leave.x_eos_service_years = 0.0
                leave.x_eos_last_wage = wage
                leave.x_eos_termination_amount = 0.0
                leave.x_eos_resignation_amount = 0.0
                continue
            joining = min(versions.mapped('contract_date_start'))
            total_days = max((as_of - joining).days, 0)
            years, term, resig = self._eos_calc(total_days, wage)
            leave.x_eos_service_years = years
            leave.x_eos_last_wage = wage
            leave.x_eos_termination_amount = term
            leave.x_eos_resignation_amount = resig

    @api.depends(
        'x_is_eos_leave',
        'x_eos_unpaid_days',
        'request_date_from',
        'employee_id',
        'employee_id.version_ids.contract_date_start',
        'employee_id.version_ids.active',
        'employee_id.current_version_id.wage',
    )
    def _compute_eos_adjusted(self):
        today = fields.Date.context_today(self)
        for leave in self:
            if not leave.x_is_eos_leave or not leave.employee_id:
                leave.x_eos_adjusted_service_years = 0.0
                leave.x_eos_adjusted_termination_amount = 0.0
                leave.x_eos_adjusted_resignation_amount = 0.0
                continue

            as_of = leave.request_date_from or today
            emp = leave.employee_id.sudo()
            wage = emp.current_version_id.wage or 0.0
            versions = emp.version_ids.filtered(lambda v: v.contract_date_start)
            if not versions or not wage:
                leave.x_eos_adjusted_service_years = 0.0
                leave.x_eos_adjusted_termination_amount = 0.0
                leave.x_eos_adjusted_resignation_amount = 0.0
                continue

            joining = min(versions.mapped('contract_date_start'))
            total_days = max((as_of - joining).days, 0)
            adjusted_days = max(total_days - (leave.x_eos_unpaid_days or 0.0), 0.0)
            years, term, resig = self._eos_calc(adjusted_days, wage)

            leave.x_eos_adjusted_service_years = years
            leave.x_eos_adjusted_termination_amount = term
            leave.x_eos_adjusted_resignation_amount = resig

    @api.depends(
        'x_eos_termination_reason',
        'x_eos_adjusted_termination_amount',
        'x_eos_adjusted_resignation_amount',
    )
    def _compute_eos_payout(self):
        for leave in self:
            if leave.x_eos_termination_reason == '84':
                leave.x_eos_payout_amount = leave.x_eos_adjusted_termination_amount
            elif leave.x_eos_termination_reason == '85':
                leave.x_eos_payout_amount = leave.x_eos_adjusted_resignation_amount
            else:
                leave.x_eos_payout_amount = 0.0

    @api.depends_context('uid')
    @api.depends('x_is_eos_leave', 'x_annual_approval_state', 'x_is_hr_approver')
    def _compute_eos_inputs_editable(self):
        for leave in self:
            leave.x_eos_inputs_editable = bool(
                leave.x_is_eos_leave
                and leave.x_is_hr_approver
                and leave.x_annual_approval_state == _EOS_INPUT_STATE
            )

    # ------------------------------------------------------------------
    # Write guard: EOS financial fields are HR-only
    # Sync: EOS leaves always have request_date_to == request_date_from
    # ------------------------------------------------------------------

    @api.onchange('request_date_from', 'x_is_eos_leave')
    def _onchange_eos_sync_dates(self):
        """Keep request_date_to == request_date_from for EOS leaves."""
        for leave in self:
            if leave.x_is_eos_leave and leave.request_date_from:
                leave.request_date_to = leave.request_date_from

    def create(self, vals_list):
        """For EOS leave types, force request_date_to == request_date_from."""
        if isinstance(vals_list, dict):
            vals_list = [vals_list]
        eos_type_ids = set(
            self.env['hr.leave.type'].search(
                [('is_eos_leave', '=', True)]).ids
        )
        for vals in vals_list:
            if (vals.get('holiday_status_id') in eos_type_ids
                    and vals.get('request_date_from')):
                vals['request_date_to'] = vals['request_date_from']
        return super().create(vals_list)

    def write(self, vals):
        # Only *setting* a meaningful value is HR-gated; clearing to 0/False
        # stays open to every role so a chain reset (_reset_annual_multi_fields,
        # driven by refuse / back-to-draft / a leave-type change) can wipe these
        # figures whoever triggers it. Same trade-off as _HR_ONLY_FIELDS in
        # KSW_annual_leave.
        eos_written = {k for k in vals if k in _EOS_HR_FIELDS and vals[k]}
        if eos_written:
            for leave in self:
                if not leave.x_is_eos_leave or self.env.su:
                    continue
                if not self.env.user.has_group(
                        'KSW_annual_leave.group_annual_leave_hr'):
                    raise UserError(_(
                        'Only HR Approvers can fill EOS financial fields.'))
                # HR owns these figures at their own step only. The view's
                # readonly= is cosmetic — without this an HR user could still
                # rewrite figures the later approvers already signed off on,
                # via RPC or a stale form.
                if leave.x_annual_approval_state != _EOS_INPUT_STATE:
                    raise UserError(_(
                        'EOS financial figures can only be filled while the '
                        'request is at the HR Approval step.\n\n'
                        'If a figure needs correcting later, the GM must '
                        'return the request to HR first.'
                    ))
        # An EOS request covers a single day. create() forces that from vals,
        # but a *later* edit — moving the start date, or changing the type to
        # EOS — must sync the end date too, or the leave keeps the range it
        # had before.
        #
        # Moving request_date_from alone has to be fixed up *before* super():
        # an EOS leave already has from == to, so a later start date makes
        # from > to and trips the hr_leave_date_check2 DB constraint inside
        # super() — a post-write pass would never be reached. Only done when
        # every target record will be EOS, since vals is shared across the
        # whole recordset; anything else is left to the post-write pass.
        if ('request_date_from' in vals and 'request_date_to' not in vals
                and self._will_all_be_eos(vals)):
            vals = dict(vals, request_date_to=vals['request_date_from'])

        result = super().write(vals)

        if {'holiday_status_id', 'request_date_from',
                'request_date_to'} & vals.keys():
            self._sync_eos_dates()

        return result

    def _will_all_be_eos(self, vals):
        """True when every record in self is an EOS leave after applying vals."""
        if not self:
            return False
        if 'holiday_status_id' in vals:
            return bool(self.env['hr.leave.type'].browse(
                vals['holiday_status_id']).is_eos_leave)
        return all(leave.x_is_eos_leave for leave in self)

    def _sync_eos_dates(self):
        """Force request_date_to == request_date_from on EOS leaves.

        The nested write() re-enters this method, but the second pass finds
        the dates already equal and stops there.
        """
        for leave in self:
            if (leave.x_is_eos_leave
                    and leave.request_date_from
                    and leave.request_date_to != leave.request_date_from):
                leave.write({'request_date_to': leave.request_date_from})

    # ------------------------------------------------------------------
    # Chain reset: EOS figures must not outlive the approvals they belong to
    # ------------------------------------------------------------------

    def _reset_annual_multi_fields(self):
        """Also clear the HR-filled EOS figures when the chain is reset.

        KSW_annual_leave wipes every HR/Accounting figure on refuse, back-to-
        draft and leave-type change so the next run through the chain starts
        clean.  The EOS fields are the exact analogue and were being left
        behind, so a refused EOS request kept its termination reason and
        payout figures — and therefore a stale x_eos_payout_amount.
        """
        result = super()._reset_annual_multi_fields()
        # Deliberately not filtered on x_is_eos_leave: on the leave-type-change
        # path the type has already flipped by the time this runs, so filtering
        # would skip exactly the record that needs clearing. Writing False over
        # an already-empty field on a non-EOS leave is a no-op.
        self.write({field: False for field in sorted(_EOS_HR_FIELDS)})
        return result

    # ------------------------------------------------------------------
    # GM Final Approval: archive the employee upon EOS approval
    # ------------------------------------------------------------------

    _EOS_DEPARTURE_REASON = {
        '84': 'hr.departure_fired',     # Termination by employer
        '85': 'hr.departure_resigned',  # Resignation
    }

    def action_gm_final_approve(self):
        # The Termination Reason is already guaranteed here: super() opens with
        # _check_annual_approval_can_advance(), which this module overrides to
        # enforce it. So the departure reason picked below is never blank.
        result = super().action_gm_final_approve()
        for leave in self.filtered('x_is_eos_leave'):
            employee = leave.employee_id
            if not employee or not employee.active:
                continue
            departure_xmlid = self._EOS_DEPARTURE_REASON.get(
                leave.x_eos_termination_reason)
            departure_reason = (
                self.env.ref(departure_xmlid, raise_if_not_found=False)
                if departure_xmlid else None
            )
            termination_date = leave.request_date_from or fields.Date.context_today(self)
            employee.sudo().with_context(no_wizard=True).action_archive()
            employee.sudo().write({
                'departure_reason_id': departure_reason.id if departure_reason else False,
                'departure_date': termination_date,
            })
            leave.sudo().message_post(
                body=Markup(
                    '<strong>🏢 Employee Archived</strong><br/>'
                    '<b>Employee:</b> %(emp)s<br/>'
                    '<b>Departure Date:</b> %(date)s<br/>'
                    '<b>Reason:</b> %(reason)s'
                ) % {
                    'emp': employee.name,
                    'date': termination_date.strftime('%Y-%m-%d'),
                    'reason': departure_reason.name if departure_reason
                              else _('Not specified'),
                },
                subtype_xmlid='mail.mt_note',
            )
        return result

    # ------------------------------------------------------------------
    # DM step: suppress attendance-sheet wizard for EOS leaves
    # ------------------------------------------------------------------

    def action_dm_approve(self):
        result = super().action_dm_approve()
        # EOS leaves don't need attendance-sheet marking — the employee is
        # leaving, not going on vacation.
        if (
            len(self) == 1
            and self.x_is_eos_leave
            and isinstance(result, dict)
            and result.get('res_model') == 'ksw.leave.attendance.wizard'
        ):
            return True
        return result

    # ------------------------------------------------------------------
    # Approval guards: an EOS request is worthless without its reason
    # ------------------------------------------------------------------

    def _check_eos_reason_filled(self):
        """Raise unless every EOS request in self has a Termination Reason.

        Without it ``_compute_eos_payout`` returns 0 and
        ``_build_eos_input_lines`` emits no EOS_AMOUNT input at all, so the
        payslip generated at GM final approval carries every other settlement
        item (vacation balance, ticket, commissions) but *not* the EOS payout
        — silently, with no error anywhere in the chain.

        Deliberately **not** exempt under ``self.env.su``, unlike the HR write
        guard in ``write()``. That one is a permission check, where sudo is a
        legitimate escape hatch; this is a data-integrity requirement, and an
        EOS payslip with no payout is just as wrong when a sudo'd caller
        produced it.
        """
        missing = self.filtered(
            lambda l: l.x_is_eos_leave and not l.x_eos_termination_reason)
        if missing:
            raise UserError(_(
                'Select a Termination Reason (Article 84 — Termination or '
                'Article 85 — Resignation) in the EOS Review tab first.\n\n'
                'Without it the End-of-Service payout is computed as 0 and '
                'will not appear on the EOS payslip.'
            ))

    def _check_annual_approval_can_advance(self):
        """Make the Termination Reason unskippable for EOS requests.

        Every forward step of the six-step chain (DM, HR, GM initial,
        Accounting, GM final) funnels through this method before touching
        state, so one override covers all of them — including a request that
        re-enters the chain through the GM's return-to-approver wizard.

        The DM step is the single exemption: at ``pending_dm`` the request has
        not reached HR yet, and the reason is an HR-only field.
        """
        self.filtered(
            lambda l: l.x_annual_approval_state not in ('pending_dm', False)
        )._check_eos_reason_filled()
        return super()._check_annual_approval_can_advance()

    # ------------------------------------------------------------------
    # Payslip hook: route EOS leaves to _create_eos_payslip
    # ------------------------------------------------------------------

    def _create_vacation_payslip(self, preview=False):
        eos = self.filtered('x_is_eos_leave')
        if eos:
            eos._create_eos_payslip(preview=preview)
        regular = self - eos
        if regular:
            # Call the KSW_payroll implementation for non-EOS leaves
            super(HrLeave, regular)._create_vacation_payslip(preview=preview)

    def _create_eos_payslip(self, preview=False):
        """Create a final-month payslip including EOS components.

        The payslip includes the employee's regular salary for the final
        month PLUS all one-time items:
          EOS_AMOUNT        — the chosen Art. 84 / Art. 85 payout
          EOS_PREV_PAYMENTS — previous EOS payments (deduction)
          EOS_NOTICE_PAY    — notice period deduction
          VACATION_BAL      — remaining annual leave balance settlement
          FLIGHT_TICKET     — flight ticket allowance (if any)
          ADDITIONAL_COMMISSIONS — accrued commissions (if any)
          REMAINING_LOANS   — outstanding loan deductions (if any)
          OTHER_DEDUCTIONS  — sum of the other-deduction lines (if any)
          PENALTY           — penalty deduction (if any)
        HRA and GOSI advances are excluded — those are already part of
        the regular monthly salary lines on this terminal payslip.
        """
        Payslip = self.env['hr.payslip'].sudo()
        today = fields.Date.context_today(self)

        if not preview:
            # A provisional run may legitimately predate the HR figures, but a
            # definitive EOS payslip without a Termination Reason would ship
            # with a 0 payout and no EOS_AMOUNT line. Also covers the
            # "Recompute EOS Payslip" button.
            self._check_eos_reason_filled()
            # The definitive payslip supersedes any provisional run.
            self._cancel_preview_vacation_payslips()

        for leave in self:
            employee = leave.employee_id
            if not employee:
                continue

            version = employee.current_version_id
            if not version:
                _logger.warning(
                    'No active version (contract) for employee %s — '
                    'skipping EOS payslip creation.',
                    employee.name,
                )
                continue

            structure = version.struct_id
            if not structure:
                _logger.warning(
                    'No salary structure for employee %s — '
                    'skipping EOS payslip creation.',
                    employee.name,
                )
                continue

            # Same month-selection logic as vacation payslip: prefer current
            # month, but fall back to previous month if it has not yet been
            # settled (avoids all-absent attendance deduction on a blank month).
            month_start = today.replace(day=1)

            if month_start.month == 1:
                prev_month_start = date(month_start.year - 1, 12, 1)
            else:
                prev_month_start = date(month_start.year, month_start.month - 1, 1)
            prev_month_end = month_start - timedelta(days=1)

            done_prev = Payslip.search([
                ('employee_id', '=', employee.id),
                ('state', '=', 'done'),
                ('date_from', '>=', prev_month_start),
                ('date_from', '<=', prev_month_end),
            ], limit=1)

            if not done_prev:
                month_start = prev_month_start
                month_end = prev_month_end
            elif month_start.month == 12:
                month_end = date(month_start.year + 1, 1, 1) - timedelta(days=1)
            else:
                month_end = date(month_start.year, month_start.month + 1, 1) - timedelta(days=1)

            payslip = Payslip.create({
                'employee_id': employee.id,
                'name': '%s — %s — %s/%s' % (
                    'EOS Payslip (Provisional)' if preview else 'EOS Payslip',
                    employee.name, month_start.year, month_start.month),
                'date_from': month_start,
                'date_to': month_end,
                'struct_id': structure.id,
                'version_id': version.id,
                'x_leave_id': leave.id,
                'x_is_vacation_preview': preview,
            })

            # EOS-specific items: payout amount, previous payments, notice pay
            input_vals = self._build_eos_input_lines(leave, payslip)

            # Also include vacation balance settlement, commissions, flight
            # ticket, loans, and penalty — same as a regular vacation payslip.
            # Skip HRA/GOSI advance (already in the regular monthly salary
            # on this terminal payslip) and excess-leave cost-recovery items.
            _EOS_SKIP = frozenset({
                'VACATION_HRA', 'VACATION_GOSI',
                'FIN_CONSIDERATION', 'VISA_COST_RECOVERY',
            })
            for item in self._build_vacation_input_lines(leave, employee, payslip):
                if item['code'] not in _EOS_SKIP:
                    input_vals.append(item)

            if input_vals:
                self.env['hr.payslip.input'].sudo().create(input_vals)

            payslip.compute_sheet()

            _logger.info(
                'EOS payslip #%s created for employee %s '
                '(leave #%s, month %s/%s).',
                payslip.id, employee.name, leave.id,
                month_start.year, month_start.month,
            )

            leave.sudo().write({'x_eos_payslip_id': payslip.id})

    @staticmethod
    def _build_eos_input_lines(leave, payslip):
        """Build hr.payslip.input values for EOS components."""
        vals_list = []
        version_id = payslip.version_id.id

        if leave.x_eos_payout_amount:
            reason_label = dict(
                leave._fields['x_eos_termination_reason'].selection
            ).get(leave.x_eos_termination_reason, '')
            vals_list.append({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': 'EOS Amount (%s)' % reason_label,
                'code': 'EOS_AMOUNT',
                'amount': leave.x_eos_payout_amount,
            })

        if leave.x_eos_previous_payments:
            vals_list.append({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': 'EOS Previous Payments',
                'code': 'EOS_PREV_PAYMENTS',
                'amount': leave.x_eos_previous_payments,
            })

        if leave.x_eos_notice_pay:
            vals_list.append({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': 'EOS Notice Pay (Deduction)',
                'code': 'EOS_NOTICE_PAY',
                'amount': leave.x_eos_notice_pay,
            })

        return vals_list

    # ------------------------------------------------------------------
    # Finalised requests (KSW_annual_leave._check_final_reversal_rights)
    # ------------------------------------------------------------------

    def _has_confirmed_payslip(self):
        """True when the EOS settlement payslip is confirmed."""
        if super()._has_confirmed_payslip():
            return True
        slip = self.sudo().x_eos_payslip_id
        return bool(slip and slip.state == 'done')

    def _is_finalised(self):
        """An EOS request whose payslip is confirmed is finalised.

        Same rule as the vacation payslip in KSW_payroll — once the
        settlement is paid, reversing the leave would cancel a paid slip.
        """
        return super()._is_finalised() or self._has_confirmed_payslip()

    # ------------------------------------------------------------------
    # Cancel EOS payslip on refuse / reset-to-draft / back-to-approval
    # ------------------------------------------------------------------

    def _cancel_eos_payslip(self):
        """Cancel any EOS payslip linked to these leaves.

        sudo(): same reasoning as ``_cancel_vacation_payslips`` — the
        refusing user (direct manager / KSW Supervisor) may have no read
        access on ``hr.payslip`` at all, and an AccessError here would roll
        the whole refuse back.
        """
        for leave in self.sudo():
            if leave.x_eos_payslip_id and leave.x_eos_payslip_id.state != 'cancel':
                leave.x_eos_payslip_id.write({'state': 'cancel'})
            if leave.x_eos_payslip_id:
                leave.write({'x_eos_payslip_id': False})

    def action_refuse(self):
        result = super().action_refuse()
        self.filtered('x_is_eos_leave')._cancel_eos_payslip()
        return result

    def _move_validate_leave_to_confirm(self):
        result = super()._move_validate_leave_to_confirm()
        self.filtered('x_is_eos_leave')._cancel_eos_payslip()
        return result

    def action_draft(self):
        result = super().action_draft()
        self.filtered('x_is_eos_leave')._cancel_eos_payslip()
        return result

    # ------------------------------------------------------------------
    # Validation: EOS leaves are not subject to working-day checks
    # ------------------------------------------------------------------

    def _get_leaves_on_public_holiday(self):
        # EOS termination requests don't need to fall on a working day
        non_eos = self.filtered(lambda l: not l.x_is_eos_leave)
        return super(HrLeave, non_eos)._get_leaves_on_public_holiday()

    # ------------------------------------------------------------------
    # Smart-button action: open EOS payslip
    # ------------------------------------------------------------------

    def action_open_eos_payslip(self):
        """Open the EOS payslip linked to this leave."""
        self.ensure_one()
        if not self.x_eos_payslip_id:
            raise UserError(_('No EOS payslip found for this leave.'))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'hr.payslip',
            'view_mode': 'form',
            'res_id': self.x_eos_payslip_id.id,
            'target': 'current',
        }

    def action_recompute_eos_payslip(self):
        """Cancel the existing EOS payslip and recreate it from current leave inputs.

        Use this when the EOS inputs (unpaid days, previous payments, notice pay,
        termination reason, etc.) were updated after the payslip was already generated.
        The old payslip is cancelled (kept for audit) and a fresh one is created.
        """
        self.ensure_one()
        if not self.env.su and not (
            self.env.user.has_group('om_hr_payroll.group_hr_payroll_user')
            or self.env.user.has_group('KSW_annual_leave.group_annual_leave_hr')
        ):
            raise UserError(_('Only HR Payroll users or HR Approvers can recompute EOS payslips.'))
        if not self.x_eos_payslip_id:
            raise UserError(_('No EOS payslip found on this leave to recompute.'))
        # A payslip generated mid-chain stays provisional when recomputed —
        # it only becomes definitive at GM final approval.
        preview = self.sudo().x_eos_payslip_id.x_is_vacation_preview
        self._cancel_eos_payslip()
        self._create_eos_payslip(preview=preview)
        self.sudo().message_post(
            body=Markup(
                '<strong>🔄 EOS Payslip Recomputed</strong><br/>'
                '<b>By:</b> %(user)s'
            ) % {'user': self.env.user.name},
            subtype_xmlid='mail.mt_note',
        )
